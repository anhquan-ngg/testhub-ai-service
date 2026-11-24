# AGENTS.md — testhub-ai-service

`CONTINUITY.md` là ghi chú bàn giao local, được gitignore. Nếu chưa tồn tại,
tạo file local với tiêu đề và trạng thái đã xác minh trước khi đọc/cập nhật;
không thêm file này vào Git. Quy tắc này áp dụng cho mọi yêu cầu đọc file bên dưới.

## Targeted OCR Recovery And Strict Evidence

- Ordinary extraction uses `ocr_mode=off`; never silently spend on vision OCR. Only explicit owner-consented recovery uses `ocr_mode=targeted` with 1..30 distinct source positions from the saved plan.
- Keep all detected `ocr_candidate_units` and per-page `ocr_pages` outcomes. Additional text belongs to `ocr_units`; a text-bearing page is not proof of complete extraction. Preserve source bytes/hash, original page numbers, and text-layer content.
- Page statuses are `PENDING|COMPLETED|NO_TEXT|FAILED|BUDGET_EXHAUSTED|DISABLED`; `reason` is optional/nullable. Cache exact transcription only, never diagram inference. Cache failure on read prevents paid work; successful backend checkpoints survive cache-write failures.
- Recovery creates a new backend version with durable checkpoints and bounded deliveries. Never revive old failed question batches. No automatic unlimited provider/daily/time-budget retries; only PAGE_LIMIT continues the next bounded delivery.
- Evidence precheck is local/no-paid, type/cognition-aware, deduplicates facts and rejects unresolved selected OCR, missing units, heading-only sources and inadequate depth. All non-OK results block backend approval; checker failure must fail closed. It is a conservative heuristic, not proof of semantic sufficiency.
- Synchronize both exported OpenAPI contracts and BE/FE schemas when changing these fields. Offline tests must not invoke providers, mutate live data, or apply migrations.

## Embedding 429 resilience (audit)

- `GoogleEmbeddingProvider` owns a per-process item limiter (`EMBEDDING_MAX_RPM`), a
  concurrency cap, an LRU cache and in-call dedup, plus bounded retries for
  429/5xx/transport errors with jitter and `Retry-After`/`RetryInfo`. Daily-quota 429s are
  `RAG_EMBEDDING_QUOTA_EXHAUSTED` (non-retryable, 5-minute breaker). `retrieve_plan` dedups queries.
- Keep the retry budget below the 40 s RAG deadline. Never change the model/dims/profile or
  rotate keys to bypass quota. Tests use MockTransport and a fake clock only
  (`tests/test_embedding_resilience.py`; mock benchmark in `scripts/benchmark_embedding_mock.py`).
- Indexing (`RETRIEVAL_DOCUMENT`) is capped to `EMBEDDING_INDEXING_RPM_PERCENT` of the RPM window,
  yields to in-flight queries and has its own concurrency semaphore; queries are never throttled by it.
  Embeddings use `EMBEDDING_GOOGLE_API_KEY` (fallback `GEMINI_API_KEY`) for quota isolation only.
- The optional L2 cache (`EMBEDDING_PERSISTENT_CACHE_ENABLED`, off by default) stores only sha256 keys
  and query vectors in `rag.embedding_cache` (migration 003, manual); it must fail open and `ready()`
  must not require it. Limiters are per process: divide the project RPM by replicas × workers.
- `DailyBudget` (`EMBEDDING_DAILY_LIMIT`, 0 = off) counts items actually sent, rejects before the send with
  `RAG_EMBEDDING_QUOTA_EXHAUSTED` (indexing earlier, at `EMBEDDING_DAILY_INDEXING_PERCENT`), and is closed by a
  Google daily 429 until the next midnight-Pacific reset. It is in-memory: per process, resets on restart.
- Report: `testhub/docs/ai-question-generation-embedding-audit.md`.

## Grounded evidence refusals

- Use RawGroundedGenerationOutcome only for internal provider output. Empty
  questions are an evidence refusal, not a successful public response; successful
  rag-generation.v1 still requires 1..20 questions. Text and embeddings do not change.
