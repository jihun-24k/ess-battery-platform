"""기존 사건·조치를 보존하며 Agent 테이블·컬럼을 추가하는 순서형 migration."""

import hashlib

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from ess_platform.db import metadata, migrations

STATEMENTS = [
    "ALTER TABLE {p}rt_incidents ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1",
    (
        "ALTER TABLE {p}rt_incidents ADD COLUMN IF NOT EXISTS "
        "response_mode VARCHAR(24) NOT NULL DEFAULT 'legacy'"
    ),
    (
        "ALTER TABLE {p}rt_actions ADD COLUMN IF NOT EXISTS "
        "proposal_id BIGINT UNIQUE REFERENCES {p}rt_action_proposals(id)"
    ),
    "ALTER TABLE {p}rt_actions ADD COLUMN IF NOT EXISTS command_key VARCHAR(100) UNIQUE",
]


VERSIONS = [
    ("001_agents", STATEMENTS),
    (
        "002_job_request_context",
        [
            "ALTER TABLE {p}rt_agent_jobs ADD COLUMN IF NOT EXISTS "
            "request_context JSONB NOT NULL DEFAULT '{}'::jsonb",
        ],
    ),
]


def migrate(engine: Engine) -> dict:
    """입력: DB engine. 반환: 적용 버전·상태.

    기존 데이터는 보존하고 순서대로 checksum을 확인한다.
    """
    applied = []
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(831010)"))
        metadata.create_all(conn)
        schema = conn.get_execution_options().get("schema_translate_map", {}).get(None)
        prefix = conn.dialect.identifier_preparer.quote(schema) + "." if schema else ""
        for version, statements in VERSIONS:
            checksum = hashlib.sha256("\n".join(statements).encode()).hexdigest()
            existing = (
                conn.execute(select(migrations).where(migrations.c.version == version))
                .mappings()
                .one_or_none()
            )
            if existing:
                if existing["checksum"] != checksum:
                    raise ValueError("migration checksum mismatch")
                continue
            for statement in statements:
                conn.execute(text(statement.replace("{p}", prefix)))
            conn.execute(migrations.insert().values(version=version, checksum=checksum))
            applied.append(version)
    return {
        "version": VERSIONS[-1][0],
        "status": "applied" if applied else "already_applied",
        "applied": applied,
    }
