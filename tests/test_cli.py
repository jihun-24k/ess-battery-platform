"""CLI 테스트. 데모 파일 생성·DB 초기화·모델 관리·워커 옵션과 잘못된 입력을 검증한다."""

import pytest

from ess_platform.cli import main
from ess_platform.collection.replay import demo_readings, replay_file
from ess_platform.db import (
    make_engine,
)


def test_cli_and_demo_validation(engine, monkeypatch, tmp_path):
    """입력: engine, monkeypatch, tmp_path. 반환: 없음. CLI 파일 생성·모델
    관리·워커 옵션·DB 설정 오류를 검증한다.
    """
    path = tmp_path / "demo.jsonl"
    assert main(["make-demo", str(path)]) == 0
    assert len(path.read_text().splitlines()) == 24
    assert main(["make-demo", str(path)]) == 1
    with pytest.raises(ValueError):
        demo_readings("")
    with pytest.raises(ValueError):
        replay_file(path, "http://127.0.0.1:1", -1)
    path.write_text("{}")
    with pytest.raises(ValueError, match="line 1"):
        replay_file(path, "http://127.0.0.1:1", 0)
    monkeypatch.setattr("ess_platform.cli.make_engine", lambda: engine)
    assert main(["init-db"]) == 0
    assert main(["train-mock", "--version", "cli-v1"]) == 0
    assert main(["approve-model", "cli-v1"]) == 0
    assert main(["status"]) == 0
    assert main(["worker", "operations", "--once"]) == 0
    assert main(["worker", "inference", "--drain"]) == 0
    assert main(["worker", "inference", "--poll", "nan"]) == 1
    assert main(["worker", "inference", "--once", "--drain"]) == 1
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError):
        make_engine()
    with pytest.raises(ValueError):
        make_engine("sqlite://")
