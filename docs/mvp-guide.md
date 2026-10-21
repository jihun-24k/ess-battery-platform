# 학습용 ESS 대응 MVP

## 먼저 실행하기

프로젝트 루트에서 실행한다. 현재 원천 파일은 `data/raw/`에 있다.

```bash
uv sync --python 3.11

# 원본 값 그대로: 앞의 3개 사이클 위치로 학습하고 다음 2개를 평가
uv run ess-mvp run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat

# 데모: 평가 복사본에만 온도 offset을 넣어 모의 대응 관찰
uv run ess-mvp run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat \
  --demo-temperature-offset 100
```

`--demo-temperature-offset 100`은 원천 온도 값에 더하는 학습용 수치다. 실제 °C 제어값이나 공장 온도 설정이 아니다. 각 평가 사이클에서 처음 `2 × window` 샘플 이후에만 적용하고, 합성 샘플이 포함된 예측을 표시한다. 원본 MAT와 학습 구간은 변경하지 않는다.

## 흐름

```text
한 셀의 앞쪽 사이클 → 8개 특징 → 모델 학습

같은 셀의 뒤쪽 사이클 → 최근 N개 샘플 → 특징 → 이상 판단
                                      ↓ 연속 이상일 때
                          사건 ID·초기 콘솔 알림
                                      ↓
                        학습 문서 조회 → 모의 냉각
                                      ↓
                          상태 확인·결과 콘솔 알림
                                      ↓
                              다음 데이터 반복
```

학습과 평가 사이클은 겹치지 않는다. 첫 구간은 라벨이 없는 reference이며 정상으로 확정된 데이터가 아니다. 현재 모델은 실제 장애 확률/고장 종류를 예측하지 않는다. 과거와 다른 측정 패턴을 탐지하는 baseline이다.

## 코드 5개 읽기

| 파일 | 함수 | 입력 → 반환 |
| --- | --- | --- |
| replay.py | available_cycles | 파일·셀 위치 → 실제 사이클 수 |
| replay.py | iter_measurements | 파일·셀·사이클 범위 → T/V/I/t 측정 generator |
| replay.py | iter_windows | 측정 generator·크기·간격 → Window 또는 ReplayIssue |
| replay.py | inject_temperature_offset | 평가 측정값·offset·시작 위치 → 합성 표시가 붙은 복사본 |
| model.py | build_features | 같은 사이클의 과거 Window → 특징 dict |
| model.py | train_model / predict | reference 특징 → 모델 / 새 특징 → 이상 여부·점수 |
| manuals.py | get_manual | 이상 종류·문서 폴더 → 문서·원본 바이트 해시 또는 None |
| actions.py | execute_mock_action | 메모리 장비·행동·사건 ID → 적용/이미 적용/거부/실패 |
| actions.py | notify | 사건 ID·메시지 → 콘솔 출력과 기록용 payload |
| main.py | run / main | 설정 → 반복 실행 결과 / CLI 종료 코드 |

기존 `battery_etl.extract`를 재사용하므로 MAT 파싱을 중복 구현하지 않았다. 데이터 타입은 단순 dataclass, 이동 창은 deque, 반복은 generator로 구성했다. 새 프레임워크/DB 스키마/실제 장비 Adapter는 추가하지 않았다.

## 모델과 기본 설정

특징은 온도 평균·최대·표준편차·샘플당 추세, 전압 평균·표준편차, 전류 평균·표준편차다. 원천 시간 단위를 확정하지 않았으므로 추세를 초당/분당으로 표현하지 않는다. 다변량 이상을 온도 고장으로 확정하지 않고 `measurement_anomaly`로 기록한다.

`StandardScaler → IsolationForest`를 reference 특징에만 fit하고 평가에는 적용만 한다. 최소 10개 reference 창이 필요하다. 모델 seed는 42로 고정했다. `anomaly_score = -decision_function`이며 값이 클수록 더 이례적이고 양수이면 모델 경계 밖이다. 확률이나 실제 위험도를 뜻하지 않는다.

| 옵션 | 기본값 | 의미 |
| --- | ---: | --- |
| --cell | 0 | 원본 안의 0-based 셀 위치 |
| --train-cycles | 3 | 학습용 앞쪽 사이클 **위치 개수** |
| --eval-cycles | 2 | 이후 평가 사이클 개수 |
| --window | 32 | 한 특징 창의 샘플 수 |
| --stride | 16 | 다음 창까지 이동할 샘플 수 |
| --contamination | 0.05 | 학습 시 경계를 정하는 설정; 실제 고장률이 아님 |
| --trigger-windows | 2 | 연속 이상 창이 이 수에 도달하면 사건 생성 |
| --recovery-windows | 3 | 연속 정상 창 이후 baseline 패턴 복귀 기록 |

사이클 경계에서 창과 연속 판단 수를 초기화한다. 빈 사이클은 샘플이 없으므로 창을 만들지 않는다. 따라서 Batch 1의 기본 학습 범위 0~2 중 빈 첫 사이클은 학습 창을 제공하지 않는다.

