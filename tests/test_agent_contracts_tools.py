"""Agent 계약·출처·조회 범위·도구 선택 예산과 SDK 경계를 검증한다."""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from ess_platform.agents.llm_client import OpenAIClient
from ess_platform.agents.retrieval import search_manuals, seed_learning_manual
from ess_platform.agents.runner import BudgetExceeded, run_agent
from ess_platform.agents.schemas import Assessment, ToolCall, Turn, WindowArgs
from ess_platform.agents.tools import execute_tool, tool_definitions, validate_refs
from ess_platform.agents.worker import agent_tick
from ess_platform.db import actions, assets, documents, proposals
from ess_platform.operations.actions import action_tick
from ess_platform.operations.proposals import analysis_for_incident, run_detail
from tests.agent_helpers import ScriptedClient


@pytest.mark.parametrize(
    "values",
    [
        {"before_seconds": 0, "limit": 1},
        {"before_seconds": 1, "limit": 201},
        {"before_seconds": "1", "limit": 10},
        {"before_seconds": 1, "limit": 10, "asset_id": "other"},
    ],
)
def test_tool_input_scope_contract(values):
    """입력: 잘못된 Tool 인자. 반환: 없음. 범위 초과·자산 추가·강제 타입 변환을
    거부한다.
    """
    with pytest.raises(ValidationError):
        WindowArgs.model_validate(values)


def test_tool_registry_and_refs(agent_incident):
    """입력: 사건 fixture. 반환: 없음. 임의 명령·허위 출처·다른 사건 action을 차단한다."""
    engine, _, context = agent_incident
    assert all(t["strict"] for t in tool_definitions())
    assert (
        execute_tool(engine, context, ToolCall("x", "execute_shell", {"cmd": "bad"}))["ok"] is False
    )
    assert not execute_tool(
        engine,
        context,
        ToolCall(
            "x", "get_measurement_window", {"before_seconds": 10, "limit": 5, "asset_id": "other"}
        ),
    )["ok"]
    assert not execute_tool(
        engine, context, ToolCall("x", "get_action_status", {"action_id": 999})
    )["ok"]
    with pytest.raises(ValueError):
        validate_refs(context, ["measurement:99999"])
    for name, args in [
        ("get_incident_context", {}),
        ("get_quality_issues", {"before_seconds": 60, "limit": 20}),
        ("get_action_history", {}),
    ]:
        assert execute_tool(engine, context, ToolCall(name, name, args))["ok"]
    bad = {
        "action": "mock_cooling_boost",
        "reason": "x",
        "evidence_refs": ["measurement:999"],
        "manual_refs": ["document:999"],
    }
    assert not execute_tool(engine, context, ToolCall("bad", "submit_response_proposal", bad))["ok"]


def test_manual_search_version_scope_and_tamper(agent_incident):
    """입력: 사건 fixture. 반환: 없음. 재등록·검색·자산 필터·문서 변조 검사를 검증한다."""
    engine, _, context = agent_incident
    assert seed_learning_manual(engine)["status"] == "existing"
    found = search_manuals(engine, context.snapshot["asset_type"], "온도 냉각", 3)
    assert found and found[0]["approval_scope"] == "simulation"
    assert search_manuals(engine, "unknown_physical", "온도", 3) == []
    assert search_manuals(engine, "simulation_cell", "unrelatedkeywordxyz", 3) == []
    with engine.begin() as conn:
        conn.execute(documents.update().values(content="modified"))
    assert search_manuals(engine, "simulation_cell", "온도", 3) == []


def test_agent_loop_selects_tools_and_finishes(agent_incident):
    """입력: 사건 fixture. 반환: 없음. 실제 Tool 결과로 분석을 구성하는 프로그램 loop를
    검증한다.
    """
    engine, _, context = agent_incident
    steps = []
    assessment, usage = run_agent(
        engine, context, ScriptedClient(context), lambda *step: steps.append(step)
    )
    assert assessment.disposition == "propose_action"
    assert context.intent is not None
    assert [step[1].name for step in steps] == [
        "get_measurement_window",
        "search_response_manuals",
        "submit_response_proposal",
        "finish_analysis",
    ]
    assert usage["tool_calls"] == 4 and usage["input_tokens"] == 40


