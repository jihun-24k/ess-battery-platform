"""수집 서비스 테스트. 중복 재전송·ID 충돌·품질 기록과 동시 수집의 멱등성을 검증한다."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from ess_platform.collection.service import EventConflict, collect
from ess_platform.operations.queries import state_snapshot
from tests.helpers import reading


def test_collection_idempotency_conflict_and_quality(engine):
    """입력: engine. 반환: 없음. 중복·ID 충돌·품질 오류 기록과 원본 보존을 검증한다."""
    raw = json.dumps(reading()).encode()
    first = collect(engine, raw)
    assert collect(engine, raw) == {
        "status": "duplicate",
        "measurement_id": first["measurement_id"],
    }
    with pytest.raises(EventConflict):
        collect(engine, json.dumps(reading(0, 99.0)).encode())
    with pytest.raises(ValidationError):
        collect(engine, b"{}")
    snapshot = state_snapshot(engine)
    assert snapshot["counts"]["measurements"] == 1
    assert snapshot["counts"]["quality_issues"] == 2
    assert snapshot["telemetry"][0]["payload"]["temperature_c"] == 28.0


def test_concurrent_duplicate_ingest(engine):
    """입력: engine. 반환: 없음. 동시에 같은 데이터를 보내도 측정값이 하나만 저장되는지
    검증한다.
    """
    raw = json.dumps(reading()).encode()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: collect(engine, raw), range(8)))
    assert sum(r["status"] == "inserted" for r in results) == 1
    assert len({r["measurement_id"] for r in results}) == 1
