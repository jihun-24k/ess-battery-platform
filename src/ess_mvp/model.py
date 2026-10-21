"""과거 MVP의 Isolation Forest 참조 모델과 8개 특징 계산 코드. 새 ML mock 서버와 별개다.

고장 확률을 산출하지 않으며 현재는 누락된 battery_etl에 의존한다.
"""

from dataclasses import dataclass

import numpy as np
from battery_etl.errors import ConfigurationError
from numpy.typing import NDArray
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from ess_mvp.replay import Window

FEATURE_NAMES = (
    "temperature_mean",
    "temperature_max",
    "temperature_std",
    "temperature_trend_per_sample",
    "voltage_mean",
    "voltage_std",
    "current_mean",
    "current_std",
)


@dataclass(frozen=True)
class Prediction:
    is_anomaly: bool
    anomaly_score: float
    kind: str = "measurement_anomaly"


def build_features(window: Window) -> dict[str, float]:
    """입력: 같은 사이클의 Window. 반환: 온도·전압·전류의 특징 8개 dict.

    시간 추이는 초당이 아닌 샘플당 변화이며 비유한 특징은 예외로 거부한다.
    """
    samples = window.measurements
    if len(samples) < 2 or len({m.cycle_index for m in samples}) != 1:
        raise ConfigurationError("features require >= 2 samples from one cycle")
    t = np.array([m.temperature for m in samples])
    v = np.array([m.voltage for m in samples])
    i = np.array([m.current for m in samples])
    values = [
        t.mean(),
        t.max(),
        t.std(),
        (t[-1] - t[0]) / (len(t) - 1),
        v.mean(),
        v.std(),
        i.mean(),
        i.std(),
    ]
    if not np.isfinite(values).all():
        raise ConfigurationError("nonfinite features are not allowed")
    return dict(zip(FEATURE_NAMES, map(float, values), strict=True))


def feature_matrix(rows: list[dict[str, float]]) -> NDArray[np.float64]:
    """입력: 특징 dict 목록. 반환: 고정된 특징 순서의 유한 NumPy 행렬.

    누락·추가 특징 또는 비유한 값이 있으면 예외를 발생시킨다.
    """
    if not rows or any(set(row) != set(FEATURE_NAMES) for row in rows):
        raise ConfigurationError("missing/extra feature names or no feature rows")
    matrix = np.array([[row[name] for name in FEATURE_NAMES] for row in rows], dtype=float)
    if not np.isfinite(matrix).all():
        raise ConfigurationError("nonfinite feature matrix")
    return matrix


def train_model(rows: list[dict[str, float]], contamination: float = 0.05) -> Pipeline:
    """입력: 참조 기간 특징 목록·contamination 설정. 반환: 학습된
    StandardScaler/IsolationForest Pipeline.
    """
    if len(rows) < 10:
        raise ConfigurationError("at least 10 reference windows required; increase training cycles")
    if not 0 < contamination <= 0.5:
        raise ConfigurationError("contamination must be in (0, 0.5]")
    model = Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "isolation",
                IsolationForest(
                    n_estimators=100, contamination=contamination, random_state=42, n_jobs=1
                ),
            ),
        ]
    )
    model.fit(feature_matrix(rows))
    return model


def predict(model: Pipeline, features: dict[str, float]) -> Prediction:
    """입력: 학습된 sklearn Pipeline·특징 dict. 반환: Prediction. 점수가 클수록 참조
    패턴에서 벗어난다.
    """
    decision = float(model.decision_function(feature_matrix([features]))[0])
    return Prediction(is_anomaly=decision < 0, anomaly_score=-decision)
