# TestHub AI Service

## Overview

Internal FastAPI service for TestHub question generation, PDF/PPTX text extraction, and retrieval-augmented generation (RAG). The NestJS backend calls this service; the web frontend must not call it directly.

This is a standalone Python repository, not a Python worker inside the NestJS application. NestJS owns authentication, authorization, uploads, queues, batches, draft review, and question-bank writes. This service owns parsing, technical RAG storage, embeddings, retrieval, prompts, and structured-output validation.

## Key Features

- Generate Vietnamese `SINGLE_CHOICE` and `MULTIPLE_CHOICE` questions with ordered cognitive-level slots and four distinct options.
- Select Gemini or an OpenAI-compatible 9Router generation adapter; optionally fall back from 9Router to Gemini for eligible availability failures only.
- Extract PDF pages and PPTX slides/optional notes with original positions, hashes, text offsets, and per-unit diagnostics. No OCR engine is included.
- Index and retrieve owner-scoped document chunks using Google embeddings and PostgreSQL/pgvector.
- Propose source-wide knowledge outlines for backend-owned plans and generate against frozen plan slots, source units, and objectives.
- Validate citations, counts, answers, and complete-batch output. Valid evidence refusals return a distinct domain error rather than partial questions.

## Technology Stack

Versions are declared dependency ranges unless identified as a runtime target; this repository has no dependency lockfile.

| Area | Technology |
| --- | --- |
| Runtime | Python 3.12 target; Docker uses `python:3.12-slim` |
| HTTP/API | FastAPI `>=0.115,<1`, Uvicorn `>=0.30,<1`, Pydantic `>=2.9,<3` |
| Providers | Google Gen AI SDK `>=1.0,<2`; HTTPX `>=0.27,<1` for 9Router |
| Documents | pypdf `>=6.21.1,<7`; python-pptx `>=1.0.2,<2` |
| RAG storage | PostgreSQL with pgvector; psycopg binary/pool `>=3.2,<4` |
| Configuration | Environment variables; python-dotenv `>=1.0,<2` through Uvicorn's environment-file option |
| Tests | pytest `>=8,<10`; additional development dependencies in `requirements-dev.txt` |

## Architecture and Project Structure

```text
TestHub web (:3000)
  -> NestJS API (:3001) -> Redis/BullMQ -> dedicated NestJS workers
       |                                      |
       | business data, uploads, review        | X-Internal-Token
       v                                      v
  PostgreSQL + S3                      FastAPI (:8000)
                                         |-- bounded parser subprocesses
                                         |-- Gemini or 9Router generation
                                         |-- Google embeddings
                                         `-- PostgreSQL rag schema / pgvector
```

```text
app\
  api\             Internal routes, authentication, error mapping
  providers\       Gemini, 9Router, provider selection and fallback
  schemas\         Request/response contracts and validation
  services\        Generation, parsing, embedding, retrieval and storage
  config.py        Environment defaults and validation
  main.py          Application lifespan, health and readiness
