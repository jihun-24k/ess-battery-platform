"""과거 MVP의 메모리 기반 가상 냉각·콘솔 알림. 새 플랫폼의 DB 영속 조치와는 별개다."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ActionResult:
    status: str
    requested_action: str
    device_mode: str
    simulated: bool = True


@dataclass
class MockCooling:
    mode: str = "normal"
    fail_requests: bool = False
    results: dict[str, ActionResult] = field(default_factory=dict)


def execute_mock_action(device: MockCooling, action: str, action_id: str) -> ActionResult:
    """입력: 메모리 가상 장비·동작 이름·고유 조치 ID. 반환: ActionResult.

    같은 ID는 이전 결과를 재사용하며 허용되지 않은 동작은 거부한다.
    """
    if action_id in device.results:
        return device.results[action_id]
    if action != "mock_cooling_boost":
        result = ActionResult("rejected", action, device.mode)
    elif device.fail_requests:
        result = ActionResult("failed", action, device.mode)
    else:
        status = "already_applied" if device.mode == "boost" else "applied"
        device.mode = "boost"
        result = ActionResult(status, action, device.mode)
    device.results[action_id] = result
    return result


def notify(incident_id: str, message: str) -> dict[str, str]:
    """입력: 사건 ID·메시지. 반환: 콘솔 알림 payload dict. 실제 메신저나 이메일은 보내지
    않는다.
    """
    payload = {"incident_id": incident_id, "channel": "console", "message": message}
    print(f"[모의 알림] {incident_id}: {message}")
    return payload
