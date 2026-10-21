"""② PostgreSQL 공통 저장 구조. 측정·추론·사건·조치·알림·모델 테이블과 연결 함수를
정의한다.

테이블 이름은 rt_로 시작하며 원본 payload와 처리 상태를 분리해 보존한다.
"""

import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine

metadata = MetaData()


def now() -> datetime:
    """입력: 없음. 반환: timezone이 포함된 현재 UTC datetime. DB 감사 시각에 사용한다."""
    return datetime.now(UTC)


# 수집한 원본 측정 payload와 멱등성 키, 추론 대기·재시도 상태.
measurements = Table(
    "rt_measurements",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("source", String(32), nullable=False),
    Column("event_id", String(100), nullable=False),
    Column("asset_id", String(100), nullable=False, index=True),
    Column("observed_at", DateTime(timezone=True), nullable=False, index=True),
    Column("received_at", DateTime(timezone=True), nullable=False, default=now),
    Column("payload", JSONB, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("status", String(24), nullable=False, default="pending", index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, default=now),
    Column("last_error", Text),
    UniqueConstraint("source", "event_id"),
)
# 모델 후보별 설정·계약 테스트·데이터 cutoff·승인 기록.
models = Table(
    "rt_model_versions",
    metadata,
    Column("version", String(100), primary_key=True),
    Column("kind", String(40), nullable=False),
    Column("config", JSONB, nullable=False),
    Column("evaluation", JSONB, nullable=False),
    Column("training_cutoff_id", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    Column("approved_at", DateTime(timezone=True)),
)
# 활성 포인터를 하나만 두어 승인 버전 전환을 원자적으로 처리한다.
# 현재 서비스에 사용할 승인 모델 버전 포인터.
registry = Table(
    "rt_model_registry",
    metadata,
    Column("name", String(32), primary_key=True),
    Column("version", ForeignKey("rt_model_versions.version"), nullable=False),
)
# 셀별 연속 판정 카운터·열린 사건·가상 냉각 상태.
assets = Table(
    "rt_assets",
    metadata,
    Column("asset_id", String(100), primary_key=True),
    Column("last_observed_at", DateTime(timezone=True)),
    Column("anomaly_streak", Integer, nullable=False, default=0),
    Column("normal_streak", Integer, nullable=False, default=0),
    Column("active_incident_id", BigInteger),
    Column("cooling_mode", String(24), nullable=False, default="normal"),
    Column("model_version", String(100)),
    Column("updated_at", DateTime(timezone=True), nullable=False, default=now),
)
# 원천 측정값과 모델 버전에 연결된 특징·점수·판정.
predictions = Table(
    "rt_predictions",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("measurement_id", ForeignKey("rt_measurements.id"), nullable=False, unique=True),
    Column("asset_id", String(100), nullable=False, index=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    Column("model_version", ForeignKey("rt_model_versions.version"), nullable=False),
    Column("features", JSONB, nullable=False),
    Column("score", Float, nullable=False),
    Column("is_anomaly", Boolean, nullable=False),
    Column("status", String(24), nullable=False, default="pending", index=True),
)
# 사건 시작/종료 추론과 당시 문서 snapshot·정책 근거.
incidents = Table(
    "rt_incidents",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("asset_id", String(100), nullable=False, index=True),
    Column("opened_prediction_id", ForeignKey("rt_predictions.id"), nullable=False, unique=True),
    Column("closed_prediction_id", ForeignKey("rt_predictions.id")),
    Column("status", String(24), nullable=False, default="open"),
    Column("opened_at", DateTime(timezone=True), nullable=False, default=now),
    Column("closed_at", DateTime(timezone=True)),
    Column("kind", String(40), nullable=False, default="measurement_anomaly"),
    Column("manual_sha256", String(64)),
    Column("manual_content", Text),
    Column("policy_version", String(32), nullable=False, default="mock-policy-1"),
)
# 사건당 한 조치 요청과 실행·차단·취소·재시도 이력.
actions = Table(
    "rt_actions",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("incident_id", ForeignKey("rt_incidents.id"), nullable=False, unique=True),
    Column("action", String(40), nullable=False),
    Column("status", String(24), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, default=now),
    Column("last_error", Text),
    Column("simulated", Boolean, nullable=False, default=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, default=now),
)
# 사건 전이별 중복 없는 콘솔 알림 outbox.
notifications = Table(
    "rt_notifications",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("dedup_key", String(100), nullable=False, unique=True),
    Column("incident_id", ForeignKey("rt_incidents.id"), nullable=False),
    Column("message", Text, nullable=False),
    Column("channel", String(24), nullable=False, default="console_mock"),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    Column("delivered_at", DateTime(timezone=True)),
)
# 형식 오류·이벤트 충돌·지연 도착의 품질 진단 기록.
issues = Table(
    "rt_quality_issues",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("measurement_id", ForeignKey("rt_measurements.id")),
    Column("code", String(40), nullable=False),
    Column("detail", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
)


def make_engine(url: str | None = None) -> Engine:
    """입력: PostgreSQL URL 또는 DATABASE_URL 환경변수. 반환: 연결을 지연 생성하는
    SQLAlchemy Engine.

    URL 누락·다른 DB 종류는 ValueError이며 실제 접속 오류는 쿼리 실행 시 발생한다.
    """
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL must point to a dedicated PostgreSQL database")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
    if engine.dialect.name != "postgresql":
        raise ValueError("PostgreSQL is required; SQLite is not a substitute")
    return engine


def row_dict(row: Any) -> dict[str, Any]:
    """입력: SQLAlchemy Row. 반환: datetime을 ISO 문자열로 바꾼 JSON 응답용 dict."""
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in row._mapping.items()}


# 사건 버전과 실행 경로를 보존한다. 기존 데이터에는 migration에서 legacy를 부여한다.
incidents.append_column(Column("revision", Integer, nullable=False, server_default="1"))
incidents.append_column(
    Column("response_mode", String(24), nullable=False, server_default="legacy")
)

# 사건 생성과 같은 transaction에 등록하는 분석 작업. lease_token은 늦은 완료를 차단한다.
agent_jobs = Table(
    "rt_agent_jobs",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("incident_id", ForeignKey("rt_incidents.id"), nullable=False),
    Column("request_key", String(160), nullable=False, unique=True),
    Column("request_context", JSONB, nullable=False, default=dict, server_default="{}"),
    Column("agent_version", String(40), nullable=False, default="agent-1"),
    Column("mode", String(24), nullable=False),
    Column("status", String(24), nullable=False, default="queued", index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt_at", DateTime(timezone=True), nullable=False, default=now),
    Column("lease_until", DateTime(timezone=True)),
    Column("lease_token", String(36)),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
)
agent_runs = Table(
    "rt_agent_runs",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("job_id", ForeignKey("rt_agent_jobs.id"), nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("lease_token", String(36), nullable=False),
    Column("status", String(24), nullable=False, default="running"),
    Column("snapshot", JSONB, nullable=False),
    Column("provider", String(40), nullable=False),
    Column("model", String(100), nullable=False),
    Column("prompt_version", String(40), nullable=False, default="investigate-1"),
    Column("toolset_version", String(40), nullable=False, default="tools-1"),
    Column("result", JSONB),
    Column("usage", JSONB),
    Column("error", Text),
    Column("started_at", DateTime(timezone=True), nullable=False, default=now),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint("job_id", "attempt"),
)
agent_steps = Table(
    "rt_agent_steps",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("run_id", ForeignKey("rt_agent_runs.id"), nullable=False),
    Column("step_index", Integer, nullable=False),
    Column("call_id", String(160), nullable=False),
    Column("tool", String(80), nullable=False),
    Column("arguments", JSONB, nullable=False),
    Column("result", JSONB, nullable=False),
    Column("elapsed_ms", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    UniqueConstraint("run_id", "call_id"),
)
documents = Table(
    "rt_documents",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("document_key", String(100), nullable=False),
    Column("version", String(40), nullable=False),
    Column("title", String(200), nullable=False),
    Column("asset_type", String(40), nullable=False),
    Column("approval_scope", String(24), nullable=False),
    Column("tags", JSONB, nullable=False),
    Column("content", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
    UniqueConstraint("document_key", "version"),
)
document_chunks = Table(
    "rt_document_chunks",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("document_id", ForeignKey("rt_documents.id"), nullable=False),
    Column("position", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    UniqueConstraint("document_id", "position"),
)
proposals = Table(
    "rt_action_proposals",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("incident_id", ForeignKey("rt_incidents.id"), nullable=False),
    Column("run_id", ForeignKey("rt_agent_runs.id"), nullable=False, unique=True),
    Column("incident_revision", Integer, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("proposal_hash", String(64), nullable=False, unique=True),
    Column("policy_version", String(40), nullable=False, default="agent-simulation-1"),
    Column("status", String(32), nullable=False),
    Column("reason", Text),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, default=now),
)
approvals = Table(
    "rt_action_approvals",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("proposal_id", ForeignKey("rt_action_proposals.id"), nullable=False, unique=True),
    Column("request_id", String(100), nullable=False, unique=True),
    Column("proposal_hash", String(64), nullable=False),
    Column("reviewer", String(100), nullable=False),
    Column("role", String(24), nullable=False),
    Column("decision", String(24), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=False, default=now),
)
actions.append_column(Column("proposal_id", ForeignKey("rt_action_proposals.id"), unique=True))
actions.append_column(Column("command_key", String(100), unique=True))
migrations = Table(
    "rt_schema_migrations",
    metadata,
    Column("version", String(80), primary_key=True),
    Column("checksum", String(64), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False, default=now),
)
