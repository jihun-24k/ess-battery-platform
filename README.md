# ESS 사건 대응 AI Agent 구현

기존 수집·추론·운영 서버에 실제 LLM Tool Calling, 문서 검색, 영속 Agent 작업, 대응 제안·승인과 관제를 추가했습니다. ML과 냉각은 학습용 가상 기능이며 실제 설비에 연결하지 않습니다.

- [Agent 1~7단계 구현·실행·함수·검증 가이드](docs/agent-guide.md)
- 관제: http://127.0.0.1:8103 (서비스 실행 중)
- 전체 실행: `DATABASE_URL=... uv run python scripts/run_platform.py --agent --response-mode assisted`
- 먼저 `migrate`, `seed-manuals`와 승인 모델을 준비하고 `OPENAI_API_KEY`를 설정합니다.
- 장비 상태 변경은 가상 조치에도 인증된 테스트/운영자 승인을 거치며, 승인 토큰이 없으면 조회만 가능합니다.

---

# 현재 추가 구현: ESS 서버 플랫폼 (ML mock)

사용자가 제안한 수집 서버 → PostgreSQL → ML mock 서버 → 운영 자동화 서버 → 관제 흐름을 `src/ess_platform`에 독립 구현했습니다. 기존 MAT 재생 코드의 누락된 의존성 없이 실행됩니다.

- [서버별 코드·실행 명령·테스트·한계](docs/platform-guide.md)
- [모듈 구조와 리팩토링 내역](docs/platform-refactoring.md)
- CLI: `uv run ess-platform --help`
- 통합 실행: `DATABASE_URL=... uv run python scripts/run_platform.py`
- 로컬 관제: http://127.0.0.1:8103 (서비스 실행 중)
- 테스트: `PLATFORM_TEST_DATABASE_URL=... uv run pytest -q`

ML은 명시적 테스트 규칙, 냉각은 DB 가상 상태, 알림은 콘솔입니다. 아래 내용은 기존 ETL/MVP 문서이며, 현재 `src/battery_etl`이 없어 과거 ETL CLI 등록과 해당 테스트를 정리했습니다. 과거 `ess_mvp` 소스는 학습 기록으로 보존하며 새 서버 실행에는 사용하지 않습니다. 새 플랫폼의 실행 경로는 위 가이드를 사용하세요.

---

# Battery ETL & ESS 학습 MVP

Stanford Battery Aging MAT v7.3 파일을 읽어 **Extract → Validate → Transform → Load**로 처리하는 독립 Python CLI입니다. 기존 Notebook/분석 코드를 사용하지 않았습니다. 원본은 읽기 전용으로 열고 변경·삭제하지 않습니다.

Python 3.11 이상과 uv를 사용합니다. PostgreSQL 적재는 선택이며, `--load` 없이도 전체 추출·검증·변환과 로컬 보고서를 실행할 수 있습니다. 별도 `ess-mvp` 명령은 학습용 이상 탐지 모델과 모의 대응 루프를 제공합니다. 실제 장비 제어·메신저 발송·에이전트·분산 배포는 포함하지 않습니다.

## 학습용 대응 MVP 빠른 실행

```bash
uv sync --python 3.11
uv run ess-mvp run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat --demo-temperature-offset 100
```

한 셀의 앞 3개 사이클 위치로 reference 모델을 학습하고, 뒤 2개를 순서대로 평가합니다. `측정 → 특징 → 이상 판단 → 학습 문서 → 모의 냉각 → 콘솔 알림`을 반복합니다. offset은 평가 복사본에만 넣는 합성 값이며 원본은 바뀌지 않습니다. 결과는 `runs/mvp/<UUID>/`의 report.json, events.jsonl, model.joblib에 저장됩니다.

파일 5개의 역할, 옵션과 학습 순서는 [MVP 학습 가이드](docs/mvp-guide.md), 실제 실행 결과는 [MVP 검증 기록](docs/mvp-verification.md)에 있습니다. 기존 ETL과 별도 명령이며, MVP에서는 DB 사건 저장이나 실제 고장 확률을 구현하지 않았습니다.

## 설치와 실행

```bash
uv sync --python 3.11
uv run battery-etl --help
uv run battery-etl inspect data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat

# 셀마다 앞의 3개 사이클만 확인하는 preview. DB 적재와 함께 사용할 수 없습니다.
uv run battery-etl run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat --batch batch1 --cycle-limit 3

# 전체 파일 검증·변환. 경고나 격리가 있으면 종료 코드는 3입니다.
uv run battery-etl run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat --batch batch1
```

