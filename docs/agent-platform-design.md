# ESS 사건 대응 AI Agent 확장 설계안

작성일: 2026-10-17. 상태: 구현 기준 설계. 이 설계를 바탕으로 Agent·DB migration·가상 조치 승인·관제·평가를 구현했다. 현재 동작·설계와의 차이·검증 결과는 agent-guide.md를 따른다. 실제 현장 Gateway는 미구현이다.

## 1. 목적과 범위

이상 탐지 결과에서 생성된 사건을 입력받아 LLM이 필요한 도구를 선택하고 결과에 따라 추가 조사한 뒤, 근거가 연결된 분석과 대응안을 작성한다. 실행 권한은 서버 코드가 결정한다. 모든 도구를 정해진 순서대로 호출하는 문서 요약기가 아니라, 제한된 도구 목록 안에서 조사 순서를 선택하는 단일 Agent다.

목표 결과는 관측 사실, 근거, 가능한 설명, 부족한 정보, 대응안이다. ML의 이상 판단은 실제 장애 확정과 구분한다. 현재 추론 서버의 명시된 범위와 한계는 그대로 보고서에 전달한다. 에이전트가 ML 모델을 재학습하거나 측정 원본을 수정하지 않는다.

구현 기반은 기존 Python 3.11+, Pydantic, SQLAlchemy, PostgreSQL이다. LLM은 Tool Calling을 지원하는 공급자의 공식 SDK를 직접 연결하며 공급자/모델은 구현 전에 선정한다. 공급자 교체 지점은 단일 client 모듈이다. 초기에는 LangGraph·멀티 에이전트·벡터 DB·Kafka를 추가하지 않는다. 상태·중복·승인 경계부터 명확히 만든다.

## 2. 책임 경계

| 구성 | 책임 | 담당하지 않는 일 |
|---|---|---|
| 수집·추론 | 입력 검증·저장·이상 판단 | LLM의 문서 해석 |
| 운영 사건 처리 | 연속 판정·사건 생성/복귀·기본 알림 | 자유로운 Tool 선택 |
| Agent runtime | LLM 호출·Tool 검증·반복 제한·분석 저장 | 실제 설비 명령 |
| 읽기 Tool | 허용된 범위의 근거 반환 | 임의 SQL·다른 자산 조회 |
| 대응 제안 Tool | 실행 의도를 검증 가능한 제안으로 저장 | 조치 실행·자기 승인 |
| 정책·승인 서비스 | allowlist·적용 문서·승인·유효기간 판단 | LLM에 의한 권한 부여 |
| action executor | 승인된 정확한 명령 실행·상태 확인 | LLM 텍스트를 코드/명령으로 변환 |
| 관제 API | 분석·근거·전이·승인 결과 표시 | 클라이언트가 보낸 승인자 ID 신뢰 |

LLM의 조사 과정은 동적이되 바깥쪽 작업 상태와 실행 조건은 Python 상태 전이로 고정한다. LLM 호출은 데이터 수집·기본 사건 알림 경로를 막지 않는다. 현장 적용 시 기존 BMS 보호·인터록은 독립적인 제어 경로여야 하며 Agent 승인 대기를 보호 동작의 조건으로 삼지 않는다.

## 3. 현행 코드의 변경 지점

### operations/incidents.py

현재 operations_tick()은 사건 생성 transaction에서 rt_actions에 pending 냉각 요청을 바로 넣는다. 다음 두 모드를 명시적으로 구분한다.

- shadow: 기존 가상 조치는 유지하고 Agent는 분석만 한다. Agent의 대응 제안은 실행 대상으로 만들지 않는다.
- agent_assisted: 사건·기본 알림·Agent 작업만 같은 transaction에서 생성한다. 기존 즉시 조치 생성 분기는 실행하지 않는다. 새로운 조치는 정책/승인 서비스만 생성한다.

