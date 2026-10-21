# ESS Control dashboard

MVP의 실제 `report.json` / `events.jsonl`을 읽어 표시하는 정적 대시보드입니다.

## 로컬 실행

프로젝트 루트에서:

```sh
python3 dashboard/export_data.py
python3 -m http.server 8765 --directory dashboard/dist --bind 127.0.0.1
```

브라우저에서 http://127.0.0.1:8765 를 엽니다. 별도 npm 설치가 필요하지 않습니다.

## 파일과 데이터 흐름

```text
runs/mvp-validation/<run_id>/report.json + events.jsonl
          ↓ export_data.py (허용 필드만 추출)
dashboard/dist/data.json
          ↓ app.js (fetch → 윈도 위치별 상태 계산)
측정 추이 / 이상 점수 / 사건 상세 / 모의 조치 이력
```

- `export_data.py`: `export_run(report_path: Path) -> dict`는 보고서와 같은 폴더의 이벤트를 읽어 로컬 경로를 제외한 실행 정보를 반환합니다. `main() -> None`은 검증 문서에 기록된 네 실행을 JSON으로 내보냅니다. 오류는 숨기지 않고 실행 실패로 표시합니다.
- `dist/index.html`: 한국어 화면과 의미 있는 HTML 구조, 네 페이지, 상세 대화상자.
- `dist/style.css`: 녹색 운영 화면, 반응형 레이아웃, 키보드 포커스.
- `dist/app.js`: 실행 선택, 재생/일시정지/초기화/속도/슬라이더, 온도·전압·전류 차트, 사건 검색·필터·상세, 현재 시점 JSON 다운로드.
- `.openai/hosting.json`: 정적 사이트 배포 설정.
- `../tests/test_dashboard_export.py`: 집계와 출처 일치, 로컬 경로 제외, 누락 파일 오류 테스트.
- `../tests/frontend/test_replay_dashboard.cjs`: DOM 스텁을 통한 재생·초기화·배치 변경·사건 상세·필터 테스트. 브라우저 렌더링 테스트는 아닙니다.

## 학습할 로직

`indexRun()`은 예측 이벤트가 나올 때 윈도 번호를 증가시켜 후속 사건·조치를 해당 윈도에 연결합니다. `visibleEvents()`는 슬라이더 위치 이전 기록만 반환합니다. `incidents()`는 사건 ID로 문서·조치·패턴 복귀를 묶습니다. 원래 JSONL 순서를 유지하므로 과거 재생에서 미래의 사건 결과가 나타나지 않습니다. SVG 차트의 가로축은 윈도 순서이며, 실제 경과 시간을 등간격으로 표현한 것이 아닙니다.

## 검증

```sh
uv run pytest tests/test_dashboard_export.py -q
node --test tests/frontend/test_replay_dashboard.cjs
node --check dashboard/dist/app.js
```

구현 시 Python 3개, JavaScript 5개 테스트를 실행해 통과했습니다. 브라우저 미리보기는 접근 거부로 실행하지 못했으므로 실제 화면 배치·모바일 조작 검증은 미완료입니다.

## 데이터와 한계

Batch 1 원본 평가, Batch 1·2·3 이상 주입 평가의 네 실제 과거 실행을 제공합니다. 기본 화면은 Batch 1 이상 주입 실행의 최종 위치이며, 재생을 시작하면 처음부터 진행합니다. Batch 1은 평가 윈도 138개, 이상 윈도 16개, 사건 4건입니다. 카운트는 선택한 실행과 재생 위치에 따라 달라집니다.

이상 주입 실행은 평가 구간의 일부 온도 값에 원천 단위 100을 더한 실험입니다. 원천 MAT는 변경하지 않습니다. 측정 단위는 이 화면에서 미확정으로 표시합니다. 이상 점수는 고장 확률이 아니며, 패턴 복귀는 냉각 효과나 실제 장애 복구를 증명하지 않습니다. 문서는 학습 예시이고, 조치는 가상 장비, 알림은 기존 콘솔 로그입니다.

실시간 수집, 실제 BMS·DB·설비 연결, 새 ML 추론, 자동 학습, 사내 문서 검색, 사용자 인증 서버는 구현하지 않았습니다. 데이터는 export 실행 시 갱신되는 스냅샷입니다. 새 실행을 추가하려면 exporter의 입력 실행 목록을 변경하고 다시 내보냅니다. 외부 Google Fonts가 차단되면 시스템 글꼴로 표시합니다.

향후 API 연동 시 `data.json` 로딩 부분을 조회 API로 바꾸고, 사건 계산과 승인된 대응 상태는 서버에서 제공하도록 확장합니다. 실제 설비 명령은 대시보드에서 직접 실행하지 않고 대응 서버의 권한·정책 검증을 거치도록 설계합니다.
