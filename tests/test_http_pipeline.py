"""실제 HTTP/PostgreSQL 통합 테스트. 수집부터 추론·사건·관제 조회까지 연결해 검증한다."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from ess_platform.collection.replay import demo_readings, replay_file
from ess_platform.inference.worker import inference_tick
from ess_platform.operations.actions import action_tick
from ess_platform.operations.incidents import operations_tick
from ess_platform.operations.notifications import notification_tick
from ess_platform.operations.queries import state_snapshot
from tests.helpers import reading, send, service


def test_end_to_end_real_http_and_replay(approved, tmp_path):
    """입력: approved, tmp_path. 반환: 없음. 실제 HTTP/PostgreSQL로 수집부터
    추론·대응·관제 조회까지 검증한다.
    """
    path = tmp_path / "demo.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in demo_readings("http-test")) + "\n")
    with service(approved, "collector") as collector, service(approved, "ml") as ml:
        assert replay_file(path, collector, 0) == {"inserted": 24, "duplicate": 0}
        assert replay_file(path, collector, 0) == {"inserted": 0, "duplicate": 24}
        while inference_tick(approved, ml):
            pass
        for _ in range(100):
            outcomes = [
                operations_tick(approved),
                action_tick(approved),
                notification_tick(approved),
            ]
            if not any(outcomes):
                break
    snapshot = state_snapshot(approved)
    assert snapshot["counts"]["measurements"] == 24
    assert snapshot["counts"]["predictions"] == 24
    assert snapshot["counts"]["incidents"] == 2
    assert snapshot["counts"]["actions"] == 2
    assert snapshot["counts"]["notifications"] == 5
    assert snapshot["measurement_statuses"] == {"predicted": 24}
    assert {a["status"] for a in snapshot["actions"]} == {"applied", "already_applied"}
    assert {i["status"] for i in snapshot["incidents"]} == {"open", "pattern_recovered"}
    assert all(n["delivered_at"] for n in snapshot["notifications"])
    assert not operations_tick(approved)
    assert not action_tick(approved)
    assert not notification_tick(approved)
    with service(approved, "operations") as url:
        code, body = send(url + "/api/state")
        assert code == 200 and body["counts"] == snapshot["counts"]
        code, body = send(url + "/api/incidents/1")
        assert code == 200 and body["incident"]["manual_sha256"]
        assert send(url + "/api/incidents/99999")[0] == 404
        assert send(url + "/api/incidents/nope")[0] == 400
        for asset in ("/", "/app.js", "/style.css"):
            with urlopen(url + asset) as response:
                assert response.status == 200 and response.read()


def test_http_validation_routes_and_no_model(engine):
    """입력: engine. 반환: 없음. 수집 HTTP 상태 코드·잘못된 경로·승인 모델 부재를
    검증한다.
    """
    with service(engine, "collector") as url:
        assert send(url + "/health")[0] == 200
        assert send(url + "/measurements", {})[0] == 422
        assert send(url + "/measurements", reading())[0] == 201
        assert send(url + "/measurements", reading())[0] == 200
        assert send(url + "/measurements", reading(0, 99.0))[0] == 409
        assert send(url + "/unknown")[0] == 404
        assert send(url + "/measurements", reading(), method="GET")[0] == 404
        req = Request(url + "/measurements", data=b"{}", headers={"Content-Type": "text/plain"})
        with pytest.raises(HTTPError) as exc:
            urlopen(req)
        assert exc.value.code == 400
    with service(engine, "ml") as url:
        assert send(url + "/predict", {"readings": [reading()]})[0] == 503
        assert send(url + "/predict", {"readings": []})[0] == 422


@pytest.mark.parametrize(
    "kind,path,payload",
    [
        ("collector", "/predict", {"readings": []}),
        ("ml", "/measurements", {}),
        ("operations", "/measurements", {}),
    ],
)
def test_server_routes_do_not_cross_roles(engine, kind, path, payload):
    """입력: 전용 DB·서버 종류·다른 서버의 경로·payload. 반환: 없음.

    서버를 분리한 뒤 다른 역할의 API로 쓰기 작업을 실행할 수 없는지 검증한다.
    """
    with service(engine, kind) as url:
        assert send(url + path, payload)[0] == 404
    assert state_snapshot(engine)["counts"]["measurements"] == 0
