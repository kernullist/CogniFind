# 시스템 설계 문서: Windows용 로컬 의미론적 파일 검색기 (ContextFinder)

본 문서는 사용자의 파일명 기억 한계를 보완하기 위해 100% 오프라인 온디바이스(On-Device) AI 기술을 활용하여 파일의 세부 내용까지 맥락 검색(Semantic Search)할 수 있는 Windows 데스크톱 유틸리티 **ContextFinder**의 시스템 아키텍처 및 설계 명세서입니다.

---

## 1. 프로젝트 개요 및 아키텍처 목표

- **100% 프라이버시 보호 (Zero-Cloud)**: 문서 내용을 외부 API나 클라우드 서버로 일절 전송하지 않고 모든 AI 임베딩 연산과 데이터베이스 저장을 사용자 PC 로컬 내에서 완수합니다.
- **초경량 백그라운드 운용 (Low-Resource)**: 일반 사무용 PC의 CPU 환경에서도 가볍게 동작하며, 백그라운드 색인 중 시스템 자원 점유율을 최소화합니다.
- **Windows 시스템 친화성**: 시스템 트레이(System Tray)에 상주하며, 단축키(`Win + Alt + F` 등) 클릭 시 즉각적인 자연어 쿼리 팝업 창을 렌더링합니다.
- **메타데이터 하이브리드 검색**: 의미론적 유사성(Vector Cosine Similarity)과 파일 메타데이터(파일 형식, 수정일 범위) 필터링을 결합한 하이브리드 검색을 제공합니다.
- **모던 UI/UX**: Tauri + React + TypeScript 기반의 빠르고 반응성 높은 사용자 인터페이스를 제공합니다.

---

## 2. 시스템 아키텍처 (System Architecture)

ContextFinder는 **듀얼 프로세스 아키텍처**를 채택하여 프론트엔드와 백엔드를 명확히 분리합니다:

```mermaid
graph TD
    User([사용자]) <--> TauriUI[Tauri 프론트엔드<br/>Rust + React + TypeScript]
    
    subgraph Tauri_Process [Tauri 프로세스]
        GlobalHotkey[글로벌 핫키<br/>Alt+Super+F]
        SystemTray[시스템 트레이]
        ProcessManager[Python 프로세스 관리]
    end
    
    subgraph Python_Backend [Python 백엔드 (FastAPI)]
        API[REST API 서버<br/>localhost:8765]
        FileWatcher[파일 실시간 감시<br/>watchdog]
        ParserFactory[문서 텍스트 파서]
        EmbeddingEngine[로컬 임베딩 엔진<br/>ONNX Runtime]
        VectorDB[(SQLite + sqlite-vec)]
    end
    
    TauriUI -->|HTTP 요청| API
    ProcessManager -->|프로세스 시작/중지| Python_Backend
    
    FileWatcher -->|이벤트 감지| ParserFactory
    ParserFactory -->|텍스트 청크| EmbeddingEngine
    EmbeddingEngine -->|384차원 벡터| VectorDB
    
    API -->|쿼리 임베딩| EmbeddingEngine
    API -->|유사도 검색| VectorDB
```

### 2.1 아키텍처 설계 원칙

1. **프로세스 분리**: UI 렌더링(Tauri)과 AI/ML 연산(Python)을 별도 프로세스로 분리하여 안정성 확보
2. **HTTP API 통신**: 로컬호스트 REST API를 통한 느슨한 결합(Loose Coupling)
3. **사이드카 패턴**: Tauri가 Python 백엔드를 자식 프로세스로 자동 관리
4. **단일 실행 파일 배포**: PyInstaller로 Python을 exe로 번들링하여 사용자 환경 의존성 제거

---

## 3. 핵심 모듈 설계 명세

### 3.1 Tauri 프론트엔드 (UI 레이어)

- **역할**: 사용자 인터페이스 렌더링, 시스템 통합, 백엔드 프로세스 관리
- **기술 스택**:
  - **Tauri 2.x**: Rust 기반 경량 데스크톱 프레임워크 (Electron 대비 1/10 크기)
  - **React + TypeScript**: 모던 컴포넌트 기반 UI
  - **WebView2**: Windows 10/11 기본 탑재 (별도 설치 불필요)
