# CONTINUITY.md — testhub-ai-service

## Windows README clarification (2026-10-23)

- README now describes asyncio parser subprocesses on Proactor loops and Popen
  with waiting in a thread on selector loops, matching DocumentParser. Existing
  psycopg pool ownership and readiness descriptions are unchanged.
- Validation: checked parser, Windows pool and readiness code; documentation-only
  change, no runtime tests run. Diff whitespace check passed with CRLF recognized.

## Intake authentication and revoke scope review (2026-10-23)

- RagBodyLimit now reuses require_internal_token before slot acquisition/body intake;
  missing/invalid tokens return the existing RAG 401 envelope without reading bytes.
  Unconfigured auth returns 503. Authenticated limits/replay behavior is unchanged.
- VectorRepository.revoke checks profile plus all five persisted source-scope fields
  under its advisory transaction lock before any write. Mismatch returns 404;
  matching REVOKED indexes and absent-index tombstones remain replayable.
- PPTX image detection uses Picture (including PlaceholderPicture) and retains group
  recursion. PDF strict=True is intentional: structural errors reported by strict
  parsing are rejected even when recovery mode could read the source.
- Extraction guidance now leaves only OCR in its outdated future-work sentence.
  RAG guide links in README/AGENTS/CONTINUITY use the backend GitHub master URL;
  owner/repo/default branch verified from Git metadata and the target exists locally.
- Validation: 44 focused tests passed; full pytest 134 passed / 3 opt-in tests skipped.
  New tests cover no-body/no-slot auth rejection, all six revoke scope mismatches,
  revoked/absent-index replay, picture placeholders and recoverable PDF xref damage.
  compileall and pip check passed; diff check passed with CRLF recognized via
  core.whitespace=cr-at-eol (existing Windows line endings). OpenAPI drift tests pass.
  No live PostgreSQL/pgvector or paid provider calls; GitHub URL access not tested.

## Grounded quote matching and 502 diagnostics (2026-10-23)

- Symptom: /internal/rag/generate 502 (RAG_OUTPUT_INVALID) with no log line. Exact
  quote matching rejected Gemini quotes that differ from PDF chunk text only by
  whitespace/line breaks or Unicode form (NFD vs NFC Vietnamese). locate_quote now
  matches after per-character NFD + whitespace collapse and stores the exact source
  span (still a verbatim substring, <=600 chars); fabricated quotes are rejected.
- Every grounded rejection logs `generation_invalid_output reason=` schema |
  duplicate_citations | unknown_chunk | quote_mismatch (plus count/answers).
- Validation: grounded tests 21 passed. Root cause inferred from code paths; the
  user's document and live Gemini output were not inspected.

## Parser fallback for uvicorn --reload on Windows (2026-10-23)

- Symptom: `document_subprocess_unsupported_loop` + 503 on /internal/documents/extract.
  uvicorn 0.54 uses SelectorEventLoop on Windows when --reload is set, which cannot
  run asyncio subprocesses. DocumentParser now falls back to subprocess.Popen waited
  via asyncio.to_thread (same minimal env, kill on deadline/cancel); log code
  `document_subprocess_thread_fallback`.
- Validation: tests/test_documents.py 18 passed, including a real Windows
  SelectorEventLoop extraction and a fallback deadline-kill test.

## Question configs for generation (2026-10-23)

- Added QuestionType/QuestionConfig/QuestionBlueprint; text and grounded requests
  accept question_configs or the legacy question_format+count. Prompts list ordered
  slots; check_slots validates count and per-type correct options.
- Validation: full pytest **108 passed, 3 skipped** (new tests for mixed configs,
  wrong-slot answers, invalid/ambiguous configs, legacy normalization and grounded
  ordering). RAG OpenAPI regenerated; drift test passes. No paid calls were made.

## RAG/parser review corrections (2026-10-23)

- RagBodyLimit releases its four intake slots as soon as body reading completes,
  before downstream processing. Failed intake never reaches FastAPI; existing
  408/413/503 and disconnect behavior is retained.
- Six RAG routes declare RagErrorResponse (detail.code/detail.retryable) for
  supported errors, including 422. RAG authentication now uses the same shape for
  401 and missing-token configuration 503; text/document auth remains unchanged.
  Regenerated contracts/rag-indexing.openapi.json; drift checks passed.
- DocumentParser catches unsupported subprocess event loops separately, logs
  document_subprocess_unsupported_loop and returns DOCUMENT_PROCESS_FAILED/503.
  Child env is limited to PYTHONUTF8 and OS/temp startup variables; no inherited
  provider keys, DB URLs or PYTHONPATH. Other spawn arguments/cleanup are unchanged.
