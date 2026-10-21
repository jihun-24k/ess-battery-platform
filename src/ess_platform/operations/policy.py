"""LLM과 독립된 가상 조치 정책. 현재 사건·데이터·문서·제안 hash를 실행 직전에
재검사한다.
"""

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from ess_platform.db import (
    assets,
    document_chunks,
    documents,
    incidents,
    issues,
    measurements,
    now,
    predictions,
)

POLICY_VERSION = "agent-simulation-1"


def payload_hash(payload: dict) -> str:
    """입력: 서버가 정규화한 대응 payload. 반환: 정확한 대상·인자·버전·근거를 묶는
    SHA-256.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def policy_reason(conn: Connection, proposal: dict) -> str | None:
    """입력: DB 연결·저장 제안. 반환: 차단 이유 또는 None. 실제 장비에는 적용하지
    않는다.
    """
    payload = proposal["payload"]
    if payload_hash(payload) != proposal["proposal_hash"]:
        return "proposal_hash_mismatch"
    if proposal["expires_at"] <= now():
        return "proposal_expired"
    if payload.get("action") != "mock_cooling_boost" or payload.get("parameters") != {}:
        return "action_not_allowlisted"
    if payload.get("asset_type") != "simulation_cell":
        return "physical_control_not_supported"
    incident = (
        conn.execute(select(incidents).where(incidents.c.id == proposal["incident_id"]))
        .mappings()
        .one()
    )
    if incident["status"] != "open" or incident["revision"] != proposal["incident_revision"]:
        return "incident_changed_or_recovered"
    if incident["response_mode"] != "assisted" or payload.get("mode") != "assisted":
        return "shadow_analysis_only"
    if payload.get("asset_id") != incident["asset_id"]:
        return "asset_scope_mismatch"
    asset = (
        conn.execute(select(assets).where(assets.c.asset_id == incident["asset_id"]))
        .mappings()
        .one()
    )
    latest = (
        conn.execute(
            select(predictions)
            .where(predictions.c.asset_id == incident["asset_id"])
            .order_by(predictions.c.observed_at.desc(), predictions.c.id.desc())
            .limit(1)
        )
        .mappings()
        .one()
    )
    cutoff = datetime.fromisoformat(payload["cutoff"])
    if abs((asset["last_observed_at"] - cutoff).total_seconds()) > 30:
        return "observations_changed_reanalysis_required"
    if latest["model_version"] != payload["model_version"] or not latest["is_anomaly"]:
        return "latest_prediction_changed_or_normal"
    quality_count = conn.execute(
        select(func.count())
        .select_from(issues)
        .join(measurements, measurements.c.id == issues.c.measurement_id)
        .where(
            measurements.c.asset_id == incident["asset_id"],
            measurements.c.observed_at >= cutoff - timedelta(seconds=30),
            measurements.c.observed_at <= asset["last_observed_at"],
        )
    ).scalar_one()
    if quality_count:
        return "data_quality_review_required"
    if not payload.get("manuals"):
        return "manual_required"
    for ref in payload["manuals"]:
        doc = (
            conn.execute(select(documents).where(documents.c.id == ref["document_id"]))
            .mappings()
            .one_or_none()
        )
        chunk = (
            conn.execute(select(document_chunks).where(document_chunks.c.id == ref["chunk_id"]))
            .mappings()
            .one_or_none()
        )
        if not doc or not chunk or chunk["document_id"] != doc["id"]:
            return "manual_reference_invalid"
        if (
            not doc["active"]
            or doc["asset_type"] != "simulation_cell"
            or doc["approval_scope"] != "simulation"
        ):
            return "manual_withdrawn_or_wrong_scope"
        if (
            doc["sha256"] != ref["sha256"]
            or doc["version"] != ref["version"]
            or hashlib.sha256(doc["content"].encode()).hexdigest() != ref["sha256"]
            or chunk["sha256"] != ref["chunk_hash"]
            or hashlib.sha256(chunk["content"].encode()).hexdigest() != ref["chunk_hash"]
            or incident["manual_sha256"] != doc["sha256"]
        ):
            return "manual_changed_or_not_incident_evidence"
    return None
