"""분석 작업·lease·사건 snapshot·Tool 실행·최종 결과를 짧은 DB transaction으로 저장한다."""

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from ess_platform.agents.schemas import AgentContext
from ess_platform.db import (
    actions,
    agent_jobs,
    agent_runs,
    agent_steps,
    assets,
    incidents,
    issues,
    measurements,
    models,
    now,
    predictions,
    row_dict,
)


class LeaseLost(RuntimeError):
    """작업 소유 token이 바뀌었거나 만료되어 결과를 저장할 수 없다는 오류."""


def enqueue_job(
    conn: Connection,
    incident_id: int,
    mode: str,
    request_key: str,
    request_context: dict | None = None,
) -> int:
    """입력: 같은 transaction의 연결·사건·모드·고유 요청 키. 반환: 신규 또는 기존 작업
    ID.
    """
    request_context = request_context or {}
    inserted = conn.execute(
        insert(agent_jobs)
        .values(
            incident_id=incident_id,
            mode=mode,
            request_key=request_key,
            request_context=request_context,
        )
        .on_conflict_do_nothing(index_elements=["request_key"])
        .returning(agent_jobs.c.id)
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    row = (
        conn.execute(select(agent_jobs).where(agent_jobs.c.request_key == request_key))
        .mappings()
        .one()
    )
    if (
        row["incident_id"] != incident_id
        or row["mode"] != mode
        or row["request_context"] != request_context
    ):
        raise ValueError("analysis request identity conflict")
    return row["id"]


def claim_job(engine: Engine, lease_seconds: int = 90) -> dict | None:
    """입력: DB·lease 초. 반환: claim한 작업 또는 None. 만료 lease는 새 token으로
    회수한다.
    """
    if not 1 <= lease_seconds <= 300:
        raise ValueError("lease_seconds must be 1..300")
    with engine.begin() as conn:
        expired = and_(agent_jobs.c.status == "running", agent_jobs.c.lease_until < now())
        exhausted = (
            conn.execute(
                agent_jobs.update()
                .where(
                    agent_jobs.c.attempts >= 3,
                    or_(agent_jobs.c.status.in_(["queued", "retry"]), expired),
                )
                .values(
                    status="failed",
                    last_error="attempt limit reached",
                    lease_token=None,
                    lease_until=None,
                )
                .returning(agent_jobs.c.id)
            )
            .scalars()
            .all()
        )
        if exhausted:
            conn.execute(
                agent_runs.update()
                .where(agent_runs.c.job_id.in_(exhausted), agent_runs.c.status == "running")
                .values(status="abandoned", finished_at=now())
            )
        row = (
            conn.execute(
                select(agent_jobs)
                .where(
                    or_(
                        and_(
                            agent_jobs.c.status.in_(["queued", "retry"]),
                            agent_jobs.c.next_attempt_at <= now(),
                        ),
                        expired,
                    )
                )
                .order_by(agent_jobs.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        conn.execute(
            agent_runs.update()
            .where(agent_runs.c.job_id == row["id"], agent_runs.c.status == "running")
            .values(status="abandoned", finished_at=now())
        )
        token = str(uuid4())
        values = {
            "status": "running",
            "attempts": row["attempts"] + 1,
            "lease_token": token,
            "lease_until": now() + timedelta(seconds=lease_seconds),
        }
        conn.execute(agent_jobs.update().where(agent_jobs.c.id == row["id"]).values(**values))
        return {**row, **values}


def assert_lease(conn: Connection, job: dict) -> None:
    """입력: DB 연결·claim 결과. 반환: 없음. 현 소유권·유효기간을 확인하며 실패 시
    LeaseLost.
    """
    valid = conn.execute(
        select(agent_jobs.c.id)
        .where(
            agent_jobs.c.id == job["id"],
            agent_jobs.c.lease_token == job["lease_token"],
            agent_jobs.c.status == "running",
            agent_jobs.c.lease_until > now(),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if valid is None:
        raise LeaseLost("agent lease expired or replaced")


def build_context(engine: Engine, job: dict) -> AgentContext:
    """입력: DB·할당 작업. 반환: 사건·원천·모델·조회 cutoff가 고정된 AgentContext."""
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        incident = conn.execute(select(incidents).where(incidents.c.id == job["incident_id"])).one()
        prediction = conn.execute(
            select(predictions).where(predictions.c.id == incident.opened_prediction_id)
        ).one()
        reading = conn.execute(
            select(measurements).where(measurements.c.id == prediction.measurement_id)
        ).one()
        asset = conn.execute(select(assets).where(assets.c.asset_id == incident.asset_id)).one()
        model = conn.execute(
            select(models).where(models.c.version == prediction.model_version)
        ).one()
        cutoff = asset.last_observed_at or prediction.observed_at
        latest = conn.execute(
            select(predictions)
            .where(predictions.c.asset_id == incident.asset_id, predictions.c.observed_at <= cutoff)
            .order_by(predictions.c.observed_at.desc())
            .limit(1)
        ).one()
        quality_count = conn.execute(
            select(func.count())
            .select_from(issues)
            .join(measurements, measurements.c.id == issues.c.measurement_id)
            .where(
                measurements.c.asset_id == incident.asset_id,
                measurements.c.observed_at <= cutoff,
                measurements.c.observed_at >= cutoff - timedelta(seconds=30),
            )
        ).scalar_one()
        action_count = conn.execute(
            select(func.count()).select_from(actions).where(actions.c.incident_id == incident.id)
        ).scalar_one()
        snapshot = {
            "incident": {**row_dict(incident), "ref": f"incident:{incident.id}"},
            "prediction": {**row_dict(prediction), "ref": f"prediction:{prediction.id}"},
            "trigger_measurement": {**row_dict(reading), "ref": f"measurement:{reading.id}"},
            "model": {**row_dict(model), "ref": f"model:{model.version}"},
            "latest_prediction": {**row_dict(latest), "ref": f"prediction:{latest.id}"},
            "quality_issue_count": quality_count,
            "existing_action_count": action_count,
            "asset_type": "simulation_cell"
            if reading.source == "synthetic_replay"
            else "unknown_physical",
            "cutoff": cutoff.isoformat(),
            "mode": job["mode"],
            "request_context": job.get("request_context", {}),
            "meaning": "pattern anomaly; not a confirmed physical fault",
        }
    evidence = {
        f"incident:{incident.id}": row_dict(incident),
        f"model:{model.version}": row_dict(model),
        f"quality_summary:{incident.id}": {
            "issue_count": quality_count,
            "cutoff": cutoff.isoformat(),
        },
        f"measurement:{reading.id}": row_dict(reading),
        f"prediction:{prediction.id}": row_dict(prediction),
        f"prediction:{latest.id}": row_dict(latest),
    }
    return AgentContext(snapshot=snapshot, evidence=evidence)


def start_run(engine: Engine, job: dict, context: AgentContext, provider: str, model: str) -> int:
    """입력: 작업·snapshot·공급자·모델. 반환: 소유권이 확인된 새 실행 ID."""
    with engine.begin() as conn:
        assert_lease(conn, job)
        return conn.execute(
            agent_runs.insert()
            .values(
                job_id=job["id"],
                attempt=job["attempts"],
                lease_token=job["lease_token"],
                snapshot=context.snapshot,
                provider=provider,
                model=model,
                prompt_version="investigate-3",
                toolset_version="tools-3",
            )
            .returning(agent_runs.c.id)
        ).scalar_one()


def save_step(
    engine: Engine, job: dict, run_id: int, index: int, call, result: dict, elapsed: int
) -> None:
    """입력: 소유 작업·실행 ID·Tool 요청/결과·시간. 반환: 없음. 내부 LLM 추론은 저장하지
    않는다.
    """
    with engine.begin() as conn:
        assert_lease(conn, job)
        conn.execute(
            insert(agent_steps)
            .values(
                run_id=run_id,
                step_index=index,
                call_id=call.call_id,
                tool=call.name,
                arguments=call.arguments,
                result=result,
                elapsed_ms=elapsed,
            )
            .on_conflict_do_nothing(index_elements=["run_id", "call_id"])
        )


def finish_run(
    engine: Engine,
    job: dict,
    run_id: int,
    status: str,
    result: dict | None,
    usage: dict,
    error: str | None = None,
) -> None:
    """입력: 실행 결과·사용량·오류 코드. 반환: 없음. lease 유효 시 작업과 실행 상태를
    함께 저장한다.
    """
    with engine.begin() as conn:
        save_completion(conn, job, run_id, status, result, usage, error)


def context_cutoff(context: AgentContext) -> datetime:
    """입력: 고정 snapshot. 반환: Tool 조회·실행 신선도 검사에 사용할 timezone 포함
    cutoff.
    """
    return datetime.fromisoformat(context.snapshot["cutoff"])


def save_completion(
    conn: Connection,
    job: dict,
    run_id: int,
    status: str,
    result: dict | None,
    usage: dict,
    error: str | None = None,
) -> None:
    """입력: 외부 완료 transaction과 결과. 반환: 없음. 제안과 실행 완료를 원자적으로
    묶는 저장 경계.
    """
    assert_lease(conn, job)
    conn.execute(
        agent_runs.update()
        .where(agent_runs.c.id == run_id)
        .values(status=status, result=result, usage=usage, error=error, finished_at=now())
    )
    conn.execute(
        agent_jobs.update()
        .where(agent_jobs.c.id == job["id"])
        .values(
            status=status,
            lease_token=None,
            lease_until=None,
            last_error=error,
            next_attempt_at=now() + timedelta(seconds=2 ** job["attempts"]),
        )
    )
