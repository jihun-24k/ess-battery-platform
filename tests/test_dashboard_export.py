"""과거 대시보드 export 테스트. 실제 집계 일치·경로/미허용 필드 제외·누락 파일 오류를
검증한다.
"""

import importlib.util
import json
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
spec = importlib.util.spec_from_file_location("dashboard_export", DASHBOARD / "export_data.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_real_counts_and_paths():
    """입력: 없음. 반환: 없음. 내보낸 네 실행의 집계·출처 hash와 로컬 경로 제외를
    검증한다.
    """
    data = json.loads((DASHBOARD / "dist/data.json").read_text())
    assert len(data["runs"]) == 4
    assert "/Users/" not in json.dumps(data)
    for run in data["runs"]:
        events = run["events"]
        assert (
            len([e for e in events if e["event"] == "prediction"])
            == run["counts"]["evaluation_windows"]
        )
        assert (
            len([e for e in events if e["event"] == "incident_opened"])
            == run["counts"]["incidents"]
        )
        assert sum(e.get("is_anomaly", False) for e in events) == run["counts"]["anomaly_windows"]
        assert len(run["sha256"]) == 64


def test_export_unknown_fields_not_exposed(tmp_path):
    """입력: tmp_path. 반환: 없음. 허용하지 않은 필드와 로컬 경로가 export에 포함되지
    않는지 검증한다.
    """
    original = json.loads((DASHBOARD / "dist/data.json").read_text())["runs"][0]
    report = {
        "run_id": original["id"],
        "source": {
            "path": "/secret/raw.mat",
            "batch_date": original["date"],
            "sha256": original["sha256"],
            "cell_count": 46,
        },
        "counts": {},
        "config": {"demo_temperature_offset": None, **original["config"]},
        "model_kind": "IsolationForest",
        "model_version": "learning-1",
        "feature_names": [],
    }
    (tmp_path / "report.json").write_text(json.dumps(report))
    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "event": "notification",
                "logged_at": "2026-01-01",
                "incident_id": "test",
                "channel": "console",
                "private_path": "/secret/token",
            }
        )
        + "\n"
    )
    exported = module.export_run(tmp_path / "report.json")
    assert exported["file"] == "raw.mat"
    assert "private_path" not in exported["events"][0]
    assert "/secret/" not in json.dumps(exported)


def test_missing_events_fails_explicitly(tmp_path):
    """입력: tmp_path. 반환: 없음. 이벤트 파일 누락이 조용히 무시되지 않고 예외가 되는지
    검증한다.
    """
    (tmp_path / "report.json").write_text("{}")
    with pytest.raises(FileNotFoundError):
        module.export_run(tmp_path / "report.json")
