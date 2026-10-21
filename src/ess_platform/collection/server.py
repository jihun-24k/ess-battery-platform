"""① 수집 서버의 URL 처리: POST /measurements만 수집 서비스에 연결한다."""

from ess_platform.collection.service import collect
from ess_platform.http_server import ServiceHandler


class CollectorHandler(ServiceHandler):
    """수집 API 요청을 검증·중복 처리 서비스로 전달한다."""

    def route(self, method: str, path: str) -> bool:
        """입력: HTTP method·경로. 반환: 수집 경로 처리 여부. 결과에 따라 201/200 응답."""
        if method != "POST" or path != "/measurements":
            return False
        result = collect(self.database, self.read_body())
        self.respond(201 if result["status"] == "inserted" else 200, result)
        return True
