# 작업 계획서: 버그 수정 + 로컬 API 보안 강화 + PDF 추출 가속

- 작성일: 2026-10-22
- 상태: 완료 (2026-10-22 승인, 구현)
- 관련 문서: [[docs/research/2026-10-tech-trends.md]]

## 1. 목표 / 배경
v0.1.6 코드 전수 리뷰에서 실제 재현 가능한 결함과, 최신 동향 대비 개선 여지를 찾았다.
결함은 "검색 결과에 이미 지운 문서가 남는다", "특정 폴더명 아래 문서가 색인됐다가 다음 실행에 사라진다",
"초대형 문서 삭제/갱신이 SQL 에러로 실패한다" 처럼 사용자가 체감하는 정확성 문제다.
보안 측면에서는 127.0.0.1:8765 API 가 Host 헤더를 검증하지 않아 DNS rebinding 으로 외부 웹페이지가
문서 내용(검색 결과)을 읽어갈 수 있다.

## 2. 범위
- 포함:
  - B1. 폴더 삭제 시 하위 문서가 인덱스에 남음 (watchdog Windows 는 삭제를 항상 확장자 없는
    `FileDeletedEvent(<폴더경로>)` 로만 보고 -> 현재 확장자 필터에서 버려짐). 확인: watchdog 6.0.0
    `read_directory_changes.py:97`.
  - B2. `is_ignored_path` 가 감시 루트의 *상위* 경로 성분까지 검사. 예: `D:/out/reports` 를 감시하면
    os.walk 는 루트 하위만 가지치기하므로 색인은 되지만, watcher 이벤트는 전부 버려지고 다음 시작 시
    `purge_deleted_files` 가 전부 삭제 -> 매 실행마다 색인/삭제 반복.
  - B3. 청크 ID 를 `IN (?,?,...)` 한 문장에 모두 바인딩 -> SQLite 변수 한도 32766 초과 시
    `too many SQL variables`. `MAX_CHUNKS_PER_DOC=50000` 이므로 대형 PDF 에서 삭제/재색인 실패.
  - B4. `/api/index/scan` (async) 가 이벤트 루프에서 DB purge 를 블로킹 실행하고, 워커 스레드가
    순회 중인 `walker` / `db_files` 를 다른 스레드에서 교체 -> 레이스. 트레이 "Re-index Now" 경로.
  - B5. 중첩/중복 감시 폴더(예: `C:/Users/me` + `C:/Users/me/Documents`) -> 같은 파일을 두 번 스캔,
    observer 이중 등록으로 이벤트 중복, 대형 PDF 텍스트 추출 중복.
  - B6. 블로킹 DB 호출을 하는 async 핸들러(`GET /api/settings`, `GET /api/model`) -> sync 로 전환.
  - B7. `delete_document_by_path`, `get_all_indexed_files`, `get_monitored_dirs`, `save_monitored_dirs`
    예외 시 커넥션 누수 -> try/finally.
  - B8. 프론트: 결과 0건에서 ArrowDown -> selectedIdx = -1.
  - S1. 로컬 API Host/Origin 검증 미들웨어 (DNS rebinding + 단순 요청 CSRF 차단).
  - P1. PDF 텍스트 추출을 PyMuPDF(이미 번들됨) 우선, pypdf 폴백으로 전환.
  - T1. 회귀 테스트 `tests/` (표준 unittest, 임시 DB 사용, 신규 의존성 없음).
- 제외(이번에 안 하는 것):
  - 임베딩 모델 교체/추가(Granite R2 등) -- 한국어 품질 검증 데이터가 없어 조사 노트에만 기록.
  - sqlite-vec metadata column 기반 필터 KNN -- vec0 스키마 변경 + 전체 재색인 필요. 조사 노트에 기록.
  - 세션 토큰 인증(Rust->backend env + 프론트 헤더) -- S1 로 원격 공격면은 막히고, 남는 위협은
    로컬 악성 프로세스(이미 사용자 권한)라 효용 대비 변경 범위가 큼. 오픈 이슈로 남김.
  - 청킹 전략 변경(문장 경계) -- chunk hash 가 바뀌어 수정 파일 전부 재임베딩. 별도 작업.
