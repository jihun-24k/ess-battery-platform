"""기존 데이터 보존 migration과 인증·분석 조회·승인 HTTP 경계를 실제 PostgreSQL로
검증한다.
"""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import select, text

from ess_platform.agents.worker import agent_tick
from ess_platform.db import actions, agent_jobs, agent_runs, incidents
from ess_platform.migrations import migrate
from ess_platform.operations.proposals import analysis_for_incident
from tests.agent_helpers import ScriptedClient
from tests.helpers import send, service


def authorized_post(url, body, token):
    """입력: URL·JSON·테스트 토큰. 반환: 상태 코드와 body. 실제 수신자에게 메시지를
    보내지 않는다.
    """
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urlopen(request) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def test_additive_migration_preserves_legacy_incident(agent_incident):
    """입력: 격리된 기존 사건. 반환: 없음. 옛 컬럼 구조에서 upgrade·재적용 시 데이터를
    보존한다.
    """
    engine, _, _ = agent_incident
    schema = engine.get_execution_options()["schema_translate_map"][None]
    prefix = '"' + schema + '".'
    with engine.begin() as conn:
        for table, column in [
            ("rt_incidents", "revision"),
            ("rt_incidents", "response_mode"),
            ("rt_actions", "proposal_id"),
            ("rt_actions", "command_key"),
        ]:
            conn.execute(text(f"ALTER TABLE {prefix}{table} DROP COLUMN {column}"))
    assert migrate(engine)["status"] == "applied"
    assert migrate(engine)["status"] == "already_applied"
    with engine.connect() as conn:
        row = conn.execute(select(incidents)).mappings().one()
        assert row["status"] == "open" and row["response_mode"] == "legacy" and row["revision"] == 1
        assert conn.execute(select(agent_jobs)).first() is not None


def test_api_analysis_and_bearer_approval(agent_incident, monkeypatch):
    """입력: 실제 운영 API·토큰 fixture. 반환: 없음. 조회·인증 실패·정확한 승인·중복
    요청을 검증한다.
    """
    engine, job, context = agent_incident
    agent_tick(engine, ScriptedClient(context))
    analysis = analysis_for_incident(engine, job["incident_id"])
    proposal = analysis["proposals"][0]
    token = "test-authentication-only-" + "x" * 40
    monkeypatch.delenv("ESS_APPROVER_TOKEN", raising=False)
    with service(engine, "operations") as url:
        path = f"/api/action-proposals/{proposal['id']}/approval"
        body = {
            "request_id": "api-approve-1",
            "proposal_hash": proposal["proposal_hash"],
            "decision": "approve",
        }
        assert send(url + path, body)[0] == 503
        monkeypatch.setenv("ESS_APPROVER_TOKEN", token)
        assert send(url + path, body)[0] == 403
        assert authorized_post(url + path, body, "wrong")[0] == 403
        assert authorized_post(url + path, body, token)[1]["status"] == "enqueued"
        assert authorized_post(url + path, body, token)[1]["duplicate"]
        assert send(url + f"/api/incidents/{job['incident_id']}/analysis")[1]["runs"]
        assert send(url + f"/api/agent-runs/{proposal['run_id']}")[1]["steps"]
        assert send(url + f"/api/action-proposals/{proposal['id']}")[1]["approvals"]
        assert send(url + "/api/agent-runs/999")[0] == 404
        assert send(url + "/api/action-proposals/999")[0] == 404
        request = {"request_id": "reanalysis-1", "reason": "regression test"}
        first = authorized_post(
            url + f"/api/incidents/{job['incident_id']}/analysis-jobs", request, token
        )
        second = authorized_post(
            url + f"/api/incidents/{job['incident_id']}/analysis-jobs", request, token
        )
        assert first == second
        changed = {**request, "reason": "different payload for same request"}
        assert (
            authorized_post(
                url + f"/api/incidents/{job['incident_id']}/analysis-jobs", changed, token
            )[0]
            == 409
        )
        assert authorized_post(url + "/api/incidents/999/analysis-jobs", request, token)[0] == 404


def test_worker_failure_requires_review_and_preserves_core(agent_incident):
    """입력: LLM 호출 실패 대역. 반환: 없음. 실패 상태를 영속화하며 조치를 만들지
    않는다.
    """
    engine, _, context = agent_incident

    class BrokenClient:
        """외부 API 오류를 재현하는 프로그램 테스트 client."""

        provider = "test"
        model = "broken"

        def next_turn(self, *args):
            """입력: 호출 요청. 반환: 없음. 유효하지 않은 모델 응답을 재현한다."""
            raise ValueError("invalid response fixture")

    assert agent_tick(engine, BrokenClient())
    with engine.connect() as conn:
        assert conn.execute(select(agent_jobs.c.status)).scalar_one() == "needs_review"
        assert conn.execute(select(agent_runs.c.error)).scalar_one() == "ValueError"
        assert conn.execute(select(actions)).first() is None


def test_shadow_proposal_cannot_be_approved(agent_incident):
    """입력: 관찰 모드 작업. 반환: 없음. 분석 제안이 있어도 자동 조치를 만들지 않는다."""
    from ess_platform.agents.schemas import ApprovalInput
    from ess_platform.operations.approvals import decide

    engine, job, context = agent_incident
    with engine.begin() as conn:
        conn.execute(agent_jobs.update().values(mode="shadow"))
        conn.execute(incidents.update().values(response_mode="shadow"))
    agent_tick(engine, ScriptedClient(context))
    proposal = analysis_for_incident(engine, job["incident_id"])["proposals"][0]
    assert proposal["status"] == "shadow_only"
    request = ApprovalInput(
        request_id="shadow", proposal_hash=proposal["proposal_hash"], decision="approve"
    )
    assert decide(engine, {"name": "test", "role": "approver"}, proposal["id"], request)[0] == 409
