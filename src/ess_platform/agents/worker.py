"""영속 작업 하나를 claim해 Agent를 실행하고 분석·대응안·상태를 같은 transaction에
저장한다.
"""

import logging

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from sqlalchemy.engine import Engine

from ess_platform.agents.repository import (
    LeaseLost,
    build_context,
    claim_job,
    finish_run,
    save_completion,
    save_step,
    start_run,
)
from ess_platform.agents.runner import BudgetExceeded, run_agent
from ess_platform.operations.notifications import enqueue_notice
from ess_platform.operations.proposals import store_proposal

LOGGER = logging.getLogger(__name__)


def agent_tick(engine: Engine, client) -> bool:
    """입력: DB·LLM client. 반환: 작업 처리 여부. 외부 호출 중에는 transaction을
    유지하지 않는다.
    """
    job = claim_job(engine)
    if job is None:
        return False
    try:
        context = build_context(engine, job)
        run_id = start_run(engine, job, context, client.provider, client.model)
        if context.snapshot["incident"]["status"] != "open":
            finish_run(
                engine, job, run_id, "cancelled", None, {}, "incident_recovered_before_analysis"
            )
            return True
    except LeaseLost:
        LOGGER.warning("agent lease lost during preparation job=%s", job["id"])
        return True
    try:

        def record(index, call, result, elapsed):
            """입력: Tool 요청/결과·시간. 반환: 없음. 소유 token이 유효한 실행의 단계만
            저장한다.
            """
            save_step(engine, job, run_id, index, call, result, elapsed)

        assessment, usage = run_agent(engine, context, client, record)
        with engine.begin() as conn:
            # 제안은 이 transaction의 lease 검사 실패 시 함께 rollback된다.
            proposal_id = store_proposal(conn, context, run_id)
            result = {**assessment.model_dump(mode="json"), "proposal_id": proposal_id}
            save_completion(
                conn,
                job,
                run_id,
                "needs_review" if assessment.disposition == "needs_review" else "completed",
                result,
                usage,
            )
            enqueue_notice(
                conn,
                job["incident_id"],
                f"agent:{job['id']}",
                f"Agent analysis stored: run={run_id}, disposition={assessment.disposition}",
            )
        return True
    except LeaseLost:
        LOGGER.warning("agent stale lease discarded job=%s run=%s", job["id"], run_id)
        return True
    except (
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
        APIStatusError,
        BudgetExceeded,
        ValueError,
    ) as exc:
        retryable = isinstance(exc, (APIConnectionError, APITimeoutError, RateLimitError)) or (
            isinstance(exc, APIStatusError) and exc.status_code >= 500
        )
        status = "retry" if retryable and job["attempts"] < 3 else "needs_review"
        error = f"{type(exc).__name__}"  # 공급자 응답 본문·키는 DB/로그에 복사하지 않는다.
        try:
            with engine.begin() as conn:
                save_completion(conn, job, run_id, status, None, {}, error)
                if status != "retry":
                    enqueue_notice(
                        conn,
                        job["incident_id"],
                        f"agent-error:{job['id']}",
                        f"Agent requires review: job={job['id']} error={error}",
                    )
        except LeaseLost:
            LOGGER.warning("agent failed result discarded after lease loss job=%s", job["id"])
        LOGGER.warning("agent job=%s status=%s error=%s", job["id"], status, error)
        return True
