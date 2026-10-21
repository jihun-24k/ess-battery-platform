"""CLI의 서버 이름을 실제 HTTP Handler에 연결하고 loopback 서버를 생성한다."""

from http.server import ThreadingHTTPServer

from sqlalchemy.engine import Engine

from ess_platform.collection.server import CollectorHandler
from ess_platform.inference.server import MockMLHandler
from ess_platform.operations.server import OperationsHandler

HANDLERS = {"collector": CollectorHandler, "ml": MockMLHandler, "operations": OperationsHandler}


def make_server(engine: Engine, kind: str, port: int) -> ThreadingHTTPServer:
    """입력: DB engine·서버 종류·port. 반환: 127.0.0.1에 bind한 미실행 HTTP 서버.

    serve_forever()와 종료 책임은 CLI 또는 테스트 호출자에게 있다.
    """
    if kind not in HANDLERS:
        raise ValueError("unknown service kind")

    class ConfiguredHandler(HANDLERS[kind]):
        """요청별 Handler에 같은 DB engine과 서버 이름을 전달한다."""

        database = engine
        service_kind = kind

    server = ThreadingHTTPServer(("127.0.0.1", port), ConfiguredHandler)
    server.daemon_threads = True
    return server