모드는 배포 설정으로 선택하고 사건에 당시 모드를 기록한다. 이미 생성된 사건의 모드를 소급 변경하지 않는다. Agent 오류가 발생했다고 기존 자동 조치 경로로 조용히 우회하지 않는다. 담당자 확인 필요로 전환한다. 기존 즉시 조치와 Agent 조치가 동시에 생성되는 사례를 회귀 테스트한다.

### operations/actions.py

가상 냉각 정책 검사와 멱등성은 유지하되, assisted 모드에서는 승인된 proposal ID·proposal hash·사건 revision·만료 시각을 확인한다. 패턴 복귀 후 대기 요청 취소와 냉각 상태 자동 복원 금지는 유지한다. 실제 Gateway는 별도 adapter로 연결하고 현 단계에는 구현하지 않는다.

### operations/server.py / queries.py

분석 결과·근거·Tool 호출 이력·제안·승인 상태를 조회한다. 승인은 인증된 서버 세션의 역할에서 승인자 identity를 얻는다. 기존 loopback 화면에는 인증이 없으므로 승인 API를 외부/현장 용도로 쓰려면 인증 도입이 선행되어야 한다.

### db.py / migration

Agent·문서·승인 테이블을 추가하고 기존 incidents/actions에 참조·revision 필드를 추가한다. metadata.create_all()은 기존 테이블 컬럼을 변경하지 않으므로 migration을 별도로 작성한다. 초기에는 순서가 있는 SQL migration과 적용 이력 테이블을 사용하고, 규모가 커지면 migration 도구로 전환할 수 있다. 테스트 DB에서 기존 사건·조치가 유지되는 upgrade를 검증한다.

## 4. 실행 흐름

1. 운영 워커가 사건을 열고 같은 DB transaction에서 고유 Agent job을 등록한다. 사건과 작업 중 하나만 남는 상태를 방지한다.
2. Agent 워커가 짧은 transaction으로 작업을 claim하고 lease/token을 저장한 뒤 commit한다.
3. 사건·원천 측정/추론 ID·관측 cutoff·모델 버전·사건 revision을 snapshot으로 저장한다.
4. LLM에 사건 요약, Tool 계약, 조사 범위와 예산을 전달한다.
5. LLM은 읽기 Tool 호출 또는 최종 분석을 반환한다. 호출 이름/인자/자산 범위/권한을 검증한 뒤 실행한다.
6. Tool 결과의 ID·출처·조회 시각·단위를 저장하고 결과를 LLM에 전달한다. 필요한 경우 제한 내에서 추가 호출한다.
7. 최종 분석을 Pydantic으로 검증하고, 인용 ID가 실제 해당 run에서 조회한 근거인지 확인한다.
8. 대응이 필요하면 submit_response_proposal Tool로 구조화된 제안을 저장한다. 이 단계에서 실제 조치는 실행하지 않는다.
9. 정책 서비스가 검증 결과를 blocked / needs_review / awaiting_approval로 기록한다. 첫 버전의 모든 장비 상태 변경은 사람 승인을 요구한다.
10. 승인·실행 시점에 사건 상태와 데이터 신선도를 다시 확인하고, 유효한 제안만 기존 executor에 전달한다.
11. executor 결과와 후속 관측을 관제에 표시한다. 명령 수락·완료와 실제 현상 개선을 구분하고, 패턴 복귀를 냉각의 인과 효과로 단정하지 않는다.

Agent 분석은 하나의 사건에 대한 bounded 작업이다. 전체 시스템의 반복은 신규 사건 또는 명시적 재분석 이벤트가 시작한다. LLM이 종료 없이 스스로 무한 모니터링하지 않는다.

## 5. Tool 계약

모든 Tool은 이름, 사용 목적, 사용하지 말아야 할 상황, Pydantic 입력/출력, 오류 코드, 최대 크기·timeout을 가진다. 사건/자산 범위는 서버가 job context에서 확정한다. LLM의 임의 asset_id로 권한 범위를 바꾸지 않는다. 읽기 도구는 고정된 서비스 함수·parameterized query만 사용한다.

