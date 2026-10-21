# PostgreSQL 기반 ESS 서비스 플랫폼 — ML mock

## 구현 범위

제안한 서버 구성에 맞춰 `src/ess_platform` 패키지를 새로 작성했다. 기존 `ess_mvp`와 누락된 `battery_etl` 소스에 의존하지 않는다. 실제 PostgreSQL과 HTTP 통신을 사용하며, ML 판단·장비 제어는 mock이다. 실제 사내 문서 대신 버전이 명시된 학습 문서를 조회하고 알림은 콘솔로 전달한다. FastAPI·Kafka·Airflow·LLM·Kubernetes는 추가하지 않았다.

현재 실행 단위는 HTTP 서버 3개와 워커 2개, 별도 실행하는 학습 설정 작업이다.

| 설계 구성 | 코드 / 실행 단위 | 책임 |
|---|---|---|
| BMS·센서 / 재생기 | `collection/replay.py` | 명시된 JSONL 측정 계약으로 수집 API 호출 |
| ① 데이터 수집 서버 | `collection/server.py` + `collection/service.py`, `serve collector` | 입력 검증, 원본 payload 보존, 중복 판정 |
| ② PostgreSQL | `db.py` | 측정값·처리 상태·추론·사건·대응·알림 저장 |
| ③ ML 서버 | `inference/server.py` + `inference/mock.py`, `serve ml` | 승인된 mock 설정을 읽고 특징·점수 반환 |
| 추론 실행 워커 | `inference/worker.py`, `worker inference` | DB 대기 데이터 → ML HTTP 호출 → DB 추론 결과 |
| ④ 운영 자동화 서버 | `operations/server.py` + `operations/{incidents,actions,notifications}.py` | 관제 API와 사건·문서·정책·모의 대응·알림 처리 |
| 관제 화면 | `static/` | 운영 API를 2초마다 조회, 셀 필터·사건 상세 표시 |
| ⑤ 별도 학습 작업 | `training.py`, `train-mock` | mock 설정 후보와 데이터 cutoff, 계약 테스트 결과 등록 |
| 모델 저장소 | `rt_model_versions`, `rt_model_registry` | immutable 후보 설정·평가와 승인 버전 포인터 |

흐름: JSONL → 수집 API → PostgreSQL → 추론 워커 → ML mock HTTP → 추론 결과 DB → 대응 워커 → 사건·문서·가상 냉각·콘솔 알림 → DB → 관제 화면.

수집 서버가 ML을 동기 호출하지 않으므로 ML 서버 장애 중에도 유효한 측정값을 저장할 수 있다. 큐 상태는 DB에 남는다.

## 빠른 시작

Python 3.11+, uv, 실행 중인 PostgreSQL과 **전용 DB**가 필요하다. 이 프로그램은 PostgreSQL 서버나 DB 자체를 생성하지 않으며 `init-db`는 해당 DB 안에 `rt_*` 테이블만 생성한다.

```sh
uv sync
# 본인의 PostgreSQL 사용자·port에 맞게 지정한다.
export DATABASE_URL='postgresql://USER@127.0.0.1:5432/ess_platform_demo'
uv run ess-platform init-db
uv run ess-platform train-mock --version mock-v1 --threshold 45
uv run ess-platform approve-model mock-v1
uv run python scripts/run_platform.py --replay examples/stream-demo.jsonl --interval 0.5
```

관제 화면: **http://127.0.0.1:8103**. 위 launcher가 수집·ML·운영 API와 두 워커를 함께 띄운다. Ctrl+C로 종료하면 자식 프로세스만 중지하고 DB는 보존한다. 로그는 `runs/platform/<session-id>/*.log`, 종료 시 마지막 상태는 같은 폴더의 `final-state.json`에 기록한다. launcher는 모델을 자동 승인하지 않는다.

서비스가 이미 실행 중인 경우 port 충돌을 해결한 뒤 시작해야 한다. 모든 서버는 `127.0.0.1`에만 bind한다. 현재는 로컬 학습용이며 인증·TLS·외부 접근 설정이 없다.