- 전제 조건 / 의존성: Python 3.12, pymupdf 1.27(이미 설치/번들), sqlite 3.43.

## 3. 접근안 비교
### S1 로컬 API 보호
| 안 | 요약 | 장점 | 단점 | 리스크 |
|---|---|---|---|---|
| A | 현상 유지(CORS 만) | 변경 없음 | CORS 는 *읽기*만 막음. 단순 POST 는 전송됨. rebinding 무방비 | 문서 내용 유출 |
| B | Host 화이트리스트 + Origin 화이트리스트 미들웨어 | 백엔드만 수정, 프론트/Rust 무변경 | 로컬 프로세스는 못 막음 | 정상 클라이언트 Host 누락 시 오탐 |
| C | B + 실행별 토큰 헤더 | 로컬 다른 앱도 차단 | Rust/프론트/트레이 모두 수정 | dev.ps1 경로 등 회귀 |

**선택: B.** rebinding/드라이브바이 웹 공격을 백엔드 단독 변경으로 차단. C 는 오픈 이슈.
Origin 이 *없는* 요청(트레이 reqwest, curl)은 허용 -- 브라우저는 교차 출처 POST 에 항상 Origin 을 붙임.

### P1 PDF 추출
| 안 | 요약 | 장점 | 단점 |
|---|---|---|---|
| A | pypdf 유지 | 변경 없음 | 수천 페이지 PDF 에서 수 분 |
| B | PyMuPDF 우선, 실패/미설치 시 pypdf | 이미 번들(OCR용), 일반적으로 수십 배 빠름 | 추출 텍스트가 달라짐 |

**선택: B.** 스캐너는 크기/mtime 이 변한 파일만 다시 추출하므로, 기존 미변경 PDF 는 재임베딩되지 않는다.
변경된 PDF 와 모델 전환 시에만 새 추출기를 탄다.

### B2 무시 폴더 판정
감시 루트 기준 *상대 경로* 성분만 검사하는 `is_ignored_path(path, roots)` 로 변경.
루트에 속하지 않는 경로는 기존처럼 전체 성분 검사(보수적).

## 4. 구현 단계
1. `src/config.py`: `is_ignored_path(path, roots=None)` 상대 판정, `normalize_watch_dirs()` (정규화 + 중복/중첩 제거).
2. `src/database.py`: ID 배치 삭제 헬퍼(500개 단위), `delete_documents_under(prefix)`, 커넥션 try/finally, `import re` 상단 이동.
3. `src/watcher.py`: 폴더 삭제 처리, 루트 기준 무시 판정, 수동 재스캔을 워커 스레드 플래그로 위임.
4. `api.py`: Host/Origin 미들웨어, async->sync, 감시 폴더 정규화 적용, `/api/index/scan` 스냅샷.
5. `src/parser.py`: PyMuPDF 우선 추출.
6. `frontend/src/App.tsx`: 빈 결과 키 이동 보정.
7. `tests/test_regressions.py`: B1~B5, S1, P1 회귀 테스트.
8. README/DESIGN/CHANGELOG 반영.

## 5. 위험 및 실패 경로
- S1 오탐: Tauri webview 가 예상 외 Origin 을 보내면 검색 전부 403. -> 허용 목록은 기존 CORS 목록과 동일하게 유지, 403 사유를 로그.
- B1 과삭제: 확장자 없는 *파일* 삭제 이벤트를 폴더로 오인 -> prefix + "/" 로만 매칭하므로 해당 이름의 폴더 하위만 삭제. 동명 폴더가 실제로 남아 있으면(`os.path.isdir`) 건너뜀.
- P1: PyMuPDF 가 특정 PDF 에서 예외 -> pypdf 폴백, 둘 다 빈 텍스트면 기존 OCR 경로.
- 롤백: 각 항목이 독립 커밋 가능한 크기. DB 스키마 변경 없음(하위 호환).

