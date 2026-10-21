# 서버별 모듈화와 한국어 주석 정리

## 무엇을 바꿨는가

2026-10-17. 기존 동작과 CLI 명령 `ess-platform`을 유지하면서 파일을 수집·추론·운영 역할로 나눴다. Python 파일의 맨 위에는 파일의 책임을, 각 함수에는 입력·반환값·주요 예외 또는 저장 효과를 한국어 docstring으로 적었다. 복잡한 잠금·transaction·패턴 복귀·순차 실행에는 이유를 설명하는 주석을 추가했다.

현재 Python 43개 파일과 함수/메서드 83개의 docstring을 AST로 확인했다. 모든 함수에 `입력:`과 `반환:` 설명이 있다. 검사 대상은 `src/`, `scripts/`, `tests/`, `dashboard/`의 Python 소스이며 가상환경과 Git 내부는 제외한다.

## 현재 구조

```text
src/ess_platform/
├── cli.py                    # CLI 옵션 해석, 서버/워커 실행과 종료
├── servers.py                # 서버 이름 → Handler 선택, loopback bind
├── http_server.py            # 공통 JSON 응답·health·오류 처리
├── http_client.py            # 서버 간 POST·timeout·응답 크기 제한
├── locking.py                # 단계별 DB advisory lock
├── db.py                     # PostgreSQL 테이블·연결·응답 변환
├── schemas.py                # 측정값과 추론 입력 계약
├── collection/
│   ├── server.py             # 수집 HTTP 경로
│   ├── service.py            # 검증·원본 저장·중복/충돌 처리
│   └── replay.py             # 합성 데이터 생성·JSONL 재생
├── inference/
│   ├── server.py             # ML mock HTTP 경로
│   ├── mock.py               # 데모 특징·점수·이상 판단
│   ├── worker.py             # 대기 측정값 추론·지연 격리·재시도
│   └── registry.py           # 현재 승인 모델 조회
├── operations/
│   ├── server.py             # 운영 조회 API·관제 화면
│   ├── queries.py            # 집계·사건별 판단 근거 조회
│   ├── incidents.py          # 연속 판정·사건·문서 근거·패턴 복귀
│   ├── actions.py            # 정책·문서 hash 검사·모의 냉각
│   └── notifications.py      # 중복 방지 outbox·콘솔 전달
├── training.py               # 별도 mock 후보 등록·검증·승인 작업
├── manuals/                  # 학습용 대응 문서
└── static/                   # 로컬 관제 화면
scripts/run_platform.py       # 5개 프로세스 통합 실행·정리
```

`api.py`의 여러 서버 조건문은 각 역할의 `server.py`로 옮겼다. 공통 `ServiceHandler`는 JSON 입출력·health·오류 응답을 맡고, 각 서버 Handler의 `route()`는 자기 URL만 처리한다. 다른 서버의 URL로 쓰기 요청을 보내면 404가 되는지도 테스트한다.

`workers.py`는 `inference/worker.py`, `operations/incidents.py`, `operations/actions.py`, `operations/notifications.py`로 분리했다. 작업 순서와 transaction 경계는 바꾸지 않았다. 사건과 조치는 같은 잠금 키를 공유하고, 추론은 별도 키를 사용한다.

`cli.py`도 `build_parser()`·`run_worker()`·`main()`으로 나눠 명령 정의, 반복 실행, 진입점 책임을 구분했다. 기존 `ess_platform.api/workers/collector/mock_ml/replay` import 경로는 제거했으므로 외부 코드가 직접 import했다면 새 경로를 사용해야 한다. 의미 없는 호환 wrapper는 남기지 않았다.

## 읽어 볼 순서

1. `collection/server.py` → `collection/service.py`: HTTP 요청 하나가 검증·중복 판정을 거쳐 DB에 저장되는 과정.
2. `inference/worker.py` → `inference/server.py` → `inference/mock.py`: 대기 측정값을 다른 서버에 보내고 결과를 저장하는 과정.
3. `operations/incidents.py` → `actions.py` → `notifications.py`: 추론 결과가 사건·조치·알림으로 바뀌는 과정.
4. `operations/server.py` → `queries.py`: 관제 화면이 저장된 상태와 판단 근거를 조회하는 과정.
5. `cli.py` → `scripts/run_platform.py`: 이 기능들을 독립 프로세스로 실행하는 과정.