이번 구현에서 사용한 전용 데모 DB는 `postgresql://u079@127.0.0.1:55437/ess_platform_demo_20261017`이다. 기존 로컬 검증용 PostgreSQL 클러스터를 재사용하되 기존 ETL DB와 테이블은 수정하지 않았다. 서버가 중지되어 있으면 다음으로 시작할 수 있다.

```sh
/opt/homebrew/opt/postgresql@17/bin/pg_ctl \
  -D /private/tmp/battery-etl-pg-20261017 \
  -l /private/tmp/battery-etl-pg-20261017/server.log \
  -o '-p 55437 -k /private/tmp -h 127.0.0.1' start
export DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_platform_demo_20261017'
uv run python scripts/run_platform.py
```

초기화와 모델 등록·승인은 이미 이 데모 DB에 수행했다. 같은 버전의 재등록은 unique 제약으로 실패하므로 다시 실행할 필요가 없다. 서버를 재시작해도 기존 결과가 관제 화면에 표시된다. 이전 비공개 Sites 대시보드는 과거 MVP 로그용이며, 이 로컬 DB에 연결되지 않는다.

## 각각의 서버를 직접 실행하며 학습하기

각 터미널에서 같은 `DATABASE_URL`을 설정한다.

```sh
uv run ess-platform serve collector     # 8101: 수집
uv run ess-platform serve ml            # 8102: ML mock
uv run ess-platform worker inference    # DB 대기 측정값을 ML mock으로 전송
uv run ess-platform serve operations    # 8103: 관제 API / 화면
uv run ess-platform worker operations   # 사건·모의 대응·콘솔 outbox 처리
```

재생은 별도 터미널에서 실행한다.

```sh
uv run ess-platform replay examples/stream-demo.jsonl --interval 0.5
uv run ess-platform status
```

- `worker ... --once`: 한 처리 tick만 실행한다.
- `worker ... --drain`: 현재 처리 가능한 작업이 없어질 때 종료한다. 재시도 시간이 미래이면 종료하므로 장시간 운영 시 기본 연속 실행을 사용한다.
- `serve ... --port 9000`: HTTP port 변경. ML port 변경 시 추론 워커에도 `--ml-url http://127.0.0.1:9000`을 지정한다. launcher는 기본 port만 사용한다.

## 새 측정 데이터 계약

이 스키마는 **새 합성 스트림의 명시적 계약**이다. MAT의 온도·시간 단위를 추정해 변환한 것이 아니다. 현재 MAT 어댑터는 연결하지 않았다.

```json
{
  "event_id": "demo-1:ESS-A-CELL-01:0",
  "asset_id": "ESS-A-CELL-01",
  "observed_at": "2026-01-01T00:00:00Z",
  "temperature_c": 28.0,
  "voltage_v": 3.6,
  "current_a": 2.0,
  "source": "synthetic_replay",
  "schema_version": "1"
}
```

`POST http://127.0.0.1:8101/measurements`, Content-Type `application/json`으로 한 건씩 보낸다. timezone 없는 시각, 문자열·bool 측정값, NaN/Infinity, 음수 전압, 누락·추가 필드는 거부한다. 전류는 충·방전 방향을 표현하므로 음수를 허용한다. 온도·전류의 현장별 물리 한계나 센서 고착 검사는 아직 구현하지 않았다. body 크기는 최대 256 KiB이다.

HTTP 응답: 신규 201, 같은 payload 재전송 200, 같은 `(source,event_id)`에 다른 값 409, 스키마 오류 422, 요청 형식 오류 400, DB/승인 모델 부재 503. `event_id`는 해당 source 안에서 자산을 포함해 유일하게 생성해야 한다. timestamp는 UTC로 정규화하고 단위와 원래 유효 payload는 보존한다.

`examples/stream-demo.jsonl`은 두 셀 × 12시점 = 24개 합성 측정값이다. A 셀은 정상 → 이상 → 정상 → 이상 패턴, B 셀은 정상 패턴이다. `make-demo NEW_PATH --stream-id ID`로 동일한 테스트 시각의 새 파일을 만들 수 있으며 기존 파일은 덮어쓰지 않는다. 같은 파일은 같은 ID를 유지하므로 재실행 시 중복으로 처리한다. 같은 자산에 새 ID만 붙여 과거 시각을 재전송하면 지연 데이터로 격리된다. 새 기간을 실험하려면 시각을 증가시킨 유효 JSONL을 준비하거나 새 테스트 DB를 사용한다.

