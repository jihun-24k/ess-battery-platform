"""모델 저장소 테스트. 명시적 승인·버전 전환과 연속 판정 카운터 초기화를 검증한다."""

import json

import pytest

from ess_platform.collection.service import collect
from ess_platform.inference.registry import active_model
from ess_platform.inference.worker import inference_tick
from ess_platform.operations.incidents import operations_tick
from ess_platform.operations.queries import state_snapshot
from ess_platform.training import approve, create_candidate
from tests.helpers import reading, service


def test_registry_approval_is_explicit(engine):
    """입력: engine. 반환: 없음. 후보만 등록해서는 활성 버전이 바뀌지 않고 승인해야
    바뀌는지 검증한다.
    """
    assert active_model(engine) is None
    result = create_candidate(engine, "candidate-1", 45.0)
    assert result["evaluation"] == {
        "contract_checks": 3,
        "passed": True,
        "real_training": False,
        "fault_metrics": None,
    }
    assert active_model(engine) is None
    approve(engine, "candidate-1")
    assert active_model(engine)["version"] == "candidate-1"
    create_candidate(engine, "candidate-2", 46.0)
    assert active_model(engine)["version"] == "candidate-1"
    approve(engine, "candidate-2")
    assert active_model(engine)["version"] == "candidate-2"
    with pytest.raises(ValueError):
        approve(engine, "missing")
    with pytest.raises(ValueError):
        create_candidate(engine, "", float("nan"))


def test_version_change_resets_streak_evidence(approved):
    """입력: approved. 반환: 없음. 모델 버전 변경 전후의 이상 판단을 하나의 연속 증거로
    합치지 않는지 검증한다.
    """
    with service(approved, "ml") as url:
        collect(approved, json.dumps(reading(0, 50.0)).encode())
        inference_tick(approved, url)
        operations_tick(approved)
        create_candidate(approved, "mock-v2", 45.0)
        approve(approved, "mock-v2")
        collect(approved, json.dumps(reading(1, 50.0)).encode())
        inference_tick(approved, url)
        operations_tick(approved)
    assert state_snapshot(approved)["counts"]["incidents"] == 0
    assert state_snapshot(approved)["assets"][0]["anomaly_streak"] == 1
