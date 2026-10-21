# 자소서용 역량 검증 — 2026-10-18

## 실행 범위

실제 로컬 PostgreSQL 및 수집·추론·운영 서버와 워커를 사용했다. 입력은 가상 배터리 측정값이며, 실물 배터리나 현장 제어 검증이 아니다. Agent는 실제 OpenAI API(gpt-4.1-mini)를 호출했다.

## 1. 데이터 파이프라인: 반복 전송 시 중복 저장 방지

- 대상 장비: `AGENT-DEMO-9d631acc6f`
- 고유 측정값 4건 최초 전송 후 동일 payload 4건을 다시 전송했다.
- 재전송 응답: 4건 모두 `duplicate`.
- 장비별 DB 행 수: 재전송 전 4건 → 재전송 후 4건.
- 추가 중복 저장: 0건.
- 별도 PostgreSQL 테스트에서도 같은 측정값을 8회 동시 수집해 최초 저장 1회 및 동일 measurement ID 반환을 확인했다.
- 파일 반복 업로드가 아니라 측정 이벤트 재전송 검증이다. 대규모 처리 성능이나 모든 장애 조건에 대한 결과로 일반화하지 않는다.

자소서 문장: “동일한 배터리 측정값 4건을 재전송한 뒤 PostgreSQL 저장 건수를 비교해 추가 중복 저장이 발생하지 않음을 확인했습니다.”

## 2. AI Agent: 실제 Tool 실행 성공 여부

- 실제 Agent run ID: 4.
- 호출: `search_response_manuals` → `get_measurement_window` → `finish_analysis`.
- DB의 Tool 실행 이력에서 `result.ok`를 확인했다.
- 정상 완료: 3건 / 전체 3건. 이번 단일 시나리오의 호출 성공률 100%.
- 최종 상태: `needs_review`. 현장 상태와 장기간 데이터 부족을 이유로 대응안 제출을 보류했다.
- 이는 Tool 실행 성공 여부이며, Tool 선택 정확도·응답 근거의 의미적 정확성·대응안 적절성을 평가한 수치가 아니다.

자소서 문장: “실제 LLM을 연결해 이상 사건을 분석하고, 대응 문서 검색·측정값 조회·분석 완료 도구의 호출 이력을 확인해 3건 모두 정상 실행됐음을 확인했습니다.”

## 승인 검증의 구분

처음 실행한 전체 통합 스크립트는 대응안 생성과 승인을 기대한다. 이번 실제 LLM은 대응안을 제출하지 않았으므로 해당 전체 시나리오는 통과로 보고하지 않는다.

별도 `test_assisted_mode_only_creates_job_and_proposal` 테스트에서는 외부 LLM 대신 정해진 응답을 제공하는 테스트 대역을 사용했다. 이 조건에서 승인 대기 대응안 생성 및 승인 전 조치 미생성, 분석·도구 호출 이력 저장을 확인했다. 실제 LLM의 대응 판단 정확성을 입증하는 테스트는 아니다.

## 실행 명령 및 증거

```sh
DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_agent_demo_20261017' uv run python scripts/verify_agent_processes.py

PLATFORM_TEST_DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_platform_test_20261017' uv run pytest -v tests/test_collection.py::test_concurrent_duplicate_ingest tests/test_agent_jobs_approvals.py::test_assisted_mode_only_creates_job_and_proposal --junitxml=runs/career-verification-20261018/test-results.xml
```

- 실제 API 재전송 및 DB 조회 결과: `runs/career-verification-20261018/results.json`
- Pytest: 2 passed, 1.03s. JUnit: `runs/career-verification-20261018/test-results.xml`
- 전체 통합 스크립트 결과: `docs/agent-process-verification.json`
- 기존 통합 결과 보관: `runs/career-verification-20261018/previous-process-verification.json`
- 검증 실행 로그: `runs/agent-verification-9d631acc6ff14e4d80cd6aa10caf9036/process.log`

운영 코드와 원천 파일은 변경하지 않았다. 검증은 전용 데모 DB에 신규 장비 데이터를 추가하고, 독립 테스트 스키마를 생성·정리했다.

전체 통합 스크립트의 실제 종료 결과는 exit code 1 / `RuntimeError`이다. Agent가 `needs_review`로 완료해 승인 대기 대응안이 없었고, 스크립트의 150초 대기 제한에 도달했다. 따라서 전체 승인·실행 시나리오 성공으로 해석하지 않는다. 이와 별개로 수행한 HTTP 재전송/DB 조회 및 실제 Agent Tool 이력 확인 결과는 위 JSON에 보존했다. 검증용 자식 프로세스는 종료됐다.

## 추가 실패 사례: 미등록 Tool 선택 차단 및 분석 계속 진행

새 테스트: `tests/test_agent_contracts_tools.py::test_unknown_tool_is_recorded_and_analysis_recovers_without_action`.

입력: 테스트 응답 대역이 제공 목록에 없는 `set_factory_temperature(target_c=18)`를 한 번 요청한다. 이 이름은 테스트용 요청 문자열이며 실제 설비 연결이나 명령 실행은 없다.

확인한 결과:

1. 서버가 `ok=false`, `error.code=unknown_tool`, `retryable=false`로 거부한다.
2. 거부 결과를 다음 Agent 턴에 전달하고 오류 호출 이력을 PostgreSQL에 저장한다.
3. 대역이 정상 측정 조회 → 문서 검색 → 분석 완료 호출로 이어가며 나머지 3건은 정상 완료된다.
4. 최종 분석은 `needs_review`이고 대응안은 생성되지 않는다.
5. 조치 워커를 실행해도 처리할 명령이 없다. DB 조치 0건·대응안 0건이며 가상 냉각 상태는 `normal`이다.

이는 실제 PostgreSQL 및 Agent 워커·실행 루프·도구 검증 코드를 사용한 통합 테스트다. **LLM 응답은 실패 상황을 확실하게 만들기 위해 대역으로 주입했다.** 실제 LLM이 스스로 오류를 수정할 확률이나 Tool 선택 정확도를 측정한 결과는 아니다.

실행 명령:

```sh
PLATFORM_TEST_DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_platform_test_20261017' uv run pytest -q tests/test_agent_contracts_tools.py::test_unknown_tool_is_recorded_and_analysis_recovers_without_action --junitxml=runs/career-verification-20261018/wrong-tool-test-results.xml
```

실행 결과: 추가 테스트 1건 통과. 증거는 `runs/career-verification-20261018/wrong-tool-test-results.xml`에 보관했다. Ruff 검사 및 포맷 검사도 통과했다. 운영 코드 변경 없이 테스트와 본 기록만 추가했다.

자소서 표현: “Agent가 허용 목록에 없는 제어 도구를 요청하는 실패 상황을 주입하고, 요청 거부와 오류 이력 저장, 조치 미실행을 확인했습니다. 이를 통해 도구 선택 실패를 고려한 실행 통제 구조를 검증했습니다.”
