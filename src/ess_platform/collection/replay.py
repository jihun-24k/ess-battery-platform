"""데이터 재생기. 합성 측정값을 만들거나 JSONL을 시간 순서대로 수집 API에 전송한다.

이벤트 ID를 유지하므로 같은 파일을 다시 전송해도 중복 적재되지 않는다.
"""

import json
import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ess_platform.http_client import request_json
from ess_platform.schemas import MeasurementInput


def demo_readings(stream_id: str) -> list[dict]:
    """입력: 이벤트 ID 접두사 stream_id. 반환: 두 셀·12시점의 합성 측정 dict 24개.

    시각과 이벤트 ID를 고정해 같은 스트림의 재전송을 검증할 수 있게 한다.
    """
    if not stream_id or len(stream_id) > 60:
        raise ValueError("stream_id length must be 1..60")
    rows = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for step, temperature in enumerate([28, 29, 48, 50, 51, 30, 29, 28, 49, 50, 30, 29]):
        for asset in ("ESS-A-CELL-01", "ESS-B-CELL-01"):
            row = {
                "event_id": f"{stream_id}:{asset}:{step}",
                "asset_id": asset,
                "observed_at": (start + timedelta(seconds=step)).isoformat(),
                "temperature_c": float(temperature if asset.startswith("ESS-A") else 27),
                "voltage_v": 3.6,
                "current_a": 2.0,
                "source": "synthetic_replay",
                "schema_version": "1",
            }
            rows.append(
                MeasurementInput.model_validate_json(json.dumps(row)).model_dump(mode="json")
            )
    return rows


def replay_file(path: Path, collector_url: str, interval: float = 0.1) -> dict:
    """입력: JSONL 경로·수집 서버 URL·전송 간격(초). 반환: inserted/duplicate 건수 dict.

    각 행을 검증한 뒤 POST하며 실패한 행 번호를 포함한 예외로 중단한다.
    """
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("interval must be nonnegative")
    counts = {"inserted": 0, "duplicate": 0}
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                reading = MeasurementInput.model_validate_json(line)
                result = request_json(
                    collector_url.rstrip("/") + "/measurements", reading.model_dump(mode="json")
                )
                counts[result["status"]] += 1
            except Exception as exc:
                raise ValueError(
                    f"replay stopped at line {line_number}: {type(exc).__name__}"
                ) from exc
            if interval:
                time.sleep(interval)
    return counts