contracts\         Committed extraction and RAG contracts
migrations\        Manual SQL bootstrap and RAG schema migrations
postman\           Optional local embedding simulation
scripts\           Contract export maintenance commands
tests\             Offline tests and opt-in integration tests
Dockerfile         Non-root Python 3.12 runtime image
```

The Python process has no Redis/BullMQ consumer and does not write business/question-bank tables. Parser subprocesses are resource-isolation helpers, not queue workers. Configure and operate the dedicated workers in the backend repository.

## Prerequisites

- Python 3.12 and pip for local development. Windows and Linux execution paths are implemented.
- A private internal token shared with NestJS for internal endpoints.
- A Google API key for Gemini generation, Gemini fallback, or any enabled RAG embeddings.
- A reachable OpenAI-compatible 9Router endpoint, API key, and supported model/combo when selecting 9Router.
- A PostgreSQL database with pgvector and the separately provisioned `rag` schema when enabling RAG. Plain PostgreSQL alone is insufficient.
- NestJS, its dedicated workers, Redis, and object storage for the complete application workflow; these are not provisioned by this repository.
- Docker is optional. PostgreSQL administration/`psql` is needed only when provisioning RAG storage.

Extraction itself does not require an AI provider key or perform a paid model call. Generation without RAG does not require PostgreSQL.

## Environment Configuration

Copy the tracked [.env.example](.env.example) without overwriting an existing local file, then replace placeholders. Never commit actual credentials.

`Settings.from_env()` reads process environment during startup. It does **not** load `.env` automatically: use `--env-file .env` or inject variables through the process manager/container. Restart the service after changing configuration.

| Variable | Code default | Requirement and behavior |
| --- | --- | --- |
| `INTERNAL_API_TOKEN` | Empty | Required for internal endpoints. Use 32-256 visible ASCII characters to satisfy the NestJS client contract; match backend `AI_SERVICE_INTERNAL_TOKEN`. |
| `LLM_PROVIDER` | `gemini` | `gemini` or `9router`; changes generation only. |
| `GEMINI_API_KEY` | Empty | Required for Gemini generation, opt-in Gemini fallback, and RAG embeddings even with 9Router generation. |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Generation model; select one available to the configured account. The default string is not an availability guarantee. |
| `GEMINI_TIMEOUT_MS` | `30000` | Gemini call timeout, `1..120000`. |
| `GENERATION_TIMEOUT_MS` | `30000` | Total generation budget, `1..180000`, including admission, provider retries, and output validation. |
| `GROUNDED_GENERATION_TIMEOUT_MS` | `0` | `0..300000`; zero derives `max(75000, GENERATION_TIMEOUT_MS + 15000)`. An explicit value must be at least generation timeout + 15000. |
| `GEMINI_MAX_ATTEMPTS` | `3` | `1..5`, including the first attempt; retries remain inside the total budget. |
| `MAX_CONCURRENT_GENERATIONS` | `4` | `1..100` per Python process; generation-slot admission waits at most one second. |
| `ROUTER_BASE_URL` | Empty | Required for 9Router; HTTP(S), path ending in `/v1`, without embedded credentials, query, fragment, backslashes, whitespace, or control characters. |
| `ROUTER_API_KEY` | Empty | Required for 9Router; visible ASCII. |
| `ROUTER_MODEL` | Empty | Required for 9Router; nonempty model or combo ID, without control characters. |
| `ROUTER_TIMEOUT_MS` | `30000` | 9Router HTTP timeout, `1..120000`. |
| `ROUTER_MAX_ATTEMPTS` | `1` | `1..5`, including the first attempt; retries remain inside the total budget. |
| `ROUTER_MAX_OUTPUT_TOKENS` | `16384` | `1..65536`; must also be supported by the selected upstream model. |
| `ROUTER_RESPONSE_FORMAT` | `json_schema` | `json_schema` or `json_object`; output validation remains mandatory. |
| `LLM_FALLBACK_PROVIDER` | Empty | Optional `gemini` only; must differ from the primary provider and requires `GEMINI_API_KEY`. |
| `RAG_ENABLED` | `false` | Exact lowercase `true` or `false`; enable only after RAG provisioning. |
| `RAG_DATABASE_URL` | Empty | Required with RAG; use the restricted AI database role, not the backend business role. |
| `EMBEDDING_MAX_RPM` | `60` | `1..10000`; per-process budget of embedded **items** per sliding minute (conservative: batch items are counted individually). Divide by the process/replica count. |
| `EMBEDDING_MAX_CONCURRENCY` | `2` | `1..16`; in-flight embedding HTTP calls per process. |
| `EMBEDDING_MAX_ATTEMPTS` | `4` | `1..6`, including the first attempt; only 429/5xx/transport errors retry. |
| `EMBEDDING_MAX_BACKOFF_MS` | `8000` | `0..30000`; a server `Retry-After`/`RetryInfo` longer than this fails fast with `RAG_EMBEDDING_RATE_LIMITED`. |
| `EMBEDDING_RETRY_BUDGET_MS` | `15000` | `0..30000`; total wait (limiter + backoff) per embedding call, kept below the 40 s RAG deadline. |
| `EMBEDDING_CACHE_SIZE` | `2048` | `0..100000`; in-memory LRU of vectors keyed by model/dimensions/task/text hash; `0` disables. |
| `EMBEDDING_GOOGLE_API_KEY` | empty | Dedicated key for embeddings, ideally from a separate Google Cloud project; falls back to `GEMINI_API_KEY`. Quota is per project, so this isolates indexing from question generation (it is not a way to bypass quota). |
| `EMBEDDING_INDEXING_RPM_PERCENT` | `50` | `1..100`; share of `EMBEDDING_MAX_RPM` indexing may use so retrieval/generation requests always fit. `100` disables the cap. Indexing also pauses 250 ms while a retrieval call is in flight, and over-budget indexing returns retryable `RAG_EMBEDDING_RATE_LIMITED`, which the backend queue retries. |
| `EMBEDDING_INDEXING_MAX_CONCURRENCY` | `1` | `1..16`; concurrent indexing embedding calls per process; retrieval is not limited by it. |
| `EMBEDDING_PERSISTENT_CACHE_ENABLED` | `false` | Store query embeddings (sha256 key + vector only, never text) in `rag.embedding_cache`; needs migration `003`. Fails open on any database error. |
| `EMBEDDING_PERSISTENT_CACHE_TTL_DAYS` | `30` | `1..365`; age after which a cached query vector is ignored. |
| `EMBEDDING_DAILY_LIMIT` | `0` | `0` disables. Local per-process daily item guard; use `floor(project RPD / replicas)` (free tier: 1000). Resets at midnight Pacific (assumed) and on restart. At the limit calls fail early with non-retryable `RAG_EMBEDDING_QUOTA_EXHAUSTED`. |
| `EMBEDDING_DAILY_WARN_PERCENT` | `80` | `1..100`; logs `embedding_daily_budget_warning` once per day. |
| `EMBEDDING_DAILY_INDEXING_PERCENT` | `80` | `1..100`; indexing is rejected above this share of the daily limit so queries keep headroom. |

The example file supplies illustrative `http://127.0.0.1:20128/v1` and PostgreSQL URLs; these are not code defaults or proof that those services exist. Blank token/router settings and placeholder Google credentials must be configured before use.

