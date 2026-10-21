# MVP 구현·검증 기록

2026-10-17. 구현 범위는 한 셀의 시간 순서 재생, reference 기반 이상 탐지, 학습 문서 조회, 메모리 장비 상태 변경과 콘솔 알림이다. 기존 전체 현장 플랫폼 설계와 구분한다.

## 변경 파일과 이유

- `src/ess_mvp/replay.py`: 기존 MAT reader를 재사용해 T/V/I/t를 순차 전달한다. 창은 사이클·결측·간격 누락에서 분리한다.
- `src/ess_mvp/model.py`: 8개 특징과 재현 가능한 Isolation Forest baseline을 구현했다. 앞 구간에만 전처리/모델을 fit한다.
- `src/ess_mvp/manuals.py`와 bundled Markdown: 고정 매핑으로 학습 문서를 찾고 원본 바이트 SHA-256을 기록한다. 텍스트로 실행 권한을 바꾸지 않는다.
- `src/ess_mvp/actions.py`: mock 냉각 상태, 같은 사건 ID의 결과 재사용, 거부/실패와 콘솔 알림을 구현했다.
- `src/ess_mvp/main.py`: 연속 이상/정상 창, 사건 중복 방지, 문서 부재 보류, 모의 동작 확인과 JSONL/보고서/모델 저장을 연결했다.
- `tests/mvp/`: 기존 작은 MAT fixture 구조를 확장한 합성 파형으로 새 기능을 검증한다.
- `pyproject.toml`, `uv.lock`: scikit-learn/joblib, ess-mvp CLI, 두 패키지의 wheel 구성을 추가했다.
- `README.md`, `mvp-guide.md`, `decisions.md`: 현재 data/raw 경로와 실행·학습·트레이드오프를 기록했다. 전체 플랫폼 설계 문서에는 후속 MVP 구현 상태를 표시했다.

새 DB 스키마나 실제 장비/메신저 연결은 추가하지 않았다. 기존 ETL 모듈의 동작은 수정하지 않았다.

## 테스트

```bash
TEST_DATABASE_URL=postgresql://u079@127.0.0.1:55437/postgres \
  BATTERY_DATA_DIR=data/raw \
  uv run pytest -q --junitxml=docs/mvp-test-results.xml

uv run ruff check src tests
uv run ruff format --check src tests
```

최종 실행: **64 passed in 5.69s, skip 없음**. 기존 ETL 35개와 MVP 29개를 포함한다. JUnit은 [mvp-test-results.xml](mvp-test-results.xml)에 있다. 원본 회귀는 실제 3개 파일의 해시·개수·대표 파형을 검사하며, 기존 PostgreSQL 통합 테스트는 UUID별 격리 schema에서 실행했다. 검증에 사용한 임시 PostgreSQL 서버는 이후 중지했다.

MVP 테스트는 다음을 포함한다.

- 실제 MAT와 같은 참조 구조의 순차 읽기, 배열 길이 불일치.
- 창 크기/간격, 사이클 경계, NaN/잘못된 타입, 시각 역행, 간격 누락, 미세 시각 오차.
- 합성 온도 복사본과 원천 불변.
- 특징의 수작업 기대값, 고정 feature 순서, 부족한 학습 데이터, 재현 가능한 모델 score.
- 시간상 겹치지 않는 학습/평가, 저장된 모델의 trained metadata.
- 문서 부재/빈 내용/잘못된 인코딩, 문서 원본 바이트 해시, 텍스트의 실행 권한 없음.
- 모의 행동 허용 목록, 같은 ID 재사용, 이미 적용/실패, 콘솔 알림.
- 사건당 1회 대응, 연속 정상 이후 새 사건, 문서 부재 보류, 실패 보고서와 CLI 종료 코드.

## 실제 MAT 실행

모든 실행은 cell_index=0, 앞 3개 사이클 위치 학습, 다음 2개 평가, window=32, stride=16이다. 전체 배치의 모든 셀/사이클을 학습·평가한 결과가 아니다.

| 원천 batch_date | 평가 offset | reference 창 | 평가 창 | 이상 창 | 사건/모의 요청 |
| --- | --- | ---: | ---: | ---: | ---: |
| 2017-05-12 | 없음 | 134 | 138 | 5 | 2 |
| 2017-05-12 | 합성 +100 | 134 | 138 | 16 | 4 |
| 2018-02-20 | 합성 +100 | 190 | 125 | 29 | 6 |
| 2018-04-12 | 합성 +100 | 138 | 92 | 13 | 2 |

모든 실행은 completed였다. 가상 장비의 첫 행동은 applied, 이후 새 사건에서 이미 boost인 경우 already_applied로 기록했다. 데이터는 승인된 실제 고장 라벨이 없고 정상/고장 정답도 없으므로 위 숫자는 accuracy/recall/고장 확률을 뜻하지 않는다. 합성 변화가 있는 모든 창을 검출한 것도 아니다.

원본 변경 없이 평가 복사본에만 offset을 적용했다. 합성 샘플 포함 여부를 창별로 기록했다. 실제 원본 source SHA-256은 기존 조사 값과 일치하는 회귀 테스트를 통과했다.

세부 근거: [mvp-validation-results.json](mvp-validation-results.json). 실행별 원천/특징/사건/문서/모의 행동은 해당 artifact_path의 events.jsonl에 있다. `cooling_effect_verified=false`이며 기록된 온도 변화에 대한 냉각의 인과 효과를 검증하지 않았다.

## 패키징 실행

```bash
uv build --wheel --out-dir /private/tmp/ess-mvp-wheel-validation
```

wheel에 battery_etl·ess_mvp, 두 CLI 진입점과 학습 Markdown이 포함됨을 검사했다. 생성한 wheel을 uv의 별도 환경에 설치해 실제 Batch 1 파일의 CLI 데모를 실행했다. editable 환경에만 있는 파일에 의존하지 않는지 확인하기 위한 검증이다.

별도 환경 CLI 종료 코드 0, 학습 창 134개·평가 창 138개, 저장된 모델 상태 trained와 packaged 문서 조회를 확인했다. 근거는 [mvp-wheel-verification.json](mvp-wheel-verification.json)에 있다.

## 발견한 오류와 수정

1. 최초 테스트에서 이벤트 함수의 kind 인자와 prediction.kind가 충돌해 TypeError가 발생했다. 이벤트 종류 인자를 event_type으로 분리했다.
2. 역행 시각의 현재 샘플이 창 초기화 후 다시 포함돼 테스트가 실패했다. 해당 샘플은 제외하고 이후 샘플부터 창을 다시 채운다. 두 원인으로 최초 MVP 테스트는 5 failed, 23 passed였으며 수정 후 전체 재검증했다.
3. 실제 실행 첫 시도에서 이전 루트 경로를 사용해 SourceReadError가 발생했다. 현재 파일이 data/raw에 있음을 확인하고 실제 경로로 실행·문서 예제를 정정했다. 원본을 이동하지 않았다.
4. 문서 해시는 텍스트 개행 변환이 아니라 원본 바이트를 기준으로 계산하도록 하고 CRLF 테스트를 추가했다.

## 남은 범위

장애 확정 라벨/예측 선행 시간/모델 성능, 사내 문서 검색·권한, 실제 장비 제어, 메신저 전송, 지속 프로세스의 재시작 복구, PostgreSQL 사건 저장은 후속 과제다. 매 run은 독립된 학습·사건·가상 장비 상태로 시작한다. 이 결과를 실제 ESS 안정화나 현장 안전 성능 검증으로 해석하지 않는다.
