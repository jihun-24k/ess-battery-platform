"""③ ML mock 계산. 승인 설정의 데모 임계값으로 특징·점수·이상 여부를 반환한다.

실제 학습 모델이나 고장 확률 예측기가 아니다.
"""

import math

from ess_platform.schemas import PredictionInput


def predict(request: PredictionInput, version: str, config: dict) -> dict:
    """입력: 검증된 측정 윈도·승인 버전·mock 설정. 반환: 특징·점수·이상 여부 dict.

    최근 온도와 시연 임계값 차이를 사용하며 실제 모델 학습이나 고장 확률 계산은 하지
    않는다.
    """
    threshold = float(config["temperature_threshold_c"])
    if not math.isfinite(threshold):
        raise ValueError("mock threshold must be finite")
    values = request.readings
    latest = values[-1]
    features = {
        "temperature_mean_c": sum(x.temperature_c for x in values) / len(values),
        "temperature_latest_c": latest.temperature_c,
        "voltage_latest_v": latest.voltage_v,
        "current_latest_a": latest.current_a,
        "sample_count": len(values),
    }
    # 시연용 판단 규칙이다. 이 숫자를 실제 설비 안전 기준으로 사용하지 않는다.
    score = latest.temperature_c - threshold
    return {
        "mock": True,
        "model_version": version,
        "features": features,
        "score": score,
        "is_anomaly": score > 0,
        "meaning": "mock temperature difference; not fault probability",
    }