### Provider selection and deadlines

9Router receives a non-streaming OpenAI-compatible `POST /v1/chat/completions`. This service chooses a model/combo, **not an individual Codex account**; account rotation and upstream rate-limit handling belong to 9Router.

Optional Gemini fallback handles eligible connection/capacity/upstream availability failures. It does not handle timeouts, malformed output, invalid answers/citations, or evidence refusal. A timeout observed during a failed 9Router attempt retains timeout semantics even if a later attempt fails differently. Retrying or falling back cannot exceed the shared generation deadline.

Keep nested deadlines ordered: backend outer deadline > grounded deadline > total generation deadline > selected provider HTTP timeout. Relevant defaults are:

| Operation | NestJS outer deadline | Python budget |
| --- | --- | --- |
| Text generation | `AI_SERVICE_TIMEOUT_MS=35000` | Generation `30000` ms |
| Grounded generation | `max(85000, AI_SERVICE_TIMEOUT_MS)` | Derived grounded `75000` ms; generation `30000` ms |
| Extraction | `DOCUMENT_SERVICE_TIMEOUT_MS=55000` | Parser `45000` ms |

Provider and generation defaults are both 30000 ms; they are not a tuned profile for slow models. An illustrative slower 9Router profile is router `100000`, generation `110000`, grounded `125000`, and backend `AI_SERVICE_TIMEOUT_MS=130000`. Apply related settings in their respective services and consider infrastructure/proxy timeouts and cost. Switching generation provider does not change the embedding profile or require reindexing.

