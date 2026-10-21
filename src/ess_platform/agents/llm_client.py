"""공식 OpenAI Responses SDK 연결. 키는 환경변수로만 받고 응답의 내부 추론은 기록하지
않는다.
"""

import json
import os

from openai import OpenAI

from ess_platform.agents.schemas import ToolCall, Turn


class OpenAIClient:
    """단일 사건 대화의 Tool Calling만 담당하며 DB·실행 권한을 갖지 않는 SDK 경계."""

    provider = "openai"

    def __init__(self, model: str | None = None):
        """입력: 선택적 모델 이름. 반환: client. API 키 미설정이면 호출 전에 실패한다."""
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is required for the agent worker")
        self.model = model or os.environ.get("ESS_AGENT_MODEL", "gpt-4.1-mini")
        self.client = OpenAI(max_retries=0)

    def next_turn(self, messages: list, tools: list, timeout: float) -> Turn:
        """입력: 대화·Tool 계약·남은 초. 반환: ToolCall과 사용량. 공급자 오류는 워커가
        처리한다.
        """
        response = self.client.responses.create(
            model=self.model,
            input=messages,
            tools=tools,
            tool_choice="required",
            parallel_tool_calls=False,
            max_output_tokens=2000,
            store=False,
            timeout=min(timeout, 25.0),
        )
        messages.extend(response.output)
        calls = []
        for item in response.output:
            if item.type == "function_call":
                arguments = json.loads(item.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                calls.append(ToolCall(item.call_id, item.name, arguments))
        usage = response.usage.model_dump() if response.usage else {}
        return Turn(calls=calls, usage=usage)
