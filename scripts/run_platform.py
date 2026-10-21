"""로컬 통합 실행기. HTTP 서버 3개와 기본 워커 2개를 실행한다.

Agent 사용 시 워커 하나를 추가하고 종료 시 자신이 실행한 프로세스를 함께 정리한다.

DATABASE_URL과 승인 모델이 필요하며 로그와 마지막 DB 상태를 runs/platform에 남긴다.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

from ess_platform.collection.replay import replay_file
from ess_platform.db import make_engine
from ess_platform.inference.registry import active_model
from ess_platform.operations.queries import state_snapshot

ROOT = Path(__file__).resolve().parents[1]


def wait_for_service(url: str, process: subprocess.Popen, timeout: float = 15) -> None:
    """입력: health URL·자식 프로세스·최대 대기 초. 반환: 준비 완료 시 None.

    자식 종료·대기 시간 초과는 RuntimeError로 보고한다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"service exited with code {process.returncode}")
        try:
            with urlopen(url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(0.1)
    raise RuntimeError("service readiness timed out")


def main(argv: list[str] | None = None) -> int:
    """입력: launcher CLI 인자. 반환: 정상 종료 0, 실행 실패 1.

    서버 3개·기본 워커 2개·선택적 Agent를 띄우고 종료 신호까지 대기한다.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--agent", action="store_true", help="also start the real LLM agent worker")
    parser.add_argument("--response-mode", choices=["shadow", "assisted"], default="shadow")
    args = parser.parse_args(argv)
    engine = None
    processes = []
    agent_process = None
    agent_exit_reported = False
    logs = []
    session_dir = ROOT / "runs/platform" / uuid4().hex
    try:
        engine = make_engine()
        if active_model(engine) is None:
            raise ValueError("run init-db, train-mock and approve-model before starting services")
        session_dir.mkdir(parents=True)
        specs = [
            ("collector", ["serve", "collector"], "http://127.0.0.1:8101"),
            ("ml", ["serve", "ml"], "http://127.0.0.1:8102"),
            ("operations-api", ["serve", "operations"], "http://127.0.0.1:8103"),
            ("inference-worker", ["worker", "inference"], None),
            (
                "operations-worker",
                ["worker", "operations", "--response-mode", args.response_mode],
                None,
            ),
        ]
        if args.agent:
            if not os.environ.get("OPENAI_API_KEY"):
                raise ValueError("OPENAI_API_KEY is required with --agent")
            specs.append(("agent-worker", ["worker", "agent"], None))
        for name, command, url in specs:
            log = (session_dir / f"{name}.log").open("w", encoding="utf-8")
            logs.append(log)
            process = subprocess.Popen(
                [sys.executable, "-m", "ess_platform.cli", *command],
                cwd=ROOT,
                env=os.environ.copy(),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            processes.append(process)
            if name == "agent-worker":
                agent_process = process
            if url:
                wait_for_service(url, process)
        print(f"Dashboard: http://127.0.0.1:8103\nLogs: {session_dir}", flush=True)
        if args.replay:
            print(
                json.dumps(replay_file(args.replay, "http://127.0.0.1:8101", args.interval)),
                flush=True,
            )
        print(
            f"{len(processes)} processes running. Ctrl+C stops them; DB data is preserved.",
            flush=True,
        )
        while True:
            if (
                agent_process is not None
                and agent_process.poll() is not None
                and not agent_exit_reported
            ):
                print(
                    "Agent worker stopped; core services continue. Inspect agent-worker.log.",
                    flush=True,
                )
                agent_exit_reported = True
            if any(p.poll() is not None for p in processes if p is not agent_process):
                raise RuntimeError("a core process stopped; inspect session logs")
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Top-level launcher records the failure type without exposing database credentials.
        print(
            f"Platform start/run failed: {type(exc).__name__}; logs: {session_dir}", file=sys.stderr
        )
        return 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for log in logs:
            log.close()
        if engine:
            if processes:
                try:
                    (session_dir / "final-state.json").write_text(
                        json.dumps(state_snapshot(engine), ensure_ascii=False, indent=2)
                    )
                except Exception as exc:
                    print(f"Final snapshot unavailable: {type(exc).__name__}", file=sys.stderr)
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
