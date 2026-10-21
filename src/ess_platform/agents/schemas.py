"""Agent context·Tool 입출력·분석·대응안·승인의 엄격한 데이터 계약."""

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

EvidenceRef = Annotated[
    str,
    StringConstraints(
        pattern=r"^(measurement|prediction|incident|model|quality_summary|issue|action|document):[^\s]{1,100}$"
    ),
]
ManualRef = Annotated[str, StringConstraints(pattern=r"^document:[0-9]+$")]


class Contract(BaseModel):
    """알 수 없는 필드·자료형 강제 변환·비유한 숫자를 허용하지 않는 공통 계약."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class NoArgs(Contract):
    """서버가 확정한 사건 context만 사용하므로 추가 인자가 없는 Tool 계약."""


class WindowArgs(Contract):
    """관측 cutoff 이전의 제한된 구간과 건수만 조회하는 계약."""

    before_seconds: int = Field(ge=1, le=3600)
    limit: int = Field(ge=1, le=200)


class SearchArgs(Contract):
    """자산 적용 범위는 서버가 결정하고 검색어와 결과 개수만 받는다."""

    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(ge=1, le=5)


class ProposalRequest(Contract):
    """실행 대신 등록할 단일 가상 냉각 대응안. 대상과 실제 명령 값은 서버가 결정한다."""

    action: Literal["mock_cooling_boost"]
    reason: str = Field(min_length=1, max_length=1200)
    evidence_refs: list[EvidenceRef] = Field(
        min_length=1,
        max_length=20,
        description="실제 제공된 ref 값만 사용. 필드 이름이나 hash를 넣지 마세요.",
    )
    manual_refs: list[ManualRef] = Field(
        min_length=1, max_length=5, description="문서 검색에서 얻은 document:숫자 출처만 사용."
    )


class Fact(Contract):
    """관측 사실과 실제 조회 결과의 식별자를 함께 보존하는 계약."""

    statement: str = Field(min_length=1, max_length=1200)
    evidence_refs: list[EvidenceRef] = Field(
        min_length=1,
        max_length=20,
        description="실제 제공된 ref 값만 사용. 필드 이름이나 hash를 넣지 마세요.",
    )


class Assessment(Contract):
    """Agent 최종 분석. 사실·가능한 설명·부족한 정보·대응 의도를 분리한다."""

    summary: str = Field(min_length=1, max_length=2000)
    facts: list[Fact] = Field(min_length=1, max_length=20)
    hypotheses: list[str] = Field(max_length=10)
    missing_information: list[str] = Field(max_length=10)
    manual_refs: list[ManualRef] = Field(
        max_length=5,
        description=("문서 검색의 document:숫자만 사용. 검색 결과가 없으면 빈 배열 []."),
    )
    disposition: Literal["no_action", "needs_review", "propose_action"]
    limitations: list[str] = Field(min_length=1, max_length=10)


class ApprovalInput(Contract):
    """서버 인증된 승인자가 정확한 제안 hash에 대해 한 번 결정하는 계약."""

    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]


class AnalysisRequest(Contract):
    """인증된 운영자가 이유와 멱등성 키를 지정하는 수동 재분석 요청."""

    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    reason: str = Field(min_length=1, max_length=500)


@dataclass
class AgentContext:
    """서버가 확정한 사건 snapshot·Tool 근거와 아직 실행되지 않은 대응 의도."""

    snapshot: dict
    evidence: dict[str, dict] = field(default_factory=dict)
    intent: ProposalRequest | None = None
    successful_tools: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ToolCall:
    """LLM이 요청한 도구 이름·JSON 인자·호출 ID. 실행 권한을 의미하지 않는다."""

    call_id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Turn:
    """한 번의 LLM 응답에서 추출한 Tool 요청과 사용량. 내부 추론은 저장하지 않는다."""

    calls: list[ToolCall]
    usage: dict[str, Any] = field(default_factory=dict)


class ActionArgs(Contract):
    """할당된 사건에 속하는 조치 하나의 결과를 확인하는 입력 계약."""

    action_id: int = Field(ge=1)
