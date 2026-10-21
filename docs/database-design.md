# PostgreSQL 저장 설계 — 4단계 구현

원천 조사에서 확인한 필드만 저장한다. 아래 id·해시·버전·실행 정보는 프로그램 운영 메타데이터다. 새로운 예측 특징은 정의하지 않았다.

| 테이블 | 저장 내용 | 핵심 제약 |
| --- | --- | --- |
| source_files | SHA-256, batch label/date, 원본 크기 | SHA-256 UNIQUE |
| processed_sources | 원천 FK, 처리 버전, 실행 UUID, 원본/산출물 경로, 최종 집계 JSONB | (source_id, version) UNIQUE; run_id UNIQUE |
| cells | 셀 위치, 정책 2개, nullable 수명, Vdlin 배열, 직렬화 식별 객체 JSONB, 관측 사이클 수 | processed_source FK; (processed_id, cell_index) UNIQUE |
| cycles | 원천 위치, 실제 cycle 번호, summary JSONB, 참조 경로 JSONB, 9개 double precision 배열 | cell FK; 위치/실제 번호 각각 셀 내 UNIQUE |
| validation_issues | 규칙과 조치, 셀/사이클 위치가 든 JSONB | processed_source FK |

summary는 고정 컬럼 대신 원천 이름 그대로 JSONB에 두되 Pydantic으로 필수 8개 필드/수치 타입을 적재 전에 검사한다. 원천 이름 추적과 SQLAlchemy 모델의 단순함을 택했다. 대신 SQL 집계 시 숫자 cast가 필요하고 DB 단독으로 JSONB 내부 스키마를 강제하지 않는다. 향후 조회 패턴이 확정되면 typed scalar 컬럼/뷰로 변경할 수 있다.

파형은 SQLAlchemy ARRAY(Double)로 저장한다. 원시 파형과 보간 파형을 별도 배열로 보존하며 같은 행의 모든 배열 길이를 같게 강제하지 않는다. 수백만 샘플 행을 추가하는 대신 사이클 단위 조회를 우선했다. 배열 관계는 NumPy validation에서 검사한다. 원본의 참조 경로는 역참조 위치를 나타내는 논리 경로다.

적재는 source content의 64-bit advisory transaction lock → source label 충돌 검사 → 완료 키 조회 → 셀/사이클/이슈 삽입 → 완료 정보 갱신 → commit 순서다. lock 해시 충돌은 불필요한 직렬화만 일으키며 DB UNIQUE가 최종 무결성을 지킨다. 파형은 최대 16사이클씩 INSERT하므로 전체 파일을 Python list로 유지하지 않는다.

어떤 SQL/소스 해독 오류도 transaction을 빠져나가면 파일 전체가 rollback된다. 실패/skip을 포함한 모든 시도는 로컬 UUID 보고서에 남긴다. 실패 시도까지 별도의 DB 테이블에 남기지는 않는다. 실패 중 이슈 역시 DB rollback되지만 로컬 issues.jsonl에서 확인할 수 있다.

예시 조회:

```sql
SELECT s.batch_label, count(*) AS cells
FROM cells c
JOIN processed_sources p ON p.id = c.processed_id
JOIN source_files s ON s.id = p.source_id
GROUP BY s.batch_label;

SELECT c.cell_index, y.cycle_number,
       (y.summary->>'QDischarge')::double precision AS discharge_summary,
       cardinality(y."I") AS current_samples
FROM cycles y
JOIN cells c ON c.id = y.cell_id
ORDER BY c.id, y.cycle_index
LIMIT 10;
```

init-db는 새 테이블 생성만 지원한다. 아래 schema.sql은 SQLAlchemy 모델에서 생성한 참조용 DDL이며 마이그레이션 도구가 아니다. 운영 DB의 변경/삭제는 자동 수행하지 않는다.
