"""① 수집 처리. HTTP로 받은 측정 JSON을 검증하고 원본·해시를 저장해 중복/충돌을
구분한다.
"""

import hashlib
import json

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from ess_platform.db import issues, measurements
from ess_platform.schemas import MeasurementInput


class EventConflict(ValueError):
    """같은 source/event_id를 다른 측정 내용으로 재사용했을 때 발생하는 충돌."""


def collect(engine: Engine, raw: bytes) -> dict:
    """입력: DB engine과 측정 JSON bytes. 반환: inserted/duplicate 상태와
    measurement_id.

    검증 실패는 ValidationError, 같은 ID의 다른 값은 EventConflict를 발생시킨다.
    원본을 덮어쓰지 않는다.
    """
    try:
        reading = MeasurementInput.model_validate_json(raw)
    except ValidationError as exc:
        # 잘못된 원문 전체 대신 오류 필드 이름과 코드만 감사 이력에 남긴다.
        details = [{"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()]
        with engine.begin() as conn:
            conn.execute(issues.insert().values(code="INVALID_INPUT", detail={"errors": details}))
        raise
    payload = reading.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    conflict = False
    with engine.begin() as conn:
        inserted = conn.execute(
            insert(measurements)
            .values(
                source=reading.source,
                event_id=reading.event_id,
                asset_id=reading.asset_id,
                observed_at=reading.observed_at,
                payload=payload,
                payload_hash=digest,
            )
            .on_conflict_do_nothing(index_elements=["source", "event_id"])
            .returning(measurements.c.id)
        ).scalar_one_or_none()
        if inserted is not None:
            return {"status": "inserted", "measurement_id": inserted}
        existing = (
            conn.execute(
                select(measurements).where(
                    measurements.c.source == reading.source,
                    measurements.c.event_id == reading.event_id,
                )
            )
            .mappings()
            .one()
        )
        conflict = existing["payload_hash"] != digest
        if conflict:
            conn.execute(
                issues.insert().values(
                    measurement_id=existing["id"],
                    code="EVENT_ID_CONFLICT",
                    detail={"message": "same source/event_id with different payload"},
                )
            )
        result = {"status": "duplicate", "measurement_id": existing["id"]}
    if conflict:
        raise EventConflict("event identity already exists with a different payload")
    return result
