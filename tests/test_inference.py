"""ML mock·추론 워커 테스트. 경계값·재시도·응답 검증·DB 장애·지연 도착을 검증한다."""

import json
from datetime import timedelta

import pytest

from ess_platform.cli import main
from ess_platform.collection.service import collect
from ess_platform.db import (
    make_engine,
    measurements,
    now,
)
from ess_platform.inference.mock import predict
from ess_platform.inference.worker import inference_tick
from ess_platform.operations.incidents import operations_tick
from ess_platform.operations.queries import state_snapshot
from ess_platform.schemas import PredictionInput
from tests.helpers import reading, send, service


@pytest.mark.parametrize("temperature,anomaly", [(44.0, False), (45.0, False), (46.0, True)])
def test_mock_is_explicit_deterministic(temperature, anomaly):
    """입력: temperature, anomaly. 반환: 없음. mock 표시와 임계값 경계 판정이 결정적인지
    검증한다.
    """
    request = PredictionInput.model_validate_json(
        json.dumps({"readings": [reading(0, temperature)]})
    )
    result = predict(request, "mock-v1", {"temperature_threshold_c": 45.0})
    assert result["is_anomaly"] is anomaly
    assert result["mock"] is True
    assert result["score"] == temperature - 45
    with pytest.raises(ValueError):
        predict(request, "bad", {"temperature_threshold_c": float("inf")})


def test_inference_failure_retries_and_manual_requeue(engine, monkeypatch):
    """입력: engine, monkeypatch. 반환: 없음. 추론 실패 3회 제한·대기·CLI 수동 재투입을
    검증한다.
    """
    collect(engine, json.dumps(reading()).encode())
    for attempt in range(3):
        assert inference_tick(engine, "http://127.0.0.1:1")
        if attempt == 0:
            assert not inference_tick(engine, "http://127.0.0.1:1")
        with engine.begin() as conn:
            conn.execute(measurements.update().values(next_attempt_at=now() - timedelta(seconds=1)))
    snapshot = state_snapshot(engine)
    assert snapshot["telemetry"][0]["attempts"] == 3
    assert snapshot["telemetry"][0]["status"] == "failed"
    monkeypatch.setattr("ess_platform.cli.make_engine", lambda: engine)
    assert main(["retry-inference", "1"]) == 0
    assert state_snapshot(engine)["telemetry"][0]["status"] == "pending"


def test_model_response_validation_and_db_outage(approved, monkeypatch):
    """입력: approved, monkeypatch. 반환: 없음. 미승인 응답 격리와 DB 접속 실패의 HTTP
    503을 검증한다.
    """
    collect(approved, json.dumps(reading()).encode())
    monkeypatch.setattr(
        "ess_platform.inference.worker.request_json",
        lambda *args: {
            "mock": True,
            "model_version": "unapproved",
            "features": {"temperature": 1.0},
            "score": 1.0,
            "is_anomaly": True,
        },
    )
    assert inference_tick(approved, "http://mock.invalid")
    assert state_snapshot(approved)["telemetry"][0]["status"] == "retry"
    broken = make_engine("postgresql+psycopg://u079@127.0.0.1:1/ess_platform_test_unavailable")
    try:
        with service(broken, "collector") as url:
            assert send(url + "/health")[0] == 503
            assert send(url + "/measurements", reading())[0] == 503
    finally:
        broken.dispose()


def test_late_data_and_gaps_do_not_trigger_stale_cooling(approved):
    """입력: approved. 반환: 없음. 큰 관측 간격·늦게 도착한 값이 오래된 냉각 판단을
    만들지 않는지 검증한다.
    """
    with service(approved, "ml") as url:
        for step in (2, 50):
            collect(approved, json.dumps(reading(step, 50.0)).encode())
            inference_tick(approved, url)
            operations_tick(approved)
        assert state_snapshot(approved)["counts"]["incidents"] == 0
        collect(approved, json.dumps(reading(1, 80.0)).encode())
        assert inference_tick(approved, url)
        assert state_snapshot(approved)["measurement_statuses"]["late"] == 1
        assert state_snapshot(approved)["quality_issues"][0]["code"] == "LATE_EVENT"