- Post-LLM verify_scope runs directly with a separate 5-second deadline, bypassing
  the 100 ms RAG admission window. RagError is preserved; unexpected storage errors
  map to retryable RAG_STORAGE_UNAVAILABLE, timeout to retryable RAG_DEADLINE.
- README's current tree/status now includes implemented RAG schemas, services,
  repositories, migrations and contracts; OCR/other providers remain future work.
- Validation: 57 focused tests passed; full pytest 96 passed / 3 skipped.
  Two additional enabled Postman simulation tests passed with real SDK HTTP;
  request events confirmed embedding 200/429/403 and grounded generation 200.
  The existing local mock uses synthetic content/vectors/provider responses;
  no paid provider or live database was used. compileall, pip check and
  git diff --check passed. Real pgvector integration remains unverified.

### Verification for grounded generation (2026-10-23)

- Full pytest: **80 passed, 3 skipped**. Eight grounded tests cover exact/corrupt
  citations, unknown chunks, duplicates, wrong answers/count, strict input/auth,
  pre/post-call scope and cancellation releasing the shared generation slot.
- Enabled local Postman Google simulation: **2 passed**, exercising real SDK HTTP
  embedding/retrieval/structured generation and 429/403 handling against synthetic
  responses. It does not prove Google semantic quality or live quota access.
- RAG OpenAPI regenerated; contract drift check passed. pip check/compileall passed.
  Existing text-generation.v1 remains unchanged. Real pgvector and live S3/browser
  E2E remain unverified; no credentials, source documents or paid calls were used.

## Grounded document question generation (2026-10-23)

- POST /internal/rag/generate implements rag-generation.v1, authenticated with the
  existing token. GroundedGeneration retrieves authorized READY chunks, shares
  QuestionGenerator admission/provider/deadline and rechecks full scope afterward.
- GeminiProvider.generate_grounded uses a separate structured response schema;
  text-generation.v1 stays unchanged. Validate exact count, distinct questions,
  four options/one answer, known chunk IDs and exact nonblank evidence quotes.
- Citations include original source chunks/positions/hashes. UNASSESSED is explicit:
  quote matching does not establish answer entailment, factual accuracy or full-file
  coverage. Never fabricate citations or fall back to general knowledge.
- Total grounded deadline 75s; NestJS uses 85s. Generation shares the existing
  semaphore/client and bounded SDK retries. No Python bank/business writes.
- Export contracts/rag-indexing.openapi.json after route/schema changes. Tests use
  synthetic providers or real SDK HTTP against local Postman fixtures, never real
  keys/documents. Backend/Next.js now accept attached PDFs/PPTX and retain evidence
  through review/edit/commit. See https://github.com/anhquan-ngg/testhub-v2-backend/blob/master/docs/rag-indexing.md section 4.

### Verified RAG checks (2026-10-21)

- Full pytest: **72 passed, 2 skipped** (local Postman simulation and real pgvector
  tests opt in). Separate enabled SDK/Postman HTTP test: **1 passed**, with actual
  simulation request events for 200, 429 and 403. No paid Google requests.
- Installed psycopg 3.3.6 / psycopg-pool 3.3.3 in Python 3.12 venv; pip check and
  compileall passed. SDK is google-genai 1.75.0. Chunk Unicode/offset/byte coverage,
  finite/dimension normalization, cancellation, checkpoint cleanup, pre/post scope,
  HTTP auth/limits/safe validation, selector bridge and contract drift passed.
- Backend-to-FastAPI RAG HTTP transport passed with synthetic embedding/repository;
  this does not test SQL persistence. Docker daemon unavailable, so real pgvector
  integration remains unverified. Migrations/account setup were not applied.

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
- On Windows, preserve the Proactor main loop for parser subprocesses; the psycopg
  pool runs on an owned selector-loop thread. Readiness checks DB schema, not Google.