## Installation and Local Development

Run commands from this repository. Clone it using the repository URL supplied by your project administrator; no public remote URL is assumed here.

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Configure `.env` before starting. For minimal local extraction, set a valid `INTERNAL_API_TOKEN`. For generation, configure the chosen provider. For RAG, complete the database steps below and provide a Google API key.

```powershell
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --env-file .env
```

- Process health: `http://127.0.0.1:8000/health`.
- Storage readiness: `http://127.0.0.1:8000/ready`.
- Interactive API reference: `http://127.0.0.1:8000/docs`.

Health returns `{"status":"ok"}` without calling providers. With RAG disabled, readiness returns `{"status":"ok","rag":"disabled"}`; with RAG enabled it checks storage/schema readiness and returns 503 on failure. Neither endpoint proves that Google or 9Router can serve requests. RAG initialization failures may leave the process running but RAG unavailable.

### RAG database provisioning

Use an operator-approved local database. These examples use host `127.0.0.1`, database `testhub`, DBA role `postgres`, and AI role `testhub_ai`; substitute your approved connection details. Do not run them against shared/production databases without authorization and backups.

1. A DBA applies [000_bootstrap.sql](migrations/000_bootstrap.sql). It enables pgvector in `public`, creates/checks the restricted AI login and schema ownership, and fails rather than silently weakening unsafe existing roles/schema. The bootstrap runs atomically with `ON_ERROR_STOP`; an existing pgvector installation outside `public` is rejected without relocation, and failed checks roll back the bootstrap changes.
2. Set the AI role password interactively; do not embed it in SQL files or shell history.
3. Apply migrations 001 and 002 as the restricted role, then configure `RAG_DATABASE_URL` and `RAG_ENABLED=true`.

```powershell
psql -h 127.0.0.1 -U postgres -d testhub -v ON_ERROR_STOP=1 -f migrations\000_bootstrap.sql
psql -h 127.0.0.1 -U postgres -d testhub -c '\password testhub_ai'
psql -h 127.0.0.1 -U testhub_ai -d testhub -v ON_ERROR_STOP=1 -f migrations\001_rag.sql
psql -h 127.0.0.1 -U testhub_ai -d testhub -v ON_ERROR_STOP=1 -f migrations\002_optional_document_chapter.sql
# Optional: durable query-embedding cache (only with EMBEDDING_PERSISTENT_CACHE_ENABLED=true)
psql -h 127.0.0.1 -U testhub_ai -d testhub -v ON_ERROR_STOP=1 -f migrations\003_embedding_cache.sql
```

Migrations are manual, not startup tasks, and separate from NestJS Prisma migrations. Readiness requires both migration versions and nullable `chapter_id` for topic-owned documents. The AI role owns only technical `rag` data; do not grant it business-table access. The profile endpoint alone does not verify the database schema.

## Common Commands

Python commands below use the local virtual environment explicitly; activation is not required.

| Command | Purpose / side effect |
| --- | --- |
| `.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt` | Install runtime and development dependencies. |
| `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --env-file .env` | Local development server. |
| `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --env-file .env` | Non-reloading server; restrict network access appropriately. |
| `.\.venv\Scripts\python.exe -m pip check` | Check installed dependency compatibility. |
| `.\.venv\Scripts\python.exe -m pytest -q` | Default test suite; real integration tests require explicit opt-in. |
| `.\.venv\Scripts\python.exe -m pytest -q tests\test_nestjs_contract.py` | Focused offline internal-boundary contract tests. |
| `.\.venv\Scripts\python.exe scripts\export_document_contract.py` | Maintenance only: rewrites the tracked extraction contract. |
| `.\.venv\Scripts\python.exe scripts\export_rag_contract.py` | Maintenance only: rewrites the tracked RAG contract. |

