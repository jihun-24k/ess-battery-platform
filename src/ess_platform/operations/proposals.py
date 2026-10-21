"""Agent가 선택한 대응 의도를 승인 가능한 제안으로 저장한다. 여기서는 조치를 실행하지
않는다.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from ess_platform.agents.schemas import AgentContext
from ess_platform.db import agent_jobs, agent_runs, agent_steps, approvals, now, proposals, row_dict
from ess_platform.operations.policy import POLICY_VERSION, payload_hash, policy_reason


def store_proposal(conn: Connection, context: AgentContext, run_id: int) -> int | None:
    """입력: 완료 transaction·context·실행 ID. 반환: 검증할 제안 ID 또는 의도가 없으면
    None.
    """
    if context.intent is None:
        return None
    snapshot, intent = context.snapshot, context.intent
    manual_refs = [
        {
            k: context.evidence[ref][k]
            for k in ("chunk_id", "document_id", "version", "sha256", "chunk_hash")
        }
        for ref in intent.manual_refs
    ]
    payload = {
        "action": intent.action,
        "parameters": {},
        "reason": intent.reason,
        "evidence_refs": intent.evidence_refs,
        "manuals": manual_refs,
        "asset_id": snapshot["incident"]["asset_id"],
        "asset_type": snapshot["asset_type"],
        "mode": snapshot["mode"],
        "cutoff": snapshot["cutoff"],
        "model_version": snapshot["prediction"]["model_version"],
        "incident_revision": snapshot["incident"]["revision"],
        "policy_version": POLICY_VERSION,
        "run_id": run_id,
    }
    candidate = {
        "incident_id": snapshot["incident"]["id"],
        "run_id": run_id,
        "incident_revision": snapshot["incident"]["revision"],
        "payload": payload,
        "proposal_hash": payload_hash(payload),
        "expires_at": now() + timedelta(minutes=10),
    }
    reason = policy_reason(conn, candidate)
    status = "awaiting_approval" if reason is None else "needs_review"
    if snapshot["mode"] == "shadow":
        status, reason = "shadow_only", "analysis_does_not_create_actions"
    return conn.execute(
        proposals.insert()
        .values(**candidate, status=status, reason=reason)
        .returning(proposals.c.id)
    ).scalar_one()


def analysis_for_incident(engine: Engine, incident_id: int) -> dict:
    """입력: DB·사건 ID. 반환: 최신 작업·실행·호출·제안·승인 이력의 bounded 목록."""
    with engine.connect() as conn:
        return {
            "jobs": [
                row_dict(r)
                for r in conn.execute(
                    select(agent_jobs)
                    .where(agent_jobs.c.incident_id == incident_id)
                    .order_by(agent_jobs.c.id.desc())
                    .limit(20)
                )
            ],
            "runs": [
                row_dict(r)
                for r in conn.execute(
                    select(agent_runs)
                    .join(agent_jobs, agent_jobs.c.id == agent_runs.c.job_id)
                    .where(agent_jobs.c.incident_id == incident_id)
                    .order_by(agent_runs.c.id.desc())
                    .limit(20)
                )
            ],
            "proposals": [
                row_dict(r)
                for r in conn.execute(
                    select(proposals)
                    .where(proposals.c.incident_id == incident_id)
                    .order_by(proposals.c.id.desc())
                    .limit(20)
                )
            ],
        }


def run_detail(engine: Engine, run_id: int) -> dict | None:
    """입력: DB·실행 ID. 반환: snapshot·사용량·최종 분석과 Tool 호출 순서 또는 None."""
    with engine.connect() as conn:
        run = conn.execute(select(agent_runs).where(agent_runs.c.id == run_id)).one_or_none()
        if not run:
            return None
        return {
            "run": row_dict(run),
            "steps": [
                row_dict(r)
                for r in conn.execute(
                    select(agent_steps)
                    .where(agent_steps.c.run_id == run_id)
                    .order_by(agent_steps.c.id)
                )
            ],
        }


def proposal_detail(engine: Engine, proposal_id: int) -> dict | None:
    """입력: DB·제안 ID. 반환: 정확한 제안·hash·승인 이력 또는 None."""
    with engine.connect() as conn:
        proposal = conn.execute(
            select(proposals).where(proposals.c.id == proposal_id)
        ).one_or_none()
        if not proposal:
            return None
        return {
            "proposal": row_dict(proposal),
            "approvals": [
                row_dict(r)
                for r in conn.execute(
                    select(approvals).where(approvals.c.proposal_id == proposal_id)
                )
            ],
        }
