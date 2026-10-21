"""ML 서버가 사용할 승인 모델 버전을 DB에서 조회한다."""

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ess_platform.db import models, registry


def active_model(engine: Engine) -> dict | None:
    """입력: DB engine. 반환: active 포인터가 가리키는 승인 모델 RowMapping 또는 None."""
    with engine.connect() as conn:
        return (
            conn.execute(
                select(models)
                .join(registry, models.c.version == registry.c.version)
                .where(registry.c.name == "active", models.c.approved_at.is_not(None))
            )
            .mappings()
            .one_or_none()
        )
