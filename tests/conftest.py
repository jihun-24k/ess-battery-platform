"""공통 DB fixture. 전용 PostgreSQL 테스트 DB 안에 테스트별 독립 schema를 생성하고
정리한다.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from ess_platform.db import make_engine, metadata
from ess_platform.training import approve, create_candidate


@pytest.fixture
def engine():
    """입력: PLATFORM_TEST_DATABASE_URL 환경변수. 반환: 테스트 전용 schema를 사용하는
    Engine fixture.

    테스트 종료 시 자신이 생성한 schema만 제거한다. 환경변수 없으면 DB 테스트를
    skip한다.
    """
    url = os.environ.get("PLATFORM_TEST_DATABASE_URL")
    if not url:
        pytest.skip("PLATFORM_TEST_DATABASE_URL is required for PostgreSQL tests")
    base = make_engine(url)
    if not base.url.database.startswith("ess_platform_test"):
        raise RuntimeError("use a dedicated database named ess_platform_test*")
    schema = "test_" + uuid4().hex
    with base.begin() as conn:
        conn.execute(CreateSchema(schema))
    scoped = base.execution_options(schema_translate_map={None: schema})
    metadata.create_all(scoped)
    try:
        yield scoped
    finally:
        with base.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        base.dispose()


@pytest.fixture
def approved(engine):
    """입력: 독립 테스트 DB engine fixture. 반환: mock-v1 후보 등록·승인을 완료한
    Engine.
    """
    create_candidate(engine, "mock-v1", 45.0)
    approve(engine, "mock-v1")
    return engine


@pytest.fixture
def agent_incident(approved):
    """입력: 승인 모델 DB fixture. 반환: assisted 사건·작업·조회 context가 준비된 tuple."""
    import json

    from sqlalchemy import select

    from ess_platform.agents.repository import build_context
    from ess_platform.agents.retrieval import seed_learning_manual
    from ess_platform.collection.service import collect
    from ess_platform.db import agent_jobs
    from ess_platform.inference.worker import inference_tick
    from ess_platform.operations.incidents import operations_tick
    from tests.helpers import reading, service

    seed_learning_manual(approved)
    with service(approved, "ml") as url:
        for step in range(2):
            collect(approved, json.dumps(reading(step, 50.0)).encode())
            inference_tick(approved, url)
            operations_tick(approved, response_mode="assisted")
    with approved.connect() as conn:
        job = dict(conn.execute(select(agent_jobs)).mappings().one())
    return approved, job, build_context(approved, job)