## 6. 검증 방법
- `python -m unittest discover -s tests -v` 전부 통과.
- FastAPI TestClient 로 Host=evil.com -> 400/403, Origin=https://evil.com -> 403, 정상 요청 200.
- 실제 백엔드 기동(`python api.py`) 후 /api/status, /api/search 응답 확인.
- 프론트 `npm run build`(tsc) 통과.

## 7. 오픈 이슈
- [ ] 실행별 토큰 인증(안 C) 도입 여부.
- [ ] Granite Embedding 97M Multilingual R2 를 한국어 코퍼스로 평가 후 모델 레지스트리 추가 여부.
- [ ] vec0 metadata column(확장자/수정일) 으로 필터 KNN 전환 -- 재색인 마이그레이션 설계 필요.
- [ ] 한국어 2음절 검색어는 trigram FTS 로 가속 불가(LIKE 풀스캔) -- bigram 보조 인덱스 검토.

## 8. 진행 로그
- 2026-10-22: 전체 코드 리뷰, watchdog 동작 소스 확인, SQLite 변수 한도(32766) 확인, 동향 조사. 계획 초안 작성.
- 2026-10-22: 사용자 승인 후 구현 완료. 회귀 테스트 18건 통과, `npm run build` 통과.
  실제 백엔드(`python api.py`, 사용자 DB) 점검: 검색 200, Host=attacker.example -> 421, Origin=https://attacker.example -> 403, Origin 없는 트레이 스캔 -> 200.
  - 관찰: 시작 시 purge 로 167건 삭제(235 -> 88). 앱 로그 마지막 실행이 2026-06-24 라 그 사이 삭제/이동된 파일로 판단.
    새 무시 규칙은 기존 규칙의 부분집합(루트 하위만, 파일명 제외)이라 기존 코드가 남겼을 파일을 지울 수 없음.
    디스크에 있지만 DB 에 없는 233건은 6월 이후 신규 파일이 대부분이며, 점검 중 백엔드를 중단해 다음 실행 때 색인됨.
  - 결정 변경 없음. 오픈 이슈(토큰 인증, Granite R2 평가, vec0 metadata 필터, 2음절 한국어 FTS)는 유지.
- 2026-10-22: 적대적 리뷰 4회 반복 (라운드 4에서 신규 결함 없음).
  - R1: 프로덕션에서도 Vite 기본 포트 Origin(`http://localhost:5173`) 신뢰 -> 다른 로컬 Vite 앱이 검색 결과 열람 가능.
    소스 실행 또는 디버그 Tauri 빌드가 넘기는 `COGNIFIND_DEV_ORIGIN=1` 일 때만 허용 (`compute_allowed_origins`).
  - R1: 설정 UI가 백엔드 정규화 결과를 반영하지 않음 + 상태 업데이터 안의 PUT (StrictMode 이중 호출) -> 응답 반영, 시퀀스 번호로 늦게 온 응답 무시.
  - R2: 폴더 삭제 처리로 인해 비문서 삭제 이벤트마다 documents 전체 스캔 (npm install 시 수만 건) ->
    무시 폴더 하위 삭제는 DB 생략, 나머지는 LIKE 사전필터('!' 이스케이프, `_`/`%` 리터럴 처리) 후 Python 정확 비교.
  - R3: 감시 루트 자체 경로를 루트 밖으로 판정 -> 루트 삭제 시 상위에 무시 이름이 있으면 정리 누락. 루트 일치 시 비무시로 처리.
  - 검증: 회귀 테스트 23건, 실제 watchdog Observer 통합 재현(루트가 `out/` 아래, node_modules 200개 삭제 + 하위 폴더 삭제), `npm run build`, `cargo check`.
