"""평가 기준과 전용 DB 경계를 검증하며 실제 LLM 호출은 eval-agent 명령으로 분리한다."""

import pytest

from ess_platform.agents.evaluation import CASES, evaluate_live, score_case
from ess_platform.agents.schemas import Assessment


def test_evaluation_requires_dedicated_database(engine, tmp_path):
    """입력: 일반 테스트 DB. 반환: 없음. 평가용이 아닌 DB에서 schema 작업을 시작하지 않는다."""
    with pytest.raises(ValueError, match="dedicated"):
        evaluate_live(engine, tmp_path / "report.json", 1)
    assert len(CASES) == 20


def test_evaluation_detects_missing_tools_and_forbidden_attempts():
    """입력: 평가용 분석·호출 이력. 반환: 없음. 잘못된 Tool 경로가 성공으로 집계되지 않는다."""
    analysis = Assessment(
        summary="테스트",
        facts=[{"statement": "값 확인", "evidence_refs": ["measurement:1"]}],
        hypotheses=[],
        missing_information=[],
        manual_refs=[],
        disposition="needs_review",
        limitations=["합성"],
    )
    bad = [{"tool": "execute_shell", "result": {"ok": False}}]
    result = score_case({"review": True}, analysis, bad)
    assert not result["passed"] and not result["checks"]["no_forbidden_tool_attempt"]


def test_evaluation_summary_keeps_failed_cases_in_denominator():
    """입력: 성공·실패 사례. 반환: 없음. 실패/사용량 누락을 숨기지 않는 평가 집계를 검증한다."""
    from ess_platform.agents.evaluation import summarize_cases

    summary = summarize_cases(
        [
            {
                "passed": True,
                "assessment": {},
                "usage": {"elapsed_ms": 10, "input_tokens": 5},
                "checks": {"required_tools": True, "no_forbidden_tool_attempt": True},
            },
            {"passed": False, "error_type": "BudgetExceeded"},
        ]
    )
    assert summary["total"] == 2 and summary["passed"] == 1
    assert summary["validated_assessments"] == 1 and summary["p95_ms_completed"] == 10
    assert summary["known_input_tokens"] == 5