There is no configured project lint/typecheck/build command. Python package installation and contract export are not read-only verification steps.

## Testing and Code Quality

Default tests use fake providers, mocked transports, parser fixtures, and contract checks; a passing mock test does not certify a live provider. Review opt-in variables before running integration tests.

- **Offline contract:** `tests\test_nestjs_contract.py` checks FastAPI boundaries. The test-only `tests\contract_http_server.py` launcher is used by backend cross-repository tests; it is not the production entry point.
- **Google embedding simulation:** requires Postman CLI and the committed simulation; it is not a runtime dependency.

```powershell
postman simulation run postman\simulations\google-embedding.sim.yaml --port 4962 --no-history --output ndjson
# In a separate terminal:
$env:RUN_EMBEDDING_SIMULATION = '1'
& .\.venv\Scripts\python.exe -m pytest -q tests\test_embedding_simulation.py
```

- **Real pgvector tests:** set `RAG_TEST_DATABASE_URL` only for a disposable database on `localhost`/`127.0.0.1` whose name ends in `_test`, with pgvector already installed. The tests require an absent `rag` schema and refuse an existing one. Never substitute production/runtime `RAG_DATABASE_URL`.
- Contract exports intentionally update tracked artifacts. Inspect their diffs and synchronize backend consumers when changing contracts.

## Service Integration

### Endpoints and ownership

Internal calls require `X-Internal-Token`; NestJS `AI_SERVICE_INTERNAL_TOKEN` must equal Python `INTERNAL_API_TOKEN`. A missing configured token makes internal functionality unavailable; a missing/wrong request token returns 401. RAG errors use `RAG_AUTH_UNAVAILABLE` or `RAG_UNAUTHORIZED` respectively.

| Method | Endpoint | Responsibility |
| --- | --- | --- |
| `GET` | `/health`, `/ready` | Process and storage readiness; no internal token required. |
| `POST` | `/internal/generate` | Text-based structured generation. |
| `POST` | `/internal/documents/extract` | Binary PDF/PPTX extraction; `source_type=PDF` or `PPTX`, optional `include_notes=true` for PPTX only. |
| `GET` | `/internal/rag/profile` | Embedding/chunking profile. |
| `POST` | `/internal/rag/prepare`, `/internal/rag/step` | Prepare and advance fenced indexing. |
| `POST` | `/internal/rag/retrieve` | Authorized scoped retrieval. |
| `POST` | `/internal/rag/outline` | Source-wide outline proposal. |
| `POST` | `/internal/rag/generate` | Grounded/planned generation. |
| `POST` | `/internal/rag/revoke` | Revoke index access; returns 204. |

NestJS downloads authorized S3 objects and sends bytes; Python does not accept arbitrary download URLs or need S3 credentials. Enable backend `AI_QUESTION_GENERATION_ENABLED` and, for RAG, `AI_RAG_ENABLED`; Python separately requires `RAG_ENABLED`. The backend base URL is an origin such as `http://127.0.0.1:8000`, not a URL ending in `/internal`.

### Extraction and source eligibility

Extraction accepts `application/octet-stream` bodies up to 20 MiB, at most 500 pages/slides, one million extracted characters, and an 8 MiB result. The parser deadline is 45 seconds with two simultaneous requests per process and a 100 ms admission wait. POSIX child limits are 768 MiB virtual memory and 40 CPU seconds; Windows has no equivalent OS memory cap here. Apply production container resource limits separately.

Original 1-based `PAGE`/`SLIDE`/`NOTES` positions and offsets are preserved. OCR classification also considers repeated-margin removal and inherited slide background images, including theme references (`p:bgRef`); an explicit local background overrides layout/master backgrounds. No-text/no-image units remain blank. PPTX entries are checked against actual decompressed-byte budgets and integrity before repacking. Parser shutdown waits for in-progress spawns and worker cleanup; cancellation or an `OSError` while killing a child does not release its intake slot or temporary-file ownership before the child exits and is reaped.

