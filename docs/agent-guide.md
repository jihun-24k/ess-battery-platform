# ESS 사건 대응 AI Agent — 구현·실행 가이드

2026-10-17. 기존 ESS 서버에 단일 Tool Calling Agent, 문서 검색, 영속 작업, 대응 제안·승인, 관제를 구현했다. 실제 OpenAI Responses API 연결을 사용한다. ML 이상 판단과 냉각은 기존 학습용 가상 기능이며 실제 BMS/현장 제어·사내 문서·메신저에는 연결하지 않았다.

## 1. 완료한 7단계

| 단계 | 코드 | 구현 결과 |
|---|---|---|
| 1 계약 | `agents/schemas.py` | Tool 인자·분석·출처 ID·대응안·승인 입력의 Pydantic 검증 |
| 2 조회 Tool | `agents/tools.py` | 사건·측정값·품질·기존 조치·결과 조회, 사건 자산 범위 제한 |
| 3 문서 검색 | `agents/retrieval.py` | 문서 버전/hash·시뮬레이션 승인 범위·chunk 검색과 출처 |
| 4 Agent | `agents/llm_client.py`, `runner.py` | 실제 LLM Tool 선택과 결과 기반 반복, 호출·시간 제한 |
| 5 영속화 | `repository.py`, `worker.py`, `migrations.py` | 작업 lease/token·snapshot·단계 기록·중복 방지·재시도 |
| 6 대응·승인 | `operations/proposals.py`, `policy.py`, `approvals.py` | 제안 hash·인증·거절·만료·승인 후 재검증·단일 가상 조치 |
| 7 관제·평가 | `static/`, `agents/evaluation.py`, `tests/` | 분석/Tool/제안 검토·승인 UI, 프로그램 테스트와 실제 LLM 평가 분리 |

실행 흐름:

```text
POST 측정값 → PostgreSQL → 추론 워커 → ML 서버
 → 운영 워커가 사건 + Agent 작업을 같은 transaction에 등록
 → Agent 워커가 DB 작업 claim 후 transaction 종료
 → 실제 LLM이 필요한 조회 Tool 선택
 → 결과/출처 확인 → 추가 조회 또는 분석 종료
 → 분석 + 미실행 대응안 + 완료 상태를 같은 transaction에 저장
 → 운영자 검토·승인 → 정책 재검증 → 가상 냉각 + outbox → 관제 조회
```

사건 기본 알림은 Agent 완료를 기다리지 않는다. Agent 프로세스가 비정상 종료해도 launcher는 수집·추론·운영 프로세스를 유지하고 로그로 알린다. Agent 프로세스 재실행 시 만료된 작업을 회수한다.

## 2. 설치와 전용 DB 준비

프로젝트 루트에서 실행한다. PostgreSQL 서버와 전용 DB는 사전에 준비해야 한다.

```sh
uv sync
export DATABASE_URL='postgresql://USER@127.0.0.1:5432/ess_agent_demo'
uv run ess-platform init-db
uv run ess-platform train-mock --version mock-v1
uv run ess-platform approve-model mock-v1
uv run ess-platform seed-manuals
```

기존 DB에는 `uv run ess-platform migrate`를 실행한다. migration은 다음과 같다.

- `001_agents`: 새 Agent/문서/제안/승인 테이블, 기존 사건 revision/response_mode와 조치 proposal_id/command_key.
- `002_job_request_context`: 수동 재분석의 이유와 서버 인증 요청자 보존.

기존 사건은 legacy 모드로 유지한다. 기존 측정·추론·사건·조치를 삭제하거나 재판정하지 않는다. 적용 이력/checksum을 확인하고 재실행은 중복 적용하지 않는다. `create_all()`만으로 기존 컬럼을 변경하지 않는다.

이번 로컬 구현에서 준비한 DB:

- 기존 결과 보존: `ess_platform_demo_20261017`
- Agent 실제 프로세스 검증: `ess_agent_demo_20261017`
- 실제 LLM 평가: `ess_agent_eval_20261017`
- 프로그램 테스트: `ess_platform_test_20261017`

