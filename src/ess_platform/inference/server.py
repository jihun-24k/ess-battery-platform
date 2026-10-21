"""③ ML mock 서버의 URL 처리: 승인 설정으로 POST /predict를 실행한다."""

from ess_platform.http_server import ServiceHandler
from ess_platform.inference.mock import predict
from ess_platform.inference.registry import active_model
from ess_platform.schemas import PredictionInput


class MockMLHandler(ServiceHandler):
    """ML mock 계약과 승인 버전을 확인하고 특징·점수·판정을 반환한다."""

    def route(self, method: str, path: str) -> bool:
        """입력: HTTP method·경로. 반환: 추론 경로 처리 여부. 승인 모델이 없으면 503."""
        if method != "POST" or path != "/predict":
            return False
        request = PredictionInput.model_validate_json(self.read_body())
        model = active_model(self.database)
        if model is None:
            self.respond(503, {"error": "no_approved_mock_model"})
        else:
            self.respond(200, predict(request, model["version"], model["config"]))
        return True
