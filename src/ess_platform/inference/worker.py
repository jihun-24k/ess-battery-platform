"""수집된 측정값을 ML 서버에 보내고 추론 결과·지연·재시도 상태를 저장한다."""

import logging
import math
from datetime import timedelta
from urllib.error import HTTPError, URLError

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine

from ess_platform.db import issues, measurements, models, now, predictions
from ess_platform.http_client import request_json
from ess_platform.locking import INFERENCE_LOCK, lock_worker

LOGGER = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def validate_prediction(conn: Connection, result: dict) -> None:
    """입력: DB Connection·ML 응답 dict. 반환: 없음.

    mock 표시·숫자·특징·승인 모델을 검사하고 잘못된 응답은 ValueError로 거부한다.
    """
    if result.get("mock") is not True or type(result.get("is_anomaly")) is not bool:
        raise ValueError("mock flag and boolean is_anomaly are required")
    score = result.get("score")
    if type(score) not in (int, float) or not math.isfinite(score):
        raise ValueError("finite numeric score required")
    features = result.get("features")
    if not isinstance(features, dict) or not features:
        raise ValueError("features must be a nonempty object")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in features.values()):
        raise ValueError("features must contain finite numbers")
    version = result.get("model_version")
    if not isinstance(version, str):
        raise ValueError("model_version required")
    approved = conn.execute(
        select(models.c.version).where(
            models.c.version == version, models.c.approved_at.is_not(None)
        )
    ).scalar_one_or_none()
    if not approved:
        raise ValueError("response references an unapproved model")


def inference_tick(engine: Engine, ml_url: str) -> bool:
    """입력: DB engine·ML 서버 URL. 반환: 측정값 또는 오류 상태를 처리했으면 True.

    가장 오래된 대기 측정값 하나를 추론한다. 작업 없음·잠금 충돌·재시도 대기면
    False다.
    """
    with engine.begin() as conn:
        if not lock_worker(conn, INFERENCE_LOCK):
            return False
        item = (
            conn.execute(
                select(measurements)
                .where(measurements.c.status.in_(["pending", "retry"]))
                .order_by(measurements.c.observed_at, measurements.c.id)
                .limit(1)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if item is None or item["next_attempt_at"] > now():
            return False
        latest = conn.execute(
            select(func.max(predictions.c.observed_at)).where(
                predictions.c.asset_id == item["asset_id"]
            )
        ).scalar_one()
        if latest is not None and item["observed_at"] <= latest:
            conn.execute(
                measurements.update()
                .where(measurements.c.id == item["id"])
                .values(status="late", last_error="event time is not newer than watermark")
            )
            conn.execute(
                issues.insert().values(
                    measurement_id=item["id"],
                    code="LATE_EVENT",
                    detail={"watermark": latest.isoformat()},
                )
            )
            return True
        history = (
            conn.execute(
                select(measurements.c.payload)
                .where(
                    measurements.c.asset_id == item["asset_id"],
                    measurements.c.observed_at < item["observed_at"],
                    measurements.c.status == "predicted",
                )
                .order_by(measurements.c.observed_at.desc())
                .limit(31)
            )
            .scalars()
            .all()
        )
        payload = {"readings": list(reversed(history)) + [item["payload"]]}
        attempt = item["attempts"] + 1
        try:
            result = request_json(ml_url.rstrip("/") + "/predict", payload)
            validate_prediction(conn, result)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            # URL의 인증 정보나 임의 응답 body가 DB 오류 기록에 남지 않도록 예외 종류만 저장한다.
            error = f"ML_REQUEST_FAILED:{type(exc).__name__}"
            conn.execute(
                measurements.update()
                .where(measurements.c.id == item["id"])
                .values(
                    attempts=attempt,
                    status="failed" if attempt >= MAX_ATTEMPTS else "retry",
                    last_error=error,
                    next_attempt_at=now() + timedelta(seconds=2**attempt),
                )
            )
            LOGGER.warning(
                "inference measurement=%s attempt=%s error=%s", item["id"], attempt, error
            )
            return True
        conn.execute(
            predictions.insert().values(
                measurement_id=item["id"],
                asset_id=item["asset_id"],
                observed_at=item["observed_at"],
                model_version=result["model_version"],
                features=result["features"],
                score=result["score"],
                is_anomaly=result["is_anomaly"],
            )
        )
        conn.execute(
            measurements.update()
            .where(measurements.c.id == item["id"])
            .values(status="predicted", attempts=attempt, last_error=None)
        )
        return True