모두 기존 로컬 PostgreSQL의 `127.0.0.1:55437`, 역할 `u079`를 사용했다. 다른 환경에서는 자신의 DB 주소를 설정한다.

## 3. 실제 LLM 연결과 실행

공식 SDK는 환경변수 `OPENAI_API_KEY`를 읽는다. 기본 모델은 `gpt-4.1-mini`이며 `ESS_AGENT_MODEL` 또는 `worker agent --model`로 바꿀 수 있다. 키는 CLI 인자·DB·로그·HTML·소스 파일에 기록하지 않는다. `.env.example`은 형식 예시일 뿐 자동으로 로딩되지 않으므로 셸에서 export하거나 실행 환경의 secret 주입을 사용한다.

```sh
export OPENAI_API_KEY='<본인의 API 키>'
export ESS_AGENT_MODEL='gpt-4.1-mini'
# 승인 기능을 쓸 때만 설정: 32자 이상 토큰과 서버가 정한 운영자 이름
export ESS_APPROVER_TOKEN='<본인이 관리하는 32자 이상의 토큰>'
export ESS_APPROVER_ID='local-operator'

uv run python scripts/run_platform.py --agent --response-mode assisted
```

관제: **http://127.0.0.1:8103**. HTTP 서버 3개·워커 3개가 실행된다. Ctrl+C로 자식 프로세스를 정리하고 DB를 보존한다. 로그와 종료 snapshot은 `runs/platform/<session-id>/`에 남는다.

독립 실행:

```sh
uv run ess-platform serve collector
uv run ess-platform serve ml
uv run ess-platform worker inference
uv run ess-platform worker operations --response-mode assisted
uv run ess-platform serve operations
uv run ess-platform worker agent
```

`worker agent --once`는 한 작업만, `--drain`은 현재 가능한 작업을 처리하고 종료한다. 재시도 시간이 미래이면 drain은 종료하므로 운영 시 기본 반복 모드를 사용한다. API 키가 없으면 Agent worker는 명확히 실패하며 가짜 LLM으로 자동 대체하지 않는다.

승인 토큰을 설정하지 않으면 조회는 가능하지만 승인/재분석 POST는 503이다. 잘못된 토큰은 403이다. 토큰은 브라우저의 암호 입력란에 직접 입력하고 localStorage에 저장하지 않는다. 현재 인증은 loopback용 단일 운영자 capability token이다. 조직 SSO·다중 사용자 RBAC·TLS·감사 저장소 분리는 아직 구현하지 않았다.

## 4. 관찰 모드와 대응 모드

- `shadow`(기본): 기존 가상 자동 조치는 유지한다. Agent 분석/제안은 shadow_only로 저장해 실행 대상으로 쓰지 않는다.
- `assisted`: 사건 발생 직후 기존 pending 냉각 요청을 만들지 않는다. Agent의 근거 기반 제안을 검토·승인한 경우에만 action을 만든다.

이 모드는 새 사건에 기록한다. CLI 옵션 변경이 기존 사건의 모드를 바꾸지 않으므로 과거 사건을 assisted로 소급 실행하지 않는다. 승인 시스템이 없거나 Agent가 실패했다고 기존 자동 조치로 조용히 우회하지 않는다.

합성 데이터 파일은 `ess-platform replay FILE.jsonl`로 수집한다. 기존 `examples/stream-demo.jsonl`은 끝부분 온도가 정상으로 돌아오는 사례라 Agent가 조치를 보류하는 것이 가능하다. 열린 사건이더라도 최신 판정이 정상·최근 품질 오류·기존 조치·문서 불일치가 있으면 새로운 냉각 제안을 막는다.

완전한 실제 LLM·승인·조치 검증을 재현하려면 전용 Agent 데모 DB의 서버가 모두 중지된 상태에서 실행한다.