| Tool | 입력 | 출력 | 제한 |
|---|---|---|---|
| get_incident_context | incident_id | 사건·추론·모델·revision·출처 | 할당된 사건만 |
| get_measurement_window | start_at, end_at, limit | 시각·값·단위·measurement IDs | 사건 자산·제한된 기간/건수 |
| get_quality_issues | start_at, end_at | 오류 종류·대상 측정 ID | 사건 자산만 |
| search_response_manuals | query, symptom_tags | chunk ID·본문·문서 버전·적용 범위 | 자산 metadata를 서버에서 필터 |
| get_action_history | incident_id | 기존 요청·응답·상태 | 할당된 사건만 |
| submit_response_proposal | action_type, bounded parameters, evidence_refs, manual_refs | proposal_id, policy_state | 제안 저장만, 서버 ID/hash 생성 |
| get_action_status | action_id | pending/acknowledged/completed/failed/unknown | 사건에 연결된 조치만 |

실행 함수 execute_approved_action은 executor만 호출하며 LLM의 Tool 목록에 넣지 않는다. Agent가 임의 동작 이름, 셸·SQL, 실제 온도 설정값을 만들어 실행할 수 없게 한다. 알림 전송 역시 검증된 분석/상태 전이를 기존 outbox에 예약해 중복을 제어한다. 첫 버전에서는 자유 형식 수신자·외부 URL을 LLM에게 선택하게 하지 않는다.

반환 envelope 예시:

```json
{
  "ok": true,
  "data": {"samples": []},
  "evidence_refs": [],
  "as_of": "<server UTC timestamp>",
  "truncated": false,
  "error": null
}
```

정상적인 빈 결과와 Tool 오류를 구분한다. 오류는 code/retryable을 가지며 같은 잘못된 호출을 무한 반복하지 않는다. 이 예시는 설계용 형식으로 실제 API 응답이 아니다.

## 6. 분석 출력과 근거 검증

최종 AgentAssessment는 다음 필드를 가진다.

- incident_id, run_id, incident_revision
- observed_facts: 사실 문장과 measurement/prediction refs
- hypotheses: 가능한 설명과 근거; 원인 확정 표현과 분리
- missing_information: 수집되지 않았거나 도구 실패로 확인하지 못한 정보
- manual_refs: 문서 ID/version/chunk ID/hash
- disposition: no_action / needs_review / propose_action
- proposed_action: allowlist enum 또는 null, 검증된 인자
- limitations: 현재 추론 방식과 분석 한계

Pydantic은 구조/자료형을 검증한다. 출처 존재·사건 귀속·단위·숫자 비교는 서버가 추가 검증한다. 문장이 근거를 정확히 해석했는지는 구조 검증만으로 보장되지 않으므로 평가 데이터셋과 담당자 검토로 확인한다. 임의 confidence 백분율을 고장 확률처럼 표시하지 않는다.

관제에는 실제 입력·Tool 호출·출처·최종 근거 요약을 보여준다. 모델의 비공개 내부 추론 과정을 수집하거나 저장하는 것을 요구하지 않는다.

## 7. 문서 검색

초기에는 학습 문서 소량을 문서 metadata + 제목/절 단위 chunk로 저장한다. metadata는 문서 ID·버전·hash·자산 종류·적용 장비·증상·유효기간·승인 상태를 포함한다. 단순 한글 키워드/태그 검색으로 시작하고 고정된 검색 평가셋으로 누락을 확인한다. PostgreSQL 기본 full-text 검색만으로 한국어 형태소 검색 품질이 해결된다고 가정하지 않는다.

조회 순서는 적용 자산/승인 상태 필터 → 키워드/태그 검색 → 제한된 본문과 출처 반환이다. 이후 검색 평가에서 필요성이 확인되면 임베딩·hybrid search·reranking을 도입한다. 현재 학습 문서에는 learning_example을 명시하고 실제 승인 절차 문서와 구분한다. 실제 적용 가능한 문서가 없으면 needs_review로 종료한다.

