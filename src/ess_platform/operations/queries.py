"""관제 화면에 필요한 DB 집계와 사건별 판단·문서·대응 근거를 조회한다."""

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from ess_platform.db import (
    actions,
    agent_jobs,
    agent_runs,
    assets,
    incidents,
    issues,
    measurements,
    models,
    notifications,
    predictions,
    proposals,
    registry,
    row_dict,
)


def state_snapshot(engine: Engine) -> dict:
    """입력: DB engine. 반환: 전체 집계와 종류별 최신 최대 100건을 담은 관제 dict.

    한 repeatable-read snapshot으로 집계와 목록의 조회 시점을 맞춘다.
    """
    with engine.connect() as conn:
        # 조회 중 데이터가 추가되어도 집계와 목록이 서로 다른 시점을 가리키지 않게 한다.
        conn = conn.execution_options(isolation_level="REPEATABLE READ")
        counts = {}
        for name, table in [
            ("measurements", measurements),
            ("predictions", predictions),
            ("incidents", incidents),
            ("actions", actions),
            ("notifications", notifications),
            ("quality_issues", issues),
        ]:
            counts[name] = conn.execute(select(func.count()).select_from(table)).scalar_one()
        queues = {
            r.status: r.count
            for r in conn.execute(
                select(measurements.c.status, func.count().label("count")).group_by(
                    measurements.c.status
                )
            )
        }
        model = conn.execute(select(registry.c.version).where(registry.c.name == "active"))
        result = {
            "mode": "local_mock_platform",
            "ml_mock": True,
            "physical_control_connected": False,
            "notification_channel": "console_mock",
            "counts": counts,
            "measurement_statuses": queues,
            "active_model_version": model.scalar_one_or_none(),
        }
        for name, table, order in [
            ("assets", assets, assets.c.asset_id),
            ("incidents", incidents, incidents.c.id.desc()),
            ("actions", actions, actions.c.id.desc()),
            ("notifications", notifications, notifications.c.id.desc()),
            ("telemetry", measurements, measurements.c.observed_at.desc()),
            ("predictions", predictions, predictions.c.id.desc()),
            ("quality_issues", issues, issues.c.id.desc()),
            ("models", models, models.c.created_at.desc()),
        ]:
            result[name] = [
                row_dict(r) for r in conn.execute(select(table).order_by(order).limit(100))
            ]
        result["agent_counts"] = {}
        for name, table in [("jobs", agent_jobs), ("runs", agent_runs), ("proposals", proposals)]:
            result["agent_counts"][name] = conn.execute(
                select(func.count()).select_from(table)
            ).scalar_one()
            result["agent_" + name] = [
                row_dict(row)
                for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(50))
            ]
        return result


def incident_detail(engine: Engine, incident_id: int) -> dict | None:
    """입력: DB engine·사건 ID. 반환: 사건·측정·추론·모델 설정·문서·조치·알림 dict.

    해당 사건이 없으면 None을 반환한다.
    """
    with engine.connect() as conn:
        row = conn.execute(select(incidents).where(incidents.c.id == incident_id)).one_or_none()
        if row is None:
            return None
        prediction = conn.execute(
            select(predictions).where(predictions.c.id == row.opened_prediction_id)
        ).one()
        measurement = conn.execute(
            select(measurements).where(measurements.c.id == prediction.measurement_id)
        ).one()
        model = conn.execute(
            select(models).where(models.c.version == prediction.model_version)
        ).one()
        return {
            "incident": row_dict(row),
            "evidence": {
                "prediction": row_dict(prediction),
                "measurement": row_dict(measurement),
                "model_config": model.config,
            },
            "actions": [
                row_dict(r)
                for r in conn.execute(select(actions).where(actions.c.incident_id == incident_id))
            ],
            "notifications": [
                row_dict(r)
                for r in conn.execute(
                    select(notifications)
                    .where(notifications.c.incident_id == incident_id)
                    .order_by(notifications.c.id)
                )
            ],
        }
