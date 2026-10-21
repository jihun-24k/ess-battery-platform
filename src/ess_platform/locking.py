"""여러 워커가 같은 처리 단계를 동시에 갱신하지 않도록 DB 잠금을 제공한다."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

INFERENCE_LOCK = 831001
# 사건 처리와 조치 처리는 같은 잠금을 사용해 잠금 순서 충돌을 방지한다.
OPERATIONS_LOCK = 831002


def lock_worker(conn: Connection, key: int) -> bool:
    """입력: transaction의 DB Connection·잠금 키. 반환: 잠금 획득 여부 bool.

    다른 워커가 이미 처리 중이면 기다리지 않으며 transaction 종료 시 자동 해제된다.
    """
    return conn.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}).scalar_one()