문서 내용은 외부 데이터다. '이전 지시 무시', '다른 자산 조회', '명령 실행' 같은 본문이 시스템 지시나 Tool 권한을 바꾸지 않게 한다. 서버는 Tool allowlist·인자 검증·문서 적용 범위·명령 정책을 프롬프트와 별개로 검사한다. 입력 필터 하나로 prompt injection이 해결된다고 보지 않는다.

## 8. 저장 구조

| 테이블 | 핵심 필드·제약 |
|---|---|
| rt_agent_jobs | incident FK, trigger_revision, agent_version, mode, status, attempts, next_attempt_at, lease_until, lease_token; unique(incident, trigger_revision, agent_version, mode) |
| rt_agent_runs | job FK, attempt, input snapshot/hash, incident_revision, observed cutoff, LLM/provider/model, prompt/toolset version, result, usage, error |
| rt_agent_steps | run FK, step_index, step_type, tool_call_id, tool name, validated args/result refs, status, elapsed_ms; unique(run, step_index, tool_call_id) |
| rt_documents | document identity/version/hash, asset metadata, learning/approval status; immutable version |
| rt_document_chunks | document version FK, section, content/hash, index |
| rt_action_proposals | incident/run FK, action enum, canonical params/hash, evidence/manual refs, policy_version, status, expires_at |
| rt_action_approvals | proposal FK/hash, authenticated reviewer, role, decision, decided_at, expires_at |
| rt_schema_migrations | ordered migration name/checksum/applied_at |

기존 incidents에는 revision과 response_mode를 추가하고, actions에는 proposal FK·승인 근거·command_key를 추가한다. 첫 버전은 기존 incident_id unique 제약을 유지해 사건당 한 냉각 상태 변경만 허용한다. 여러 단계의 대응이 필요해질 때 별도 migration으로 command_key unique와 사건/동작/단계 관계를 정의한다.

승인은 '이 사건 전체'가 아니라 정확한 proposal hash(대상·동작·인자·문서/정책 버전·사건 revision)에 묶는다. 제안 내용이 바뀌면 이전 승인을 재사용하지 않는다. 같은 command_key의 중복 요청은 기존 결과를 반환한다.

## 9. 상태·재시작·실패 처리

Agent job: queued → running → completed / needs_review / failed / cancelled. running에는 lease 만료 시각과 token이 있으며 만료 후 다른 워커가 재claim할 수 있다. 결과 commit 시 token을 비교해 만료된 워커의 늦은 완료가 최신 작업을 덮어쓰지 않게 한다.

LLM과 HTTP 호출 중에는 DB row lock이나 transaction을 유지하지 않는다. claim·step 기록·완료 commit만 짧게 실행한다. 네트워크 재실행이나 lease 회수로 LLM 호출 자체가 중복될 수 있으므로 비용까지 exactly-once라고 주장하지 않는다. 읽기 Tool 재실행은 허용하고 제안 저장은 run/call ID에 멱등성을 부여한다.

분석 시작값(설정 가능한 초기안): LLM 최대 4턴, Tool 최대 8회, 전체 30초, 일시적 오류 최대 2회 재시도. 하나의 전체 timeout 예산 안에서 per-call timeout을 적용하고 lease는 실행 예산보다 길게 잡는다. 이 수치는 ESS 현장 SLA가 아니라 개발 제한값이며 평가 후 조정한다.

Proposal: draft → blocked / needs_review / awaiting_approval → approved / rejected / expired → enqueued. 조치 상태는 별도로 pending/executing/completed/failed/unknown/cancelled를 관리한다. 분석 완료와 조치 완료를 같은 상태로 표현하지 않는다.