파일 위치는 자유롭게 지정합니다. 배치는 `--batch`로 명시하며 파일명에서 추측하지 않습니다. 실제 조사한 주요 파일 대응은 다음과 같습니다.

| batch label | 원본 파일 |
| --- | --- |
| `batch1` | `data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat` |
| `batch2` | `data/raw/2018-02-20_batchdata_updated_struct_errorcorrect.mat` |
| `batch3` | `data/raw/2018-04-12_batchdata_updated_struct_errorcorrect.mat` |

추가 `2018-04-03_varcharge` 파일은 자동 포함하지 않습니다. 같은 형식의 파일을 명시적으로 입력하면 처리할 수 있으나, 주요 3개 배치의 검증 결과와 혼합하지 않았습니다.

## PostgreSQL 적재

먼저 별도의 PostgreSQL 서버와 비어 있는 프로젝트용 데이터베이스를 준비합니다. 아래 URL의 USER, PASSWORD, DB 이름을 환경에 맞게 바꾸십시오. 비밀번호가 있는 `.env`는 Git에서 제외하지만, 프로그램이 `.env`를 자동 로드하지는 않습니다.

```bash
export DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/battery_etl'
uv run battery-etl init-db
uv run battery-etl run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat --batch batch1 --load
uv run battery-etl run data/raw/2018-02-20_batchdata_updated_struct_errorcorrect.mat --batch batch2 --load
uv run battery-etl run data/raw/2018-04-12_batchdata_updated_struct_errorcorrect.mat --batch batch3 --load
```

`init-db`는 누락 테이블만 생성합니다. 데이터베이스 생성, 기존 테이블 삭제/마이그레이션은 하지 않습니다. `--load`를 지정하지 않으면 DATABASE_URL이 있어도 DB에 접근하지 않습니다. DB 연결 실패 시 자동으로 로컬 실행 성공으로 전환하지 않습니다.

동일 **SHA-256 + 처리 버전**이 성공했으면 `skipped`로 반환합니다. 이름/경로가 달라도 내용이 같으면 중복 적재하지 않습니다. 같은 내용을 다른 배치로 선언하면 오류입니다. advisory transaction lock과 DB UNIQUE로 동시 실행도 보호합니다. 한 파일의 셀·사이클·품질 이슈·완료 기록은 하나의 트랜잭션으로 적재합니다.

## 모듈 구조와 입력/출력

```text
src/battery_etl/
  extract.py     # MAT 참조/문자/빈 배열 해독
  records.py     # dataclass 원천 레코드, Pydantic 모델
  validate.py    # 의미·품질 검사와 이슈
  transform.py   # 저장용 행 변환
  load.py        # SQLAlchemy Core 테이블/트랜잭션
  pipeline.py    # 실행 순서, 보고서, bounded batch 적재
  cli.py         # argparse 명령과 종료 코드
  errors.py      # 오류 종류
tests/           # 합성 HDF5 단위 테스트 + 실제 PostgreSQL 통합 테스트
 docs/           # 원천 조사, 결정, 스키마, 실측 결과
```

| 주요 함수 | 입력 | 출력 |
| --- | --- | --- |
| `inspect_mat` | MAT `Path` | 배치 날짜, 셀 개수, 필드 shape 사전 |
| `iter_cells` | MAT `Path` | `CellRecord` generator: 정책·라벨·grid·직렬화 객체·사이클 수 |
| `iter_cycles` | MAT `Path`, 0-based 셀 위치 | `CycleRecord` generator: summary·9개 NumPy 파형·원천 경로 |
| `validate_cell` | 원천 셀 | `list[Issue]`; 원천 변경 없음 |
| `validate_cycle` | 원천 사이클, grid 크기 | `list[Issue]`; 원천 변경 없음 |
| `transform_cell/cycle` | 검증 통과 레코드 | Python dict 저장 행; 배열은 list로 복사 |
| `source_transaction` | Engine, 원천, 처리 버전, 실행 ID, 산출물 경로 | Connection·처리 ID·skip 여부를 주는 context manager |
| `insert_cell/insert_cycles` | Connection, 부모 ID, 행 | 셀 PK / bounded bulk INSERT |
| `identify_source` | Path, 명시한 배치 | SHA-256·파일 크기/수정 시각·실제 구조 |
| `run_pipeline` | Path, 배치, 출력 경로, 선택 Engine | 상태·개수·규칙별 이슈·원천 정보가 든 결과 dict |