```sh
export DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_agent_demo_20261017'
uv run python scripts/verify_agent_processes.py
```

이 스크립트는 실제 LLM 호출 비용이 발생한다. 네 합성 측정값으로 사건을 만들고, 메모리에만 생성한 임시 토큰을 사용해 **통합 테스트 승인**을 수행한다. 실제 담당자의 승인이나 실장비 제어를 의미하지 않는다. 검증 후 자신이 실행한 프로세스를 정리하고 `docs/agent-process-verification.json`에 결과를 남긴다.

## 5. Tool과 검색의 구현 원리

정적 registry에 정의된 다음 함수만 호출할 수 있다.

- `get_incident_context`: 사건·모델·snapshot·출처 목록.
- `get_measurement_window`: 서버가 정한 사건 자산/cutoff 이전 1~3600초, 최대 200건.
- `get_quality_issues`: 같은 자산·시간 범위에서 측정값에 연결된 오류.
- `search_response_manuals`: metadata 필터 → 단순 키워드/태그 검색 → 최대 5개 chunk와 버전/hash.
- `get_action_history`, `get_action_status`: 사건에 속한 조치만 조회.
- `submit_response_proposal`: 미실행 의도를 등록/수정. 최종 완료 시에만 DB 제안 한 건을 저장.
- `finish_analysis`: 사실·가설·누락 정보·문서 출처·종료 판단·한계를 검증하고 종료.

필수 측정/문서 조회가 성공하고 적용 문서·현재 이상 판정·시뮬레이션 자산 조건이 맞을 때만 제안 Tool을 LLM 목록에 표시한다. 실행 함수에는 표시 여부와 별개인 독립된 검사도 있다. 최종 보류 선택은 미실행 의도를 철회한다.

출처는 `measurement:ID`, `prediction:ID`, `incident:ID`, `model:VERSION`, `quality_summary:ID`, `issue:ID`, `action:ID`, `document:CHUNK_ID` 형식이다. 실제 해당 run에서 받은 ref만 인용할 수 있다. 특히 manual_refs는 document:숫자만 허용하며 검색 결과가 없으면 빈 배열이다. schema 검증이 의미 정확성까지 보장하지는 않는다.

문서는 local 학습 문서를 시뮬레이션 전용으로 등록한다. 실제 사내 문서 승인과 혼동하지 않게 asset_type/approval_scope를 저장한다. 문서·chunk hash를 검증하고 철회/다른 자산/내용 변조는 검색·승인·실행 경로에서 차단한다. 검색 본문은 비신뢰 데이터이며 권한을 부여하지 않는다. 현재 문서 수가 작아 키워드 검색을 택했고 embedding·vector DB·reranking은 추가하지 않았다.

## 6. 실행·승인·재시작 보장과 한계

- Agent loop: 최대 7턴·10 Tool 호출·전체 60초. SDK 단일 호출은 남은 예산과 25초 중 작은 값, SDK 자동 재시도는 0.
- 일시적 공급자 오류는 DB 작업 단위 최대 3회 재시도한다. 계약 오류·예산 초과는 needs_review로 남기고 콘솔 outbox에 기록한다.
- 작업 claim lease는 기본 90초다. DB transaction은 claim/단계 기록/완료에만 열고 LLM 호출 중 유지하지 않는다. 만료 후 token이 바뀌면 늦은 저장을 거부한다. 반복 실패 작업 때문에 다음 작업이 막히지 않게 한다.
- 재시작은 사건 snapshot부터 새 attempt를 시작한다. LLM 대화의 중간 턴부터 재개하지 않으며 네트워크 호출 비용까지 exactly-once라고 주장하지 않는다.
- 분석 완료·대응안·알림 예약은 한 transaction에 저장한다. 승인과 action 등록도 같은 transaction/운영 잠금으로 묶는다.
- 제안 hash에 자산·동작·인자·근거·문서/정책·사건 revision·run을 포함한다. 승인 유효기간은 제안 생성 후 10분이다.
- 승인과 조치 직전 사건 복귀·문서 철회/변조·최신 정상 판정·모델 변경·최근 품질 이슈를 다시 검사한다. 시뮬레이션의 원천 관측 시각이 snapshot보다 30초 이상 달라지면 재검토한다. 실제 센서의 wall-clock freshness나 BMS 보호 제어는 별도 미구현 범위다.
- 동일 승인/명령은 기존 결과를 재사용한다. 첫 버전은 사건당 한 action 제약을 유지한다. 실패·취소 뒤 같은 사건에서 새로운 명령을 무제한 생성하지 않는다.
- 가상 장비만 지원한다. 실제 Gateway가 없으므로 네트워크 장비의 unknown/reconcile 프로토콜은 설계 항목으로 남아 있다. 실제 외부 알림도 현재 콘솔 대체다.