- unsupported_slots contains unique 1-based slots within the request and reason
  INSUFFICIENT_EVIDENCE. Never accept questions together with refusals. Invalid
  metadata/schema, counts, answers and citations still fail RAG_OUTPUT_INVALID.
- Valid refusals become nonretryable RAG_INSUFFICIENT_EVIDENCE (422); diagnostics
  contain only issues [{slot, reason}]. Legacy empty lists have unknown slots
  (issues:[]); do not invent them. Missing planned chunks use NO_RETRIEVED_CONTEXT
  for the first deficient item's expanded slots before any provider call.
- No semantic retry, partial success, automatic splitting or evidence-triggered
  fallback. Preserve slot-scoped citations and UNASSESSED quality status.
- Log only bounded sanitized schema field/type metadata and retrieval slot/count/
  byte summaries. Never log validation input/message, arbitrary JSON keys, prompt,
  source or model output. Keep the exported RAG OpenAPI synchronized.

## Source-wide knowledge plans (2026-10-25)

- POST /internal/rag/outline reconstructs all authorized chunks, recognizes numbered chapter/section headings and proposes inferred page topics when no outline exists. These are editable heuristics, not validated semantic classifications.
- Normalize numeric slide-continuation suffixes `(n)` for both topic names and
  identity keys. Union source units; retain first-title evidence. Keep distinct
  section codes/parents and nonnumeric qualifiers separate; never rewrite saved
  plans or bank chapters when outline heuristics change.
- GroundedGenerateRequest.plan_items binds each cognitive cell to an objective and source units. SQL filters allowed units BEFORE nearest-neighbor LIMIT; missing cell evidence fails rather than substituting another topic.
- One provider invocation receives isolated ordered slot contexts. Validate each quote against that slot only and assign plan_item_id server-side. Recheck authorization after I/O.
- Validate every raw citation before consolidating repeated chunk IDs into one
  contiguous source span <=600 characters; never drop unsupported duplicate
  quotes or lose cited evidence to satisfy backend uniqueness. Planned schema,
  scope and quote failures log only reason/mode/slot metadata.
- Apply new manual migration migrations/002_optional_document_chapter.sql before topic-owned document indexing; old migrations remain unchanged.
- Config rows now max20 / total questions max20. Keep exported RAG OpenAPI in sync. CONTINUITY.md is local and ignored/untracked.


## Question configs for generation (2026-10-23)

- GenerateRequest and GroundedGenerateRequest share QuestionBlueprint: either
  ordered question_configs [{question_type, question_format, count}] (1-20 rows,
  total <=20, SINGLE_CHOICE|MULTIPLE_CHOICE) or the legacy question_format+count
  (single choice). Both forms together are rejected; use request.slots().
- Prompts list one slot per question; check_slots validates count and the
  correct-option rule per position (single: exactly 1; multiple: 2-3 of 4).
  Output type/format come from the slot, never from the model.
- All generation prompts include numeric option/correct/distractor budgets per
  slot (target 1/3 for single, 2/2 for multiple); the SDK schema requires 4 options.
  Keep the 2-3 correct-option validator for multiple-choice compatibility. Answer
  failures log only slot/type/count metadata, never answer text or provider JSON.
- Both text-generation.v1 fixtures now use question_configs; keep them identical.
  RAG OpenAPI was regenerated. ESSAY generation is not supported.

## Grounded document question generation (2026-10-23)

- POST /internal/rag/generate implements rag-generation.v1, authenticated with the
  existing token. GroundedGeneration retrieves authorized READY chunks, shares
  QuestionGenerator admission/provider/deadline and rechecks full scope afterward.
- GeminiProvider.generate_grounded uses a separate structured response schema;
  text-generation.v1 stays unchanged. Validate exact count, distinct questions,
  four options per question, exactly one correct answer for SINGLE_CHOICE or
  two or three for MULTIPLE_CHOICE as required by each slot, known chunk IDs
  and exact nonblank evidence quotes.
- Citations include original source chunks/positions/hashes. UNASSESSED is explicit:
  quote matching does not establish answer entailment, factual accuracy or full-file
  coverage. Never fabricate citations or fall back to general knowledge.
