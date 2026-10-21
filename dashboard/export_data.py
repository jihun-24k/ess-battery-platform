"""과거 MVP 대시보드 데이터 생성기. report.json/events.jsonl에서 허용 필드만 추출한다.

MAT·모델 파일과 로컬 절대 경로를 내보내지 않는다. 실시간 서버 관제와는 별도 경로다.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENT_FIELDS = {
    "prediction": (
        "cycle_index",
        "start_sample",
        "end_sample",
        "source_time_start",
        "source_time_end",
        "contains_synthetic_samples",
        "features",
        "is_anomaly",
        "anomaly_score",
        "kind",
    ),
    "incident_opened": ("incident_id", "kind", "anomaly_score"),
    "incident_pattern_recovered": ("incident_id", "meaning"),
    "action_result": ("incident_id", "status", "requested_action", "device_mode", "simulated"),
    "manual_found": ("incident_id", "sha256", "learning_example"),
    "notification": ("incident_id", "channel", "message"),
    "action_blocked": ("incident_id", "reason"),
}


def export_run(report_path: Path) -> dict:
    """입력: MVP report.json 경로. 반환: 같은 폴더의 이벤트와 보고서에서 추출한 portable
    dict.

    허용 필드만 복사해 로컬 절대 경로를 제외하며 누락·손상 파일 오류는 호출자에게
    전달한다.
    """
    report = json.loads(report_path.read_text())
    events = []
    for line in report_path.with_name("events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event["event"] in EVENT_FIELDS:
            events.append(
                {
                    "event": event["event"],
                    "logged_at": event["logged_at"],
                    **{k: event[k] for k in EVENT_FIELDS[event["event"]] if k in event},
                }
            )
    source = report["source"]
    return {
        "id": report["run_id"],
        "date": source["batch_date"],
        "file": Path(source["path"]).name,
        "sha256": source["sha256"],
        "cells": source["cell_count"],
        "counts": report["counts"],
        "offset": report["config"]["demo_temperature_offset"],
        "config": {
            k: report["config"][k]
            for k in ("cell_index", "window_size", "stride", "trigger_windows", "recovery_windows")
        },
        "model": report["model_kind"],
        "model_version": report["model_version"],
        "feature_names": report["feature_names"],
        "events": events,
    }


def main() -> None:
    """입력: 없음. 반환: 없음. 검증된 네 실행을 읽어 dist/data.json을 생성한다."""
    reports = json.loads((ROOT / "docs/mvp-validation-results.json").read_text())
    runs = [export_run(ROOT / "runs/mvp-validation" / r["run_id"] / "report.json") for r in reports]
    payload = {
        "mode": "historical_simulation",
        "runs": runs,
        "manual": (ROOT / "src/ess_mvp/manuals/measurement_anomaly.md").read_text(),
    }
    (Path(__file__).parent / "dist/data.json").write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    )


if __name__ == "__main__":
    main()