- **주요 기능**:
  - 프레임리스 투명 윈도우 (700x520px)
  - 글로벌 핫키 등록 (`tauri-plugin-global-shortcut`)
  - 시스템 트레이 아이콘 및 컨텍스트 메뉴
  - Python 백엔드 프로세스 라이프사이클 관리
  - 300ms 디바운싱 검색 입력

### 3.2 Python 백엔드 (FastAPI)

- **역할**: AI/ML 연산, 데이터베이스 관리, 파일 시스템 모니터링
- **기술 스택**:
  - **FastAPI**: 고성능 비동기 웹 프레임워크
  - **uvicorn**: ASGI 서버
  - **PySide6**: 백그라운드 스레딩 (QThread)
- **API 엔드포인트**:

| 엔드포인트 | 메서드 | 설명 |
|-----------|--------|------|
| `/api/search` | POST | 의미론적 검색 (필터 포함) |
| `/api/status` | GET | 인덱싱 상태 조회 |
| `/api/settings` | GET/PUT | 모니터링 디렉토리 설정 |
| `/api/index/scan` | POST | 수동 재스캔 트리거 |
| `/api/open-file` | POST | 파일 기본 프로그램으로 열기 |

### 3.3 실시간 파일 변경 감시 모듈 (File Monitor)

- **역할**: 사용자가 지정한 로컬 디렉토리의 파일 생성, 수정, 삭제 이벤트를 실시간 모니터링합니다.
- **Windows API 연동**: Python `watchdog` 라이브러리를 사용하며, 내부적으로 Windows의 `ReadDirectoryChangesW` API를 비동기식으로 호출하여 최소한의 CPU 오버헤드로 이벤트 감지.
- **안전 장치 (Debounce)**: 대용량 파일 저장이나 잦은 수정 시 발생하는 중복 트리거를 방지하기 위해 파일 쓰기가 완전히 끝난 후 1초간 대기했다가 색인을 트리거(Debouncing)하는 큐(Queue) 구조 채택.

### 3.4 문서 텍스트 파서 및 청크 분할기 (Text Extractor & Chunker)

- **파서 팩토리**: 파일 확장자별 라이브러리를 연동하여 비정형 데이터를 일반 텍스트로 추출합니다.
  - `.txt`, `.md`: 기본 UTF-8 디코딩 (cp949, euc-kr, latin1, utf-16 폴백)
  - `.pdf`: `pypdf`
  - `.docx`: `python-docx`
  - `.xlsx`: `openpyxl`
- **텍스트 청크 분할 (Chunking)**: 임베딩 모델의 최대 토큰 입력 한계(예: 512 토큰) 및 매칭 정확도를 고려하여 텍스트를 일정 크기로 분할합니다.
  - **정책**: 500자 단위 분할, 50자 오버랩(Overlap) 적용 (문맥 끊김 방지)

### 3.5 로컬 임베딩 엔진 (Local Embedding Engine)

- **모델 선정**: `all-MiniLM-L6-v2`
  - **크기**: 90MB 내외의 ONNX 포맷 모델 채택
  - **특징**: 384차원의 의미론적 밀집 벡터(Dense Vector) 생성. 뛰어난 영문 및 기초 다국어 지원 성능. 한국어 의미 강화를 원할 경우 `ko-sbert` 계열의 경량 모델(약 120MB)로 유연하게 교체 가능하도록 추상화 계층 설계.
- **실행 모듈**: `onnxruntime` (C++ 백엔드 기반으로 구동되어 별도의 대형 PyTorch 프레임워크 설치 없이 신속하게 연산 수행).
- **전처리**: Mean Pooling + L2 Normalization 적용

### 3.6 로컬 벡터 데이터베이스 (Embedded Vector DB)

- **기술**: **SQLite (sqlite-vec 확장 모듈 활성화)**
  - **선정 이유**: 디스크 기반 단일 파일 데이터베이스로 설치가 불필요하며, C 기반의 `sqlite-vec` 라이브러리를 통해 벡터 유사도 연산(Cosine Similarity)을 인덱스 수준에서 고속 수행 가능.
  - **WAL 모드**: Write-Ahead Logging 활성화로 동시 읽기/쓰기 성능 향상
  - **Busy Timeout**: 5초 타임아웃으로 데이터베이스 잠금 충돌 방지
- **스키마**:
  - `documents`: 파일 자체의 메타데이터 저장
  - `document_chunks`: 분할된 텍스트 및 의미 벡터 데이터 저장 (상호 외래키 참조)
  - `settings`: 애플리케이션 설정 (모니터링 디렉토리 등)

