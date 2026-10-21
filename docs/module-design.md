> 이 문서는 1단계 당시의 설계 기록입니다. 이후 사용자가 5단계까지 구현을 요청하여 구현·테스트를 진행했습니다. 최신 동작은 README.md와 implementation-report.md를 참조하십시오.

# 새 Python ETL 프로그램 설계 제안

1단계 당시 상태: 제안, 승인 대기. 현재는 후속 사용자 요청에 따라 5단계까지 구현을 진행했다. 근거는 [실제 원천 조사](source-inspection.md). 이번 단계에서는 새 Python 모듈과 테스트, CLI, DB 스키마를 구현하지 않는다. 기존 Notebook/분석 코드를 재사용하지 않는다.

## 디렉터리와 책임

아래는 향후 제안 구조다. 현재 존재하는 구현 파일 목록이 아니다.

```text
pyproject.toml                 # Python >=3.11, uv 의존성/Ruff 설정
uv.lock
src/battery_etl/
  cli.py                      # argparse, 경로/명시적 배치 입력, 종료 코드
  extract.py                  # HDF5 참조와 MATLAB 표현 해독, 원천 위치 보존
  records.py                  # 공통 레코드 타입; 추출용 dataclass/TypedDict
  validate.py                 # Pydantic 경계 검증 + NumPy 배열 품질 검사
  transform.py                # 승인한 공통 구조 변환, 셀/사이클 분리
  load.py                     # SQLAlchemy, PostgreSQL 트랜잭션/중복 방지
  pipeline.py                 # 단계 연결, 실행 이력과 실패 전파
  errors.py                   # 원인별 예외
tests/
  test_extract.py
  test_validate.py
  test_transform.py
  test_load.py
  test_pipeline.py
  fixtures/                   # 실제 구조를 모사한 작은 합성 HDF5
docs/
  source-inspection.md
  mat-inspection.json
  module-design.md
  decisions.md
```

파일 규모가 커질 때만 extract 내부 helper를 별도 모듈로 나눈다. 추상 기반 클래스, 플러그인 레지스트리, 단계별 서비스 클래스는 처음부터 만들지 않는다. 원천 MAT 파일은 현재 위치에서 읽을 수 있게 하고 자동 이동하지 않는다. 원본, 향후 실행 산출물과 비밀 정보는 Git에 넣지 않는다.

## 공통 자료구조

아래 필드는 실제 조사로 확인한 값과 추적용 메타데이터다. 단위 변환이나 새로운 예측 특징을 임의로 정의하지 않는다.

| 레코드 | 예정 내용 |
| --- | --- |
| 원천 메타데이터 | 실제 경로, SHA-256, 크기, `batch_date`, 사용자가 지정한 batch label, 구조 signature |
| 셀 메타데이터 | 원천 ID, 0-based 셀 위치, `policy`, `policy_readable`, 원천 `cycle_life`, `Vdlin`, barcode/channel_id 미해독 객체 정보 |
| 사이클 레코드 | 셀 식별자, 0-based 원천 위치, `summary.cycle`, summary의 나머지 7개 값, 9개 파형 배열, 빈 배열 상태/원천 경로 |
| 검증 이슈 | 원천/셀/사이클 위치, 규칙 코드, 심각도, 관측 내용, 적용 조치, 실행/규칙 버전 |

파일마다 같은 셀 위치가 존재하므로 위치 하나만으로 셀을 식별하지 않는다. 직렬화된 문자열 payload는 실제 barcode/channel ID와 구별해 이름 붙인다. 해독 실패를 일반 문자열로 바꾸거나 숫자 토큰을 ID로 쓰지 않는다.

추출용 레코드는 NaN과 빈 배열을 원천 상태로 담을 수 있어야 한다. Pydantic의 유한성 제한을 먼저 걸어 원천 정보를 잃지 않는다. 3단계에서 정제 출력의 nullable 필드와 이슈를 정의한다. 배열은 NumPy로 유지하고, scalar를 엄격하게 검사하며 문자열 숫자를 자동 강제 변환하지 않는다.

## 제안 함수 계약

타입 이름과 함수는 아직 구현하지 않은 설계다.