## ML mock과 모델 승인

`POST :8102/predict`는 `{"readings": [측정값, ...]}`를 받는다. 한 자산의 엄격히 증가하는 시각, 최대 100건만 허용한다. 추론 워커는 이전 처리 측정값 31개와 현재 측정값을 묶는다.

반환값은 `mock`, `model_version`, `features`, `score`, `is_anomaly`, `meaning`이다. 특징은 평균 온도·최근 온도·최근 전압·최근 전류·샘플 개수다. **판단 규칙은 최근 온도 − 설정 임계값 > 0**이며 기본 임계값 45°C는 시연용 숫자다. 현장 안전 기준·고장 확률·학습 모델이 아니다.

```sh
uv run ess-platform train-mock --version mock-v2 --threshold 46
# 아직 추론 서버는 기존 승인 버전을 사용한다.
uv run ess-platform approve-model mock-v2
```

후보 작업은 DB의 최대 측정 ID를 cutoff로 기록하고, 경계 아래/동일/위 세 계약 테스트를 실행한다. 실데이터로 모델을 fitting하거나 예측 성능을 평가하지 않는다. 테스트가 통과한 후보만 CLI로 명시적으로 승인할 수 있다. ML 서버는 다음 요청부터 승인 포인터의 설정을 사용하고, 각 추론에는 사용 버전을 남긴다. 모델 변경 시 연속 판정 카운터를 초기화하되 이미 열린 사건은 유지한다. 과거 승인 버전을 다시 지정해 rollback할 수 있다.

실제 ML 교체 시에는 `/predict` 계약의 mock 모드 검증, 특징 버전, 성능 검증·승인 작업을 함께 변경해야 한다. 현재 계약은 실제 모델을 연결했다고 오인하지 않도록 `mock=true`를 필수로 검사한다.

## PostgreSQL 테이블

| 테이블 | 주요 데이터 / 중복 방지 |
|---|---|
| `rt_measurements` | source/event_id unique, 원본 payload, hash, event/receive time, pending/retry/predicted/late/failed |
| `rt_predictions` | measurement_id unique, 모델 FK, 특징·점수·판정, pending/handled/late |
| `rt_assets` | 자산 PK, 최근 처리 시각, 연속 판정 카운터, 열린 사건 ID, 가상 냉각 상태 |
| `rt_incidents` | 시작 prediction unique FK, 종료 prediction FK, 상태, 문서 snapshot/hash, 정책 버전 |
| `rt_actions` | incident_id unique FK, allowlist 동작, 처리·재시도 상태, simulated=true |
| `rt_notifications` | incident FK, 전이별 dedup_key unique, 콘솔 전달 상태 |
| `rt_quality_issues` | 유효성 오류·중복 ID 충돌·지연 도착 진단 |
| `rt_model_versions` | 버전 PK, mock 설정, cutoff, 계약 테스트, 승인 시각 |
| `rt_model_registry` | active 포인터와 모델 버전 FK |

추론·사건·조치 변경은 각각 DB transaction으로 묶는다. 단계별 advisory lock으로 여러 워커가 동일 상태를 동시에 처리하는 것을 방지한다. 운영 상태 변경과 가상 조치는 동일한 lock으로 직렬화해 잠금 순서 충돌을 피한다. 알림은 outbox row lock을 사용한다. 모든 단계에서 DB rollback 후 재시작하면 미완료 상태부터 이어간다.

## 예외·시간 순서·재시도

