"""과거 MAT 단일 실행 진입점. 참조 학습·평가 재생·사건·모의 대응을 한 프로세스로
처리한다.

현재 누락된 battery_etl 소스에 의존하므로 새 서버 실행에는 사용하지 않는다.
"""

import argparse
import json
import sys
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

import joblib
import sklearn
from battery_etl.errors import ConfigurationError, ETLError
from battery_etl.pipeline import identify_source, write_json

from ess_mvp.actions import MockCooling, execute_mock_action, notify
from ess_mvp.manuals import DEFAULT_MANUAL_DIR, get_manual
from ess_mvp.model import FEATURE_NAMES, build_features, predict, train_model
from ess_mvp.replay import (
    ReplayIssue,
    available_cycles,
    inject_temperature_offset,
    iter_measurements,
    iter_windows,
)


def record_event(stream: TextIO, event_type: str, **payload: Any) -> None:
    """입력: 열린 텍스트 stream·이벤트 종류·payload. 반환: 없음. UTC 시각과 JSONL을
    기록하고 flush한다.
    """
    event = {"event": event_type, "logged_at": datetime.now(UTC).isoformat(), **payload}
    stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


def run(
    path: Path,
    output: Path,
    *,
    cell_index: int = 0,
    train_cycles: int = 3,
    eval_cycles: int = 2,
    window_size: int = 32,
    stride: int = 16,
    contamination: float = 0.05,
    trigger_windows: int = 2,
    recovery_windows: int = 3,
    manual_dir: Path = DEFAULT_MANUAL_DIR,
    demo_temperature_offset: float | None = None,
    simulate_action_failure: bool = False,
) -> dict[str, Any]:
    """입력: MAT 경로·결과 디렉터리·학습/평가/사건 옵션. 반환: 실행 보고서 dict.

    모델·이벤트·검증 결과를 저장하며 실패 보고서를 남긴 뒤 원래 예외를 다시
    발생시킨다.
    """
    if min(train_cycles, eval_cycles, trigger_windows, recovery_windows) < 1:
        raise ConfigurationError("cycle counts and trigger/recovery windows must be positive")
    if window_size < 2 or not 1 <= stride <= window_size or not 0 < contamination <= 0.5:
        raise ConfigurationError("invalid window/stride/contamination")
    if demo_temperature_offset is not None and (
        not isfinite(demo_temperature_offset) or demo_temperature_offset <= 0
    ):
        raise ConfigurationError("demo temperature offset must be finite and positive")
    run_id = str(uuid4())
    directory = output.resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    config = {
        "cell_index": cell_index,
        "train_cycles": train_cycles,
        "eval_cycles": eval_cycles,
        "window_size": window_size,
        "stride": stride,
        "contamination": contamination,
        "trigger_windows": trigger_windows,
        "recovery_windows": recovery_windows,
        "manual_dir": str(manual_dir.resolve()),
        "demo_temperature_offset": demo_temperature_offset,
        "simulate_action_failure": simulate_action_failure,
    }
    report: dict[str, Any] = {
        "run_id": run_id,
        "status": "running",
        "artifact_path": str(directory),
        "mode": "simulation_only",
        "config": config,
        "model_kind": "IsolationForest",
        "model_version": "learning-1",
        "sklearn_version": sklearn.__version__,
        "feature_version": "learning-1",
        "feature_names": list(FEATURE_NAMES),
        "reference_labels": "unlabelled_reference",
        "score_meaning": "negative decision_function; higher is more anomalous; not probability",
    }
    counts: Counter[str] = Counter()
    write_json(directory / "report.json", report)
    try:
        source = identify_source(path, f"mvp-cell-{cell_index}")
        if train_cycles + eval_cycles > available_cycles(path, cell_index):
            raise ConfigurationError("selected training + evaluation cycles exceed source length")
        report["source"] = source
        report["training_cycle_positions"] = [0, train_cycles - 1]
        report["evaluation_cycle_positions"] = [train_cycles, train_cycles + eval_cycles - 1]
        device = MockCooling(fail_requests=simulate_action_failure)
        active_incident: str | None = None
        anomaly_streak = normal_streak = 0
        previous_cycle: int | None = None
        with (directory / "events.jsonl").open("w", encoding="utf-8") as log:
            record_event(log, "run_started", run_id=run_id, source=source, config=config)
            reference_rows = []
            with closing(iter_measurements(path, cell_index, 0, train_cycles)) as raw:
                for item in iter_windows(raw, window_size, stride):
                    if isinstance(item, ReplayIssue):
                        counts["training_quality_issues"] += 1
                        record_event(log, "quality_issue", phase="training", **asdict(item))
                        continue
                    features = build_features(item)
                    reference_rows.append(features)
                    record_event(
                        log,
                        "reference_window",
                        cycle_index=item.measurements[0].cycle_index,
                        start_sample=item.measurements[0].sample_index,
                        end_sample=item.measurements[-1].sample_index,
                        features=features,
                    )
            model = train_model(reference_rows, contamination)
            counts["training_windows"] = len(reference_rows)
            joblib.dump(
                {
                    "model": model,
                    "metadata": {
                        **report,
                        "status": "trained",
                        "training_windows": len(reference_rows),
                    },
                },
                directory / "model.joblib",
            )
            record_event(log, "model_trained", reference_windows=len(reference_rows))

            with closing(iter_measurements(path, cell_index, train_cycles, eval_cycles)) as raw:
                measurements = (
                    inject_temperature_offset(raw, demo_temperature_offset, window_size * 2)
                    if demo_temperature_offset is not None
                    else raw
                )
                for item in iter_windows(measurements, window_size, stride):
                    if isinstance(item, ReplayIssue):
                        counts["evaluation_quality_issues"] += 1
                        anomaly_streak = normal_streak = 0
                        record_event(log, "quality_issue", phase="evaluation", **asdict(item))
                        continue
                    first, last = item.measurements[0], item.measurements[-1]
                    if first.cycle_index != previous_cycle:
                        anomaly_streak = normal_streak = 0
                        previous_cycle = first.cycle_index
                    features = build_features(item)
                    prediction = predict(model, features)
                    counts["evaluation_windows"] += 1
                    counts["anomaly_windows"] += int(prediction.is_anomaly)
                    record_event(
                        log,
                        "prediction",
                        cycle_index=first.cycle_index,
                        start_sample=first.sample_index,
                        end_sample=last.sample_index,
                        source_time_start=first.source_time,
                        source_time_end=last.source_time,
                        contains_synthetic_samples=any(m.simulated for m in item.measurements),
                        features=features,
                        **asdict(prediction),
                    )
                    if not prediction.is_anomaly:
                        anomaly_streak = 0
                        normal_streak += 1
                        if active_incident and normal_streak >= recovery_windows:
                            record_event(
                                log,
                                "incident_pattern_recovered",
                                incident_id=active_incident,
                                meaning="baseline pattern returned; not proven fault resolution",
                            )
                            counts["recovered_patterns"] += 1
                            active_incident = None
                        continue
                    normal_streak = 0
                    anomaly_streak += 1
                    if active_incident is not None or anomaly_streak < trigger_windows:
                        continue
                    active_incident = f"{run_id}:{counts['incidents'] + 1}"
                    counts["incidents"] += 1
                    record_event(
                        log,
                        "incident_opened",
                        incident_id=active_incident,
                        kind=prediction.kind,
                        anomaly_score=prediction.anomaly_score,
                    )
                    record_event(
                        log,
                        "notification",
                        **notify(
                            active_incident,
                            "측정 이상 징후 감지. 고장 확정 또는 장애 확률을 뜻하지 않습니다.",
                        ),
                    )
                    counts["notifications"] += 1
                    manual = get_manual(prediction.kind, manual_dir)
                    if manual is None:
                        counts["blocked_incidents"] += 1
                        record_event(
                            log,
                            "action_blocked",
                            incident_id=active_incident,
                            reason="manual missing/unreadable/empty",
                        )
                        record_event(
                            log,
                            "notification",
                            **notify(active_incident, "대응 문서가 없어 모의 조치를 보류했습니다."),
                        )
                    else:
                        record_event(
                            log,
                            "manual_found",
                            incident_id=active_incident,
                            path=manual.path,
                            sha256=manual.sha256,
                            learning_example=manual.learning_example,
                        )
                        action = execute_mock_action(device, manual.action, active_incident)
                        counts["action_requests"] += 1
                        counts["action_failures"] += int(action.status in ("failed", "rejected"))
                        record_event(
                            log, "action_result", incident_id=active_incident, **asdict(action)
                        )
                        record_event(
                            log,
                            "notification",
                            **notify(
                                active_incident,
                                f"모의 냉각 결과={action.status}, 가상 장비 상태={device.mode}",
                            ),
                        )
                    counts["notifications"] += 1
            if counts["evaluation_windows"] == 0:
                raise ConfigurationError(
                    "no valid evaluation windows; increase cycles or reduce window"
                )
            after = path.stat()
            if (after.st_size, after.st_mtime_ns) != (source["size_bytes"], source["mtime_ns"]):
                raise ConfigurationError("source changed during replay")
            report.update(
                status="completed",
                counts=dict(counts),
                active_incident=active_incident,
                mock_device_mode=device.mode,
                cooling_effect_verified=False,
            )
            record_event(
                log,
                "evaluation_ended",
                counts=dict(counts),
                active_incident=active_incident,
                mock_device_mode=device.mode,
            )
        write_json(directory / "report.json", report)
        return report
    except BaseException as exc:
        report.update(
            status="failed",
            error_type=type(exc).__name__,
            error_message=str(exc),
            counts=dict(counts),
        )
        write_json(directory / "report.json", report)
        raise


