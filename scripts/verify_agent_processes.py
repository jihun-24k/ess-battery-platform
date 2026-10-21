"""실제 LLM과 6개 프로세스의 통합 검증. 임시 승인 토큰은 메모리에만 두고 가상 장비만 조작한다."""

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from run_platform import wait_for_service

from ess_platform.http_client import request_json

ROOT = Path(__file__).resolve().parents[1]


def get_json(path: str) -> dict:
    """입력: 로컬 운영 API 경로. 반환: JSON 응답. 외부 URL과 인증 정보는 받지 않는다."""
    with urlopen("http://127.0.0.1:8103" + path, timeout=5) as response:
        return json.load(response)


def main() -> int:
    """입력: 전용 데모 DB·API 키 환경변수. 반환: 전체 통합 검증 성공 0 또는 실패 1."""
    if not os.environ.get("DATABASE_URL", "").rsplit("/", 1)[-1].startswith("ess_agent_demo"):
        raise ValueError("use a dedicated ess_agent_demo* database")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required; this script makes actual LLM calls")
    token = secrets.token_urlsafe(32)
    environment = {
        **os.environ,
        "ESS_APPROVER_TOKEN": token,
        "ESS_APPROVER_ID": "integration-verification",
    }
    session_id = uuid4().hex
    folder = ROOT / "runs" / ("agent-verification-" + session_id)
    folder.mkdir(parents=True)
    report = {
        "kind": "actual_six_process_integration",
        "physical_control": False,
        "reviewer": "integration-verification",
    }
    with (folder / "process.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "scripts/run_platform.py", "--agent", "--response-mode", "assisted"],
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for_service("http://127.0.0.1:8103", process)
            wait_for_service("http://127.0.0.1:8101", process)
            start = datetime.now(UTC)
            asset = "AGENT-DEMO-" + session_id[:10]
            payloads = []
            for step, temperature in enumerate([28.0, 49.0, 50.0, 51.0]):
                payload = {
                    "event_id": session_id + ":" + str(step),
                    "asset_id": asset,
                    "observed_at": (start + timedelta(seconds=step)).isoformat(),
                    "temperature_c": temperature,
                    "voltage_v": 3.6,
                    "current_a": 2.0,
                    "source": "synthetic_replay",
                }
                payloads.append(payload)
                assert (
                    request_json("http://127.0.0.1:8101/measurements", payload)["status"]
                    == "inserted"
                )
            deadline = time.monotonic() + 150
            proposal = None
            while time.monotonic() < deadline:
                state = get_json("/api/state")
                incident = next((i for i in state["incidents"] if i["asset_id"] == asset), None)
                if incident:
                    proposal = next(
                        (p for p in state["agent_proposals"] if p["incident_id"] == incident["id"]),
                        None,
                    )
                    if proposal:
                        break
                if process.poll() is not None:
                    raise RuntimeError("platform process stopped")
                time.sleep(0.5)
            if not proposal:
                raise RuntimeError("no response proposal within verification deadline")
            assert proposal["status"] == "awaiting_approval", proposal["status"]
            assert not any(a["incident_id"] == incident["id"] for a in state["actions"])
            body = {
                "request_id": "verify:" + session_id,
                "proposal_hash": proposal["proposal_hash"],
                "decision": "approve",
            }
            request = Request(
                f"http://127.0.0.1:8103/api/action-proposals/{proposal['id']}/approval",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            )
            with urlopen(request, timeout=5) as response:
                approved = json.load(response)
            with urlopen(request, timeout=5) as response:
                duplicate = json.load(response)
            assert approved["status"] == "enqueued" and duplicate["duplicate"]
            deadline = time.monotonic() + 10
            action = None
            while time.monotonic() < deadline:
                state = get_json("/api/state")
                action = next(
                    (a for a in state["actions"] if a["incident_id"] == incident["id"]), None
                )
                if action and action["status"] == "applied":
                    break
                time.sleep(0.2)
            assert action and action["status"] == "applied" and action["attempts"] == 1
            for payload in payloads:
                assert (
                    request_json("http://127.0.0.1:8101/measurements", payload)["status"]
                    == "duplicate"
                )
            detail = get_json("/api/agent-runs/" + str(proposal["run_id"]))
            report.update(
                status="passed",
                asset_id=asset,
                incident_id=incident["id"],
                proposal_id=proposal["id"],
                run_id=proposal["run_id"],
                provider=detail["run"]["provider"],
                model=detail["run"]["model"],
                tool_calls=[s["tool"] for s in detail["steps"]],
                usage=detail["run"]["usage"],
                no_action_before_approval=True,
                duplicate_approval=True,
                duplicate_ingestion=4,
                action={
                    "status": action["status"],
                    "attempts": action["attempts"],
                    "simulated": action["simulated"],
                },
            )
            assert detail["run"]["provider"] == "openai"
        except Exception as exc:
            report.update(status="failed", error_type=type(exc).__name__)
            print(f"Integration failed: {type(exc).__name__}. Inspect {folder}", file=sys.stderr)
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            (ROOT / "docs/agent-process-verification.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
