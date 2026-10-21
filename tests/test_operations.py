"""운영 자동화 테스트. 재시작·문서 누락/변조·모의 냉각 실패·조치 취소·동시 워커를
검증한다.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from ess_platform.collection.service import collect
from ess_platform.db import (
    actions,
    incidents,
    now,
)
from ess_platform.inference.worker import inference_tick
from ess_platform.operations.actions import action_tick
from ess_platform.operations.incidents import operations_tick
from ess_platform.operations.queries import incident_detail, state_snapshot
from tests.helpers import infer_sequence, reading, service


def test_restarts_preserve_streak_and_idempotent_action(approved):
    """입력: approved. 반환: 없음. 새 DB 연결에서도 사건·조치가 중복되지 않고 냉각
    상태가 유지되는지 검증한다.
    """
    infer_sequence(approved, [49.0, 50.0])
    approved.dispose()  # New DB connections; workers keep no in-memory incident state.
    assert action_tick(approved)
    assert not action_tick(approved)
    snapshot = state_snapshot(approved)
    assert snapshot["counts"]["incidents"] == 1
    assert snapshot["counts"]["actions"] == 1
    assert snapshot["assets"][0]["cooling_mode"] == "boost"
    assert incident_detail(approved, 1)["actions"][0]["attempts"] == 1
    assert incident_detail(approved, 999) is None


def test_missing_manual_blocks_actions(approved, tmp_path):
    """입력: approved, tmp_path. 반환: 없음. 학습 문서가 없으면 모의 조치를 차단하는지
    검증한다.
    """
    with service(approved, "ml") as url:
        for step in range(2):
            collect(approved, json.dumps(reading(step, 50.0)).encode())
            inference_tick(approved, url)
            operations_tick(approved, tmp_path / "missing.md")
    assert not action_tick(approved)
    assert state_snapshot(approved)["actions"][0]["status"] == "blocked"
    assert state_snapshot(approved)["assets"][0]["cooling_mode"] == "normal"


def test_cooling_failures_are_bounded(approved):
    """입력: approved. 반환: 없음. 모의 냉각 실패가 최대 3회로 제한되고 normal 상태를
    유지하는지 검증한다.
    """
    infer_sequence(approved, [49.0, 50.0])
    for i in range(3):
        assert action_tick(approved, simulate_failure=True)
        if i == 0:
            assert not action_tick(approved, simulate_failure=True)
        with approved.begin() as conn:
            conn.execute(actions.update().values(next_attempt_at=now() - timedelta(seconds=1)))
    state = state_snapshot(approved)
    assert state["actions"][0]["status"] == "failed"
    assert state["actions"][0]["attempts"] == 3
    assert state["assets"][0]["cooling_mode"] == "normal"
    assert not action_tick(approved)


def test_recovered_pattern_cancels_unexecuted_action(approved):
    """입력: approved. 반환: 없음. 패턴 복귀 후 대기 조치가 실행되지 않고 취소되는지
    검증한다.
    """
    infer_sequence(approved, [49.0, 50.0, 28.0, 28.0, 28.0])
    assert state_snapshot(approved)["actions"][0]["status"] == "cancelled"
    assert state_snapshot(approved)["incidents"][0]["status"] == "pattern_recovered"
    assert not action_tick(approved)
    assert state_snapshot(approved)["assets"][0]["cooling_mode"] == "normal"


def test_multiple_workers_do_not_duplicate_state(approved):
    """입력: approved. 반환: 없음. 여러 워커가 동시에 동작해도 추론·사건·조치가 중복되지
    않는지 검증한다.
    """
    for step in range(10):
        collect(approved, json.dumps(reading(step, 50.0)).encode())
    with service(approved, "ml") as url, ThreadPoolExecutor(max_workers=3) as pool:
        for _ in range(15):
            list(pool.map(lambda _: inference_tick(approved, url), range(3)))
        for _ in range(15):
            list(pool.map(lambda _: operations_tick(approved), range(3)))
        list(pool.map(lambda _: action_tick(approved), range(3)))
    assert state_snapshot(approved)["counts"]["predictions"] == 10
    assert state_snapshot(approved)["counts"]["incidents"] == 1
    assert state_snapshot(approved)["actions"][0]["attempts"] == 1


def test_tampered_manual_is_blocked_by_policy(approved):
    """입력: approved. 반환: 없음. 문서 내용/hash가 달라지면 조치가 차단되고 판단 근거가
    조회되는지 검증한다.
    """
    infer_sequence(approved, [49.0, 50.0])
    with approved.begin() as conn:
        conn.execute(incidents.update().values(manual_content="modified after snapshot"))
    assert action_tick(approved)
    state = state_snapshot(approved)
    assert state["actions"][0]["status"] == "blocked"
    assert state["assets"][0]["cooling_mode"] == "normal"
    detail = incident_detail(approved, 1)
    assert detail["evidence"]["prediction"]["model_version"] == "mock-v1"
    assert detail["evidence"]["measurement"]["payload"]["temperature_c"] == 50.0
