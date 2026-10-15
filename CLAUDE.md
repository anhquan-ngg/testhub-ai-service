# CLAUDE.md — testhub-ai-service

Hướng dẫn chính nằm trong `AGENTS.md`; file này chỉ là điểm vào cho Claude Code. Trạng thái đã xác minh và việc tiếp theo nằm trong `CONTINUITY.md`.

@AGENTS.md

## Bắt đầu phiên làm việc

- Đọc `CONTINUITY.md` và chạy `git status` trước khi sửa file.
- Repo đã có MVP FastAPI; phân biệt các module đã hiện thực với cây RAG dự kiến trong `README.md`.
- `../../AGENTS.md` mô tả monorepo khác với các repo TestHub hiện tại; dùng quy ước dành riêng cho repo này trong `AGENTS.md`.
- Không mở, in hoặc sao chép `.env`, khóa API hay nội dung tài liệu riêng của giảng viên. Chỉ dùng `.env.example` để liệt kê tên biến.
- Chỉ chạy API LLM thật, migration, deploy, commit hoặc push khi yêu cầu của người dùng cho phép. Ghi kết quả kiểm tra đã chạy vào `CONTINUITY.md`.
