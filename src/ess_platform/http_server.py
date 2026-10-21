"""서버 공통 HTTP 처리: JSON 입출력, 상태 확인, 오류 응답과 요청 로그.

업무별 URL 처리는 collection/inference/operations의 server.py가 담당한다.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ess_platform.collection.service import EventConflict

LOGGER = logging.getLogger(__name__)
BODY_LIMIT = 262_144


class ServiceHandler(BaseHTTPRequestHandler):
    """서버별 route()만 바꿔 사용하며 DB 연결과 오류 처리 방식은 공유한다."""

    database: Engine
    service_kind: str

    def log_message(self, fmt: str, *args) -> None:
        """입력: HTTP 로그 형식과 값. 반환: 없음. Python logging으로 요청을 기록한다."""
        LOGGER.info("%s %s", self.service_kind, fmt % args)

    def respond(self, status: int, payload, content_type="application/json; charset=utf-8") -> None:
        """입력: 상태 코드·JSON 또는 bytes·MIME. 반환: 없음. HTTP 응답을 전송한다."""
        raw = (
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            if isinstance(payload, (dict, list))
            else payload
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> bytes:
        """입력: 현재 요청 헤더/body. 반환: 최대 256 KiB의 JSON bytes. 형식 오류는 예외."""
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= BODY_LIMIT:
            raise ValueError("body size must be 1..262144 bytes")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type application/json required")
        return self.rfile.read(length)

    def route(self, method: str, path: str) -> bool:
        """입력: HTTP method·경로. 반환: 처리 여부. 서버별 하위 클래스에서 구현한다."""
        return False

    def dispatch(self, method: str) -> None:
        """입력: GET/POST. 반환: 없음. health·업무 경로를 실행하고 오류를 HTTP로
        변환한다.
        """
        self.connection.settimeout(5)
        try:
            path = urlsplit(self.path).path
            if method == "GET" and path == "/health":
                with self.database.connect() as conn:
                    conn.execute(select(1))
                self.respond(
                    200,
                    {
                        "status": "ok",
                        "service": self.service_kind,
                        "mock": self.service_kind == "ml",
                        "database": "connected",
                    },
                )
            elif not self.route(method, path):
                self.respond(404, {"error": "route_not_found"})
        except ValidationError as exc:
            errors = [
                {"field": ".".join(map(str, e["loc"])), "type": e["type"]} for e in exc.errors()
            ]
            self.respond(422, {"error": "validation_failed", "details": errors})
        except EventConflict as exc:
            self.respond(409, {"error": "event_identity_conflict", "detail": str(exc)})
        except (ValueError, UnicodeError):
            self.respond(400, {"error": "invalid_request"})
        except SQLAlchemyError as exc:
            LOGGER.error("%s database operation failed (%s)", self.service_kind, type(exc).__name__)
            self.respond(503, {"error": "database_unavailable_or_operation_failed"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            LOGGER.warning("%s client disconnected or timed out", self.service_kind)
        except Exception:
            LOGGER.exception("%s unexpected server error", self.service_kind)
            self.respond(500, {"error": "internal_error"})

    def do_GET(self) -> None:
        """입력: 표준 HTTP GET 요청. 반환: 없음. 공통 dispatch로 위임한다."""
        self.dispatch("GET")

    def do_POST(self) -> None:
        """입력: 표준 HTTP POST 요청. 반환: 없음. 공통 dispatch로 위임한다."""
        self.dispatch("POST")