| 모듈/함수 | 입력 | 반환/동작 |
| --- | --- | --- |
| `extract.inspect_mat` | `Path` | `MatStructure`: 키, shape, dtype, MATLAB class/empty 속성, 참조 관계의 구조 요약 |
| `extract.iter_cells` | `Path` | `Iterator[CellRecord]`: 셀 메타데이터를 하나씩 반환 |
| `extract.iter_cycles` | `Path`, `cell_index: int` | `Iterator[CycleRecord]`: 그 셀의 summary/파형을 사이클별로 반환 |
| `extract._read_numeric_vector` | 열린 HDF5 파일과 reference | `NDArray`: MATLAB_empty 처리, 비어 있지 않으면 벡터 여부 확인 후 1차원화 |
| `extract._read_char_vector` | 열린 HDF5 파일과 reference | `str`: 확인된 char 표현을 UTF-16로 해독; 직렬화된 string 객체는 별도 경로 |
| `validate.validate_cell/cycle` | 추출 레코드, 검증 정책 | 승인된 레코드 상태와 `list[ValidationIssue]` |
| `transform.transform_cell/cycle` | 검증된 레코드, 변환 버전 | 저장용 공통 레코드; 원천값/출처와 정제 조치 연결 |
| `load.load_source` | DB session, 원천/처리 버전, 변환 레코드 iterator | `LoadResult`: 파일 단위 commit 또는 rollback |
| `pipeline.run_pipeline` | 실행 설정 | `PipelineResult`: 개수, 이슈, 완료/실패 상태 |

`iter_*`는 generator가 열린 동안 `with h5py.File(..., 'r')`를 유지하고 사용 종료 시 닫는다. 닫힌 HDF5 dataset/reference를 호출자에게 넘기지 않고 읽어낸 NumPy 값과 원천 경로를 반환한다. 조기 중단 시 generator도 명시적으로 닫는 사용 규약과 테스트가 필요하다.

`iter_cells`는 모든 셀의 파형을 담지 않는다. `iter_cycles`는 사이클 하나씩 읽는다. 셀별 파일 재열기는 간단하지만 약간의 I/O 비용이 있으므로 측정 후 개선한다. 초기부터 파일 전체를 중첩 dict/DataFrame으로 읽는 대안은 메모리 사용이 커질 수 있어 피한다.

추출에서 검사하는 것은 필수 그룹 존재, 참조/자료형의 해독 가능성 같은 구조 조건이다. 온도 범위, 라벨 누락의 사용 가능 여부 같은 의미/품질 판단은 validate에 둔다. 문자 해독/빈 배열 복원은 파일 표현을 읽는 책임이며 의미 보정과 구분한다.

## 배치별 차이 대응

현재 4개 파일은 필드 구성이 같으므로 공통 HDF5 reader 하나를 제안한다. Batch별 전용 파서를 세 개 만들 이유는 아직 없다. 차이가 발견되면 해당 파일의 구조 signature에 대응하는 작은 매핑 함수를 추가한다. 날짜 이름만 보고 특정 필드를 버리거나 수명을 보정하지 않는다.

구조 변화는 누락/추가 필드로 구분한다. 필수 구조 누락은 구체적인 경로와 함께 실패시킨다. 추가 필드는 로그와 조사 보고서에 남기고 처리 가능 여부를 판단한다. 모르는 구조를 억지로 기존 구조로 바꾸지 않는다.

## 검증·정제 정책 제안

- `cycle_life=NaN`: 셀은 보존하고 라벨 누락 이슈를 남긴다. 원천값과 정제 출력의 null을 연결한다. 학습 라벨로 쓸지는 후속 단계에서 판단한다.
- 첫 사이클 파형 빈 배열: 원래 사이클과 summary를 보존하고 파형 부재를 표시한다. 배열 전체 삭제나 번호 재부여를 하지 않는다.
- 수명과 사이클 수 불일치: 관측 이슈로 기록하며 자동 보정하지 않는다.
- 온도 등의 극단값/0: 임의 임계값으로 삭제하지 않는다. 단위/센서 설명을 확인한 뒤 규칙을 승인받는다.
- NaN/Inf, 원시 파형 길이 불일치, summary 길이 불일치: 위치와 원인을 기록한다. 보간·자르기·padding은 하지 않는다.
- 원시 파형 6개끼리의 길이와 보간 파형/`Vdlin` 관계는 분리해 검사한다. 9개 파형 모두를 동일 길이로 강제하지 않는다.
- float64로 저장된 `summary.cycle`은 유한한 정수 값인지 검증한 후 정수로 변환한다. 배열 위치에서 임의 생성하지 않는다.

격리 정책은 두 가지가 있다. 유효한 셀/사이클만 적재하면 데이터 활용도가 높지만 부분 성공을 분명히 관리해야 한다. 이슈 하나라도 파일 전체 적재를 막으면 운영은 단순하지만 이미 확인한 결측 때문에 많은 데이터를 못 쓸 수 있다. **라벨 누락/빈 파형은 상태로 보존하고, 해독 불가/관계 불일치는 영향 범위를 격리**하는 방안을 추천하며 3단계에서 기준을 확정한다.

## PostgreSQL의 향후 방향 — 상세 설계는 4단계

출처 파일, 처리 시도, 셀, 사이클, 검증 이슈를 FK로 연결한다. 실제 DDL과 최종 테이블/컬럼은 4단계에 제안한다. 현재 별도의 예측 특징은 정의하지 않는다.

