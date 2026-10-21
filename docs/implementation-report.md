# 2~5단계 구현 및 검증 기록

날짜: 2026-10-17 (Asia/Seoul). 사용자 요청으로 단계별 승인 대기를 확장해 5단계까지 구현했다. 기존 조사 문서와 결정 기록은 유지했으며 Notebook과 과거 분석 코드는 사용하지 않았다.

## 2단계 — Extract

새 파일: extract.py, records.py, errors.py, 테스트 fixture와 test_extract.py. h5py로 실제 MATLAB reference/char/empty 표현을 처리한다. inspect_mat은 구조 dict, iter_cells는 셀 메타데이터 generator, iter_cycles는 summary/파형 generator를 반환한다. 원천 필수 구조를 검사하며 파일은 읽기 전용으로 연다.

단일 공통 reader를 선택한 이유는 실제 4개 파일의 필드 구성이 같기 때문이다. 고수준 범용 MATLAB decoder는 빠른 시작에 유리하지만 이번에는 참조·자료형·빈 배열을 직접 이해하고 테스트할 수 있는 작은 함수를 택했다. 아직 직렬화 string 객체 전체는 해독하지 않는다.

최초 extract 테스트 5개가 통과했다. 이후 잘못된 UTF-16, 추가 필드, 잘못된 참조 shape도 보강했다. 함수가 돌려준 NumPy 배열은 generator를 닫은 뒤에도 사용할 수 있음을 확인했다.

실제 대형 파일에서 HDF5 참조 대상 `.name`을 조회하자 역탐색 비용으로 실행이 지연됐다. 스택에서 h5i.get_name을 확인하고 조사 실행을 중단한 뒤 논리 원천 경로로 변경했다. 수정 후 첫 셀 메타데이터는 약 0.0063초, 빈 첫 사이클 추출은 약 0.00145초였다. 이는 단일 로컬 측정이며 일반적인 성능 보장은 아니다.

직접 검토할 Python: `f[reference]`, dtype/shape/attrs의 역할, UTF-16 surrogate pair, MATLAB_empty와 물리적 측정 배열의 차이, with와 generator.close.

## 3단계 — Validate / Transform

새 파일: validate.py, transform.py, test_validate_transform.py. scalar schema는 Pydantic strict 모델, 벡터 관계는 NumPy로 검사한다. 모든 sample을 Pydantic 객체로 만들면 비용이 커지므로 scalar와 배열 검증을 분리했다. 검증 함수는 Issue 목록을 반환하고 원천 레코드는 변경하지 않는다.

transform_cell은 nonfinite 라벨을 null로 표현하되 이슈로 원천 상태를 남긴다. transform_cycle은 원천 cycle 값의 정수 여부를 검증한 뒤 정수 라벨을 만들고 파형 list를 복사한다. 실제 cycle 번호와 원천 위치를 분리하며 예측 특징/물리적 단위 변환은 추가하지 않았다.

당시 extract와 합쳐 18개 테스트가 통과했다. NaN/Inf, 잘못된 scalar/벡터 타입, 결측 파형, 배열 길이, 온도 순서, 시각 역행, 빈 배열 보존과 원천 불변을 검사했다.

부동소수점 수준 시간 감소도 현재는 격리한다는 트레이드오프가 있다. 실제 전체 실행의 142개 격리 사이클에 대해 최소 감소 폭은 약 -57539.65부터 -6.04e-14까지 분포했다. [상세 목록](time-reversal-analysis.json)을 남겼다. 원인/단위는 단정하지 않으며, tolerance 도입은 새로운 처리 버전으로 검토한다.

직접 검토할 Python: np.isfinite, np.diff, set으로 배열 길이 비교, Pydantic strict 검증, 원본 dict/배열과 반환값의 복사 경계.

## 4단계 — PostgreSQL Load

