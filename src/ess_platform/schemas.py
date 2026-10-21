"""새 스트리밍 입력 계약. 측정값의 타입·단위·필수 필드와 추론 윈도의 순서를 검증한다.

이 계약의 °C/V/A 단위는 합성 스트림의 명시적 정의이며 MAT 단위를 추정한 것이 아니다.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class MeasurementInput(BaseModel):
    """호출자가 지정한 고유 이벤트 ID와 명시적 단위를 가진 셀 측정값."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    event_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    asset_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    observed_at: AwareDatetime
    temperature_c: float
    voltage_v: float = Field(ge=0)
    current_a: float
    source: Literal["synthetic_replay", "bms"] = "synthetic_replay"
    schema_version: Literal["1"] = "1"

    @field_validator("observed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        """입력: timezone을 가진 관측 datetime. 반환: 같은 시점을 표현하는 UTC datetime."""
        return value.astimezone(UTC)


class PredictionInput(BaseModel):
    """한 자산의 시간 순서 측정값을 담는 크기가 제한된 mock 추론 요청."""

    model_config = ConfigDict(extra="forbid", strict=True)
    readings: list[MeasurementInput] = Field(min_length=1, max_length=100)

    @field_validator("readings")
    @classmethod
    def ordered_one_asset(cls, readings: list[MeasurementInput]) -> list[MeasurementInput]:
        """입력: 측정값 목록. 반환: 검증된 동일 목록.

        자산 혼합·이벤트 중복·증가하지 않는 시각은 ValueError로 거부한다.
        """
        if len({r.asset_id for r in readings}) != 1:
            raise ValueError("one asset per inference request required")
        if len({(r.source, r.event_id) for r in readings}) != len(readings):
            raise ValueError("duplicate reading identity")
        if any(
            a.observed_at >= b.observed_at for a, b in zip(readings, readings[1:], strict=False)
        ):
            raise ValueError("readings must have strictly increasing timestamps")
        return readings