---

## 4. 데이터베이스 스키마 설계 (Database Schema)

```sql
-- 1. 문서 메타데이터 테이블
CREATE TABLE documents (
    id TEXT PRIMARY KEY,               -- 파일 절대 경로의 해시값 (SHA-256)
    file_path TEXT NOT NULL UNIQUE,    -- 파일 절대 경로 (정규화)
    file_name TEXT NOT NULL,           -- 파일명
    file_extension TEXT NOT NULL,      -- 파일 확장자
    file_size INTEGER NOT NULL,        -- 파일 크기 (Bytes)
    last_modified TIMESTAMP NOT NULL,  -- 최종 수정 시간
    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 분할된 텍스트 청크 및 벡터 매핑 테이블
CREATE TABLE document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,         -- 외래키 (documents.id)
    chunk_index INTEGER NOT NULL,      -- 청크 순서 (0, 1, 2...)
    text_content TEXT NOT NULL,        -- 실제 텍스트 조각
    FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
);

-- 3. 벡터 데이터 테이블 (sqlite-vec 전용 가상 테이블)
CREATE VIRTUAL TABLE chunk_embeddings USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding float[384] distance_metric=cosine
);

-- 4. 애플리케이션 설정 테이블
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 인덱스
CREATE INDEX idx_chunks_doc_id ON document_chunks(document_id);
CREATE INDEX idx_documents_file_path ON documents(file_path);
```

---

## 5. 핵심 데이터 흐름 (Data Flow)

### 5.1 파일 실시간 인덱싱 시퀀스

```
[파일 탐색기] ➔ (수정 완료) ➔ [FileWatcher] 
                              │ (경로 송신)
                              ▼
                       [Parser Factory] ➔ 텍스트 추출 및 청크 분할
                                           │ (청크 데이터 전달)
                                           ▼
                                    [Embedding Engine] ➔ ONNX 모델 연산 (384차원)
                                                          │ (임베딩 실수값 반환)
                                                          ▼
                                                   [SQLite Vector DB] ➔ DB 적재
```

### 5.2 자연어 의미론적 검색 시퀀스

```
[Tauri UI] ➔ 검색어 입력 (300ms 디바운싱)
              │
              ▼
      [POST /api/search] ➔ HTTP 요청 (localhost:8765)
              │
              ▼
      [Embedding Engine] ➔ 검색어의 384차원 벡터 추출
              │
              ▼
      [SQLite Vector DB] ➔ vec_distance_cosine() 함수로 코사인 유사도 연산
                          ➔ 메타데이터 조건 추가 (last_modified >= ?)
                          ➔ 파일 확장자 필터 (선택적)
              │
              ▼ (유사도 순 탑 5 리스트 반환)
      [JSON 응답] ➔ Tauri UI로 전송
              │
              ▼
      [React 렌더링] ➔ 결과 리스트 표시
              │
              ▼ (더블클릭)
      [POST /api/open-file] ➔ Windows 기본 프로그램으로 파일 오픈
```

### 5.3 애플리케이션 시작 시퀀스

```
[사용자 실행] ➔ CogniFind.exe
                    │
                    ├─► [Tauri 프로세스 시작]
                    │       │
                    │       ├─► [Python 백엔드 사이드카 실행]
                    │       │       │
                    │       │       ├─► FastAPI 서버 시작 (port 8765)
                    │       │       ├─► ONNX 모델 로드
                    │       │       ├─► watchdog 파일 감시 시작
                    │       │       └─► 초기 디렉토리 스캔
                    │       │
                    │       ├─► [글로벌 핫키 등록] (Alt+Super+F)
                    │       ├─► [시스템 트레이 아이콘 표시]
                    │       └─► [메인 윈도우 생성] (숨김 상태)
                    │
                    └─► [사용자 대기]
```

---

## 6. 상세 기술 스택 및 라이브러리 구성

### 6.1 프론트엔드 (Tauri)