파형은 PostgreSQL의 수치 배열로 사이클별 저장하는 방안을 우선 검토한다. 원시 파형과 1,000점 파형을 구별해 보존할 수 있다. 샘플마다 한 행을 만드는 방식은 SQL 조회는 유리하지만 행 수가 급증한다. 파일에 파형을 두고 DB에 경로만 두는 방식은 DB 용량을 줄이지만 외부 파일 관리가 필요하다. 실제 파형 전체의 규모/성능을 측정하고 4단계에서 선택한다.

동일 파일 해시 + 처리 버전의 성공 완료 키에 DB UNIQUE 제약을 두고, 동일 처리 재실행은 skip한다. 동일 내용이 다른 배치 label로 들어오면 매핑 충돌로 보고 조용히 중복 생성하지 않는다. 새 처리 버전 결과의 보존 범위는 4단계에 확정한다. 파일 단위 transaction을 기본으로 하고 실패 시 해당 파일을 rollback한다. 사전 SELECT만으로 동시 적재 중복을 막지 않는다.

DB 오류는 데이터 해독 오류와 구분하고, DB 연결이 없어도 실행 로그는 로컬에 남긴다. 로컬 로그와 DB commit은 원자적이지 않으므로 완료 키를 확인해 재실행한다. 예외를 무시하거나 일부 실패를 성공으로 보고하지 않는다.

## 단계별 테스트 계획

| 단계 | 작성/실행할 검증 |
| --- | --- |
| 2: extract | 모든 신규 helper/공개 함수 단위 테스트; 실제 구조 기반 작은 HDF5 fixture; UTF-16 char, 직렬화 string 보존, MATLAB_empty, null/dangling reference, 필수 키 누락, 손상 파일, 잘못된 shape/dtype; generator 종료 시 자원 해제 |
| 2: 실제 파일 확인 | 주요 파일별 전체 메타데이터 개수와 첫·중간·마지막 파형 회귀 확인; 별도의 느린 테스트에서 전체 참조 대상 읽기. 이번 관측 개수는 해당 SHA-256 파일의 회귀 기준으로만 사용 |
| 3 | 라벨 NaN/Inf/타입 오류, 빈 파형 보존, 원천값 불변, 배열 관계, 정제 조치/이슈, 위치와 실제 cycle 값 보존 |
| 4 | 실제 PostgreSQL에서 재실행/동시 실행 중복 방지, 연결 실패, 적재 중간 rollback, nullable 값/배열 저장 |
| 5 | CLI 경로 입력, 전체 순서, 이슈 개수, 실패 종료 코드, 전체 배치 실행 |

정상 테스트뿐 아니라 손상·누락 경로를 실제로 실행한다. 단위 테스트에서 작은 합성 파일을 사용하면 빠르지만 원본 형식과 달라질 위험이 있어 실제 파일 회귀 확인을 병행한다. 테스트가 실제 파일/DB 부재로 skip되면 성공과 구분해서 보고한다. PostgreSQL 고유 동작을 SQLite 결과로 대체하지 않는다.

## 학습하며 직접 검토할 Python 로직

1. `f['batch/cycles'][i, 0]`는 파형이 아니라 reference이고, `f[reference]`로 실제 그룹에 접근한다는 점.
2. shape, dtype, MATLAB 속성을 확인한 뒤 값을 읽는 순서. 특히 `MATLAB_empty` payload와 측정 배열의 차이.
3. UTF-16 char와 직렬화 string 객체가 다른 표현이라는 점.
4. Python 0-based 위치와 원천 cycle 번호, 수명 라벨과 관측 길이를 각각 보존하는 이유.
5. `with`, generator, NumPy 배열의 메모리/파일 수명과 예외 전파.
6. 추출에서 NaN을 보존하는 것과 검증 후 저장용 null로 표현하는 것의 차이.
7. 향후 DB UNIQUE/트랜잭션이 재실행과 동시 실행에 어떻게 작용하는지.

## 승인 요청

1. 필수 HDF5 reader로 **h5py를 기술 스택에 추가**하고 공통 reader부터 구현한다.
2. 날짜별 주요 파일 3개를 Batch 1·2·3으로 명시적으로 매핑하고, `2018-04-03_varcharge`는 우선 기본 처리 대상에서 제외한다. 추가 파일 원본은 유지한다.
3. 첫 사이클/라벨 결측을 임의 삭제·보정하지 않고, 미해독 barcode/channel 객체도 보존하며 해시 + 셀 위치로 추적한다. 사람이 읽을 수 있는 barcode/channel 해독이 필수라면 reader 구현 범위를 추가 조사한다.

승인 후에는 2단계인 extract.py와 그 테스트까지만 진행하고 결과를 설명한 뒤 멈춘다. Pydantic 검증/변환, DB DDL/적재와 전체 연결은 이후 단계다.