- JSON 검증 실패: 진단 필드·오류 코드만 `rt_quality_issues`에 저장한다. 임의 요청 body 전체를 진단 로그에 복사하지 않는다.
- 이벤트 ID 충돌: 원래 payload는 수정하지 않고 409와 품질 이슈를 남긴다.
- 시간 순서: 대기 큐 안에서는 관측 시각/DB ID 순으로 처리한다. 이미 추론한 자산 watermark 이하의 측정값은 `late`로 격리한다. 허용 지연을 기다리는 버퍼나 과거 결과 재계산은 없다.
- ML HTTP 실패/잘못된 응답/미승인 버전: 최대 3회, 2·4·8초 지수 대기 정보를 기록한다. 세 번째 실패 후 `failed`로 멈추며 다음 작업으로 넘어간다. 마지막 대기 시각은 감사용이고 자동 네 번째 시도는 없다.
- 재시도 중인 가장 오래된 측정값은 다음 재시도 시각까지 추론 큐를 막는다. 소규모 MVP의 순서를 단순하게 보장하기 위한 선택이다. 향후 자산별 파티션으로 분리할 지점이다.
- 수동 재투입: `uv run ess-platform retry-inference MEASUREMENT_ID`. 더 최신 값이 이미 처리됐다면 해당 값은 late로 격리된다.
- 사건: 이상 판단 연속 2회 → 열림, 정상 판단 연속 3회 → 기준 패턴 복귀. 연속성은 **유효하게 처리된 판단** 기준이며 관측 간격이 30초보다 크면 카운터를 초기화한다. 30초 역시 데모 정책이다.
- 문서 없음/비어 있음: 모의 조치를 blocked로 기록하고 알림을 남긴다. 문서 내용은 명령으로 실행하지 않는다.
- 정책: `measurement_anomaly` 사건의 `mock_cooling_boost`만 허용한다. 저장된 문서 내용과 hash가 다르면 차단한다.
- 모의 냉각 실패: `worker operations --simulate-action-failure`로 재현한다. 최대 3회 재시도 후 실패를 기록한다. 이미 기준 패턴이 복귀했으면 대기·재시도 조치는 취소한다.
- 가상 냉각 상태와 조치 결과는 같은 transaction에 저장된다. 이후 사건은 기존 boost를 유지하며 실제 재명령을 가정하지 않는다. normal로 자동 복원하지 않는다.
- 알림: 사건 발생·모의 조치 최종 결과·패턴 복귀를 DB에 기록하고 콘솔 출력한다. 외부 메신저·이메일 전송은 없다. 콘솔 출력 직후 DB commit 전에 종료되면 같은 notice ID가 다시 출력될 수 있는 at-least-once 방식이다.
- DB 장애: HTTP 503 또는 워커 오류 로그를 남긴다. 연속 워커는 poll 주기 후 재시도하고 `--once/--drain`은 비정상 종료한다.

## 주요 함수 입력과 반환값

| 함수 | 입력 → 반환 / 저장 효과 |
|---|---|
| `collect(engine, raw)` | JSON bytes → inserted/duplicate + 측정 ID; 충돌은 `EventConflict` |
| `predict(request, version, config)` | 검증된 윈도·버전·mock 설정 → 특징·점수·판정 dict |
| `inference_tick(engine, ml_url)` | DB/ML 주소 → 이번 tick에서 처리했는지 bool; 실패 상태도 저장 |
| `operations_tick(engine, manual_path)` | DB/학습 문서 → 처리 bool; 연속 카운터·사건·조치 요청 저장 |
| `action_tick(engine, simulate_failure)` | DB/실패 주입 → 처리 bool; DB 가상 장비 상태 갱신 |
| `notification_tick(engine)` | DB → 전달 처리 bool; 콘솔 출력과 전달 시각 저장 |
| `create_candidate(engine, version, threshold)` | mock 설정 → 후보·계약 테스트·cutoff dict |
| `approve(engine, version)` | 후보 버전 → 승인 버전 dict; active 포인터 갱신 |
| `state_snapshot(engine)` | DB → 집계·각 항목 최신 최대 100건 dict |
| `incident_detail(engine, id)` | 사건 ID → 측정값·추론·설정·문서·조치·알림, 없으면 None |
| `replay_file(path, url, interval)` | JSONL/API/간격 → inserted/duplicate 집계; 실패 행에서 중단 |