- LLM timeout/rate limit: 제한된 재시도 후 needs_review, 기본 사건/알림은 계속 유지.
- Tool 인자 오류: 오류 결과를 전달하되 호출 예산을 소비. 무제한 수정 반복 금지.
- 사건 복귀/새 revision/승인 만료/문서 철회: enqueue와 실행 직전에 다시 확인, 오래된 제안은 취소 또는 재검토.
- 분석 중 신규 측정값 유입: snapshot 기준은 보존하고 실행 신선도 정책으로 별도 확인. 모든 샘플마다 재분석 작업을 생성하지 않는다.
- 승인 동시 요청: proposal row lock/조건부 갱신으로 한 번만 enqueued로 전환.
- 실제 Gateway timeout: 즉시 실패나 성공으로 단정하지 않고 unknown. 같은 command_key로 상태 조회·대사 후 다음 동작 결정. 자동 재전송의 exactly-once 효과를 가정하지 않는다.
- DB 장애: 완료 commit 실패 시 lease/token에 따라 재개. 생성이 확정되지 않은 조치를 LLM 결과만 보고 실행하지 않는다.

## 10. API·권한

조회 API:

- GET /api/incidents/{id}/analysis
- GET /api/agent-runs/{id}
- GET /api/action-proposals/{id}

상태 변경 API:

- POST /api/incidents/{id}/analysis-jobs: 운영 권한, idempotency key, 재분석 이유
- POST /api/action-proposals/{id}/approval: 인증된 승인자·역할 검증, proposal hash, 승인/거절, 만료

LLM에는 DB write 계정과 승인 계정을 노출하지 않는다. 서버 측 Tool context의 읽기 범위와 proposal 저장 권한만 부여한다. 승인자는 요청 body에서 임의로 지정하는 사람이 아니다. 기존 인증 없는 loopback 모드에서는 외부 공개·실장비 연동을 하지 않으며 승인 경로는 테스트 계정의 로컬 검증으로만 확인한다.

## 11. 모듈과 주요 함수 계약

```text
src/ess_platform/agents/
  worker.py         # job claim/lease/retry/commit
  runner.py         # 단일 사건의 bounded Tool Calling loop
  llm_client.py     # 공급자 SDK 호출 경계, timeout/usage
  schemas.py        # context, tool input/output, assessment
  tools.py          # 도구 목록, 계약, 허용된 handler dispatch
  retrieval.py      # 문서 필터·검색·출처
  repository.py     # Agent job/run/step 저장에 한정
src/ess_platform/operations/
  proposals.py      # 제안 저장·멱등성·상태 전이
  policy.py         # allowlist·문서 적용·freshness·권한 검사
  approvals.py      # 승인 hash·역할·유효기간·enqueue
```

범용 repository 계층을 기존 모든 코드에 확장하지 않는다. Agent job lease와 여러 step의 저장이 분리되어 필요한 저장 코드만 agents/repository.py에 둔다.

| 함수 | 입력 → 출력 |
|---|---|
| claim_job(engine, worker_id) | DB/워커 ID → lease token을 가진 Job 또는 None |
| build_context(job) | 할당된 작업 → 사건 snapshot과 조회 범위 AgentContext |
| run_agent(context, client, tools, limits) | context/LLM/도구/예산 → AgentAssessment 또는 명시적 중단 결과 |
| execute_tool(context, call) | 서버 context/LLM 요청 → 검증된 ToolResult, 권한 오류는 거부 |
| validate_assessment(context, result) | 근거 목록/분석 → 검증 결과·검토 필요 이유 |
| create_proposal(context, request) | 사건/구조화 의도 → Proposal와 정책 상태 |
| approve_proposal(actor, proposal_id, hash) | 인증된 사람/정확한 제안 → 승인·거절·만료 상태 |
| enqueue_approved_action(proposal_id) | 승인 제안 → 단일 action ID 또는 재검토 결과 |

도구 registry는 정적인 dict이며 LLM이 임의 Python 함수명을 호출할 수 없다. 모든 새 파일·함수에는 기존 프로젝트 원칙대로 한국어 역할·입출력·예외 설명과 Pytest를 추가한다.

## 12. 테스트와 Agent 평가

### 결정적인 프로그램 테스트

