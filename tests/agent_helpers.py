"""Agent 프로그램 테스트용 결정적 응답 대역. 실제 LLM 품질 평가와 구분한다."""

import json

from ess_platform.agents.schemas import ToolCall, Turn


class ScriptedClient:
    """측정·문서·제안·분석 흐름을 재현해 DB/정책을 테스트하는 대역."""

    provider = "test_fixture"
    model = "scripted-not-llm"

    def __init__(self, context, propose=True):
        """입력: 사건 context·제안 여부. 반환: 재현 가능한 client. 외부 API를 호출하지
        않는다.
        """
        self.context = context
        self.position = 0
        self.propose = propose
        self.finished = False

    def next_turn(self, messages, tools, timeout):
        """입력: loop 대화·계약·예산. 반환: 다음 결정적 ToolCall. 도구 결과에서 실제
        출처를 얻는다.
        """
        outputs = [
            json.loads(m["output"])
            for m in messages
            if isinstance(m, dict) and m.get("type") == "function_call_output"
        ]
        doc_refs = [
            d["ref"]
            for out in outputs
            if out.get("ok")
            for d in out.get("data", {}).get("documents", [])
        ]
        reading_ref = next(ref for ref in self.context.evidence if ref.startswith("measurement:"))
        if self.position == 0:
            name, args = "get_measurement_window", {"before_seconds": 60, "limit": 20}
        elif self.position == 1:
            name, args = "search_response_manuals", {"query": "온도 냉각", "limit": 3}
        elif self.position == 2 and self.propose and doc_refs:
            name, args = (
                "submit_response_proposal",
                {
                    "action": "mock_cooling_boost",
                    "reason": "가상 학습 대응",
                    "evidence_refs": [reading_ref],
                    "manual_refs": doc_refs[:1],
                },
            )
        else:
            name, args = (
                "finish_analysis",
                {
                    "summary": "합성 측정값의 패턴 이상 분석",
                    "facts": [
                        {"statement": "사건 측정값을 확인했습니다.", "evidence_refs": [reading_ref]}
                    ],
                    "hypotheses": [],
                    "missing_information": ["실제 냉각 상태 미수집"],
                    "manual_refs": doc_refs[:1],
                    "disposition": "propose_action"
                    if self.propose and doc_refs
                    else "needs_review",
                    "limitations": ["고장 확정이나 고장 확률이 아닙니다."],
                },
            )
            self.finished = True
        call = ToolCall(f"test-call-{self.position}", name, args)
        self.position += 1
        return Turn([call], {"input_tokens": 10, "output_tokens": 5})