NaN/Inf/타입 오류, 샘플 간격 누락, 시각 역행은 이슈를 남기고 창을 초기화한다. 시각 역행 샘플은 제외한다. 온라인 재생에서는 크기에 따른 8 ULP 이하 시각 오차만 허용한다. 기존 offline ETL의 엄격한 규칙은 바꾸지 않았다. 사이클을 가로질러 창을 만들거나 값을 보간하지 않는다.

## 문서와 모의 행동

기본 문서는 패키지 내부 `src/ess_mvp/manuals/measurement_anomaly.md`이며 직접 작성한 학습 예시다. 실제 사내 승인 문서가 아니다. 고정된 이상 종류→문서 매핑으로 조회하며 문서 내용은 Python 명령으로 실행하지 않는다. 문서의 mock 행동도 코드의 허용 목록으로 한정한다.

가상 냉각 장비는 메모리의 `normal → boost` 상태만 변경한다. 같은 사건 ID에는 저장된 결과를 반환하며, 이미 boost라면 `already_applied`다. baseline 패턴 복귀가 실제 고장 해결을 뜻하지 않으므로 장비를 자동으로 normal로 되돌리지 않는다. 실제 하드웨어/설정 온도/메신저에 접속하지 않는다.

열린 사건 동안에는 새 조치와 반복 알림을 만들지 않는다. 패턴 복귀로 사건이 닫힌 뒤 다시 이상이 지속되면 새 사건이다. 다음 run은 새 학습·사건·가상 장비 상태로 시작한다. 프로세스 재시작에 걸친 사건 복구/물리 제어 멱등성은 현재 범위 밖이다.

문서가 없거나 읽을 수 없거나 비어 있으면 초기 알림 후 조치를 보류하고 이유를 기록한다. 모의 행동이 실패하면 결과를 알리고 같은 열린 사건에서 자동 재시도하지 않는다.

```bash
# 문서 누락을 직접 확인: 이 폴더에 지정 문서가 없으면 보류
uv run ess-mvp run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat \
  --demo-temperature-offset 100 --manual-dir /private/tmp/no-mvp-manual

# 모의 장비 실패 확인
uv run ess-mvp run data/raw/2017-05-12_batchdata_updated_struct_errorcorrect.mat \
  --demo-temperature-offset 100 --simulate-action-failure
```

종료 코드: 0=실행 완료, 1=파일/I/O 오류, 2=옵션·학습/평가 데이터 부족, 3=실행은 완료됐으나 조치 보류/실패 발생. 예측 이상 자체는 실행 실패가 아니다.

## 저장 결과

`runs/mvp/<UUID>/`에 아래 세 파일을 저장한다.

- `report.json`: 원천 SHA-256, 셀/사이클 선택, 모델/특징 버전, 설정, 집계와 종료 상태.
- `events.jsonl`: reference 창, 품질 이슈, 예측 특징/점수/위치, 사건, 문서 해시, 모의 행동과 알림.
- `model.joblib`: 이번 실행의 fitted model과 metadata. 프로그램은 외부 모델을 읽지 않는다. 이후 사용할 때도 본인이 생성한 신뢰 가능한 파일만 로드해야 한다.

예측 로그의 `source_time`은 원천 상대 시각이고 `logged_at`은 실행 UTC 시각이다. 실제 시간 간격을 기다리지 않고 순서대로 빠르게 재생한다. 셀은 파일 해시와 cell_index로 추적하며 실제 설비 ID로 간주하지 않는다.

MAT 재생값은 냉각 명령에 반응하지 않는다. `cooling_effect_verified=false`이며, 확인한 것은 메모리 장비 상태와 소프트웨어 처리 흐름이다. 모델 정확도, 고장 선행 예측 성능이나 실제 냉각 효과를 입증한 것이 아니다.

## 검증과 직접 해볼 학습

```bash
uv run pytest tests/mvp -q
uv run ruff check src tests
uv run ruff format --check src tests

# 전체 회귀: 실제 파일 + 별도로 준비된 PostgreSQL
BATTERY_DATA_DIR=data/raw TEST_DATABASE_URL='postgresql://USER@HOST:PORT/DB' \
  uv run pytest -q
```

직접 해볼 순서:

1. `replay.py`에서 한 창의 샘플과 사이클 경계를 확인한다.
2. `model.py`의 8개 특징을 수작업 계산과 비교한다.
3. `main.py`에서 `prediction → incident_opened → manual_found → action_result → notification`을 따라간다.
4. `--trigger-windows`와 `--recovery-windows`를 바꾸며 사건 개수 변화를 비교한다.
5. 문서 누락/장비 실패/NaN/시각 역행 테스트를 읽고 예상 결과를 말해본다.

실행한 테스트와 원본 재생 결과는 [MVP 검증 기록](mvp-verification.md)에 별도로 남긴다.
