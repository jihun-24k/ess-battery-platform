"""프로그램 진입점. CLI 옵션을 해석하고 수집·ML mock·운영 서버 또는 워커를 실행한다.

업무 처리는 각 역할의 패키지에 위임하며 이 파일은 실행과 종료 코드만 조정한다.
"""

import argparse
import json
import logging
import math
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ess_platform.agents.llm_client import OpenAIClient
from ess_platform.agents.retrieval import seed_learning_manual
from ess_platform.agents.worker import agent_tick
from ess_platform.collection.replay import demo_readings, replay_file
from ess_platform.db import make_engine, measurements, now
from ess_platform.inference.worker import inference_tick
from ess_platform.migrations import migrate
from ess_platform.operations.actions import action_tick
from ess_platform.operations.incidents import MANUAL_PATH, operations_tick
from ess_platform.operations.notifications import notification_tick
from ess_platform.operations.queries import state_snapshot
from ess_platform.servers import make_server
from ess_platform.training import approve, create_candidate

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """입력: 없음. 반환: 서버·워커·재생·모델 관리 명령을 정의한 ArgumentParser."""
    parser = argparse.ArgumentParser(prog="ess-platform")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create/update rt_* tables in an existing dedicated database")
    sub.add_parser("migrate", help="apply versioned additive schema migrations")
    sub.add_parser("seed-manuals", help="register versioned simulation learning manual")
    serve = sub.add_parser("serve")
    serve.add_argument("kind", choices=["collector", "ml", "operations"])
    serve.add_argument("--port", type=int)
    worker = sub.add_parser("worker")
    worker.add_argument("kind", choices=["inference", "operations", "agent"])
    worker.add_argument("--response-mode", choices=["shadow", "assisted"], default="shadow")
    worker.add_argument("--model", help="OpenAI model override for agent worker")
    worker.add_argument("--ml-url", default="http://127.0.0.1:8102")
    worker.add_argument("--poll", type=float, default=0.25)
    worker.add_argument("--once", action="store_true", help="run one processing tick then exit")
    worker.add_argument("--drain", action="store_true", help="process available work then exit")
    worker.add_argument("--manual", type=Path, default=MANUAL_PATH)
    worker.add_argument("--simulate-action-failure", action="store_true")
    demo = sub.add_parser("make-demo")
    demo.add_argument("output", type=Path)
    demo.add_argument("--stream-id", default="demo-1")
    replay = sub.add_parser("replay")
    replay.add_argument("path", type=Path)
    replay.add_argument("--collector-url", default="http://127.0.0.1:8101")
    replay.add_argument("--interval", type=float, default=0.1)
    train = sub.add_parser("train-mock", help="register a mock candidate, not real model training")
    train.add_argument("--version", required=True)
    train.add_argument("--threshold", type=float, default=45.0)
    approval = sub.add_parser("approve-model")
    approval.add_argument("version")
    retry = sub.add_parser("retry-inference")
    retry.add_argument("measurement_id", type=int)
    evaluation = sub.add_parser(
        "eval-agent", help="run actual LLM evaluation in a dedicated ess_agent_eval DB"
    )
    evaluation.add_argument("--output", type=Path, default=Path("runs/agent-evaluation.json"))
    evaluation.add_argument("--limit", type=int, default=20)
    evaluation.add_argument("--model")
    sub.add_parser("status")
    return parser


def run_worker(engine: Engine, args: argparse.Namespace) -> int:
    """입력: DB engine·해석된 CLI 옵션. 반환: 성공 0 또는 DB 실패 1.

    선택한 워커의 반복 실행·poll 대기·once/drain 종료 조건을 조정한다.
    """
    if not math.isfinite(args.poll) or args.poll < 0.01:
        raise ValueError("poll must be finite and >= 0.01 seconds")
    if args.once and args.drain:
        raise ValueError("choose --once or --drain")
    client = OpenAIClient(args.model) if args.kind == "agent" else None
    while True:
        try:
            if args.kind == "inference":
                worked = inference_tick(engine, args.ml_url)
            elif args.kind == "agent":
                worked = agent_tick(engine, client)
            else:
                # any(함수 호출 generator)는 앞에서 True면 나머지를 생략하므로 먼저 모두 실행한다.
                outcomes = [
                    operations_tick(engine, args.manual, args.response_mode),
                    action_tick(engine, args.simulate_action_failure),
                    notification_tick(engine),
                ]
                worked = any(outcomes)
        except SQLAlchemyError as exc:
            LOGGER.error("worker database failure (%s); retry after poll", type(exc).__name__)
            if args.once or args.drain:
                return 1
            worked = False
        if args.once or (args.drain and not worked):
            break
        if not worked:
            time.sleep(args.poll)
    return 0


def main(argv: list[str] | None = None) -> int:
    """입력: CLI 인자 목록 또는 None. 반환: 종료 코드 0/1.

    명령을 해석해 각 역할에 위임하고 오류·자원 정리를 처리한다. --help는 argparse가
    종료한다.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    engine = None
    try:
        if args.command == "make-demo":
            rows = demo_readings(args.stream_id)
            # x 모드로 생성해 기존 데모 파일을 덮어쓰지 않는다.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write("\n".join(json.dumps(r) for r in rows) + "\n")
            print(json.dumps({"file": str(args.output), "readings": len(rows)}))
            return 0
        if args.command == "replay":
            print(json.dumps(replay_file(args.path, args.collector_url, args.interval)))
            return 0
        engine = make_engine()
        if args.command in {"init-db", "migrate"}:
            print(json.dumps(migrate(engine)))
        elif args.command == "seed-manuals":
            print(json.dumps(seed_learning_manual(engine)))
        elif args.command == "train-mock":
            print(json.dumps(create_candidate(engine, args.version, args.threshold)))
        elif args.command == "approve-model":
            print(json.dumps(approve(engine, args.version)))
        elif args.command == "eval-agent":
            from ess_platform.agents.evaluation import evaluate_live

            summary = evaluate_live(engine, args.output, args.limit, args.model)
            print(json.dumps(summary))
            return 0 if summary["passed"] == summary["total"] else 1
        elif args.command == "status":
            print(json.dumps(state_snapshot(engine), ensure_ascii=False, indent=2))
        elif args.command == "retry-inference":
            with engine.begin() as conn:
                result = conn.execute(
                    measurements.update()
                    .where(
                        measurements.c.id == args.measurement_id, measurements.c.status == "failed"
                    )
                    .values(status="pending", attempts=0, next_attempt_at=now(), last_error=None)
                )
                print(json.dumps({"requeued": result.rowcount}))
        elif args.command == "serve":
            port = args.port or {"collector": 8101, "ml": 8102, "operations": 8103}[args.kind]
            with engine.connect() as conn:
                conn.execute(select(1))
            with make_server(engine, args.kind, port) as server:
                LOGGER.info("%s listening at http://127.0.0.1:%s", args.kind, server.server_port)
                server.serve_forever()
        elif args.command == "worker":
            return run_worker(engine, args)
        return 0
    except KeyboardInterrupt:
        LOGGER.info("service stopped; committed processing state remains in PostgreSQL")
        return 0
    except SQLAlchemyError as exc:
        LOGGER.error(
            "database operation failed (%s); check DATABASE_URL and schema", type(exc).__name__
        )
        return 1
    except (ValueError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1
    finally:
        if engine:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
