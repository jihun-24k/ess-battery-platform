"""Agent 도구의 정적 목록·엄격한 인자·사건 범위·근거 ID를 검사하고 기존 조회를 실행한다."""

from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine

from ess_platform.agents.repository import context_cutoff
from ess_platform.agents.retrieval import search_manuals
from ess_platform.agents.schemas import (
    ActionArgs,
    AgentContext,
    Assessment,
    NoArgs,
    ProposalRequest,
    SearchArgs,
    ToolCall,
    WindowArgs,
)
from ess_platform.db import actions, issues, measurements, row_dict

# LLM이 임의 함수명·SQL·외부 URL을 지정해 실행할 수 없는 고정 registry다.
TOOL_SPECS = {
    "get_incident_context": (NoArgs, "서버가 확정한 사건, 추론 방식과 한계를 확인합니다."),
    "get_measurement_window": (
        WindowArgs,
        "사건 자산의 cutoff 이전 측정 추이를 확인합니다. before_seconds와 limit를 제한하세요.",
    ),
    "get_quality_issues": (
        WindowArgs,
        "같은 자산/기간의 데이터 오류·지연을 확인합니다. 빈 결과는 오류 이력이 없다는 뜻입니다.",
    ),
    "search_response_manuals": (
        SearchArgs,
        "자산에 적용되는 학습 대응 문서와 버전·출처를 찾습니다. 없으면 대응을 보류하세요.",
    ),
    "get_action_history": (NoArgs, "사건에 이미 연결된 조치와 재시도 결과를 확인합니다."),
    "get_action_status": (
        ActionArgs,
        "사건에 속한 조치 ID의 상태를 확인합니다. 다른 사건 ID는 거부됩니다.",
    ),
    "submit_response_proposal": (
        ProposalRequest,
        "측정 윈도·문서 검색 성공 후 가상 냉각 제안을 등록합니다. 승인·실행이 아닙니다.",
    ),
    "finish_analysis": (
        Assessment,
        "조사 후 한국어 분석을 종료합니다. 사실·가설·부족 정보·한계를 분리하세요.",
    ),
}


def strict_schema(schema: Any) -> Any:
    """입력: Pydantic JSON Schema. 반환: 모든 object 필드를 명시하는 SDK strict schema
    복사본.
    """
    if isinstance(schema, list):
        return [strict_schema(x) if isinstance(x, (dict, list)) else x for x in schema]
    if not isinstance(schema, dict):
        return schema
    result = {k: strict_schema(v) for k, v in schema.items() if k not in {"default", "title"}}
    if result.get("type") == "object":
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


def tool_definitions(context: AgentContext | None = None) -> list[dict]:
    """입력: 선택적 현재 context. 반환: 근거 수집 단계에 맞춰 허용되는 strict Tool 목록.

    표시 범위를 줄여도 execute_tool과 정책 서비스는 권한을 독립적으로 재검증한다.
    """
    ready = context is None or (
        {"get_measurement_window", "search_response_manuals"}.issubset(context.successful_tools)
        and context.snapshot["incident"]["status"] == "open"
        and context.snapshot["asset_type"] == "simulation_cell"
        and context.snapshot.get("latest_prediction", {}).get("is_anomaly", False)
        and not context.snapshot.get("quality_issue_count", 0)
        and not context.snapshot.get("existing_action_count", 0)
        and any(
            ref.startswith("document:")
            and value.get("sha256") == context.snapshot["incident"]["manual_sha256"]
            for ref, value in context.evidence.items()
        )
    )
    result = []
    for name, (model, description) in TOOL_SPECS.items():
        if name == "submit_response_proposal" and not ready:
            continue
        schema = strict_schema(model.model_json_schema())
        if name == "finish_analysis" and context is not None and context.intent is None:
            schema["properties"]["disposition"]["enum"] = ["no_action", "needs_review"]
        result.append(
            {
                "type": "function",
                "name": name,
                "description": description,
                "parameters": schema,
                "strict": True,
            }
        )
    return result


def validate_refs(context: AgentContext, refs: list[str], manual: bool = False) -> None:
    """입력: context·참조 목록·문서 여부. 반환: 없음. 조회하지 않은 근거·잘못된 종류는
    거부한다.
    """
    for ref in refs:
        if ref not in context.evidence or (manual and not ref.startswith("document:")):
            raise ValueError(f"unretrieved_or_wrong_reference:{ref}")


def validate_assessment(context: AgentContext, assessment: Assessment) -> None:
    """입력: 근거 registry·최종 분석. 반환: 없음. 허위 출처와 제안 없이 실행을 요청하는
    분석을 거부한다.
    """
    for fact in assessment.facts:
        validate_refs(context, fact.evidence_refs)
    validate_refs(context, assessment.manual_refs, manual=True)
    if assessment.disposition == "propose_action" and context.intent is None:
        raise ValueError("submit_response_proposal must succeed before proposing action")
    if assessment.disposition != "propose_action":
        # 후속 조사에서 보류를 선택하면 아직 실행하지 않은 의도를 철회한다.
        context.intent = None