| 구분 | 기술 스택 | 상세 스택 및 목적 |
|------|-----------|-------------------|
| **프레임워크** | **Tauri 2.x** | Rust 기반 경량 데스크톱 프레임워크 (~10-15MB) |
| **UI 라이브러리** | **React 18 + TypeScript** | 컴포넌트 기반 모던 UI |
| **빌드 도구** | **Vite** | 빠른 개발 서버 및 번들링 |
| **스타일링** | **CSS3** | 다크 테마, 투명도, 애니메이션 |
| **시스템 통합** | **tauri-plugin-global-shortcut** | 글로벌 핫키 등록 |
| **프로세스 관리** | **tauri-plugin-shell** | 사이드카 프로세스 제어 |

### 6.2 백엔드 (Python)

| 구분 | 기술 스택 | 상세 스택 및 목적 |
|------|-----------|-------------------|
| **웹 프레임워크** | **FastAPI** | 고성능 비동기 REST API |
| **ASGI 서버** | **uvicorn** | 프로덕션급 웹 서버 |
| **임베딩** | **onnxruntime** | CPU 기반 ONNX 모델 추론 |
| **토크나이저** | **tokenizers** | Hugging Face 토크나이저 |
| **모델 허브** | **huggingface_hub** | ONNX 모델 자동 다운로드 |
| **벡터 DB** | **sqlite-vec** | SQLite 벡터 검색 확장 |
| **파일 감시** | **watchdog** | 크로스플랫폼 파일 시스템 이벤트 |
| **문서 파싱** | **pypdf, python-docx, openpyxl** | PDF, Word, Excel 텍스트 추출 |
| **스레딩** | **PySide6 (QThread)** | 백그라운드 인덱싱 스레드 |

### 6.3 빌드 및 배포

| 구분 | 기술 스택 | 상세 스택 및 목적 |
|------|-----------|-------------------|
| **Python 번들링** | **PyInstaller** | Python + 의존성을 단일 exe로 패키징 (~90MB) |
| **앱 번들링** | **Tauri bundler** | NSIS/MSI 인스톨러 생성 |
| **사이드카** | **Tauri externalBin** | Python exe를 앱 번들에 포함 |

---

## 7. Windows 시스템 트레이 및 단축키 연동 상세 (System Integration)

- **백그라운드 상주**: 앱을 실행하면 바탕화면에 작업 창이 나타나지 않고, Windows 우측 하단 알림 영역(System Tray)에 ContextFinder 돋보기 아이콘이 활성화됩니다.
- **글로벌 핫키 등록**: Tauri의 `tauri-plugin-global-shortcut`을 사용하여 `Alt + Super + F`를 등록합니다. 사용자가 어느 화면에 있든 해당 단축키를 누르면 화면 정중앙에 투명 효과가 적용된 검색 팝업을 즉시 오픈합니다.
- **파일 더블클릭 액션**: 검색 결과 리스트에서 파일을 더블클릭하면 `/api/open-file` 엔드포인트를 통해 Windows OS 내부 `os.startfile(filepath)` API를 호출하여 사용자가 평소 사용하던 기본 문서 편집기(Acrobat Reader, MS Word, Notepad++ 등)로 즉각 파일을 실행해 줍니다.
- **트레이 컨텍스트 메뉴**:
  - **Search Documents**: 검색 창 열기
  - **Re-index Now**: 수동 재스캔 트리거
  - **Exit**: 애플리케이션 종료 (Python 백엔드 포함)

---

## 8. 빌드 및 배포 파이프라인

### 8.1 빌드 프로세스

```
[1] PyInstaller 빌드
    api.py + src/* → cognifind-backend.exe (~90MB)
    
[2] 사이드카 복사
    cognifind-backend.exe → frontend/src-tauri/binaries/
    
[3] 프론트엔드 빌드
    React + TypeScript → dist/ (정적 파일)
    
[4] Tauri 빌드
    Rust + WebView2 + 사이드카 → CogniFind.exe
    
[5] 인스톨러 생성
    NSIS: CogniFind_0.1.0_x64-setup.exe (~92MB)
    MSI:  CogniFind_0.1.0_x64_en-US.msi (~93MB)
```

### 8.2 배포 구조

```
C:\Program Files\CogniFind\
├── CogniFind.exe                    # Tauri 메인 실행 파일
├── cognifind-backend.exe            # Python 백엔드 (사이드카)
└── resources/
    └── icons/                       # 애플리케이션 아이콘
```

### 8.3 런타임 의존성