새 파일: load.py, test_load.py, database-design.md, schema.sql. SQLAlchemy Core와 psycopg를 사용한다. ORM 세션/클래스보다 테이블과 INSERT/transaction 경계가 직접 드러나는 구성을 택했다. 상세 스키마와 JSONB/ARRAY 선택 이유는 [DB 설계](database-design.md)에 있다.

advisory lock과 UNIQUE를 함께 사용한다. 이름 변경과 동시 실행에서도 같은 내용/처리 버전의 결과는 한 번만 저장된다. 오류 시 파일 전체를 rollback하며, DB 접속 실패와 SQL/제약 실패를 서로 다른 예외로 보고한다. 성공 완료 키는 데이터와 같은 transaction에 기록한다.

실제 PostgreSQL 17의 격리 schema에서 재실행·파일명 변경·동시 실행·배치 충돌·수치 배열/빈 배열/null 왕복을 확인했다. 적재 후 `SELECT 1/0`을 유발해 source/processed/cell/cycle 행이 모두 rollback되는 것을 확인하고 재실행 복구도 테스트했다. 연결 불가 주소와 초기화 오류도 실행했다.

DB URL/비밀번호나 대형 SQL 파라미터를 오류 문구에 노출하지 않고 예외 분류 및 SQLSTATE를 남긴다. DB 오류를 세분화한 뒤 테스트의 이전 기대값(DatabaseError)을 DatabaseConnectionError로 맞추는 과정에서 1개 실패가 있었고 수정 후 전체 재검증했다.

직접 검토할 Python/SQL: engine.begin, FK, 복합 UNIQUE, advisory lock 수명, commit과 예외 전파, SELECT만으로는 동시성 중복 방지가 안 되는 이유.

## 5단계 — Pipeline / CLI

새 파일: pipeline.py, cli.py, test_pipeline.py, README.md, pyproject.toml, uv.lock, .gitignore. argparse로 inspect/init-db/run을 분리했다. --load가 있을 때만 DB를 사용한다. generator를 closing으로 닫고 DB INSERT는 최대 16사이클로 묶는다.

실행 UUID별 report.json/execution.log/issues.jsonl/cells.jsonl/cycles.jsonl/counts.csv를 남긴다. 파일 처리 전 SHA-256을 계산하고 전후 크기/수정 시각을 비교한다. 부분 검증과 전체 완료를 혼동하지 않도록 preview의 DB 적재를 막았다. report JSON은 임시 파일 후 rename으로 교체한다.

DB 없이도 완전한 Extract/Validate/Transform을 실행할 수 있지만 cycles.jsonl은 scalar와 파형 길이/출처의 목록이다. 원시 파형 전체의 독립 정제 파일은 아니며 원본 MAT 또는 PostgreSQL ARRAY를 사용한다. 이 선택은 로컬 산출물의 중복 용량을 줄이지만 DB 없는 파형 조회에는 MAT reader가 필요하다.

실행 로그와 DB는 원자적이지 않다. commit 이후 보고서 작성 실패 시 DB 완료 키가 우선이며 재실행으로 확인한다. 프로그램 중단은 실패 보고서 후 재전파하며, 프로세스 강제 종료 시에는 running 보고서가 남을 수 있다. 종료 상태가 running/failed인 산출물을 정상 결과로 사용하면 안 된다.

직접 검토할 Python: argparse, 환경 변수, SHA-256 chunk 반복, Counter, ExitStack, context manager, bounded list, logging handler 해제, 실패 재전파.

## 실제 원천 전체 실행

Batch 1·2·3 전체 파일을 읽고 Validate/Transform을 실행했다. 초기 단계의 417개 파형 표본 조사와 별개의 실행이다.

| 배치 | 셀 | 읽은 사이클 | 정상 대상 | 격리 | DB 없는 실행 시간(초) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Batch 1 | 46 | 38,811 | 38,688 | 123 | 31.753 |
| Batch 2 | 47 | 26,904 | 26,885 | 19 | 29.919 |
| Batch 3 | 46 | 51,007 | 51,007 | 0 | 60.342 |
| 합계 | 139 | 116,722 | 116,580 | 142 | — |