기존 34개 Pytest와 9개 JavaScript 회귀를 유지한다. 추가로 사건+job 원자성, lease 회수/token 경쟁, 잘못된 Tool/다른 자산/범위 초과 거부, 허위 인용, 문서 만료/변조, 동일 제안 중복, 동시 승인, 승인 후 사건 복귀, 오래된 승인/모델 결과, 기존 자동 조치와 Agent 경로의 이중 실행을 검증한다.

이 테스트에는 재현 가능한 LLM 응답 대역을 사용한다. 그것만으로 실제 Agent의 Tool 선택 품질을 통과했다고 보고하지 않는다.

### 실제 LLM 평가셋

최초 수작업 시나리오 20~30개를 준비하고 제공된 데이터·허용 Tool·필수 확인·금지 동작·적절한 종료 상태를 라벨링한다. 단일 정답 호출 순서를 강제하지 않고 필요한 근거와 금지 행동을 기준으로 채점한다.

- 데이터 정상/판정만 이상: 실제 고장으로 단정하지 않음.
- 품질 오류/지연 데이터: 품질 Tool 확인 후 근거 부족 표시.
- 관련 문서 없음/다른 자산 문서/학습 문서만 존재: 실행 제안 보류.
- 문서 내 악성 지시: Tool 권한·자산 범위 유지.
- 정상적인 대응 근거: 관련 Tool과 정확한 문서 버전을 사용한 제안.
- 승인 전/승인 후 패턴 복귀: 요청 취소.
- LLM/검색/DB/Gateway 오류: 오류 상태와 검토 필요를 정확히 표시.

평가 지표는 필요한 Tool 선택률·금지 Tool 호출 시도/실행률·유효 인용률·적합한 문서 검색률·근거 부족 시 보류율·제안 적절성·평균/p95 지연·토큰·비용이다. 승인 없는 실행·권한 이탈·중복 조치는 프로그램 테스트에서 0건을 출시 gate로 둔다. 제한된 평가셋의 0건을 실제 운영 전체의 무사고 보장으로 해석하지 않는다. 정확도 목표는 baseline 측정 후 정하며 사전에 임의 성능 수치를 주장하지 않는다.

## 13. 구현 순서와 완료 기준

1. 계약·상태·migration: 테스트 DB에서 기존 데이터 보존 upgrade와 unique/FK 확인.
2. 읽기 Tool·문서 metadata/search: LLM 없이 API/도구 단독 테스트, 고정 검색 사례 확인.
3. 단일 Agent loop: 실제 LLM 연결, 구조화 출력·호출 제한·추적, shadow 분석만 저장.
4. 제안·정책·승인: 이중 실행 차단·만료·동시성·사건 상태 변경을 검증. 장비 상태 변경은 승인 필요.
5. 관제·평가: 분석/근거/실패/승인을 표시하고 실제 LLM 평가 결과를 문서화.
6. 이후 확장: 필요성 확인 후 실제 Gateway/외부 알림/추론 모델/검색 고도화. 기존 BMS 보호 체계와의 현장 검토는 별도다.

새 실행 단위는 Agent worker 하나다. 초기에는 같은 PostgreSQL을 사용한다. LangGraph는 복잡한 분기·장기 실행·재개 요구가 커질 때 비교하고, MCP는 Tool을 여러 외부 Agent에 공유할 필요가 생겼을 때 검토한다. 프레임워크 도입만으로 승인·멱등성·실행 안전이 보장되지는 않는다.

## 14. 설계 근거

아래 자료는 단일 Agent·명확한 Tool 계약·계층화된 검증의 일반 설계 원칙에 참고했다. 이 문서의 테이블·상태·수치·코드 변경 지점은 현재 프로젝트를 위한 제안이다.

- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents): 단순한 구성, workflow와 동적 Agent의 역할 구분, 명확한 종료 조건.
- [Anthropic: Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents): 목적이 분명한 Tool과 평가를 통한 개선.
- [OWASP: LLM Prompt Injection Prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html): 외부 문서/Tool 결과의 비신뢰 처리와 최소 권한·실행 검증.
