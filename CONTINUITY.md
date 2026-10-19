# CONTINUITY.md — testhub-ai-service

Tệp bàn giao trạng thái. Chỉ ghi điều đã xác minh từ repo hoặc được người dùng xác nhận; tách rõ thiết kế dự kiến khỏi mã nguồn đã có.

## Trạng thái hiện tại

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