def execute_tool(engine: Engine, context: AgentContext, call: ToolCall) -> dict:
    """입력: DB·서버 context·LLM 요청. 반환: 검증된 결과 envelope. 임의 명령은 실행하지
    않는다.
    """
    if call.name not in TOOL_SPECS:
        return {"ok": False, "error": {"code": "unknown_tool", "retryable": False}}
    try:
        args = TOOL_SPECS[call.name][0].model_validate(call.arguments)
        data = _dispatch(engine, context, call.name, args)
        context.successful_tools.add(call.name)
        return {"ok": True, "data": data, "error": None, "as_of": context.snapshot["cutoff"]}
    except (ValidationError, ValueError) as exc:
        # 외부 데이터 원문·내부 stack 대신 수정 가능한 오류 설명만 전달한다.
        code = "invalid_arguments" if isinstance(exc, ValidationError) else str(exc)
        return {
            "ok": False,
            "error": {"code": code, "retryable": False},
            "available_refs": list(context.evidence)[:80],
        }


def _dispatch(engine: Engine, context: AgentContext, name: str, args) -> dict:
    """입력: 검증된 Tool 이름/인자. 반환: 사건으로 범위가 고정된 조회 또는 대응 의도
    결과.
    """
    incident = context.snapshot["incident"]
    if name == "get_incident_context":
        return {"snapshot": context.snapshot, "available_refs": list(context.evidence)}
    if name == "search_response_manuals":
        found = search_manuals(engine, context.snapshot["asset_type"], args.query, args.limit)
        context.evidence.update({item["ref"]: item for item in found})
        return {"documents": found, "learning_only": True}
    if name == "submit_response_proposal":
        if not {"get_measurement_window", "search_response_manuals"}.issubset(
            context.successful_tools
        ):
            raise ValueError("measurement_window_and_manual_search_required_first")
        validate_refs(context, args.evidence_refs)
        validate_refs(context, args.manual_refs, manual=True)
        if any(
            context.evidence[ref]["sha256"] != incident["manual_sha256"] for ref in args.manual_refs
        ):
            raise ValueError("manual_not_incident_document")
        if not any(ref.startswith("measurement:") for ref in args.evidence_refs):
            raise ValueError("measurement evidence required")
        if incident["status"] != "open":
            raise ValueError("incident is not open")
        if context.snapshot.get("quality_issue_count", 0):
            raise ValueError("recent_quality_issues_require_review")
        if context.snapshot.get("existing_action_count", 0):
            raise ValueError("incident_already_has_action")
        if not context.snapshot.get("latest_prediction", {}).get("is_anomaly", False):
            raise ValueError("latest_prediction_normal_requires_review")
        if context.snapshot["asset_type"] != "simulation_cell":
            raise ValueError("physical_control_not_supported")
        context.intent = args
        return {"intent_recorded": True, "execution": "requires_server_policy_and_human_approval"}
    if name == "finish_analysis":
        validate_assessment(context, args)
        return {"assessment": args.model_dump(mode="json")}
    with engine.connect() as conn:
        if name in {"get_measurement_window", "get_quality_issues"}:
            cutoff = context_cutoff(context)
            filters = [
                measurements.c.asset_id == incident["asset_id"],
                measurements.c.observed_at <= cutoff,
                measurements.c.observed_at >= cutoff - timedelta(seconds=args.before_seconds),
            ]
            if name == "get_measurement_window":
                rows = conn.execute(
                    select(measurements)
                    .where(*filters)
                    .order_by(measurements.c.observed_at.desc(), measurements.c.id.desc())
                    .limit(args.limit)
                ).all()
                samples = [
                    {**row_dict(row), "ref": f"measurement:{row.id}"} for row in reversed(rows)
                ]
                context.evidence.update({item["ref"]: item for item in samples})
                return {
                    "samples": samples,
                    "limit_reached": len(samples) == args.limit,
                    "units": {"temperature_c": "C", "voltage_v": "V", "current_a": "A"},
                }
            rows = conn.execute(
                select(issues)
                .join(measurements, measurements.c.id == issues.c.measurement_id)
                .where(*filters)
                .order_by(issues.c.id.desc())
                .limit(args.limit)
            ).all()
            found = [{**row_dict(row), "ref": f"issue:{row.id}"} for row in rows]
            context.evidence.update({item["ref"]: item for item in found})
            return {"issues": found}
        statement = select(actions).where(actions.c.incident_id == incident["id"])
        if name == "get_action_status":
            statement = statement.where(actions.c.id == args.action_id)
        rows = conn.execute(statement.order_by(actions.c.id).limit(20)).all()
        if name == "get_action_status" and not rows:
            raise ValueError("action outside incident scope or missing")
        found = [{**row_dict(row), "ref": f"action:{row.id}"} for row in rows]
        context.evidence.update({item["ref"]: item for item in found})
        return {"actions": found}
