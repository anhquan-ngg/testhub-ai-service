# testhub-ai-service

## Source-wide knowledge plans (2026-10-25)

- POST /internal/rag/outline reconstructs all authorized chunks, recognizes numbered chapter/section headings and proposes inferred page topics when no outline exists. These are editable heuristics, not validated semantic classifications.
- Repeated slide titles ending in numeric counters such as `Constructor method(2)`
  or `Constructor method (3)` become one topic with the union of their source pages.
  Numbered sections stay scoped to their section code/parent; named qualifiers
  such as `(Java)` remain distinct. Existing saved plans are not rewritten: restart
  the AI service and create a new plan to use the revised outline.
- GroundedGenerateRequest.plan_items binds each cognitive cell to an objective and source units. SQL filters allowed units BEFORE nearest-neighbor LIMIT; missing cell evidence fails rather than substituting another topic.
- One provider invocation receives isolated ordered slot contexts. Validate each quote against that slot only and assign plan_item_id server-side. Recheck authorization after I/O.
- Apply new manual migration migrations/002_optional_document_chapter.sql before topic-owned document indexing; old migrations remain unchanged.
- Config rows now max20 / total questions max20. Keep exported RAG OpenAPI in sync. CONTINUITY.md is local and ignored/untracked.


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
- On Windows, parser subprocesses use asyncio on Proactor loops and fall back to
  Popen with waiting in a thread on selector loops; the psycopg
  pool runs on an owned selector-loop thread. Readiness checks DB schema, not Google.