- **WebView2 Runtime**: Windows 10/11에 기본 탑재 (없으면 자동 설치)
- **Python**: 불필요 (PyInstaller로 번들링됨)
- **ONNX 모델**: 포터블 배포에서 실행 파일 옆 `models/` 폴더에 위치하여 런타임 네트워크 불필요. 백엔드(frozen)는 `Path(sys.executable).parent / "models"`를 참조(또는 `COGNIFIND_MODELS_DIR` 환경변수 우선). 빌드 시 `scripts/fetch_models.py`가 모델을 받아 포터블 폴더에 배치. (프로덕션 앱은 기본적으로 다운로드 비활성 `ALLOW_MODEL_DOWNLOAD=False`) 업데이트 시 두 exe만 교체하면 `models/`는 그대로 유지.

---

## 9. 성능 최적화 전략

### 9.1 데이터베이스 최적화

- **WAL (Write-Ahead Logging) 모드**: 읽기와 쓰기가 동시에 가능하여 백그라운드 인덱싱 중에도 검색 응답성 유지
- **Busy Timeout (5초)**: 동시 접근 시 즉시 실패하지 않고 대기하여 충돌 최소화
- **인덱스**: `document_chunks.document_id`, `documents.file_path`에 인덱스 생성

### 9.2 검색 최적화

- **디바운싱 (300ms)**: 사용자가 타이핑을 멈춘 후 300ms 대기하여 불필요한 임베딩 연산 방지
- **KNN 오버페치**: `limit * 3`개의 후보를 가져온 후 파일별로 최고 점수만 선택 (중복 제거)
- **코사인 거리 정렬**: `ce.distance ASC`로 직접 정렬하여 계산 오버헤드 제거

### 9.3 CPU 쓰로틀링

- **Windows API 연동**: `GetLastInputInfo()`로 시스템 유휴 시간 감지
- **적응형 슬립**:
  - 사용자 활동 중 (idle < 5초): 50ms 슬립 per chunk
  - 유휴 상태 (idle >= 5초): 10ms 슬립 per chunk
- **스레드 우선순위**: `QThread.IdlePriority`로 백그라운드 스레드 우선순위 낮춤

### 9.4 메모리 최적화

- **지연 로딩**: ONNX 모델은 첫 검색 시 로드
- **배치 처리**: 여러 청크를 한 번에 임베딩 (배치 크기 조절 가능)
- **제너레이터 패턴**: 디렉토리 스캔 시 파일을 하나씩 yield하여 메모리 사용량 최소화

---

> [!WARNING]
> **초기 대용량 색인(Initial Indexing) 처리 지침**
> 
> 최초 실행 시 지정한 폴더 내의 수천 개 문서를 한 번에 색인할 때 CPU 점유율이 일시적으로 급증할 수 있습니다. 이를 예방하기 위해 초기 색인은 백그라운드 스레드(Worker Thread)에서 순차적으로(Queueing) 실행되도록 제어해야 하며, 컴퓨터 사용이 없는 유휴 시간(System Idle)에 가동률을 높이고 사용자가 활발히 마우스를 움직일 때는 색인 속도를 늦추는 쓰로틀링(Throttling) 정책을 필수로 적용해야 합니다.
> 
> 현재 구현:
> - `IndexingWorker` (QThread)가 백그라운드에서 실행
> - `throttle_cpu()` 메서드가 Windows API로 유휴 시간을 확인하여 적응형 슬립 적용
> - `QThread.IdlePriority`로 스레드 우선순위 최소화

---

## 10. 향후 개선 사항

- [x] **한국어 임베딩 모델 지원**: 모델 레지스트리(`EMBEDDING_MODELS`)로 `multilingual-e5-small`(한국어/다국어) 전환 지원. e5 계열은 `query:`/`passage:` 프리픽스를 적용하며, 전환 시 인덱스를 비우고 차원에 맞춰 벡터 테이블을 재생성 후 재색인. `GET/PUT /api/model`로 제어.
- [x] **증분 인덱싱**: 문서 단위 `content_hash`로 내용 미변경 시 임베딩 전체 스킵, 청크 단위 `chunk_hash`로 변경/추가된 청크만 재임베딩(나머지 임베딩 재사용)
- [ ] **검색 히스토리**: 최근 검색어 저장 및 자동완성
- [ ] **다중 언어 UI**: 영어/한국어 인터페이스 전환
- [ ] **GPU 가속**: CUDA/cuDNN 지원으로 임베딩 속도 향상
- [ ] **플러그인 아키텍처**: 사용자 정의 파일 파서 추가 지원