- Grounded deadline defaults to max(75000, GENERATION_TIMEOUT_MS + 15000) ms.
  Nonzero GROUNDED_GENERATION_TIMEOUT_MS must preserve that 15000 ms headroom;
  the NestJS outer deadline must be longer. Generation shares the existing
  semaphore/client and bounded SDK retries. No Python bank/business writes.
- Export contracts/rag-indexing.openapi.json after route/schema changes. Tests use
  synthetic providers or real SDK HTTP against local Postman fixtures, never real
  keys/documents. Backend/Next.js now accept attached PDFs/PPTX and retain evidence
  through review/edit/commit. See https://github.com/anhquan-ngg/testhub-v2-backend/blob/master/docs/rag-indexing.md section 4.
- RAG errors, including token failures and validation 422, use
  detail: {code, retryable}; declare the common model in route responses.
  Authenticate before reading a RAG body or acquiring an intake slot.
  Body intake slots are released before downstream processing. Post-LLM scope
  verification bypasses RAG admission and has a separate 5-second deadline.
- Parser subprocesses receive only PYTHONUTF8 and the OS/temp variables needed
  for startup; never inherit provider keys, DB URLs or PYTHONPATH. Loops without
  subprocess support (uvicorn --reload on Windows uses SelectorEventLoop) fall
  back to subprocess.Popen spawned/waited in a thread. Cancellation during spawn
  returns promptly and transfers child/temp-file ownership to a tracked cleanup
  task; hold the intake slot until late spawn cleanup finishes.

## Current RAG implementation (2026-10-21)

- Chunking, Google embedding and scoped retrieval are implemented. This supersedes
  the earlier extraction-only snapshots below. Grounded generation is implemented; OCR, hybrid/reranking and additional
  providers remain future work.
- `api/rag.py`, `schemas/rag.py`, `services/chunking.py`, `embedding.py`,
  `retrieval.py`, `repositories/vector_repository.py` own the internal contract.
  NestJS authorizes business scope and orchestrates checkpointed BullMQ work.
- Use the same PostgreSQL DB with dedicated `testhub_ai` role/schema `rag`.
  Migrations in migrations/ are manual operator setup, never applied at startup.
  RAG_ENABLED defaults false; RAG_DATABASE_URL is private. GEMINI_API_KEY is reused.
- Fixed profile: gemini-embedding-001, 768 dimensions, explicit document/query tasks,
  L2 normalization; deterministic byte-budgeted source chunks and codepoint offsets.
  Never silently change model/dimensions/profile or reset paid-call budgets.
- Only READY owner/profile/index scopes are searchable. Keep full-scope pre/post
  checks, SQL filters, tombstones and fenced leases. Python never writes bank data.
- Retrieval source_units identify index_id/kind/position together. Plan/outline
  source units stay kind/position because the enclosing plan pins one index.
- On Windows, parser subprocesses use asyncio on Proactor loops and the threaded
  Popen fallback on selector loops; the psycopg pool runs on an owned selector-loop thread. Readiness checks DB schema, not Google.