모든 격리는 TIME_REVERSED 규칙 때문이다. 라벨 결측/관측 길이 차이, 빈 파형, 미해독 식별자, IR=0은 경고로 보존했다. 온도의 임의 정상 범위를 적용하지 않았다. 원본 해시는 최초 조사와 같았다. 처리 시간은 로컬 I/O·DB 병행 실행 영향이 있는 실측치이며 비교 벤치마크가 아니다.

근거: [full-validation-results.json](full-validation-results.json). PostgreSQL 전체 적재 근거는 [postgres-validation-results.json](postgres-validation-results.json)에 별도로 기록한다.

## 최종 테스트

Python 3.11.15와 uv 가상환경, 실제 로컬 PostgreSQL 17에서 실행했다. 버전은 uv.lock으로 고정한다.

```bash
TEST_DATABASE_URL=postgresql://u079@127.0.0.1:55437/postgres BATTERY_DATA_DIR=. uv run pytest -q --junitxml=docs/test-results.xml
uv run ruff check .
uv run ruff format --check .
```

35 passed in 4.39s, PostgreSQL 통합 테스트 및 원본 회귀 테스트 skip 없음. JUnit 근거는 [test-results.xml](test-results.xml)에 남긴다. 기본 pytest만 실행하면 TEST_DATABASE_URL이 없을 때 통합 테스트 4개가 skip된다. 원본 회귀 테스트 3개는 SHA-256, 전체 셀 메타데이터/사이클 수와 첫 파형을 확인한다. BATTERY_DATA_DIR이 없으면 이 3개도 skip된다. 실제 원천 3개 전체 파형 실행은 빠른 테스트 suite와 분리한 검증이다.

격리된 DB는 검증용이며 기존 사용자 DB를 변경하지 않았다. 테스트는 매번 별도 UUID schema를 만들고 그 schema만 정리했다. 실제 원천 적재용 DB 역시 임시 클러스터에 별도로 생성했다. 운영 배포/백업/마이그레이션/장애 복구 서비스까지 검증한 것은 아니다.

## PostgreSQL 전체 적재 최종 대조

주요 3개 파일의 원시/보간 파형 전체를 PostgreSQL에 적재하고 commit을 확인했다.

| 배치 | 적재 셀 | 적재 사이클 | 이슈 행 | 적재 실행 시간(초) |
| --- | ---: | ---: | ---: | ---: |
| batch1 | 46 | 38,688 | 326 | 258.292 |
| batch2 | 47 | 26,885 | 5,017 | 179.515 |
| batch3 | 46 | 51,007 | 92 | 299.377 |

합계: 3개 원천/처리 완료 기록, 139셀, 116,580사이클, 5,435개 품질 이슈. 수명 null 10셀과 첫 사이클의 빈 전류 파형 46개가 보존됐다. DB 크기는 검증 시 약 7.71 GB였다.

배치마다 대표 셀의 비어 있지 않은 사이클 1개를 DB에서 읽어 9개 파형 배열 전체와 summary가 원본과 정확히 같은지 비교했다. 이는 전체 행의 값 왕복 검증을 뜻하지 않는다. 전체 행 수/규칙별 이슈 합계는 전체 실행 보고서와 대조했다. 세 원본을 모두 다시 실행해 skipped를 확인했고, 전후 모든 테이블 행 수가 동일했다.

근거: [DB 대조 결과](database-verification.json), [전체 적재 결과](postgres-validation-results.json), [재실행 결과](replay-validation-results.json), [실행 환경](verification-environment.json).

검증용 PostgreSQL 17.10 서버는 검증 완료 후 중지했다. 데이터 디렉터리는 보존했다. 다시 열기/조회 명령과 임시 경로의 보존 한계는 [로컬 검증 DB](local-validation-db.md)에 기록했다. 기존 사용자 DB/서비스를 변경하지 않았다.
