"""입력 계약 테스트. 타입·결측·timezone·중복·시각 순서·자산 혼합 오류를 검증한다."""

import json

import pytest
from pydantic import ValidationError

from ess_platform.schemas import MeasurementInput, PredictionInput
from tests.helpers import reading


@pytest.mark.parametrize(
    "override",
    [
        {"temperature_c": "hot"},
        {"temperature_c": True},
        {"temperature_c": float("nan")},
        {"voltage_v": -1},
        {"observed_at": "2026-01-01T00:00:00"},
        {"extra_field": 1},
        {"event_id": ""},
        {"source": "unknown"},
    ],
)
def test_stream_validation(override):
    """입력: override. 반환: 없음. 문자열·bool·NaN·음수 전압·시각·추가 필드 오류가
    거부되는지 검증한다.
    """
    with pytest.raises(ValidationError):
        MeasurementInput.model_validate_json(json.dumps(reading(**override)))


def test_missing_and_timezone_and_prediction_contract():
    """입력: 없음. 반환: 없음. 필수값·UTC 정규화와 추론 요청의 자산/중복/순서를
    검증한다.
    """
    with pytest.raises(ValidationError):
        MeasurementInput.model_validate_json("{}")
    parsed = MeasurementInput.model_validate_json(
        json.dumps(reading(observed_at="2026-01-01T09:00:00+09:00"))
    )
    assert parsed.observed_at.hour == 0
    for rows in (
        [],
        [reading(), reading()],
        [reading(1), reading(0)],
        [reading(), reading(1, asset_id="another")],
    ):
        with pytest.raises(ValidationError):
            PredictionInput.model_validate_json(json.dumps({"readings": rows}))
