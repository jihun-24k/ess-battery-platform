"""알림을 DB outbox에 중복 없이 예약하고 콘솔로 전달한다."""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

from ess_platform.db import notifications, now

LOGGER = logging.getLogger(__name__)


def enqueue_notice(conn: Connection, incident_id: int, key: str, message: str) -> None:
    """입력: DB Connection·사건 ID·전이 키·메시지. 반환: 없음.

    같은 사건 전이의 알림을 unique key로 한 번만 outbox에 예약한다.
    """
    conn.execute(
        insert(notifications)
        .values(
            incident_id=incident_id,
            dedup_key=f"{incident_id}:{key}",
            message=message,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
    )


def notification_tick(engine: Engine) -> bool:
    """입력: DB engine. 반환: 대기 알림 하나를 콘솔로 전달했으면 True.

    전달 시각을 DB에 남긴다. 출력 직후 비정상 종료 시 같은 알림이 다시 출력될 수
    있다.
    """
    with engine.begin() as conn:
        notice = (
            conn.execute(
                select(notifications)
                .where(notifications.c.delivered_at.is_(None))
                .order_by(notifications.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            .mappings()
            .one_or_none()
        )
        if notice is None:
            return False
        LOGGER.warning(
            "[CONSOLE MOCK] notice=%s incident=%s %s",
            notice["id"],
            notice["incident_id"],
            notice["message"],
        )
        conn.execute(
            notifications.update()
            .where(notifications.c.id == notice["id"])
            .values(delivered_at=now())
        )
        return True
