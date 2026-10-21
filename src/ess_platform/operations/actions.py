"""고정 정책을 검사한 뒤 가상 냉각 상태·조치 결과·재시도를 DB에 저장한다."""

import hashlib
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ess_platform.db import actions, approvals, assets, incidents, now, proposals
from ess_platform.locking import OPERATIONS_LOCK, lock_worker
from ess_platform.operations.notifications import enqueue_notice
from ess_platform.operations.policy import policy_reason

MAX_ATTEMPTS = 3


def action_tick(engine: Engine, simulate_failure: bool = False) -> bool:
    """입력: DB engine·실패 주입 여부. 반환: 모의 조치 하나를 처리했으면 True.

    문서 hash와 고정 정책을 검사하고 DB 가상 냉각 상태·결과·재시도를 원자적으로
    기록한다.
    """
    with engine.begin() as conn:
        if not lock_worker(conn, OPERATIONS_LOCK):
            return False
        action = (
            conn.execute(
                select(actions)
                .where(actions.c.status.in_(["pending", "retry"]))
                .order_by(actions.c.id)
                .limit(1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if action is None or action["next_attempt_at"] > now():
            return False
        incident = (
            conn.execute(
                select(incidents).where(incidents.c.id == action["incident_id"]).with_for_update()
            )
            .mappings()
            .one()
        )
        asset = (
            conn.execute(
                select(assets).where(assets.c.asset_id == incident["asset_id"]).with_for_update()
            )
            .mappings()
            .one()
        )
        agent_block_reason = None
        if incident["response_mode"] == "assisted":
            proposal = (
                conn.execute(select(proposals).where(proposals.c.id == action["proposal_id"]))
                .mappings()
                .one_or_none()
            )
            approval = (
                conn.execute(
                    select(approvals).where(
                        approvals.c.proposal_id == action["proposal_id"],
                        approvals.c.decision == "approve",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if not proposal or not approval or proposal["status"] != "enqueued":
                agent_block_reason = "approved_proposal_required"
            elif approval["proposal_hash"] != proposal["proposal_hash"]:
                agent_block_reason = "approval_hash_mismatch"
            else:
                agent_block_reason = policy_reason(conn, proposal)
        if agent_block_reason:
            status, error = "blocked", agent_block_reason
        elif incident["status"] != "open":
            status, error = "cancelled", "incident pattern already recovered"
        elif (
            not incident["manual_content"]
            or hashlib.sha256(incident["manual_content"].encode()).hexdigest()
            != incident["manual_sha256"]
            or action["action"] != "mock_cooling_boost"
            or incident["kind"] != "measurement_anomaly"
        ):
            status, error = "blocked", "mock policy rejected action"
        elif simulate_failure:
            status = "failed" if action["attempts"] + 1 >= MAX_ATTEMPTS else "retry"
            error = "simulated cooling failure"
        else:
            status = "already_applied" if asset["cooling_mode"] == "boost" else "applied"
            error = None
            conn.execute(
                assets.update()
                .where(assets.c.asset_id == incident["asset_id"])
                .values(cooling_mode="boost", updated_at=now())
            )
        conn.execute(
            actions.update()
            .where(actions.c.id == action["id"])
            .values(
                status=status,
                attempts=action["attempts"] + 1,
                last_error=error,
                next_attempt_at=now() + timedelta(seconds=2 ** (action["attempts"] + 1)),
                updated_at=now(),
            )
        )
        if action["proposal_id"] and status != "retry":
            conn.execute(
                proposals.update()
                .where(proposals.c.id == action["proposal_id"])
                .values(
                    status="completed" if status in {"applied", "already_applied"} else status,
                    reason=error,
                )
            )
        if status != "retry":
            enqueue_notice(conn, incident["id"], "action", f"Mock cooling result: {status}")
        return True
