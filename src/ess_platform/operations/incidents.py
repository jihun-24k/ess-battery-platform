"""추론 결과의 연속 패턴을 판단해 사건 생성·복귀·문서 근거를 기록한다."""

import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from ess_platform.agents.repository import enqueue_job
from ess_platform.db import actions, assets, incidents, issues, now, predictions, proposals
from ess_platform.locking import OPERATIONS_LOCK, lock_worker
from ess_platform.operations.notifications import enqueue_notice

MANUAL_PATH = Path(__file__).resolve().parents[1] / "manuals" / "measurement_anomaly.md"


def operations_tick(
    engine: Engine, manual_path: Path = MANUAL_PATH, response_mode: str = "shadow"
) -> bool:
    """입력: DB engine·학습 문서 경로. 반환: 추론 하나의 상태를 처리했으면 True.

    연속 판정을 갱신해 사건·문서 근거·분석 작업·패턴 복귀를 저장한다.
    shadow만 즉시 가상 요청을 만들고 assisted는 승인 경로에 맡긴다.
    작업 없음/잠금 충돌은 False다.
    """
    if response_mode not in {"shadow", "assisted"}:
        raise ValueError("response_mode must be shadow or assisted")
    with engine.begin() as conn:
        if not lock_worker(conn, OPERATIONS_LOCK):
            return False
        prediction = (
            conn.execute(
                select(predictions)
                .where(predictions.c.status == "pending")
                .order_by(predictions.c.observed_at, predictions.c.id)
                .limit(1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if prediction is None:
            return False
        asset_id = prediction["asset_id"]
        conn.execute(
            insert(assets)
            .values(asset_id=asset_id)
            .on_conflict_do_nothing(index_elements=["asset_id"])
        )
        state = (
            conn.execute(select(assets).where(assets.c.asset_id == asset_id).with_for_update())
            .mappings()
            .one()
        )
        if state["last_observed_at"] and prediction["observed_at"] <= state["last_observed_at"]:
            conn.execute(
                predictions.update()
                .where(predictions.c.id == prediction["id"])
                .values(status="late")
            )
            conn.execute(
                issues.insert().values(
                    measurement_id=prediction["measurement_id"],
                    code="LATE_PREDICTION",
                    detail={"asset_id": asset_id},
                )
            )
            return True
        # 모델 변경·큰 시각 간격에서는 연속 증거를 초기화하지만 이미 열린 사건은 유지한다.
        same_model = state["model_version"] == prediction["model_version"] and (
            state["last_observed_at"] is None
            or (prediction["observed_at"] - state["last_observed_at"]).total_seconds() <= 30
        )
        anomaly_streak = state["anomaly_streak"] if same_model else 0
        normal_streak = state["normal_streak"] if same_model else 0
        anomaly_streak = anomaly_streak + 1 if prediction["is_anomaly"] else 0
        normal_streak = normal_streak + 1 if not prediction["is_anomaly"] else 0
        active = state["active_incident_id"]
        if active is None and anomaly_streak >= 2:
            try:
                manual = manual_path.read_text(encoding="utf-8")
                if not manual.strip():
                    raise ValueError("empty manual")
            except (OSError, UnicodeError, ValueError):
                manual = None
            active = conn.execute(
                incidents.insert()
                .values(
                    asset_id=asset_id,
                    response_mode=response_mode,
                    opened_prediction_id=prediction["id"],
                    manual_content=manual,
                    manual_sha256=hashlib.sha256(manual.encode()).hexdigest() if manual else None,
                )
                .returning(incidents.c.id)
            ).scalar_one()
            enqueue_job(conn, active, response_mode, f"incident:{active}:revision:1:auto")
            if response_mode == "shadow":
                conn.execute(
                    actions.insert().values(
                        incident_id=active,
                        action="mock_cooling_boost",
                        status="pending" if manual else "blocked",
                        last_error=None if manual else "approved learning manual unavailable",
                    )
                )
            enqueue_notice(conn, active, "opened", f"{asset_id}: mock measurement anomaly opened")
            if not manual:
                enqueue_notice(conn, active, "blocked", "Mock action blocked: manual unavailable")
        if active is not None and normal_streak >= 3:
            conn.execute(
                incidents.update()
                .where(incidents.c.id == active)
                .values(
                    status="pattern_recovered",
                    revision=incidents.c.revision + 1,
                    closed_at=now(),
                    closed_prediction_id=prediction["id"],
                )
            )
            # 패턴이 복귀한 뒤에는 대기·재시도 중인 과거 냉각 요청을 실행하지 않는다.
            conn.execute(
                actions.update()
                .where(actions.c.incident_id == active, actions.c.status.in_(["pending", "retry"]))
                .values(
                    status="cancelled",
                    last_error="pattern recovered before mock action completed",
                    updated_at=now(),
                )
            )
            conn.execute(
                proposals.update()
                .where(
                    proposals.c.incident_id == active,
                    proposals.c.status.in_(["awaiting_approval", "enqueued"]),
                )
                .values(status="cancelled", reason="incident_pattern_recovered")
            )
            enqueue_notice(
                conn, active, "recovered", "Reference pattern returned; not confirmed repair"
            )
            active = None
        conn.execute(
            assets.update()
            .where(assets.c.asset_id == asset_id)
            .values(
                last_observed_at=prediction["observed_at"],
                anomaly_streak=anomaly_streak,
                normal_streak=normal_streak,
                active_incident_id=active,
                model_version=prediction["model_version"],
                updated_at=now(),
            )
        )
        conn.execute(
            predictions.update()
            .where(predictions.c.id == prediction["id"])
            .values(status="handled")
        )
        return True
