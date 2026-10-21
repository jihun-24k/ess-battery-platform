"""Agent 영속화·재시작·승인·명령 멱등성과 사건 변화 시 취소를 PostgreSQL로 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from ess_platform.agents.repository import (
    LeaseLost,
    assert_lease,
    claim_job,
    enqueue_job,
    start_run,
)
from ess_platform.agents.schemas import ApprovalInput
from ess_platform.agents.worker import agent_tick
from ess_platform.db import (
    actions,
    agent_jobs,
    agent_runs,
    assets,
    documents,
    incidents,
    now,
    proposals,
)
from ess_platform.operations.actions import action_tick
from ess_platform.operations.approvals import ApprovalUnavailable, authenticate, decide
from ess_platform.operations.proposals import analysis_for_incident, proposal_detail, run_detail
from tests.agent_helpers import ScriptedClient

ACTOR = {"name": "test-operator", "role": "approver"}


def completed_proposal(fixture):
    """입력: 사건 fixture. 반환: 분석 완료 후 승인 대기 제안. 테스트용 LLM 대역만
    사용한다.
    """
    engine, job, context = fixture
    assert agent_tick(engine, ScriptedClient(context))
    data = analysis_for_incident(engine, job["incident_id"])
    assert data["runs"][0]["provider"] == "test_fixture"
    return data["proposals"][0]


def decision(proposal, request_id="test-approval", choice="approve"):
    """입력: 제안·요청 ID·선택. 반환: 정확한 hash를 가진 승인 입력."""
    return ApprovalInput(
        request_id=request_id, proposal_hash=proposal["proposal_hash"], decision=choice
    )


def test_assisted_mode_only_creates_job_and_proposal(agent_incident):
    """입력: assisted 사건. 반환: 없음. 승인 전에는 조치가 생기지 않으며 분석/근거가
    영속화된다.
    """
    engine, job, _ = agent_incident
    with engine.connect() as conn:
        assert conn.execute(select(actions)).first() is None
    proposal = completed_proposal(agent_incident)
    assert proposal["status"] == "awaiting_approval"
    detail = run_detail(engine, proposal["run_id"])
    assert len(detail["steps"]) == 4 and detail["run"]["result"]["proposal_id"] == proposal["id"]
    assert proposal_detail(engine, proposal["id"])["approvals"] == []
    assert not agent_tick(engine, ScriptedClient(agent_incident[2]))


def test_approval_and_execution_are_idempotent(agent_incident):
    """입력: 제안 fixture. 반환: 없음. 단일 승인·단일 명령·DB 냉각 상태 변경을 검증한다."""
    engine, _, _ = agent_incident
    proposal = completed_proposal(agent_incident)
    status, body = decide(engine, ACTOR, proposal["id"], decision(proposal))
    assert status == 200 and body["status"] == "enqueued"
    assert decide(engine, ACTOR, proposal["id"], decision(proposal))[1]["duplicate"]
    assert action_tick(engine)
    assert not action_tick(engine)
    with engine.connect() as conn:
        assert conn.execute(select(assets.c.cooling_mode)).scalar_one() == "boost"
        assert conn.execute(select(actions.c.attempts)).scalar_one() == 1
    assert proposal_detail(engine, proposal["id"])["proposal"]["status"] == "completed"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("expire", "proposal_expired"),
        ("recover", "incident_changed_or_recovered"),
        ("withdraw", "manual_withdrawn_or_wrong_scope"),
        ("tamper", "proposal_hash_mismatch"),
        ("stale", "observations_changed_reanalysis_required"),
    ],
)
def test_stale_approval_is_rejected(agent_incident, change, reason):
    """입력: 제안과 만료/복귀/철회/변조/시간 변화. 반환: 없음. 오래된 승인으로 조치를
    만들지 않는다.
    """
    engine, _, _ = agent_incident
    proposal = completed_proposal(agent_incident)
    with engine.begin() as conn:
        if change == "expire":
            conn.execute(proposals.update().values(expires_at=now() - timedelta(seconds=1)))
        elif change == "recover":
            conn.execute(incidents.update().values(status="pattern_recovered", revision=2))
        elif change == "withdraw":
            conn.execute(documents.update().values(active=False))
        elif change == "tamper":
            conn.execute(proposals.update().values(payload={"action": "bad"}))
        else:
            conn.execute(assets.update().values(last_observed_at=now()))
    code, body = decide(engine, ACTOR, proposal["id"], decision(proposal))
    assert code == 409 and body["error"] == reason
    with engine.connect() as conn:
        assert conn.execute(select(actions)).first() is None


def test_executor_rechecks_after_approval(agent_incident):
    """입력: 승인 뒤 문서가 철회된 제안. 반환: 없음. 실행 직전에도 정책을 재검사한다."""
    engine, _, _ = agent_incident
    proposal = completed_proposal(agent_incident)
    decide(engine, ACTOR, proposal["id"], decision(proposal))
    with engine.begin() as conn:
        conn.execute(documents.update().values(active=False))
    assert action_tick(engine)
    with engine.connect() as conn:
        assert conn.execute(select(actions.c.status)).scalar_one() == "blocked"
        assert conn.execute(select(assets.c.cooling_mode)).scalar_one() == "normal"


def test_concurrent_approval_and_reject(agent_incident):
    """입력: 같은 제안의 동시 승인. 반환: 없음. 하나의 조치만 생성하고 상반된 재결정을
    거부한다.
    """
    engine, _, _ = agent_incident
    proposal = completed_proposal(agent_incident)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(lambda _: decide(engine, ACTOR, proposal["id"], decision(proposal)), range(3))
        )
    assert any(code == 200 for code, _ in results)
    with engine.connect() as conn:
        assert len(conn.execute(select(actions)).all()) == 1
    assert decide(engine, ACTOR, proposal["id"], decision(proposal, "reject", "reject"))[0] == 409


def test_reject_and_authentication(agent_incident, monkeypatch):
    """입력: 승인 환경·결정. 반환: 없음. 토큰·서버 역할·거절 상태를 검증한다."""
    monkeypatch.delenv("ESS_APPROVER_TOKEN", raising=False)
    with pytest.raises(ApprovalUnavailable):
        authenticate(None)
    monkeypatch.setenv("ESS_APPROVER_TOKEN", "test-only-" + "x" * 40)
    with pytest.raises(PermissionError):
        authenticate("Bearer wrong")
    assert authenticate("Bearer " + "test-only-" + "x" * 40)["role"] == "approver"
    engine, _, _ = agent_incident
    proposal = completed_proposal(agent_incident)
    assert (
        decide(engine, ACTOR, proposal["id"], decision(proposal, choice="reject"))[1]["status"]
        == "rejected"
    )
    assert not action_tick(engine)
    with pytest.raises(PermissionError):
        decide(engine, {"role": "viewer"}, proposal["id"], decision(proposal))


def test_lease_reclaim_and_late_worker_fencing(agent_incident):
    """입력: 만료 작업. 반환: 없음. 새 token을 발급하고 오래된 워커의 완료를 차단한다."""
    engine, _, context = agent_incident
    old = claim_job(engine)
    start_run(engine, old, context, "test", "test")
    assert claim_job(engine) is None
    with engine.begin() as conn:
        conn.execute(agent_jobs.update().values(lease_until=now() - timedelta(seconds=1)))
    new = claim_job(engine)
    assert new["lease_token"] != old["lease_token"] and new["attempts"] == 2
    with engine.begin() as conn:
        with pytest.raises(LeaseLost):
            assert_lease(conn, old)
    with engine.connect() as conn:
        assert conn.execute(select(agent_runs.c.status)).scalar_one() == "abandoned"


def test_recovered_job_cancelled_without_llm(agent_incident):
    """입력: 분석 전 복귀한 사건. 반환: 없음. LLM 호출 비용 없이 작업을 취소한다."""
    engine, _, context = agent_incident
    with engine.begin() as conn:
        conn.execute(incidents.update().values(status="pattern_recovered", revision=2))
    client = ScriptedClient(context)
    assert agent_tick(engine, client) and client.position == 0
    with engine.connect() as conn:
        assert conn.execute(select(agent_jobs.c.status)).scalar_one() == "cancelled"


def test_enqueue_duplicate_and_wrong_identity(agent_incident):
    """입력: 반복 분석 요청 ID. 반환: 없음. 같은 요청은 재사용하며 모드가 다르면
    충돌한다.
    """
    engine, job, _ = agent_incident
    with engine.begin() as conn:
        assert enqueue_job(conn, job["incident_id"], "assisted", job["request_key"]) == job["id"]
        with pytest.raises(ValueError):
            enqueue_job(conn, job["incident_id"], "shadow", job["request_key"])


def test_transient_llm_error_retries_without_action(agent_incident):
    """입력: 일시 통신 실패 대역. 반환: 없음. 재시도 상태와 조치 미생성을 검증한다."""
    import httpx2
    from openai import APIConnectionError

    engine, _, _ = agent_incident

    class TransientClient:
        """API 연결 실패만 재현하는 외부 통신 없는 테스트 client."""

        provider = "test"
        model = "transient"

        def next_turn(self, *args):
            """입력: 호출 요청. 반환: 없음. SDK가 정의한 연결 오류를 발생시킨다."""
            raise APIConnectionError(request=httpx2.Request("POST", "https://example.invalid"))

    assert agent_tick(engine, TransientClient())
    with engine.connect() as conn:
        assert conn.execute(select(agent_jobs.c.status)).scalar_one() == "retry"
        assert conn.execute(select(actions)).first() is None


def test_recent_quality_prevents_proposal(agent_incident):
    """입력: 품질 오류가 추가된 사건. 반환: 없음. LLM 선택과 무관하게 대응 등록을 차단한다."""
    from ess_platform.agents.repository import build_context
    from ess_platform.db import issues

    engine, job, _ = agent_incident
    with engine.begin() as conn:
        conn.execute(
            issues.insert().values(measurement_id=2, code="TIME_REVERSED", detail={"test": True})
        )
    context = build_context(engine, job)
    client = ScriptedClient(context)
    # 대역은 계속 제안하지만 서버가 거부하므로 예산 종료 후 검토 필요로 남는다.
    assert agent_tick(engine, client)
    with engine.connect() as conn:
        assert conn.execute(select(agent_jobs.c.status)).scalar_one() == "needs_review"
        assert conn.execute(select(actions)).first() is None


def test_proposal_intent_can_be_withdrawn(agent_incident):
    """입력: 아직 실행되지 않은 대응 의도. 반환: 없음. 후속 조사에서 보류로 안전하게 철회한다."""
    from ess_platform.agents.runner import run_agent
    from ess_platform.agents.schemas import ToolCall
    from ess_platform.agents.tools import execute_tool

    engine, _, context = agent_incident
    result, _ = run_agent(engine, context, ScriptedClient(context), lambda *args: None)
    assert context.intent is not None
    body = result.model_dump()
    body["disposition"] = "needs_review"
    assert execute_tool(engine, context, ToolCall("withdraw", "finish_analysis", body))["ok"]
    assert context.intent is None


def test_exhausted_job_does_not_starve_next_job(agent_incident):
    """입력: 횟수 초과 작업과 새 작업. 반환: 없음. 실패 작업 뒤의 정상 작업도 claim할 수 있다."""
    engine, job, _ = agent_incident
    with engine.begin() as conn:
        conn.execute(
            agent_jobs.update().values(
                status="running", attempts=3, lease_until=now() - timedelta(seconds=1)
            )
        )
        next_id = enqueue_job(conn, job["incident_id"], "assisted", "next-job")
    assert claim_job(engine)["id"] == next_id
