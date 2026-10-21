"""로컬 승인자 bearer 인증과 정확한 제안 hash에 묶인 승인·거절·단일 조치 등록."""

import hmac
import os

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ess_platform.agents.schemas import ApprovalInput
from ess_platform.db import actions, approvals, incidents, proposals
from ess_platform.locking import OPERATIONS_LOCK, lock_worker
from ess_platform.operations.policy import policy_reason


class ApprovalUnavailable(RuntimeError):
    """승인 토큰이 설정되지 않아 상태 변경 API를 열 수 없다는 오류."""


def authenticate(authorization: str | None) -> dict:
    """입력: Authorization 헤더. 반환: 서버가 확정한 승인자. body의 identity는 받지
    않는다.
    """
    expected = os.environ.get("ESS_APPROVER_TOKEN", "")
    if len(expected) < 32:
        raise ApprovalUnavailable("ESS_APPROVER_TOKEN must contain at least 32 characters")
    actual = (authorization or "").removeprefix("Bearer ")
    if (
        not authorization
        or not authorization.startswith("Bearer ")
        or not hmac.compare_digest(actual.encode(), expected.encode())
    ):
        raise PermissionError("approver authentication required")
    return {"name": os.environ.get("ESS_APPROVER_ID", "local-operator"), "role": "approver"}


def decide(
    engine: Engine, actor: dict, proposal_id: int, request: ApprovalInput
) -> tuple[int, dict]:
    """입력: DB·인증된 승인자·제안·결정. 반환: HTTP 코드와 상태. 중복·만료·복귀를
    재검증한다.
    """
    if actor.get("role") != "approver":
        raise PermissionError("approver role required")
    with engine.begin() as conn:
        if not lock_worker(conn, OPERATIONS_LOCK):
            return 409, {"error": "operations_busy_retry"}
        proposal = (
            conn.execute(select(proposals).where(proposals.c.id == proposal_id).with_for_update())
            .mappings()
            .one_or_none()
        )
        if proposal is None:
            return 404, {"error": "proposal_not_found"}
        if not hmac.compare_digest(request.proposal_hash, proposal["proposal_hash"]):
            return 409, {"error": "proposal_hash_mismatch"}
        reused = (
            conn.execute(select(approvals).where(approvals.c.request_id == request.request_id))
            .mappings()
            .one_or_none()
        )
        if reused and (
            reused["proposal_id"] != proposal_id or reused["decision"] != request.decision
        ):
            return 409, {"error": "approval_request_identity_conflict"}
        previous = (
            conn.execute(select(approvals).where(approvals.c.proposal_id == proposal_id))
            .mappings()
            .one_or_none()
        )
        if previous:
            if previous["decision"] != request.decision:
                return 409, {"error": "proposal_already_decided"}
            action_id = conn.execute(
                select(actions.c.id).where(actions.c.proposal_id == proposal_id)
            ).scalar_one_or_none()
            return 200, {"status": proposal["status"], "action_id": action_id, "duplicate": True}
        if proposal["status"] != "awaiting_approval":
            return 409, {"error": "proposal_not_awaiting_approval", "status": proposal["status"]}
        reason = policy_reason(conn, proposal)
        if reason:
            conn.execute(
                proposals.update()
                .where(proposals.c.id == proposal_id)
                .values(
                    status="expired" if reason == "proposal_expired" else "cancelled", reason=reason
                )
            )
            return 409, {"error": reason}
        conn.execute(
            approvals.insert().values(
                proposal_id=proposal_id,
                request_id=request.request_id,
                proposal_hash=request.proposal_hash,
                reviewer=actor["name"],
                role=actor["role"],
                decision=request.decision,
            )
        )
        if request.decision == "reject":
            conn.execute(
                proposals.update().where(proposals.c.id == proposal_id).values(status="rejected")
            )
            return 200, {"status": "rejected"}
        exists = conn.execute(
            select(actions.c.id).where(actions.c.incident_id == proposal["incident_id"])
        ).scalar_one_or_none()
        if exists:
            conn.execute(
                proposals.update()
                .where(proposals.c.id == proposal_id)
                .values(status="cancelled", reason="incident_already_has_action")
            )
            return 409, {"error": "incident_already_has_action"}
        # 동일 잠금 아래 승인·사건 상태·제안 검증과 action 등록을 원자적으로 처리한다.
        conn.execute(
            select(incidents).where(incidents.c.id == proposal["incident_id"]).with_for_update()
        )
        action_id = conn.execute(
            actions.insert()
            .values(
                incident_id=proposal["incident_id"],
                proposal_id=proposal_id,
                command_key=f"proposal:{proposal['proposal_hash']}",
                action="mock_cooling_boost",
                status="pending",
            )
            .returning(actions.c.id)
        ).scalar_one()
        conn.execute(
            proposals.update().where(proposals.c.id == proposal_id).values(status="enqueued")
        )
        return 200, {"status": "enqueued", "action_id": action_id}