- Setup/API/limits: [backend RAG guide](https://github.com/anhquan-ngg/testhub-v2-backend/blob/master/docs/rag-indexing.md).
  Contract: contracts/rag-indexing.openapi.json; regenerate via
  `python scripts/export_rag_contract.py`. Run pytest and opt-in SDK HTTP simulation;
  real pgvector tests require isolated RAG_TEST_DATABASE_URL, never RAG_DATABASE_URL.

## Implementation update: document extraction (2026-10-21)

- NestJS/Next.js batch, worker and review/commit are implemented. Earlier "next
  step" descriptions below are historical text-MVP snapshots.
- `api/documents.py`, `schemas/document.py`, `services/document_parser.py` and
  `document_parser_worker.py` implement PDF/PPTX extraction. Receive authorized
  bytes from NestJS under X-Internal-Token, never arbitrary URLs or S3 credentials.
  Maintain document-extraction.v1 separately from unchanged text-generation.v1.
- Lifespan owns DocumentParser. Enforce actual input/output/ZIP expansion limits,
  admission/deadlines. Always kill/await child on cancellation. Linux resource
  limits are implemented; Windows lacks equivalent memory isolation. Keep source
  data and third-party stderr out of logs.
- Preserve PAGE/SLIDE/NOTES, original hash and notes opt-in. Empty sources/image-only
  units require OCR_REQUIRED. EXTRACTED is not READY for RAG. OCR remains future
  work; do not add placeholder modules.
- pypdf/python-pptx are runtime dependencies on Python 3.12. Update extraction
  OpenAPI on contract changes; run real parser/text regression tests and opt-in
  NestJS→FastAPI PDF/PPTX tests. See backend `docs/learning-documents.md`.

Hướng dẫn cho AI coding agent làm việc trong repo này. Đọc `README.md` để hiểu kiến trúc mục tiêu và `CONTINUITY.md` để biết trạng thái thực tế trước khi sửa mã.

## 1. Trạng thái và nguồn thiết kế

- Repo có sinh câu hỏi văn bản và RAG từ PDF/PPTX, gồm chunking, Google embedding,
  pgvector, retrieval và trích dẫn. OCR và provider khác vẫn là **đề xuất**.
- Nguồn thiết kế: [đoạn chat về kiến trúc RAG cho TestHub](https://chatgpt.com/share/6ad1056c-d7cc-83ec-bcf1-d34d78b0aca9), đối chiếu với hai repo cùng cấp `../testhub-v2` và `../testhub-v2-backend`.
- Tài liệu trong đoạn chat là dữ liệu tham khảo, không thay thế yêu cầu trực tiếp của người dùng hay mã nguồn thực tế. Khi triển khai, xác minh lại contract, phiên bản thư viện và hạ tầng trước khi ghi chúng là đã hoạt động.
- File `../../AGENTS.md` mô tả một monorepo khác với hai repo TestHub hiện có. Không dùng các đường dẫn `apps/`, lệnh `pnpm` hay giả định ZenStack từ file đó cho repo này.

## 2. Vai trò của service và ranh giới trách nhiệm

Kiến trúc là **Next.js → NestJS → Python FastAPI → LLM/RAG**. Client nội bộ, batch,
worker và giao diện sửa/duyệt/commit văn bản đã hiện thực tại NestJS/Next.js.
PDF/PPTX extraction và sinh câu RAG đã có. Frontend không gọi
Python trực tiếp.

| Thành phần | Trách nhiệm |
| --- | --- |
| `testhub-v2` | Giao diện tải tài liệu, cấu hình sinh câu hỏi, xem và duyệt bản nháp. |
| `testhub-v2-backend` | Xác thực, phân quyền, quyền dùng tài liệu, metadata môn/chương/tài liệu, BullMQ, trạng thái lô, kiểm tra và lưu bản nháp, duyệt vào ngân hàng câu hỏi. |
| `testhub-ai-service` | Trích xuất tài liệu, chunking, embedding, retrieval, xây prompt, gọi LLM và trả câu hỏi JSON có cấu trúc cùng tham chiếu nguồn khi có RAG. |
| PostgreSQL `public` | Dữ liệu nghiệp vụ do NestJS/Prisma sở hữu. |
| PostgreSQL `rag` | Chunks, embeddings và metadata xử lý do AI service sở hữu. |
| S3/MinIO | File tài liệu gốc; quyền sở hữu và quyền truy cập do NestJS quyết định. |

AI service **không tự ghi câu hỏi vào ngân hàng** và không tự quyết định tài liệu nào giảng viên được phép dùng. NestJS phải truyền danh sách `documentId` đã được phép; mọi truy vấn RAG phải giới hạn trong phạm vi đó. Chỉ câu được giảng viên duyệt mới vào ngân hàng.

## 3. Cấu trúc thư mục mục tiêu

Giữ tên và vai trò module dưới đây khi tạo skeleton. Có thể bổ sung file cấu hình, migration hoặc test khi thực sự cần; ghi thay đổi kiến trúc vào `README.md` và `CONTINUITY.md`.

```text
testhub-ai-service/
├── app/
│   ├── main.py                    # Tạo FastAPI app, đăng ký router, health check
│   ├── config.py                  # Đọc/kiểm tra env một lần khi startup
│   ├── errors.py                  # Lỗi domain dùng chung
│   ├── api/
│   │   ├── dependencies.py        # Xác thực nội bộ, lấy service từ app state
│   │   ├── errors.py              # Ánh xạ lỗi domain sang HTTP tập trung
│   │   ├── documents.py           # API nội bộ xử lý/truy vấn trạng thái tài liệu
│   │   └── generation.py          # API nội bộ sinh câu hỏi
│   ├── services/
│   │   ├── document_parser.py     # PDF, DOCX, PPTX; giữ trang/slide nguồn
│   │   ├── chunking.py            # Chia nội dung thành đoạn
│   │   ├── embedding.py           # Vector hóa nội dung/truy vấn
│   │   ├── retrieval.py           # Tìm chunks trong phạm vi tài liệu được phép
│   │   ├── prompt_builder.py      # Xây prompt từ cấu hình và đoạn nguồn
│   │   └── question_generator.py  # Điều phối provider, kiểm tra đầu ra
│   ├── providers/
│   │   ├── base.py                # Protocol chung cho LLM provider
│   │   ├── gemini.py              # Provider mặc định (LLM_PROVIDER=gemini)
│   │   ├── nine_router.py         # Adapter OpenAI-compatible cho 9Router (LLM_PROVIDER=9router)
│   │   ├── factory.py             # Chọn provider theo LLM_PROVIDER, không fallback ngầm
│   │   ├── openai.py              # Dự kiến sau MVP
│   │   └── grok.py                # Dự kiến sau MVP
│   ├── schemas/
│   │   └── question.py            # Pydantic v2 request/response/question draft
│   └── repositories/
│       └── vector_repository.py   # Truy cập dữ liệu thuộc schema rag
├── tests/
├── requirements.txt
├── Dockerfile
└── .env.example
```

Đã hiện thực `main.py`, `config.py`, `errors.py`, `api/generation.py`, `api/dependencies.py`, `api/errors.py`, `services/prompt_builder.py`, `services/question_generator.py`, `providers/base.py`, `providers/gemini.py` và `schemas/question.py`; các file khác trong cây vẫn là mục tiêu. Không tạo file rỗng chỉ để khớp sơ đồ.

## 4. Luồng tích hợp mục tiêu

1. **MVP không RAG (đã có):** FastAPI nhận văn bản dán trực tiếp và cấu hình, gọi Gemini, kiểm tra JSON, trả bản nháp qua `POST /internal/generate`. Cần header `X-Internal-Token` khớp `INTERNAL_API_TOKEN`.
2. **Tích hợp NestJS:** NestJS worker gọi FastAPI; khi dùng tác vụ nền, NestJS tạo batch, đưa job vào BullMQ, nhận JSON rồi kiểm tra/lưu bản nháp. FastAPI không cần tham gia BullMQ hoặc Redis trong phiên bản đầu.
3. **Ingestion RAG:** NestJS kiểm tra quyền và lưu file trên S3/MinIO; AI service trích xuất PDF/DOCX/PPTX, giữ trang/slide, chia chunks, tạo embeddings, lưu vào `rag`. Chỉ đánh dấu tài liệu `READY` sau khi xử lý xong.
4. **Generation RAG:** Truy xuất chunks trong danh sách tài liệu được phép, phân bổ nguồn để bao phủ nhiều mục kiến thức, sinh câu hỏi kèm tham chiếu nguồn; NestJS kiểm duyệt và chỉ lưu câu được duyệt.

Contract văn bản `text-generation.v1` đã thống nhất qua `contracts/text-generation.v1.json` và fixture tương ứng ở NestJS. Backend dùng `X-Internal-Token`, deadline mặc định 35000 ms và một HTTP invocation; Python giữ deadline 30000 ms/retry SDK (mặc định; cấu hình được qua `GENERATION_TIMEOUT_MS`, `ROUTER_TIMEOUT_MS`, `GROUNDED_GENERATION_TIMEOUT_MS`, và chuỗi deadline phải lồng nhau với `AI_SERVICE_TIMEOUT_MS`). `LLM_FALLBACK_PROVIDER=gemini` (opt-in) chỉ fallback khi primary không khả dụng (kết nối/429/5xx), không fallback khi timeout hoặc output không hợp lệ. `AI_SERVICE_INTERNAL_TOKEN` của NestJS phải trùng `INTERNAL_API_TOKEN`, dùng 32–256 ký tự ASCII nhìn thấy được để tương thích HTTP header. Batch/persistence, chuyển file và schema RAG chưa triển khai. Đối chiếu `../testhub-v2-backend/docs/ai-question-generation.md` khi đổi contract; cập nhật cả hai fixture và type frontend.

## 5. Quy ước khi triển khai

- Python **3.12** là phiên bản mục tiêu cho local và Docker (`Dockerfile` dùng `python:3.12-slim`); FastAPI dùng async, request/response dùng Pydantic v2. Khai báo kiểu đầy đủ cho hàm public. Khi thêm dependency, kiểm tra tương thích Python 3.12.
- Router chỉ nhận/kiểm tra request và gọi service. Parser, retrieval, prompt và provider ở đúng module riêng.
- Lifespan sở hữu settings và Gemini client dùng chung cho mỗi process; không đọc env hoặc tạo connection pool cho từng request. Service quản lý semaphore và deadline tổng; API dependencies xử lý token, exception handler tập trung ánh xạ lỗi HTTP.
- Retry SDK có giới hạn qua `GEMINI_MAX_ATTEMPTS`, nằm trong deadline `GENERATION_TIMEOUT_MS`; timeout phải trả 504 và giải phóng suất xử lý. Log upstream chỉ ghi loại exception/mã HTTP/giai đoạn, không log message hoặc traceback chứa dữ liệu nguồn. Cleanup client không được che lỗi request.
- `providers/base.py` là contract chung cho các LLM; `LLM_PROVIDER` chọn `gemini` (mặc định) hoặc `9router` (`providers/nine_router.py`, httpx không streaming, `ROUTER_*`), không fallback ngầm. Validation đầu ra vẫn nằm ở `QuestionGenerator`. Tách embedding khỏi LLM provider: RAG vẫn dùng `GEMINI_API_KEY`.
- Kiểm tra cấu trúc và ngữ nghĩa tối thiểu của đầu ra: số câu, loại/mức nhận thức hợp lệ, số phương án, đúng một đáp án cho câu `SINGLE_CHOICE` và 2–3 đáp án cho `MULTIPLE_CHOICE` theo từng vị trí cấu hình, câu rỗng/trùng. JSON hợp lệ chưa chứng minh câu hỏi đúng kiến thức; giảng viên vẫn phải duyệt.
- Sinh câu hỗ trợ **một đáp án và nhiều đáp án** theo `question_configs`, văn bản qua API cũ và PDF/PPTX qua RAG có trích dẫn.
  DOCX, provider khác và OCR là bước sau. Giữ `text-generation.v1` tương thích.
- Kiểm thử logic thuần và xử lý lỗi bằng provider giả lập; chỉ chạy smoke test API thật khi có key và người dùng yêu cầu. Không gọi API trả phí trong unit test. Xem `requirements-dev.txt` và `tests/`.
- Không đưa API key, token, tài liệu nhạy cảm hay toàn bộ prompt vào log. Cấu hình lấy từ biến môi trường; `.env.example` chỉ chứa tên biến và giá trị mẫu vô hại. Thêm `.gitignore` trước khi tạo `.env` thật.
- FastAPI chỉ được truy cập qua mạng nội bộ; xác thực service-to-service, timeout, retry hữu hạn và giới hạn concurrency trước khi triển khai.
- Không commit, push, deploy, chạy migration hay thao tác DB/S3 thật nếu người dùng chưa yêu cầu rõ ràng.

## 6. Trước và sau mỗi nhiệm vụ

1. Đọc `CONTINUITY.md`, kiểm tra `git status`, phân biệt phần đã có với phần dự kiến.
2. Khi thêm mã, cập nhật lệnh chạy/test thật trong `README.md`; không bịa lệnh khi dependencies chưa được tạo.
3. Chạy kiểm tra phù hợp với phần đã sửa và ghi **kết quả thực tế** vào `CONTINUITY.md`.
4. Nếu đổi kiến trúc, contract, quyền sở hữu dữ liệu hoặc cây thư mục, cập nhật `AGENTS.md`, `README.md` và `CONTINUITY.md` cùng lúc.