## 7. 관제/API

- `GET /api/state`: 기존 상태와 agent_jobs/agent_runs/agent_proposals 최신 목록.
- `GET /api/incidents/{id}/analysis`: 사건별 작업·실행·제안.
- `GET /api/agent-runs/{id}`: snapshot·최종 결과·사용량·Tool 인자/결과·시간.
- `GET /api/action-proposals/{id}`: 정확한 payload/hash·상태·승인 이력.
- `POST /api/incidents/{id}/analysis-jobs`: bearer 인증, request_id와 reason. 서버 인증 요청자와 이유를 저장하며 같은 ID의 다른 내용은 409.
- `POST /api/action-proposals/{id}/approval`: bearer 인증, request_id/proposal_hash/decision(approve 또는 reject).

승인자는 요청 body로 지정하지 않고 환경변수로 구성한 서버 identity에서 결정한다. API가 일반 사용자가 직접 입력한 actor/role을 신뢰하지 않는다. 관제의 ‘근거·제안 검토’에서 payload/hash를 확인한 후 승인하거나 거절한다. LLM에게 승인 토큰을 제공하지 않는다.

## 8. 함수 입력·출력 읽는 순서

1. `operations_tick(..., response_mode)`: 사건과 job을 같은 transaction에서 생성.
2. `claim_job(engine) -> dict | None`: 작업 소유 token 확보.
3. `build_context(engine, job) -> AgentContext`: 분석 기준과 허용 자산·기간 확정.
4. `run_agent(engine, context, client, record_step) -> (Assessment, usage)`: 동적 Tool 선택과 예산 관리.
5. `execute_tool(engine, context, call) -> dict`: 계약·출처·범위 검증.
6. `store_proposal(conn, context, run_id) -> int | None`: 아직 실행되지 않은 제안과 정책 상태 저장.
7. `decide(engine, actor, proposal_id, request) -> (HTTP code, result)`: 승인·거절·단일 action 등록.
8. `action_tick(engine) -> bool`: 현재 상태와 승인을 다시 확인하고 가상 냉각 결과 기록.

모든 새 Python 파일/함수는 한국어 역할·입출력 docstring을 가진다. 상세 상태/관계는 원래 `agent-platform-design.md`와 `decisions.md`를 함께 본다.

## 9. 검증과 실제 LLM 평가

```sh
export PLATFORM_TEST_DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_platform_test_20261017'
uv run pytest -q --junitxml=docs/agent-test-results.xml
node --test tests/frontend/*.cjs
uv run ruff check src scripts tests dashboard/export_data.py

# API 키와 전용 평가 DB 필요: 실제 호출을 수행한다.
export DATABASE_URL='postgresql://u079@127.0.0.1:55437/ess_agent_eval_20261017'
uv run ess-platform eval-agent --limit 20 --output docs/agent-live-evaluation.json
```

Pytest는 실제 PostgreSQL·HTTP와 결정적 LLM 응답 대역으로 프로그램의 계약·상태·실패·승인·멱등성을 검증한다. API 키가 없어도 프로그램 테스트는 가능하지만 DB 환경변수가 없으면 일부 skip되므로 실제 전체 통과 근거와 구분해야 한다.

