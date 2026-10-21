"""⑤ 별도 mock 학습 작업. 설정 후보·데이터 cutoff·경계 계약 테스트를 저장하고 명시적으로
승인한다.

실제 모델 fitting이나 고장 성능 평가를 수행하지 않는다.
"""

import json
import math

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from ess_platform.db import measurements, models, now, registry
from ess_platform.inference.mock import predict
from ess_platform.schemas import PredictionInput


def create_candidate(engine: Engine, version: str, threshold: float) -> dict:
    """입력: DB engine·새 버전 이름·데모 온도 임계값. 반환: 후보와 계약 테스트·cutoff
    dict.

    경계값 아래/동일/위 세 테스트를 수행한다. 실제 데이터로 모델을 fitting하지
    않는다.
    """
    if not version or len(version) > 100 or not math.isfinite(threshold):
        raise ValueError("version (1..100 chars) and finite threshold required")
    config = {"temperature_threshold_c": threshold, "feature_version": "mock-features-1"}
    checks = []
    for delta in (-1, 0, 1):
        request = PredictionInput.model_validate_json(
            json.dumps(
                {
                    "readings": [
                        {
                            "event_id": "test",
                            "asset_id": "contract-test",
                            "observed_at": "2026-01-01T00:00:00Z",
                            "temperature_c": threshold + delta,
                            "voltage_v": 3.5,
                            "current_a": 0.0,
                        }
                    ]
                }
            )
        )
        checks.append(predict(request, version, config)["is_anomaly"] == (delta > 0))
    evaluation = {
        "contract_checks": len(checks),
        "passed": all(checks),
        "real_training": False,
        "fault_metrics": None,
    }
    with engine.begin() as conn:
        cutoff = conn.execute(select(func.coalesce(func.max(measurements.c.id), 0))).scalar_one()
        conn.execute(
            models.insert().values(
                version=version,
                kind="threshold_mock",
                config=config,
                evaluation=evaluation,
                training_cutoff_id=cutoff,
            )
        )
    return {"version": version, "evaluation": evaluation, "training_cutoff_id": cutoff}


def approve(engine: Engine, version: str) -> dict:
    """입력: DB engine·후보 버전. 반환: 승인 버전과 mock 표시 dict.

    계약 테스트가 통과한 후보만 승인하고 active 포인터를 같은 transaction에서
    변경한다.
    """
    with engine.begin() as conn:
        model = (
            conn.execute(select(models).where(models.c.version == version).with_for_update())
            .mappings()
            .one_or_none()
        )
        if not model or not model["evaluation"].get("passed"):
            raise ValueError("candidate missing or contract tests failed")
        conn.execute(models.update().where(models.c.version == version).values(approved_at=now()))
        conn.execute(
            insert(registry)
            .values(name="active", version=version)
            .on_conflict_do_update(index_elements=["name"], set_={"version": version})
        )
    return {"approved_version": version, "mock": True}