helper 함수의 타입 힌트와 docstring도 구현에 포함했습니다. generator를 조기 중단하면 `contextlib.closing`으로 닫아 파일을 해제합니다. 반환된 배열은 HDF5 핸들이 아닌 독립 NumPy 값입니다.

## 품질 정책

- MATLAB 참조를 따라 실제 값을 읽습니다. `MATLAB_empty`는 차원 payload를 측정값으로 쓰지 않고 빈 배열로 복원합니다.
- `cycle_life` NaN/Inf는 셀을 보존하고 저장용 값만 null로 표현합니다. 이슈에 원천 상태를 남깁니다. 사이클 수로 라벨을 보정하지 않습니다.
- `barcode/channel_id`는 MATLAB 직렬화 string 객체입니다. 원시 payload와 dtype/속성을 JSONB로 보존하고 미해독 경고를 남깁니다. 실제 식별자는 원천 해시 + 셀 위치입니다.
- 원시 파형 6개는 서로 길이가 같아야 합니다. 보간 파형 3개는 비어 있거나 해당 셀 Vdlin 길이와 같아야 합니다. 원시 파형과 보간 파형의 길이를 같다고 강제하지 않습니다.
- summary의 필수값·타입·NaN/Inf, 잘못된 cycle 번호, 중복 cycle, 파형 타입/NaN/Inf, 길이 불일치, 역행 시각은 해당 사이클을 격리합니다.
- **시간 역행은 `diff(t) < 0`인 모든 경우를 격리하는 엄격한 기준**입니다. 부동소수점 수준의 작은 차이도 포함합니다. tolerance 도입은 처리 버전을 올려 별도 검토해야 합니다.
- 빈 파형은 경고로 남기고 summary와 사이클 위치를 보존합니다. 파형을 보간하거나 사이클 번호를 다시 매기지 않습니다.
- 온도 순서 모순, IR=0은 경고입니다. -270/400 같은 관측값에 임의의 물리적 임계값을 적용하지 않습니다.
- 구조 해독 불가·필수 필드 누락·summary 길이 불일치는 파일 실패로 처리합니다. 셀 grid 오류는 그 셀을 격리하고 하위 사이클은 읽지 않았다고 집계합니다.

격리는 원본 삭제가 아닙니다. 원천 위치와 규칙을 `issues.jsonl`과 DB 이슈에 남기고 해당 레코드의 정상 적재를 제외합니다. 격리된 파형의 별도 복사본은 생성하지 않습니다. 원본 MAT에서 다시 조회할 수 있습니다.

## 저장 결과와 추적

기본 위치는 `runs/<UUID>/`이며 `--output`으로 변경합니다.

| 파일 | 내용 |
| --- | --- |
| `report.json` | 상태, 원천 SHA-256/경로/배치, 버전, 개수, 규칙별 이슈, 소요 시간 또는 오류 종류/메시지 |
| `execution.log` | 셀 처리 진행과 최종 상태/오류 로그 |
| `issues.jsonl` | 규칙·심각도·셀/사이클 위치·관측 내용·조치 |
| `cells.jsonl` | 정상 셀 메타데이터와 Vdlin, null 라벨, 직렬화 객체 |
| `cycles.jsonl` | 정상 summary·파형 길이·원천 경로 목록; **파형 값 전체는 포함하지 않음** |
| `counts.csv` | Pandas로 저장한 정상/격리 집계 |

skip 실행에는 새 report.json과 execution.log만 생성합니다. 실패 단계에 따라 일부 산출물이 없을 수 있습니다.

파형 전체는 원본 MAT와 PostgreSQL 배열에 보존합니다. DB 없는 실행의 JSONL은 파형 전체의 독립 정제 파일이 아닙니다. 실패 실행에 남은 산출물은 부분 결과이므로 `report.status`를 먼저 확인하십시오. 파일과 DB는 원자적이지 않으며, commit 후 보고서 저장이 실패하면 DB 완료 키를 확인하고 재실행하면 skip됩니다. skip 시 로컬 실행 보고서는 새로 생성하지만 원래 완료 기록은 덮어쓰지 않습니다.

DB 관계는 `source_files → processed_sources → cells → cycles`이며 `validation_issues`는 processed source에 연결됩니다. 상세 제약과 예시는 [DB 설계](docs/database-design.md), 실제 DDL은 [schema.sql](docs/schema.sql)에 있습니다. 이번에 전체 적재를 검증한 임시 DB를 다시 여는 방법은 [로컬 검증 DB](docs/local-validation-db.md)에 기록했습니다.