실제 LLM 평가는 고온/최근 정상/전압 변화/음수 전류/문서 누락·다른 장비·철회·변조/물리 자산/품질 오류/기존 조치/문서 injection의 작은 합성 사례 20개다. 필수 Tool·종료 판단·금지 Tool 시도·중간 검증 오류 유무를 채점한다. ‘통과’는 이 고정된 프로그램 기준이며 실제 고장 예측 정확도나 전 문장의 전문가 의미 평가가 아니다. 보수적인 검토 요청도 제안을 기대한 사례에서는 실패로 집계한다.

이전 실패 결과도 보존했다. 초기에는 문서가 없는데 사건 ID를 manual_refs에 쓰거나 제안 Tool을 너무 일찍 호출하는 문제가 있었다. 실제 출처를 명시하고 typed 문서 참조·단계별 Tool 노출로 계약을 보완했다. 실패 사례를 제거하거나 프로그램 테스트 대역을 실제 LLM 결과로 보고하지 않는다.

근거 파일:

- `agent-test-results.xml`: 프로그램 테스트 결과.
- `agent-live-evaluation.json`: 최신 실제 LLM 평가와 단계별 결과.
- `agent-live-evaluation-initial.json`, `agent-live-evaluation-baseline.json`, `agent-live-evaluation-v2.json`: 보완 이전 결과.
- `agent-process-verification.json`: 실제 6개 프로세스·LLM·테스트 승인·가상 조치.
- `agent-implementation-verification.json`: 최종 검사 요약.

브라우저 접근 거부가 이전에 발생해 이번에도 다른 경로로 우회하지 않았다. API와 JavaScript DOM 스텁은 검증하되 실제 브라우저 레이아웃/모바일 클릭 검증은 미완료다.


### 이번 실행에서 확인한 수치

- 실제 PostgreSQL/HTTP 프로그램 테스트 **71개 통과, 실패·skip 0**.
- JavaScript DOM 스텁 테스트 **11개 통과**. 실제 브라우저 레이아웃 검증과 구분한다.
- 실제 LLM 평가 **20개 중 16개 기준 통과**. 구조·출처 검증을 통과한 최종 분석은 20개, 필수 Tool 충족은 19개, 금지 Tool 호출 시도는 0개다.
- 미통과 4개: 음수 전류/적은 샘플 사례는 기대한 제안 대신 보수적 검토를 선택했고, 샘플 누락 사례는 잘못된 출처를 수정하는 과정이 있었으며, 기존 조치 사례는 일부 필수 조회를 생략했다. 이 사례를 성공으로 바꾸어 집계하지 않았다.
- 완료 응답의 p95는 9,343 ms, 확인된 input/output token은 231,563/12,554다. 작은 합성 평가의 수치이며 현장 SLA·고장 정확도를 의미하지 않는다. 전체 문장에 대한 정식 전문가 의미 평가는 수행하지 않았다.
- 실제 6개 프로세스 검증에서 OpenAI가 측정 조회 → 문서 검색 → 제안 → 최종 분석 Tool을 선택했다. 승인 전 action 0, 테스트 승인 후 가상 냉각 1회, 중복 승인/측정 재전송에도 중복 조치 0을 확인했다.
- 기존 DB의 측정 24·추론 24·사건 2·조치 2·알림 5건을 migration 후에도 보존했다.
- Python 62개 파일·함수/메서드 158개의 역할·입출력 docstring을 확인했다. Ruff와 wheel 빌드를 검증했다.

## 10. 공식 API 구현 근거

[OpenAI 공식 Function Calling 문서](https://developers.openai.com/api/docs/guides/function-calling)의 Responses API 도구 호출/결과 전달 방식을 사용했다. 모델의 도구 선택과 프로그램의 실행 검증을 분리하고 strict schema·store=false·명시적 timeout을 적용했다.
