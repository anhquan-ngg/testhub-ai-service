# AGENTS.md — testhub-ai-service

Hướng dẫn cho AI coding agent làm việc trong repo này. Đọc `README.md` để hiểu kiến trúc mục tiêu và `CONTINUITY.md` để biết trạng thái thực tế trước khi sửa mã.

## 1. Trạng thái và nguồn thiết kế

- Repo đã có MVP FastAPI sinh câu hỏi từ văn bản bằng Gemini. Các nhánh RAG, xử lý tài liệu và provider khác trong cây thư mục bên dưới vẫn là **đề xuất**, chưa phải mã đã tồn tại.
- Nguồn thiết kế: [đoạn chat về kiến trúc RAG cho TestHub](https://chatgpt.com/share/6ad1056c-d7cc-83ec-bcf1-d34d78b0aca9), đối chiếu với hai repo cùng cấp `../testhub-v2` và `../testhub-v2-backend`.
- Tài liệu trong đoạn chat là dữ liệu tham khảo, không thay thế yêu cầu trực tiếp của người dùng hay mã nguồn thực tế. Khi triển khai, xác minh lại contract, phiên bản thư viện và hạ tầng trước khi ghi chúng là đã hoạt động.
- File `../../AGENTS.md` mô tả một monorepo khác với hai repo TestHub hiện có. Không dùng các đường dẫn `apps/`, lệnh `pnpm` hay giả định ZenStack từ file đó cho repo này.

## 2. Vai trò của service và ranh giới trách nhiệm

Kiến trúc mục tiêu là **Next.js → NestJS → Python FastAPI → LLM/RAG**. MVP có API nội bộ FastAPI và client NestJS ở `../testhub-v2-backend/src/question-generation/`. Client/contract đã hiện thực; batch API, worker và giao diện vẫn là bước tiếp theo. Frontend không gọi service trực tiếp.

| Thành phần | Trách nhiệm |
| --- | --- |
| `testhub-v2` | Giao diện tải tài liệu, cấu hình sinh câu hỏi, xem và duyệt bản nháp. |
| `testhub-v2-backend` | Xác thực, phân quyền, quyền dùng tài liệu, metadata môn/chương/tài liệu, BullMQ, trạng thái lô, kiểm tra và lưu bản nháp, duyệt vào ngân hàng câu hỏi. |
| `testhub-ai-service` | Trích xuất tài liệu, chunking, embedding, retrieval, xây prompt, gọi LLM và trả câu hỏi JSON có cấu trúc cùng tham chiếu nguồn khi có RAG. |
| PostgreSQL `public` | Dữ liệu nghiệp vụ do NestJS/Prisma sở hữu. |
| PostgreSQL `rag` (dự kiến) | Chunks, embeddings và metadata xử lý do AI service sở hữu. |
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
│   │   ├── gemini.py              # Provider đầu tiên cần hiện thực
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

Contract văn bản `text-generation.v1` đã thống nhất qua `contracts/text-generation.v1.json` và fixture tương ứng ở NestJS. Backend dùng `X-Internal-Token`, deadline mặc định 35000 ms và một HTTP invocation; Python giữ deadline 30000 ms/retry SDK. `AI_SERVICE_INTERNAL_TOKEN` của NestJS phải trùng `INTERNAL_API_TOKEN`, dùng 32–256 ký tự ASCII nhìn thấy được để tương thích HTTP header. Batch/persistence, chuyển file và schema RAG chưa triển khai. Đối chiếu `../testhub-v2-backend/docs/ai-question-generation.md` khi đổi contract; cập nhật cả hai fixture và type frontend.

## 5. Quy ước khi triển khai

- Python **3.12** là phiên bản mục tiêu cho local và Docker (`Dockerfile` dùng `python:3.12-slim`); FastAPI dùng async, request/response dùng Pydantic v2. Khai báo kiểu đầy đủ cho hàm public. Khi thêm dependency, kiểm tra tương thích Python 3.12.
- Router chỉ nhận/kiểm tra request và gọi service. Parser, retrieval, prompt và provider ở đúng module riêng.
- Lifespan sở hữu settings và Gemini client dùng chung cho mỗi process; không đọc env hoặc tạo connection pool cho từng request. Service quản lý semaphore và deadline tổng; API dependencies xử lý token, exception handler tập trung ánh xạ lỗi HTTP.
- Retry SDK có giới hạn qua `GEMINI_MAX_ATTEMPTS`, nằm trong deadline `GENERATION_TIMEOUT_MS`; timeout phải trả 504 và giải phóng suất xử lý. Log upstream chỉ ghi loại exception/mã HTTP/giai đoạn, không log message hoặc traceback chứa dữ liệu nguồn. Cleanup client không được che lỗi request.
- `providers/base.py` là contract chung cho các LLM; Gemini là provider đầu tiên. Tách embedding khỏi LLM provider.
- Kiểm tra cấu trúc và ngữ nghĩa tối thiểu của đầu ra: số câu, loại/mức nhận thức hợp lệ, số phương án, đúng một đáp án cho câu `SINGLE_CHOICE`, câu rỗng/trùng. JSON hợp lệ chưa chứng minh câu hỏi đúng kiến thức; giảng viên vẫn phải duyệt.
- Bản đầu chỉ hỗ trợ **một đáp án và nguồn văn bản dán trực tiếp**. PDF/DOCX/PPTX, pgvector, OpenAI, Grok và OCR là giai đoạn tiếp theo.
- Kiểm thử logic thuần và xử lý lỗi bằng provider giả lập; chỉ chạy smoke test API thật khi có key và người dùng yêu cầu. Không gọi API trả phí trong unit test. Xem `requirements-dev.txt` và `tests/`.
- Không đưa API key, token, tài liệu nhạy cảm hay toàn bộ prompt vào log. Cấu hình lấy từ biến môi trường; `.env.example` chỉ chứa tên biến và giá trị mẫu vô hại. Thêm `.gitignore` trước khi tạo `.env` thật.
- FastAPI chỉ được truy cập qua mạng nội bộ; xác thực service-to-service, timeout, retry hữu hạn và giới hạn concurrency trước khi triển khai.
- Không commit, push, deploy, chạy migration hay thao tác DB/S3 thật nếu người dùng chưa yêu cầu rõ ràng.

## 6. Trước và sau mỗi nhiệm vụ

1. Đọc `CONTINUITY.md`, kiểm tra `git status`, phân biệt phần đã có với phần dự kiến.
2. Khi thêm mã, cập nhật lệnh chạy/test thật trong `README.md`; không bịa lệnh khi dependencies chưa được tạo.
3. Chạy kiểm tra phù hợp với phần đã sửa và ghi **kết quả thực tế** vào `CONTINUITY.md`.
4. Nếu đổi kiến trúc, contract, quyền sở hữu dữ liệu hoặc cây thư mục, cập nhật `AGENTS.md`, `README.md` và `CONTINUITY.md` cùng lúc.