- Setup/API/limits: [backend RAG guide](https://github.com/anhquan-ngg/testhub-v2-backend/blob/master/docs/rag-indexing.md).
  Contract: contracts/rag-indexing.openapi.json; regenerate via
  `python scripts/export_rag_contract.py`. Run pytest and opt-in SDK HTTP simulation;
  real pgvector tests require isolated RAG_TEST_DATABASE_URL, never RAG_DATABASE_URL.

FastAPI nội bộ sinh câu hỏi từ văn bản, trích xuất PDF/PPTX, chia chunks, tạo
embedding Google và truy xuất ngữ cảnh. Python chỉ ghi schema `rag`; NestJS sở hữu
dữ liệu nghiệp vụ và ngân hàng câu hỏi. Python không giữ credential S3.

## Trích xuất PDF/PPTX (2026-10-21)

`POST /internal/documents/extract?source_type=PDF&include_notes=false` nhận bytes
`application/octet-stream`, xác thực `X-Internal-Token`. Không cần key Gemini,
không gửi nội dung sang nhà cung cấp AI. `include_notes=true` chỉ dành cho PPTX
và cần giảng viên chọn. Contract:
[`document-extraction.openapi.json`](contracts/document-extraction.openapi.json).

- Tối đa 20 MiB, 500 trang/slide, 1 triệu ký tự; giữ vị trí từ 1.
- Kết quả `document-extraction.v1`: hash nguồn, loại file, tùy chọn notes,
  `unit_count`, `spans[{kind,position,text}]`, warnings và trạng thái.
- `EXTRACTED` là đã lấy text; `OCR_REQUIRED` là nguồn thiếu text/có trang ảnh
  không lấy được text. Không đánh dấu index READY ở giai đoạn này.
- pypdf/python-pptx chạy trong process con. ZIP được đọc theo ngân sách giải nén
  thực tế rồi repack trước parser. Không chạy nội dung nhúng hoặc fetch URL.
- Deadline 45s; tối đa 2 request/process, chờ slot 100ms. Hủy/quá hạn sẽ kill và
  await process con. Trên Linux/POSIX: thêm cap 768 MiB virtual memory, 40s CPU,
  output 8 MiB. Windows local chưa có OS memory cap tương đương; production dùng
  Linux container với resource limits. Dockerfile không chứng minh đã deploy thật.
- Lỗi: 401 token, 413 quá cỡ, 415 media type, 422 file/cấu hình sai, 503 hết năng
  lực/infra, 504 deadline. Không lộ source, mật khẩu file hay traceback parser.

Backend chạy `npm run start:document-worker` riêng sau khi áp migration local và
build. UI: `/{locale}/lecturer/questions/documents`. Xem
[hướng dẫn cả luồng](../testhub-v2-backend/docs/learning-documents.md).
Cập nhật `.venv` hiện có bằng
`.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`; không chép đè `.env`.

## Chạy local

Yêu cầu **Python 3.12**; Gemini API key dùng cho sinh câu hỏi và embedding. Dockerfile dùng
`python:3.12-slim`. Trong PowerShell, tại repo, kiểm tra `python --version` rồi tạo môi trường:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Nếu `.venv` đã được tạo bằng phiên bản Python khác, hãy tạo lại bằng Python 3.12 trước khi cài dependency.

Điền `GEMINI_API_KEY` và một chuỗi bí mật đủ mạnh vào `INTERNAL_API_TOKEN` trong `.env`. Giữ file này ở local, không commit. `GEMINI_MODEL` mặc định là `gemini-3.5-flash-lite`; có thể đổi sang model mà key của bạn được phép dùng. Chạy:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --env-file .env --host 127.0.0.1 --port 8000
```

`GET /health` trả `{"status":"ok"}` mà không gọi Gemini. Swagger UI ở `http://127.0.0.1:8000/docs`. Chỉ bind `127.0.0.1` khi phát triển local; trong Docker, chỉ kết nối service này qua mạng nội bộ.

## Contract `POST /internal/generate`

Header bắt buộc: `X-Internal-Token: <INTERNAL_API_TOKEN>`.

```json
{
  "text": "Tìm kiếm nhị phân hoạt động trên mảng đã sắp xếp và có độ phức tạp O(log n).",
  "topic": "Cấu trúc dữ liệu",
  "question_configs": [
    { "question_type": "SINGLE_CHOICE", "question_format": "KNOWLEDGE", "count": 1 }
  ]
}
```

- `text`: 20–20.000 ký tự sau khi bỏ khoảng trắng ở đầu/cuối.
- `topic`: tùy chọn, tối đa 200 ký tự; chuỗi rỗng hoặc chỉ có khoảng trắng được chuyển thành `null`.
- `question_configs`: 1–10 dòng `{question_type, question_format, count}`, tổng 1–20 câu. `question_type` là `SINGLE_CHOICE` hoặc `MULTIPLE_CHOICE`; `question_format` là `KNOWLEDGE`, `UNDERSTANDING`, `APPLYING` hoặc `ADVANCED`. Câu được sinh và kiểm tra theo đúng thứ tự các dòng.
- Tương thích ngược: vẫn nhận dạng cũ `question_format` + `count` (1–20, một đáp án); không được gửi cùng lúc hai dạng. `/internal/rag/generate` dùng cùng quy tắc.

Ví dụ kết quả:

```json
{
  "questions": [
    {
      "question_text": "Độ phức tạp của tìm kiếm nhị phân là gì?",
      "options": [
        { "text": "O(1)", "isCorrect": false },
        { "text": "O(log n)", "isCorrect": true },
        { "text": "O(n)", "isCorrect": false },
        { "text": "O(n²)", "isCorrect": false }
      ],
      "question_type": "SINGLE_CHOICE",
      "question_format": "KNOWLEDGE",
      "correct_answer": null
    }
  ]
}
```

`question_text`, `options`, `question_type`, `question_format` và `correct_answer` dùng các tên trường của ngân hàng câu hỏi TestHub; khi tích hợp NestJS cần chuyển mảng `options` thành chuỗi JSON theo schema backend. Prototype gọi Gemini trong backend đã được gỡ; Python service là nơi thực hiện sinh câu hỏi. Service kiểm tra số câu, bốn lựa chọn khác nhau, số đáp án đúng theo loại câu ở từng vị trí (`SINGLE_CHOICE` đúng 1, `MULTIPLE_CHOICE` 2–3), độ dài và câu hỏi trùng. Việc kiểm chứng nội dung với tài liệu và duyệt cuối vẫn thuộc giảng viên/backend.

Mã lỗi chính: `401` token thiếu/sai; `422` request sai; `503` thiếu cấu hình hoặc hết năng lực xử lý; `502` Gemini lỗi hoặc JSON không hợp lệ; `504` Gemini timeout. Nội dung lỗi upstream không chứa API key, prompt hay đoạn văn bản.

## Cấu hình và xử lý lỗi

Ứng dụng đọc biến môi trường một lần trong lifespan khi khởi động, tạo một Gemini client dùng chung cho mỗi process và đóng các transport khi dừng. Thay đổi `.env` cần khởi động lại ứng dụng. Thiếu key hoặc không khởi tạo được client thì `/internal/generate` trả `503`; `/health` vẫn là kiểm tra tiến trình, không xác nhận Gemini khả dụng. Cấu hình số không hợp lệ khiến startup thất bại với thông báo tên biến, không in giá trị bí mật.

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `GEMINI_TIMEOUT_MS` | `30000` | Timeout HTTP của SDK, từ 1 đến 120.000 ms. |
| `GENERATION_TIMEOUT_MS` | `30000` | Deadline tổng từ lúc vào service, gồm chờ suất xử lý, retry và kiểm tra JSON; từ 1 đến 180.000 ms. |
| `GEMINI_MAX_ATTEMPTS` | `3` | Tổng số lần gọi tối đa, gồm lần đầu; từ 1 đến 5, đặt 1 để tắt retry. |
| `MAX_CONCURRENT_GENERATIONS` | `4` | Số lượt sinh đồng thời trong mỗi process, từ 1 đến 100. Chờ suất tối đa 1 giây; hết suất trả `503`. |

Retry dùng exponential backoff có jitter, độ trễ tối đa 2 giây, cho HTTP `408`, `429`, `500`, `502`, `503`, `504` và lỗi kết nối/timeout do SDK hỗ trợ. Lỗi cố định như `400`/`401`/`403` và JSON sai không được thử lại. Mọi lần gọi vẫn nằm trong deadline tổng; hết deadline trả `504`, hủy tác vụ và giải phóng suất xử lý. Vì vậy deadline có thể kết thúc trước khi đủ số lần thử tối đa. Xem [cấu hình retry của SDK](https://googleapis.github.io/python-genai/genai.html#genai.types.HttpRetryOptions).

Log lỗi Gemini chỉ ghi giai đoạn, tên loại exception và mã HTTP nếu có; không ghi exception message, traceback, key, prompt hay tài liệu nguồn. Lỗi đóng client được log riêng và không thay thế lỗi của request.

Ví dụ gọi local trong PowerShell (chạy sau khi cấu hình `.env`):

```powershell
$body = @{ text = 'Tìm kiếm nhị phân hoạt động trên mảng đã sắp xếp và có độ phức tạp O(log n).'; topic = 'Cấu trúc dữ liệu'; question_configs = @(@{ question_type = 'SINGLE_CHOICE'; question_format = 'KNOWLEDGE'; count = 1 }) } | ConvertTo-Json -Depth 4
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/internal/generate' -Method Post -ContentType 'application/json' -Headers @{ 'X-Internal-Token' = '<giá trị INTERNAL_API_TOKEN của bạn>' } -Body $body
```

## Kiểm thử

Contract tích hợp NestJS được cố định ở `contracts/text-generation.v1.json`;
`tests/test_nestjs_contract.py` kiểm tra fixture qua FastAPI thật với provider giả.
Backend đã có client và service kiểm tra quyền tại
`../testhub-v2-backend/src/question-generation/`. Xem
[hướng dẫn tích hợp](https://github.com/anhquan-ngg/testhub-v2-backend/blob/master/docs/ai-question-generation.md) để cấu hình
`AI_SERVICE_INTERNAL_TOKEN` khớp `INTERNAL_API_TOKEN` và chạy test HTTP liên repo.
Token cho client Node dùng 32–256 ký tự ASCII nhìn thấy được; deadline NestJS mặc
định 35 giây, lớn hơn deadline Python 30 giây cho sinh câu từ văn bản. NestJS và
frontend đã có batch/worker/staging, sửa/duyệt và commit. Trích xuất tài liệu dùng
deadline riêng (Python 45s, NestJS mặc định 55s).

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Test dùng provider giả lập và SDK thật với `httpx.MockTransport`, không cần key thật và không gọi mạng. Chúng kiểm tra contract, token ngoài ASCII, topic rỗng, JSON đầu ra, timeout, deadline/cancellation, concurrency, retry hữu hạn, vòng đời client và log an toàn. Lời gọi Gemini thật cần key hợp lệ; service không tự thực hiện smoke test thật.

## Khắc phục lỗi RAG

### Planned generation returns 502

The access log alone does not identify the failure. Inspect the preceding
`generation_invalid_output reason=... mode=planned slot=...` or `gemini_failure`
line. Planned schema and citation failures now emit sanitized diagnostics;
question text, quotes, provider output and credentials remain private.

`reason=duplicate_citations` in an older process means the model repeated a
chunk ID within one question, not that questions duplicate the bank. Generation
now validates every supplied quote against the question's authorized chunks,
then merges repeated references into one exact contiguous source span (<=600
characters). Identical references are deduplicated; different quotes from the
same chunk retain all cited evidence in the merged span. An unknown chunk,
unsupported quote or combined span over 600 characters still fails the batch.
The returned contract retains one citation per chunk; answers are not modified.

Restart AI after updating code. For a stable demo, omit `--reload`; for editing,
limit watching to `--reload --reload-dir app` so test edits do not restart it.
If interrupted Windows reloads print KeyboardInterrupt/BrokenPipeError/EOFError,
stop Uvicorn with Ctrl+C and start it again; that traceback alone does not explain
an earlier generation 502. Wait for `Application startup complete`.

To intentionally generate a new batch from an unchanged approved plan in the
current UI, reload the browser page, reopen the plan and generate. Repeated
clicks in one page session reuse its Idempotency-Key and can return the failed
batch; reloading the plan alone does not reset that key. No reupload/migration
is needed for this citation fix, and old failed batches retain their status.

### RAG generation fails with reason=answers

`generation_invalid_output reason=answers` means the returned JSON passed shape
validation but a question's `isCorrect` count does not match its requested type.
SINGLE_CHOICE requires one true; MULTIPLE_CHOICE accepts two or three, never zero,
one or all four. This is model-output validation, not a database/key setup error.

Prompts now give every ordered slot an explicit four-option budget, targeting
one correct/three distractors for single choice and two correct/two distractors
for multiple choice. The SDK schema also constrains options to exactly four.
Validation still accepts three supported correct options for existing multiple
choice results. It never changes answer flags or silently reissues a paid call.
Sanitized diagnostics identify the first failing slot, for example:

```text
generation_invalid_output reason=answers slot=2 question_type=MULTIPLE_CHOICE correct_count=1 option_count=4
```

Restart the AI service after updating code, then open the approved plan again
and create a new generation batch. Existing failed batches are not overwritten.
Prompt constraints reduce ambiguity but do not guarantee correct model output;
lecturer review and server-side validation remain required.

### RAG prepare fails with NotNullViolation

Topic-owned documents may not have a chapter until approved questions are
committed. If the database still has `rag.document_indexes.chapter_id NOT NULL`,
apply migration 002 to the confirmed local database as its schema owner:

```powershell
psql -h 127.0.0.1 -p 5432 -U testhub_ai -d testhub -W -v ON_ERROR_STOP=1 -f migrations/002_optional_document_chapter.sql
```

This preserves existing rows and allows a NULL chapter. Do not edit migration
001 or invent a chapter ID. `/internal/rag/profile` does not check storage;
`/ready` requires migrations 1/2 and the nullable chapter column. Restart the AI
service after updating readiness code. Retry failed indexing from the document
workspace; the original uploaded file need not be uploaded again. If a generation
batch already failed, finish indexing before creating a new batch.

## Cấu trúc hiện có

```text
app/
├── main.py
├── config.py
├── errors.py
├── api/{generation,documents,rag,dependencies,errors}.py
├── providers/{base,gemini}.py
├── schemas/{question,document,rag,grounded_generation}.py
├── services/{prompt_builder,question_generator,document_parser,document_parser_worker}.py
├── services/{chunking,embedding,retrieval,rag_errors,grounded_generation}.py
└── repositories/{vector_repository,windows_pool}.py
migrations/{000_bootstrap,001_rag,002_optional_document_chapter}.sql
contracts/{text-generation.v1,document-extraction.openapi,rag-indexing.openapi}.json
scripts/{export_document_contract,export_rag_contract}.py
tests/
requirements.txt
requirements-dev.txt
Dockerfile
.env.example
```

Parser PDF/PPTX, chunking, Google embedding, pgvector, retrieval và sinh câu RAG có
trích dẫn đã được triển khai. OCR và provider khác vẫn là bước sau. NestJS
giữ quyền truy cập, metadata, batch, bản nháp và duyệt/commit. Xem
`CONTINUITY.md` local (nếu chưa có, tạo ghi chú trạng thái đã xác minh; file được
gitignore, không thêm vào Git) và hướng dẫn backend cho trạng thái/lệnh chạy.