## 오류와 종료 코드

| 종류 | 의미 |
| --- | --- |
| `SourceReadError` | 파일 없음, 손상/I/O, 처리 중 파일 변경 |
| `StructureError` / `SourceTypeError` | 필수 구조 누락, 지원하지 않는 자료형/표현 |
| `ConfigurationError` | 잘못된 옵션/DB URL, 상충하는 배치 선언 |
| `DatabaseConnectionError` | 접속 불가 또는 연결 유실 |
| `DatabaseLoadError` | 스키마/SQL/제약 실패; 해당 파일 rollback |

CLI 종료 코드는 0=정상/skip/preview, 1=원천·DB 오류, 2=입력/설정 오류, 3=경고나 격리가 있는 완료입니다. 현재 미해독 식별자 경고가 있어 원본의 첫 정상 처리도 보통 3입니다. shell의 `set -e`를 사용하면 3에서도 중단된다는 점에 주의하십시오. 예상하지 못한 프로그래밍 오류는 실패 보고서를 남기고 다시 전파합니다. 비밀번호/SQL 파라미터 전체를 로그에 출력하지 않습니다. 자동 재시도는 하지 않습니다.

## 테스트와 실제 파일 검증

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .

# 격리 가능한 테스트용 PostgreSQL을 지정하면 통합 테스트도 실행됩니다.
export TEST_DATABASE_URL='postgresql+psycopg://USER:PASSWORD@localhost:5432/battery_test'
uv run pytest -q

# 원본 SHA-256/셀·사이클 수/첫 파형 회귀 테스트 3개도 실행
BATTERY_DATA_DIR=data/raw uv run pytest -q
```

통합 테스트는 지정 DB에 UUID가 붙은 임시 schema만 만들고 자기 schema만 삭제합니다. schema 생성 권한이 필요합니다. TEST_DATABASE_URL이 없으면 PostgreSQL 통합 테스트 4개는 **skip**되며 DB 적재가 검증된 것으로 보지 않습니다. 접속 실패 테스트는 별도로 실행합니다. BATTERY_DATA_DIR이 없으면 실제 원본 회귀 테스트 3개도 skip됩니다. 전체 파형 검증은 별도 CLI 실행입니다.

2026-10-17 실측 결과와 실행 환경, 단계별 설명은 [구현/검증 기록](docs/implementation-report.md)에 정리합니다. 초기 표본 조사 [source-inspection.md](docs/source-inspection.md)와 전체 실행 [full-validation-results.json](docs/full-validation-results.json)은 조사 범위가 다릅니다.

## 알려진 한계와 학습 순서

지원 범위는 실제 조사한 v7.3의 struct/reference 표현입니다. 모든 MATLAB 버전/객체를 해독하는 범용 reader가 아닙니다. SciPy는 요청한 스택에 포함했지만 이 v7.3 reader에는 사용하지 않습니다. MATLAB string 객체 완전 해독, 단위 확정, 배치 간 물리적 셀 연결, 새로운 예측 특징, 수명 라벨 보정은 구현하지 않았습니다.

처리/규칙을 바꾸면 `PROCESSING_VERSION`을 올려 기존 결과와 구분해야 합니다. 자동 코드 해시/스키마 마이그레이션은 없습니다. 파일 단위 transaction은 이해와 rollback에 유리하지만 큰 파일에서는 장시간 DB 자원을 점유합니다. 최초 해시 계산과 처리 전후 크기·수정 시각으로 변경을 감지하지만, 실행 중 원본이 안정적으로 유지된다는 전제가 필요합니다.

학습할 때 `extract.dereference → read_numeric_vector → iter_cycles`, `validate_cycle`, `transform_cycle`, `source_transaction`, `run_pipeline` 순으로 읽으십시오. Python 위치와 실제 cycle 번호, NaN과 저장용 null, generator 자원 수명, FK/UNIQUE와 transaction의 차이를 직접 확인하는 것이 핵심입니다.

## ESS 운영 대시보드

`dashboard/`에 기존 MVP 실행 로그를 조회·재생하는 대시보드를 추가했습니다. Batch별 평가 결과, 온도·전압·전류 추이, ML 이상 점수, 사건별 문서·모의 대응 이력을 제공합니다. 실행 명령과 파일별 역할은 [대시보드 가이드](dashboard/README.md)를 참고하세요. 실제 장비·실시간 DB와 연결하지 않은 학습용 화면입니다.
