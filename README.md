# testhub-ai-service

FastAPI nội bộ sinh câu hỏi trắc nghiệm từ đoạn văn bản bằng Gemini, **không dùng RAG**. API trả bản nháp JSON để backend TestHub kiểm duyệt và lưu sau; service này không ghi database.

## Chạy local

Yêu cầu **Python 3.12** và Gemini API key. Dockerfile cũng dùng `python:3.12-slim`. Trong PowerShell, tại thư mục repo, kiểm tra `python --version` hiển thị `Python 3.12.x` rồi tạo môi trường:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
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
  "question_format": "KNOWLEDGE",
  "count": 1
}
```

- `text`: 20–20.000 ký tự sau khi bỏ khoảng trắng ở đầu/cuối.
- `topic`: tùy chọn, tối đa 200 ký tự; chuỗi rỗng hoặc chỉ có khoảng trắng được chuyển thành `null`.
- `question_format`: `KNOWLEDGE`, `UNDERSTANDING`, `APPLYING` hoặc `ADVANCED`.
- `count`: số nguyên từ 1 đến 20.

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

`question_text`, `options`, `question_type`, `question_format` và `correct_answer` dùng các tên trường của ngân hàng câu hỏi TestHub; khi tích hợp NestJS cần chuyển mảng `options` thành chuỗi JSON theo schema backend. Prototype gọi Gemini trong backend đã được gỡ; Python service là nơi thực hiện sinh câu hỏi. Service kiểm tra số câu, bốn lựa chọn khác nhau, đúng một đáp án, độ dài và câu hỏi trùng. Việc kiểm chứng nội dung với tài liệu và duyệt cuối vẫn thuộc giảng viên/backend.

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
$body = @{ text = 'Tìm kiếm nhị phân hoạt động trên mảng đã sắp xếp và có độ phức tạp O(log n).'; topic = 'Cấu trúc dữ liệu'; question_format = 'KNOWLEDGE'; count = 1 } | ConvertTo-Json
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
định 35 giây, lớn hơn deadline Python 30 giây. API lô, hàng đợi và giao diện duyệt
chưa được triển khai.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Test dùng provider giả lập và SDK thật với `httpx.MockTransport`, không cần key thật và không gọi mạng. Chúng kiểm tra contract, token ngoài ASCII, topic rỗng, JSON đầu ra, timeout, deadline/cancellation, concurrency, retry hữu hạn, vòng đời client và log an toàn. Lời gọi Gemini thật cần key hợp lệ; service không tự thực hiện smoke test thật.

## Cấu trúc hiện có

```text
app/
├── main.py
├── config.py
├── errors.py
├── api/{generation,dependencies,errors}.py
├── providers/{base,gemini}.py
├── schemas/question.py
└── services/{prompt_builder,question_generator}.py
tests/
requirements.txt
requirements-dev.txt
Dockerfile
.env.example
```

Thiết kế lâu dài trong [AGENTS.md](AGENTS.md) gồm `api/documents.py`, parser, chunking, embedding, retrieval, pgvector và provider khác. Các phần đó **chưa được triển khai**. Sơ đồ mục tiêu là Next.js → NestJS/BullMQ → FastAPI → Gemini; NestJS giữ quyền truy cập, batch, bản nháp và quy trình duyệt. Xem [CONTINUITY.md](CONTINUITY.md) để biết trạng thái bàn giao mới nhất.