- Setup/API/limits: [backend RAG guide](https://github.com/anhquan-ngg/testhub-v2-backend/blob/master/docs/rag-indexing.md).
  Contract: contracts/rag-indexing.openapi.json; regenerate via
  `python scripts/export_rag_contract.py`. Run pytest and opt-in SDK HTTP simulation;
  real pgvector tests require isolated RAG_TEST_DATABASE_URL, never RAG_DATABASE_URL.

Tệp bàn giao trạng thái. Chỉ ghi điều đã xác minh từ repo hoặc được người dùng xác nhận; tách rõ thiết kế dự kiến khỏi mã nguồn đã có.

## Trạng thái hiện tại

### Cập nhật 2026-10-21 — PDF/PPTX extraction

- Đã triển khai `/internal/documents/extract`, application/octet-stream và
  X-Internal-Token. Không fetch URL, không credential S3/DB và không gọi Gemini.
- Schema document-extraction.v1 giữ source hash, phiên bản parser thực, notes opt-in,
  unit_count, PAGE/SLIDE/NOTES, warnings và EXTRACTED/OCR_REQUIRED. Không có RAG index.
- pypdf 6.21.1 và python-pptx 1.0.2 được cài vào .venv Python 3.12. Parser nằm trong
  process con có deadline/kill/await, temp cleanup, giới hạn thực bytes/ZIP/text;
  cap memory/CPU bổ sung trên POSIX, Windows local không có OS cap tương đương.
- OpenAPI export ở contracts/document-extraction.openapi.json, tái tạo bằng
  `python scripts/export_document_contract.py`; test chống contract drift.
- Backend/Next.js đã có batch/review/commit văn bản và nay có document metadata,
  immutable versions, queue worker riêng, upload/polling/preview. Migration do
  NestJS sở hữu, không áp vào DB người dùng trong phiên này. Xem hướng dẫn backend.
- Kiểm tra parser và generation regression: **53 pytest pass**; compileall pass,
  pip check không dependency hỏng. Test có file PDF text/mã hóa/scan-trộn-text,
  PPTX text/notes/group-image, malformed/oversized/ZIP, deadline giết child và
  giải phóng slot. HTTP thật Nest client→FastAPI→PDF/PPTX đã pass; không gọi LLM trả phí.
- Chưa xác nhận live S3/CORS/IAM hoặc full browser/database E2E; Docker daemon
  local không hoạt động. Chunking/embedding/pgvector/retrieval/OCR vẫn chưa có.

### Historical text MVP snapshot (2026-10-16)

- Repo `testhub-ai-service` ở nhánh `master`, ban đầu chỉ có `.git`. Hiện đã có MVP FastAPI trong `app/`, test trong `tests/`, dependency, Dockerfile, `.dockerignore`, `.env.example` và bốn file tài liệu. Chưa có DB, migration, parser hoặc RAG. NestJS đã có client HTTP và service kiểm tra quyền/consent; public batch API/worker/UI chưa triển khai.
- Kiến trúc dài hạn bắt nguồn từ [đoạn chat chia sẻ](https://chatgpt.com/share/6ad1056c-d7cc-83ec-bcf1-d34d78b0aca9); MVP được triển khai theo yêu cầu người dùng ngày 16/10/2026.
- Hai repo cùng cấp là `../testhub-v2` (Next.js) và `../testhub-v2-backend` (NestJS). Prototype gọi Gemini trực tiếp trong backend đã được gỡ ngày 16/10/2026; sinh câu hỏi thuộc Python service này. Module `question-generation` mới ở NestJS chỉ gọi FastAPI, kiểm tra JSON và trả bản nháp, không ghi ngân hàng. Contract `text-generation.v1` có fixture hai repo và test HTTP liên repo dùng provider giả.

## Kiến trúc đã chọn để hướng dẫn triển khai

- Phiên bản Python mục tiêu là **3.12** cho local và Docker; `Dockerfile` dùng `python:3.12-slim`. Môi trường `.venv` local đã xác nhận chạy Python 3.12.3.
- Python FastAPI là service AI/RAG nội bộ; frontend không gọi trực tiếp.
- NestJS giữ xác thực, quyền tài liệu, BullMQ, batch/drafts, duyệt và ngân hàng câu hỏi. Python xử lý tài liệu, chunking, embedding, retrieval, prompt và LLM.
- MVP đã có: `POST /internal/generate` nhận `text`, `topic?`, `question_format`, `count`; yêu cầu `X-Internal-Token`; gọi Gemini qua `google-genai` với structured output; kiểm tra JSON rồi trả danh sách bản nháp câu một đáp án theo shape TestHub. `GET /health` không gọi Gemini. Giới hạn request 20.000 ký tự, 1–20 câu; timeout và số request Gemini đồng thời cấu hình bằng env.
- Đã sửa 10 điểm trong review: config đọc một lần và client Gemini dùng chung qua lifespan; router chỉ gọi service, auth ở dependencies và lỗi HTTP ở handler tập trung. `QuestionGenerator` sở hữu semaphore/deadline tổng; SDK retry tối đa 3 lần gọi mặc định, nằm trong deadline. `httpx.TimeoutException` và hết deadline trả 504; client constructor/cleanup được xử lý an toàn; log chỉ có giai đoạn/loại lỗi/mã HTTP.
- Token so sánh bằng byte UTF-8 để không lỗi với input ngoài ASCII; topic trắng chuyển thành `None`. JSON được parse/validate bằng `model_validate_json` và ánh xạ sang bản nháp từ các trường đã kiểm tra.
- Mục tiêu RAG: tài liệu gốc trên S3/MinIO; NestJS sở hữu metadata và bảng nghiệp vụ trong `public`; AI service sở hữu chunks/embeddings trong schema PostgreSQL `rag` với pgvector. Các bảng, schema và migration này **chưa tồn tại trong repo này**.
- Tích hợp nền dự kiến: NestJS worker gọi endpoint đang có, nhận câu hỏi có cấu trúc rồi lưu bản nháp; về sau có thể tách Python worker riêng cho tác vụ dài.
- Cây thư mục mục tiêu và phần đã hiện thực được ghi ở `AGENTS.md` và `README.md`. Provider OpenAI/Grok và RAG chưa có.

## Việc tiếp theo

1. Đã kiểm tra một lượt Node client → FastAPI → Gemini thật với fixture văn bản: trả 1 câu SINGLE_CHOICE hợp lệ. Khi đổi env/model/key cần khởi động lại process; kết quả này không xác nhận chất lượng trên cả bộ tài liệu.
2. Client/contract NestJS đã có; tiếp theo thêm batch/draft persistence, BullMQ, polling UI, chỉnh sửa/duyệt/commit ở NestJS và frontend. Python giữ nguyên endpoint văn bản.
3. Khi MVP đã tích hợp, mới làm parser, embeddings, pgvector và retrieval.

## Chưa quyết định

- Contract văn bản giữ `text/topic/question_format/count`; không gửi `batchId` cho endpoint stateless hiện tại. Cơ chế durable replay hoặc contract RAG bổ sung sau.
- Cách chuyển file sau MVP và phương án quản lý/luân chuyển token nội bộ giữa hai service.
- Mô hình embedding, số chiều vector, chiến lược chunking và migration schema `rag`.
- Có dùng chung PostgreSQL hay tách database khi triển khai; phương án vận hành Python worker dài hạn.

## Kiểm tra

- Ngày 20/10/2026: đổi liên kết hướng dẫn tích hợp trong README sang URL GitHub trực tiếp của backend; xác nhận owner/repo từ remote, nhánh mặc định `origin/master`, file đích tồn tại ở local và `git diff --check` pass. Chưa kiểm tra truy cập URL trên GitHub.
- `python -m pytest -q`: **37 passed**, không warning, trên Python 3.12.3. Test mới dùng SDK google-genai 1.75.0 thật với `httpx.MockTransport`: timeout → 504, retry 429/503 hữu hạn, không retry 403, log không chứa exception message/traceback, client dùng lại và đóng khi shutdown, constructor/cleanup lỗi, deadline hủy request và giải phóng slot, token ngoài ASCII, topic trắng, config snapshot, giới hạn concurrency và caller cancellation.
- `httpx>=0.27,<1` được khai báo trực tiếp trong `requirements.txt` vì runtime bắt `httpx.TimeoutException`; test dùng `httpx2>=2.13,<3` trong `requirements-dev.txt`.
- `python -m compileall -q app tests` chạy thành công; `python -m pip check` báo không có dependency hỏng.
- OpenAPI đã xác nhận có `/health` và `/internal/generate`; kiểm tra khoảng trắng cuối dòng trên các file nguồn/tài liệu không báo lỗi.
- Kiểm tra bổ sung bước 1–2 ngày 16/10/2026: **39/39 pytest pass**, gồm hai test contract NestJS mới. HTTP liên repo bằng Python 3.12 và Jest Node 22 đã pass; hai fixture versioned giống nhau. `tests/contract_http_server.py` chỉ là test host với provider giả trên loopback, không nạp `.env` hay gọi Gemini.
- Kiểm tra riêng bằng client Node đã build → FastAPI thực → Gemini thực: **pass, 1 câu SINGLE_CHOICE**. Python tự nạp cấu hình local trong runtime, không in/sửa bí mật, không ghi DB. 37 test cũ và hai test mới vẫn không gọi API trả phí.