- `EXTRACTED`: no OCR-suspected units and some text remains after boundary filtering, or extracted slide notes are available. This status alone does not prove meaningful knowledge coverage.
- `PARTIAL`: OCR-suspected units exist, but the remaining base text passes the usefulness heuristic. NestJS requires explicit owner consent before using partial content.
- `OCR_REQUIRED`: missing usable text or an insufficient useful remainder. `NO_USABLE_TEXT` can describe a blank/textless file; the status is not proof of a scanned document.

Both `EXTRACTED` and `PARTIAL` require at least one nonblank text span, including legacy payloads without `quality`. When supplied, `quality.text_units` equals the number of nonblank `PAGE`/`SLIDE` spans, excludes `NOTES`, and may overlap `ocr_suspected_units` for pages/slides containing both text and images.

Usefulness and OCR classification discount entire contiguous boundary blocks of short lines (at most 100 normalized characters) appearing in at least two and at least half of nonempty base units. Repeated interior lines remain body content and count toward usefulness; a short body repeated at the boundary is conservatively treated as a margin. Margin-only sources return `OCR_REQUIRED` and cannot be chunked, even without images (`NO_USABLE_TEXT`). Partial eligibility requires at least 200 non-whitespace characters across two useful base units, or one for a two-unit document. These checks do not modify original spans: accepted sources retain their text, including margins, and exact offsets in chunks. Extraction success/consent is separate from an index becoming `READY`; no OCR recognition is performed.

### RAG and generation contracts

- Embeddings stay on `gemini-embedding-001`: 768 dimensions, L2 normalization, explicit document/query tasks. 9Router does not replace this dependency.
- Chunks target 1200 UTF-8 bytes, maximum 1800, with 200-byte overlap; maximum 5000 chunks and embedding batches of 16.
- Embedding calls share a per-process item limiter, a concurrency cap, an LRU cache and in-call dedup; planned retrieval embeds identical objective queries once. Retries use jittered exponential backoff and honor `Retry-After`/`RetryInfo`; daily-quota 429s return non-retryable `RAG_EMBEDDING_QUOTA_EXHAUSTED` and stop calls for 5 minutes. Exhausted per-minute retries return retryable 503 `RAG_EMBEDDING_RATE_LIMITED`. Each call logs `embedding_call` (items, unique, cache hits, attempts, outcome, latency) without text. Attempts count actual requests within that invocation, including failure paths: zero before sending, one after one send, and the full retry total on exhaustion; concurrent calls do not share this counter. `scripts/benchmark_embedding_mock.py` compares behaviors against a mock rate-limited server only. Its CPython 3.12 virtual event scheduler shares one clock across concurrent jobs, mock-server latency, retries and deadlines, with deterministic midpoint jitter and no scaled real-time sleeps. Results model the synthetic scenarios, not live Gemini quota guarantees.
- General retrieval defaults to six chunks, maximum eight, across 1-10 unique indexes with a 12 KB context budget. Planned retrieval caps context at two chunks per item and 72 KB total, with source-unit filtering before nearest-neighbor limiting.
- Generation storage failures use the existing `retryable` flag as a phase guarantee: `RAG_STORAGE_UNAVAILABLE` is retryable only before any model call. Post-model scope-check failures, including typed upstream errors and timeouts, are non-retryable so a retry cannot repeat a paid generation. Nest also excludes ambiguous `RAG_DEADLINE` failures from automatic retries.
- Embedding admission rechecks cooldown and quota breakers after concurrency waits, and records RPM usage only when a send can begin. `Retry-After` accepts seconds or HTTP dates; batches larger than the configured per-minute item limit are rejected before sending. Indexing leases refund a claim only when its token still matches and the adapter proves no upstream request started; incurred or ambiguous upstream usage is never reset.
- Only authorized `READY` owner/index/profile scopes are searchable. Revocation tombstones, fenced indexing leases, and pre/post-I/O scope checks prevent stale access.
- Outlines are editable source-wide heuristics, not paid semantic plans or a guarantee of document completeness. Existing saved plans are not silently rewritten.
- Text generation accepts 20-20000 characters and an optional topic of at most 200 characters. Use 1-20 `question_configs` rows, each count 1-20, with 1-20 total questions. Legacy `question_format` + `count` is accepted only without `question_configs`.
- Types are `SINGLE_CHOICE` and `MULTIPLE_CHOICE`, not essay. Cognitive levels are `KNOWLEDGE`, `UNDERSTANDING`, `APPLYING`, and `ADVANCED`; single-choice has one correct option, multiple-choice two or three.
- Grounded success has 1-20 questions and `quality_status=UNASSESSED`. Exact citations prove source provenance, not semantic entailment, answer correctness, or full-document coverage. Quotes referencing the same chunk are validated individually and merged only into an exact contiguous span of at most 600 characters.
- Planned slot context is isolated and ordered; plans require exactly one index and at most 20 items/20 total questions. Two chunks of at most 1800 bytes per item fit within the 72 KB context budget. Generation is atomic: no partial drafts, automatic semantic retries, automatic batch splitting, evidence-triggered provider fallback, or automatic bank commit. NestJS/frontend own review and explicit approval.