def main(argv: list[str] | None = None) -> int:
    """입력: 과거 MVP CLI 인자. 반환: 성공 0·입력 오류 2·실행 오류 1·모의 대응 실패 3."""
    parser = argparse.ArgumentParser(prog="ess-mvp")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("run", help="fit reference cycles and replay later cycles")
    command.add_argument("path", type=Path)
    command.add_argument("--output", type=Path, default=Path("runs/mvp"))
    command.add_argument("--cell", type=int, default=0)
    command.add_argument("--train-cycles", type=int, default=3)
    command.add_argument("--eval-cycles", type=int, default=2)
    command.add_argument("--window", type=int, default=32)
    command.add_argument("--stride", type=int, default=16)
    command.add_argument("--contamination", type=float, default=0.05)
    command.add_argument("--trigger-windows", type=int, default=2)
    command.add_argument("--recovery-windows", type=int, default=3)
    command.add_argument("--manual-dir", type=Path, default=DEFAULT_MANUAL_DIR)
    command.add_argument(
        "--demo-temperature-offset",
        type=float,
        help="evaluation-only synthetic offset in raw source units",
    )
    command.add_argument("--simulate-action-failure", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(
            args.path,
            args.output,
            cell_index=args.cell,
            train_cycles=args.train_cycles,
            eval_cycles=args.eval_cycles,
            window_size=args.window,
            stride=args.stride,
            contamination=args.contamination,
            trigger_windows=args.trigger_windows,
            recovery_windows=args.recovery_windows,
            manual_dir=args.manual_dir,
            demo_temperature_offset=args.demo_temperature_offset,
            simulate_action_failure=args.simulate_action_failure,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return (
            3
            if (
                report["counts"].get("blocked_incidents", 0)
                or report["counts"].get("action_failures", 0)
            )
            else 0
        )
    except ConfigurationError as exc:
        print(f"ConfigurationError: {exc}", file=sys.stderr)
        return 2
    except (ETLError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