## 테스트 정리 기준

테스트 개수를 줄이는 것보다 현재 동작의 회귀를 잡을 수 있는지를 기준으로 판단했다.

- **삭제/대체:** 현재 없는 `battery_etl`에 의존하는 ETL 테스트와, 현재 서버 구조 대신 과거 메모리 기반 MVP를 검증하던 테스트. 과거 테스트 경로는 `removed-legacy-tests.txt`에 남겼다. 그 목록의 `tests/conftest.py`는 삭제 후 현재 DB fixture로 다시 작성한 경로다.
- **보존·이동:** PostgreSQL 멱등성·재시도·재시작·시간 순서·문서/정책·동시 실행·HTTP 전체 흐름 테스트. 기존 큰 `platform_tests/test_platform.py`를 기능별로 분리했다.
- **통합:** 과거 정적 대시보드의 export 테스트와 프런트엔드 테스트. 실제 유지 중인 화면의 데이터 노출·재생 회귀를 잡으므로 삭제하지 않았다.
- **추가:** 리팩토링 후 각 서버가 다른 서버의 쓰기 경로를 처리하지 않는지 세 경우를 검증했다.

```text
tests/
├── conftest.py               # 테스트별 PostgreSQL schema와 승인 모델 fixture
├── helpers.py                # 테스트 전용 HTTP/측정값/임시 서버 도우미
├── test_schemas.py
├── test_collection.py
├── test_inference.py
├── test_registry.py
├── test_operations.py
├── test_http_pipeline.py
├── test_cli.py
├── test_dashboard_export.py
└── frontend/
    ├── test_live_dashboard.cjs
    └── test_replay_dashboard.cjs
```

`tests/` 한 곳에서 기본 수집이 가능해졌다. 과거 `platform_tests/`, `tests/mvp/`, `dashboard/tests/`의 남은 캐시와 빈 디렉터리도 정리했다. 원천 MAT·DB 데이터·실행 로그는 삭제하지 않았다.

## 실행과 검증

```sh
uv sync
export DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_platform_demo_20261017'
uv run python scripts/run_platform.py

# 별도 터미널: 반드시 테스트 전용 DB
export PLATFORM_TEST_DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_platform_test_20261017'
uv run pytest -q --junitxml=docs/platform-refactor-test-results.xml
node --test tests/frontend/*.cjs
uv run ruff check src scripts tests dashboard/export_data.py
```

실행 결과: **Pytest 34개 통과, skip 0 / JavaScript 9개 통과 / Ruff 통과**. 두 대시보드의 JavaScript 테스트는 DOM 스텁 기반이며 실제 브라우저 레이아웃 검증이 아니다.

독립 프로세스 실행과 기존 24건 중복 재전송도 확인한다. 최초 구현의 24개 측정값·24개 추론·2개 사건·2개 조치·5개 알림이 리팩토링 후에도 유지되는지 `platform-refactor-verification.json`에 기록한다. 기존 배포용 정적 Sites 화면은 이 작업에서 새로 배포하지 않는다.

## 남겨 둔 과거 코드와 trade-off

`src/ess_mvp`는 과거 구현을 학습할 수 있게 주석과 함께 보존했다. 현재 없는 `battery_etl`에 의존하므로 실행 대상으로 쓰지 않는다. 실행 불가능한 `battery-etl`·`ess-mvp` CLI 등록을 제거하고 배포 wheel에는 현재 `ess_platform`만 포함한다. 원래 distribution 이름 `battery-etl`은 호환을 위해 유지한다.

서버별 파일 수는 늘었지만 URL 처리·업무 로직·워커·관제 조회를 독립적으로 읽고 바꿀 수 있다. 별도 repository 계층·DI 프레임워크·추상 인터페이스는 도입하지 않았다. SQLAlchemy 쿼리는 해당 업무 함수 안에 두어 학습자가 transaction 흐름을 직접 따라갈 수 있게 했다. DB schema와 mock 판단·대응 정책은 변경하지 않았다.
