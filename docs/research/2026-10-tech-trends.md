# 연구 노트: 로컬 시맨틱 검색 기술 동향 (2026-10) 과 CogniFind 적용성

- 작성일: 2026-10-22
- 상태: 결론 도출 (일부 미확정)
- 관련: [[docs/plan/2026-10-22-bugfix-and-hardening.md]]

## 질문
v0.1.6 스택(all-MiniLM-L6-v2 / multilingual-e5-small ONNX, sqlite-vec 0.1.9 brute-force KNN + trigram FTS5 하이브리드,
pypdf 추출, 127.0.0.1 무인증 REST)이 2026-10 기준으로 어디가 뒤처졌고, 무엇을 지금 바꿀 가치가 있는가.

## 결론 (먼저 쓴다)
1. **지금 적용**: 로컬 API Host 헤더 검증(DNS rebinding 방어)과 PyMuPDF 우선 PDF 추출. 둘 다 스키마/모델 변경 없이 즉시 효과.
2. **후보로 기록(검증 필요)**: IBM Granite Embedding 97M Multilingual R2 -- 384차원(현 vec 테이블과 동일), 모델카드 기준
   Multilingual MTEB Retrieval 60.3 vs e5-small 50.9. 한국어 개별 수치 미확인이라 자체 평가 후 결정.
3. **후보로 기록(설계 필요)**: sqlite-vec 0.1.6+ 의 vec0 metadata column / partition key 로 확장자/수정일 필터를 KNN 내부 사전필터로 전환.
   현재는 k=4096 풀을 가져와 사후 필터 -> 필터가 선택적일수록 결과 누락 가능. 스키마 변경 + 재색인 필요.

## 환경
- Windows 11 Pro 10.0.26200, Python 3.12.2, SQLite 3.43.1 (변수 한도 32766)
- sqlite-vec 0.1.9, onnxruntime 1.26.0, tokenizers 0.22.2, pypdf 6.13.2, pymupdf 1.27.2.3, watchdog 6.0.0, fastapi 0.135.3

## 실험 및 관찰
### 관찰 1: watchdog Windows 폴더 삭제 이벤트
- 소스 확인: `watchdog/observers/read_directory_changes.py:97` -- 삭제(FILE_ACTION_REMOVED)는 대상이 폴더여도
  항상 `FileDeletedEvent(src_path)` 로 발행(이미 사라져 isdir 판정 불가). 하위 파일 이벤트는 없음.
- 이동은 `:81-85` 에서 dest 가 폴더면 `generate_sub_moved_events` 로 하위 이벤트를 생성 -> 이동은 정상 처리됨.
- 해석: 확장자 필터만 쓰는 현재 핸들러는 폴더 삭제를 놓친다 (계획서 B1).

### 관찰 2: SQLite 바인딩 변수 한도
- `sqlite3.connect(':memory:').getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)` -> 32766.
- `MAX_CHUNKS_PER_DOC = 50000` 이므로 단일 `IN (...)` 삭제는 32766 청크 초과 문서에서 실패 (계획서 B3).

### 관찰 3: DNS rebinding 위협 모델
- 2026 공개 권고(Vibe-Trading, Glances CVE-2026-32632)가 공통으로 "루프백 바인딩만으로는 부족, Host 헤더를 모든 라우트에서 검증" 을 지적.
- CORS 는 응답 *읽기*만 막고, rebinding 후에는 동일 출처가 되어 무력화됨. CogniFind `/api/search` 응답에는 문서 본문 스니펫이 포함 -> 유출 경로.

## 반증 / 실패한 시도
- multilingual-e5 패딩 토큰 불일치(pad_id=0 은 XLM-R 에서 `<s>`) 의심 -> 오른쪽 패딩 + attention mask + 마스크 평균 풀링이라
  패딩 위치는 실제 토큰 출력/위치 ID 에 영향 없음. **버그 아님**으로 결론, 수정하지 않음.
- vec0 의 `chunk_id IN (subquery)` 로 배치 삭제 대체 검토 -> 가상 테이블 풀스캔 위험이 있어 기각, 고정 크기 배치로 결정.

## 레퍼런스
- sqlite-vec v0.1.6 release (metadata/partition/auxiliary columns): https://github.com/asg017/sqlite-vec/releases/tag/v0.1.6
- Granite Embedding 97M Multilingual R2: https://mixpeek.com/model/ibm-granite/granite-embedding-97m-multilingual-r2
- EmbeddingGemma: https://deepmind.google/models/gemma/embeddinggemma/
- Glances CVE-2026-32632 (Host 검증 누락): https://advisories.gitlab.com/pypi/glances/CVE-2026-32632/
- Vibe-Trading DNS rebinding 권고: https://www.vulncheck.com/advisories/vibe-trading-loopback-trust-and-missing-host-validation-enable-dns-rebinding-authentication-bypass-and-remote-code-execution

## 미해결
- [ ] Granite R2 ONNX(quantized) 의 한국어 recall@k, CPU 지연 측정.
- [ ] vec0 metadata column 전환 시 마이그레이션(재색인 vs 컬럼 백필) 비용.