def test_unknown_tool_is_recorded_and_analysis_recovers_without_action(agent_incident):
    """입력: 독립 PostgreSQL 사건 fixture. 반환: 없음.

    미등록 제어 Tool 요청을 주입해 거부·오류 전달·정상 조회 복귀·조치 미실행을 검증한다.
    실제 LLM 대신 결정적 응답 대역을 사용하며 현장 설비에는 연결하지 않는다.
    """
    engine, job, context = agent_incident

    class WrongToolThenReviewClient(ScriptedClient):
        """미등록 Tool을 한 번 요청한 뒤 측정·문서 조회와 검토 필요 분석을 반환한다."""

        def __init__(self, context):
            """입력: 사건 context. 반환: 실패 주입 및 오류 수신 확인 상태를 가진 대역."""
            super().__init__(context, propose=False)
            self.injected = False
            self.received_rejection = False

        def next_turn(self, messages, tools, timeout):
            """입력: 대화·도구 목록·예산. 반환: 미등록 요청 또는 오류 확인 후 정상 호출."""
            if not self.injected:
                self.injected = True
                assert "set_factory_temperature" not in {tool["name"] for tool in tools}
                return Turn(
                    [ToolCall("injected-wrong-tool", "set_factory_temperature", {"target_c": 18})],
                    {"input_tokens": 0, "output_tokens": 0},
                )
            rejected = next(
                message for message in messages if message.get("call_id") == "injected-wrong-tool"
            )
            assert json.loads(rejected["output"]) == {
                "ok": False,
                "error": {"code": "unknown_tool", "retryable": False},
            }
            self.received_rejection = True
            return super().next_turn(messages, tools, timeout)

    client = WrongToolThenReviewClient(context)
    assert agent_tick(engine, client)
    analysis = analysis_for_incident(engine, job["incident_id"])
    run = analysis["runs"][0]
    detail = run_detail(engine, run["id"])
    assert client.received_rejection
    assert run["status"] == "needs_review"
    assert run["result"]["proposal_id"] is None
    assert [step["tool"] for step in detail["steps"]] == [
        "set_factory_temperature",
        "get_measurement_window",
        "search_response_manuals",
        "finish_analysis",
    ]
    assert detail["steps"][0]["result"]["error"]["code"] == "unknown_tool"
    assert all(step["result"]["ok"] for step in detail["steps"][1:])
    assert not action_tick(engine)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(actions)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(proposals)).scalar_one() == 0
        assert conn.execute(select(assets.c.cooling_mode)).scalar_one() == "normal"


def test_loop_budget_and_false_evidence(agent_incident):
    """입력: 사건 fixture. 반환: 없음. 무한 도구 선택과 허위 분석 인용을 종료/거부한다."""
    engine, _, context = agent_incident
    with pytest.raises(BudgetExceeded):
        run_agent(engine, context, ScriptedClient(context), lambda *args: None, max_turns=1)
    args = {
        "summary": "x",
        "facts": [{"statement": "x", "evidence_refs": ["measurement:999"]}],
        "hypotheses": [],
        "missing_information": [],
        "manual_refs": [],
        "disposition": "needs_review",
        "limitations": ["x"],
    }
    assert not execute_tool(engine, context, ToolCall("fake", "finish_analysis", args))["ok"]
    with pytest.raises(ValueError):
        run_agent(engine, context, ScriptedClient(context), lambda *args: None, timeout=0)


def test_sdk_missing_key_and_response_conversion(monkeypatch):
    """입력: 환경변수/SDK 대역. 반환: 없음. 키 미설정과 SDK 호출·함수 인자 추출을
    검증한다.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        OpenAIClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-fixture-not-a-real-key")
    client = OpenAIClient("test-model")
    from types import SimpleNamespace

    capture = {}

    def create(**kwargs):
        """입력: SDK 요청. 반환: 결정적인 함수 호출 응답. 외부 API는 호출하지 않는다."""
        capture.update(kwargs)
        return SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call",
                    name="get_incident_context",
                    arguments="{}",
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(client.client.responses, "create", create)
    turn = client.next_turn([], tool_definitions(), 10)
    assert turn.calls[0].name == "get_incident_context"
    assert capture["store"] is False and capture["parallel_tool_calls"] is False


@pytest.mark.parametrize("wrong_ref", ["incident:1", "model:eval-1", "a" * 64])
def test_manual_refs_cannot_use_non_document_ids(wrong_ref):
    """입력: 문서가 아닌 출처 ID. 반환: 없음.

    실제 평가에서 확인한 잘못된 manual_refs를 회귀 검증한다.
    """
    with pytest.raises(ValidationError):
        Assessment(
            summary="x",
            facts=[{"statement": "x", "evidence_refs": ["incident:1"]}],
            hypotheses=[],
            missing_information=[],
            manual_refs=[wrong_ref],
            disposition="needs_review",
            limitations=["x"],
        )


def test_proposal_tool_is_exposed_only_after_evidence(agent_incident):
    """입력: 사건 context. 반환: 없음. 선행 조회가 없을 때 제안 Tool을 노출·실행하지 않는다."""
    engine, _, context = agent_incident
    assert "submit_response_proposal" not in {tool["name"] for tool in tool_definitions(context)}
    assert execute_tool(
        engine,
        context,
        ToolCall("window", "get_measurement_window", {"before_seconds": 60, "limit": 20}),
    )["ok"]
    assert execute_tool(
        engine,
        context,
        ToolCall("manual", "search_response_manuals", {"query": "온도", "limit": 3}),
    )["ok"]
    assert "submit_response_proposal" in {tool["name"] for tool in tool_definitions(context)}
    finish = next(t for t in tool_definitions(context) if t["name"] == "finish_analysis")
    assert finish["parameters"]["properties"]["disposition"]["enum"] == [
        "no_action",
        "needs_review",
    ]