A valid model evidence refusal, including legacy empty-question output, becomes HTTP 422 `RAG_INSUFFICIENT_EVIDENCE`, mapped by NestJS to `AI_INSUFFICIENT_EVIDENCE`. Optional sanitized diagnostics identify 1-based frozen-plan slots. Planned retrieval can emit the same code with `NO_RETRIEVED_CONTEXT` before calling a model. Malformed output, partial questions mixed with refusals, invalid slots/counts/answers/citations remain 502 `RAG_OUTPUT_INVALID`. Historical failed batches are not rewritten after code/configuration changes.

## Contract `POST /internal/generate`

The request rows expand into ordered slots. The server supplies each slot's type and cognitive level; a single-choice slot requires exactly one correct option, while a multiple-choice slot requires two or three. Do not combine `question_configs` with the legacy `question_format`/`count` fields.

Example request (synthetic source text):

```json
{
  "text": "Stacks use LIFO ordering; queues use FIFO ordering.",
  "question_configs": [
    {"question_type": "SINGLE_CHOICE", "question_format": "KNOWLEDGE", "count": 1},
    {"question_type": "MULTIPLE_CHOICE", "question_format": "UNDERSTANDING", "count": 1}
  ]
}
```

Example response (illustrative only, not a provider result):

```json
{
  "questions": [
    {
      "question_text": "Which ordering does a stack use?",
      "question_type": "SINGLE_CHOICE",
      "question_format": "KNOWLEDGE",
      "correct_answer": null,
      "options": [
        {"text": "LIFO", "isCorrect": true},
        {"text": "FIFO", "isCorrect": false},
        {"text": "Random", "isCorrect": false},
        {"text": "Sorted", "isCorrect": false}
      ]
    },
    {
      "question_text": "Which statements match the source?",
      "question_type": "MULTIPLE_CHOICE",
      "question_format": "UNDERSTANDING",
      "correct_answer": null,
      "options": [
        {"text": "Stacks use LIFO", "isCorrect": true},
        {"text": "Queues use FIFO", "isCorrect": true},
        {"text": "Stacks use FIFO", "isCorrect": false},
        {"text": "Queues use LIFO", "isCorrect": false}
      ]
    }
  ]
}
```

## Deployment and Production

The [Dockerfile](Dockerfile) installs runtime dependencies, copies `app`, runs as a non-root user, exposes 8000, and starts Uvicorn without reload. It does not include tests, contracts, or migrations; provision the database from the checkout separately.