관제 화면은 `/api/state`와 `/api/incidents/<id>`를 읽는다. 최신 데이터가 표시되더라도 워커 실행 중임을 보장하지 않으며, `/health`는 해당 HTTP 서버와 DB 연결 상태만 확인한다. 화면 상단 수치는 전체 DB 집계, 셀 필터는 최신 목록에만 적용된다.

## 검증 방법과 실제 결과

테스트는 `tests/` 아래에 기능별로 통합했다. 누락된 ETL 소스에 의존하거나 현재 서버 구조와 다른 과거 MVP 테스트는 삭제했다. 기본 `uv run pytest`로 현재 플랫폼과 대시보드 export 테스트를 모두 실행한다. 과거 테스트 기록은 당시 결과이며 이번 검증과 구분한다.

```sh
export PLATFORM_TEST_DATABASE_URL='postgresql://USER@127.0.0.1:5432/ess_platform_test'
uv run pytest -q --junitxml=docs/platform-refactor-test-results.xml
node --test tests/frontend/*.cjs
node --check src/ess_platform/static/app.js
uv run ruff check src scripts tests dashboard/export_data.py
```

DB 테스트는 `ess_platform_test`로 시작하는 전용 DB에서만 실행하며 테스트마다 고유 schema를 생성·정리한다. 환경변수가 없으면 DB 테스트는 skip하므로 전체 성공 근거가 될 수 없다. 이번에는 실제 PostgreSQL 17의 `ess_platform_test_20261017`에 연결해 실행했다.

- Pytest **34개 통과**, skip 없음: 타입·결측·timezone·중복·충돌·동시 수집·서버 경로 분리·대시보드 export·HTTP 전체 흐름·DB 장애·ML 실패·재시도·재시작·모델 승인·문서 누락/변조·가상 냉각 실패·패턴 복귀 후 취소·지연 도착·모델 전환·다중 워커.
- JavaScript **9개 통과**: 실시간 상태 표시·셀 필터·HTML escaping·사건 상세와 과거 로그 재생·검색·초기화. DOM 스텁 테스트이며 브라우저 조작·레이아웃 테스트는 아니다.
- 실제 별도 프로세스 5개 실행: 측정 24 → 추론 24 → 사건 2 → 조치 2 → 콘솔 알림 5. 조치 1회 적용, 1회 boost 유지.
- 동일 24건 재전송: 신규 0, 중복 24. 프로세스 전체 종료·재시작 후에도 집계와 조치 시도 횟수 유지.
- wheel 빌드 및 별도 환경에서 CLI와 패키지 내 HTML·학습 문서 포함을 확인했다.
- 브라우저 미리보기 접근 거부가 이전에 발생해 실제 화면 조작·모바일 배치는 검증하지 않았다.

근거: `docs/platform-refactor-test-results.xml`, `docs/platform-refactor-verification.json`. 이전 최초 구현 근거는 `docs/platform-test-results.xml`, `docs/platform-process-verification.json`에 보존한다.

## 직접 확인할 Python 로직과 다음 확장

1. `collection/service.py`의 `on_conflict_do_nothing()`과 hash 비교로 왜 원본을 덮어쓰지 않는지 확인한다.
2. `inference/worker.py`와 `operations/`에서 DB transaction과 `with_for_update()`, advisory lock의 수명을 따라가 본다.
3. 메모리 변수가 아닌 `rt_assets`의 연속 카운터가 재시작 후 사건 생성 기준을 유지하는 과정을 확인한다.
4. 모델 승인 포인터와 prediction의 버전을 구분하고 변경 시 연속 증거를 초기화하는 이유를 설명해 본다.
5. 사건 복귀와 명령 실행의 순서를 바꿔 `cancelled`와 `applied` 결과 차이를 확인한다.

현재는 소규모 polling·직렬 처리다. 다음 단계는 자산별 큐 분리·지연 허용 정책, 인증·TLS·DB 권한 분리, DB migration, 실제 센서 어댑터, 실데이터 모델 검증, 승인된 현장 Gateway와 외부 알림 어댑터다. 실제 설비 명령은 관제 화면에서 직접 보내지 않고 별도 대응 정책과 Gateway의 확인 응답을 거쳐야 한다. 현 단계에는 이를 구현하지 않았다.
