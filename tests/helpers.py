"""공통 테스트 도우미. 합성 측정값·HTTP 요청·임시 서버·추론 시퀀스를 제공한다."""

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ess_platform.collection.service import collect
from ess_platform.inference.worker import inference_tick
from ess_platform.operations.incidents import operations_tick
from ess_platform.servers import make_server


@contextmanager
def service(engine, kind):
    """입력: 테스트 DB engine·서버 종류. 반환: context 안에서 사용할 임시 HTTP URL.

    context 종료 시 서버를 닫고 전용 thread를 정리한다.
    """
    server = make_server(engine, kind, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def reading(step=0, temperature=28.0, **overrides):
    """입력: 샘플 순서·온도·덮어쓸 테스트 필드. 반환: 유효 입력 또는 오류 주입용 측정
    dict.
    """
    return {
        "event_id": f"event-{step}",
        "asset_id": "ESS-A",
        "observed_at": (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=step)).isoformat(),
        "temperature_c": temperature,
        "voltage_v": 3.6,
        "current_a": -2.0,
        **overrides,
    }


def send(url, payload=None, method=None):
    """입력: URL·선택적 JSON payload·HTTP method. 반환: (상태 코드, JSON body) tuple.

    오류 응답도 상태 코드를 반환해 테스트에서 4xx/5xx를 검증한다.
    """
    req = Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(req, timeout=5) as result:
            return result.status, json.load(result)
    except HTTPError as exc:
        return exc.code, json.load(exc)


def infer_sequence(engine, temperatures):
    """입력: 테스트 DB engine·온도 순서. 반환: 없음.

    임시 ML 서버를 통해 측정 수집·추론·사건 처리를 순서대로 실행하고 성공을
    assert한다.
    """
    with service(engine, "ml") as url:
        for step, temperature in enumerate(temperatures):
            collect(engine, json.dumps(reading(step, temperature)).encode())
            assert inference_tick(engine, url)
            assert operations_tick(engine)