```powershell
docker build -t testhub-ai-service .
docker run --rm --name testhub-ai-service --env-file .env -p 127.0.0.1:8000:8000 testhub-ai-service
```

These are local image commands, not evidence of a completed deployment. This repository has no Compose stack or deployment workflow. Inject secrets at runtime, restrict internal endpoints to a private network, add resource limits, and monitor readiness and sanitized provider errors. Per-process concurrency limits multiply when scaling processes/replicas.

Inside a container, `127.0.0.1` refers to the container, not host PostgreSQL or 9Router. Use an appropriate private service address; when using Docker Desktop, an explicitly configured `host.docker.internal` address can reach host services. Do not expose the internal token publicly. Dependency ranges are not a reproducible lock; validate the built image in your deployment environment.

## Troubleshooting

| Symptom | Checks and interpretation |
| --- | --- |
| Internal 401 or auth-unavailable 503 | Compare the two service token settings, header presence, visible-ASCII/length requirements, environment-file loading, and restart state. Do not log tokens. |
| Startup configuration error | Check exact lowercase booleans, numeric bounds, URL shape, selected provider credentials, and grounded/generation timeout relationship. |
| `/health` succeeds but RAG fails | Health is process-only. Check `/ready`, RAG flags, pgvector, restricted role, migrations 001/002, database access, and Google credentials. Readiness does not call providers. |
| Topic-owned indexing fails with a non-null chapter constraint | Apply approved migration 002; do not patch generated business schemas or remove scope checks. |
| HTTP 422 from RAG | Inspect the sanitized response code: input validation can be `RAG_INPUT_INVALID`; valid empty/refusal output can be `RAG_INSUFFICIENT_EVIDENCE`. A status/access-log line alone is insufficient. Reduce unsupported slots or improve source context after reviewing diagnostics. |
| HTTP 502 / invalid structured output | Check model JSON/schema support, output-token budget, counts, answer flags, and citation validity. Switching response format does not disable validation. |
| `RAG_EMBEDDING_RATE_LIMITED` / `RAG_EMBEDDING_QUOTA_EXHAUSTED` | Google embedding quota is per project across all keys and processes. Check `embedding_call`/`embedding_retry` logs and AI Studio usage; lower `EMBEDDING_MAX_RPM` to the real tier or per replica. Daily quota resets at midnight Pacific; do not rotate keys to bypass it. |
| HTTP 504 or rate-limited Codex account | Inspect nested deadlines and 9Router account/combo routing. AI cannot select a healthy individual account. Timeouts do not trigger Gemini fallback; fix routing or tune bounded budgets deliberately. |
| `OCR_REQUIRED` despite some extracted text | Inspect per-unit diagnostics and usefulness rather than the text-presence ratio alone. `PARTIAL` requires explicit consent; blank/header-only content is not automatically usable. No OCR engine runs here. |
| Windows extraction/indexing problems | Parser code supports Proactor subprocesses and a threaded fallback on Selector loops; RAG storage owns a selector-loop thread. Confirm Python 3.12, restart after configuration changes, and inspect sanitized logs. |
| Host setup works but a container fails | Replace container-local loopback addresses with reachable database/router addresses and check networking/secrets. |

Do not log document text, model output, credentials, or full provider payloads while investigating failures. Record sanitized error codes, schema locations, counts, timing, and slot diagnostics instead.

## Further Documentation

- [Frontend README](../testhub-v2/README.md) and [backend README](../testhub-v2-backend/README.md): application setup and worker ownership.
- [Backend AI question generation](../testhub-v2-backend/docs/ai-question-generation.md), [learning documents](../testhub-v2-backend/docs/learning-documents.md), and [RAG indexing](../testhub-v2-backend/docs/rag-indexing.md).
- [Runtime dependencies](requirements.txt), [development dependencies](requirements-dev.txt), [contracts](contracts), and [RAG migrations](migrations).

Sibling links assume the three repositories are checked out side by side; they may not resolve in a standalone hosted repository view.
