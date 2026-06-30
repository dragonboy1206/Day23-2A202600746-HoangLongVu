# Báo cáo Lab LangGraph Agentic Orchestration

## 1. Thông tin

- Tên: Hoàng Long Vũ
- Repo/commit: cập nhật sau khi nộp
- Ngày: 2026-06-30

## 2. Kiến trúc

Workflow (luồng công việc) dùng LangGraph để xử lý ticket hỗ trợ theo các bước:
intake -> classify -> route -> xử lý theo nhánh -> finalize.

Các nhánh chính:

- simple: trả lời trực tiếp bằng LLM.
- tool: gọi mock tool, đánh giá kết quả, rồi trả lời.
- missing_info: hỏi thêm thông tin thay vì đoán.
- risky: chuẩn bị hành động rủi ro, xin approval, rồi mới gọi tool.
- error: đi qua retry loop, nếu quá giới hạn thì vào dead letter.

## 2.1. LLM integration

LLM (mô hình ngôn ngữ lớn) là hệ AI dùng để hiểu và sinh văn bản.
Lần chạy report này phát hiện provider: OpenAI qua biến môi trường OPENAI_API_KEY.

- classify_node dùng structured output (đầu ra có cấu trúc) để ép LLM trả về route hợp lệ.
- answer_node dùng grounded generation (sinh câu trả lời dựa trên ngữ cảnh) từ query,
tool_results và approval.
- Khi thiếu API key hoặc lỗi provider, code có fallback để project không dừng đột ngột,
nhưng lần chạy hiện tại đã dùng API key cấu hình trong `.env`.

## 3. State schema

State (trạng thái) là dữ liệu được truyền giữa các node trong graph.
Reducer (bộ gộp dữ liệu) quyết định field được ghi đè hay nối thêm.

| Field | Reducer | Lý do |
|---|---|---|
| query | overwrite | Lưu câu hỏi đã chuẩn hóa |
| route | overwrite | Chỉ cần route hiện tại |
| risk_level | overwrite | Mức rủi ro mới nhất |
| attempt | overwrite | Số lần retry hiện tại |
| max_attempts | overwrite | Giới hạn retry |
| evaluation_result | overwrite | Kết quả đánh giá tool mới nhất |
| pending_question | overwrite | Câu hỏi bổ sung hiện tại |
| proposed_action | overwrite | Hành động rủi ro cần duyệt |
| approval | overwrite | Quyết định duyệt hiện tại |
| final_answer | overwrite | Câu trả lời cuối cùng |
| messages | append | Lưu log ngắn theo từng bước |
| tool_results | append | Lưu lịch sử kết quả tool |
| errors | append | Lưu lỗi/retry |
| events | append | Audit trail phục vụ chấm điểm |

## 4. Kết quả metrics

| Chỉ số | Giá trị |
|---|---:|
| Tổng số scenario | 7 |
| Tỉ lệ thành công | 100.00% |
| Số node trung bình | 6.57 |
| Tổng retry | 4 |
| Tổng approval/HITL | 2 |
| Resume success | Không |

## 5. Kết quả từng scenario

| Scenario | Expected route | Actual route | Thành công | Retries | Interrupts |
|---|---|---|---:|---:|---:|
| S01_simple | simple | simple | Có | 0 | 0 |
| S02_tool | tool | tool | Có | 0 | 0 |
| S03_missing | missing_info | missing_info | Có | 0 | 0 |
| S04_risky | risky | risky | Có | 0 | 1 |
| S05_error | error | error | Có | 3 | 0 |
| S06_delete | risky | risky | Có | 0 | 1 |
| S07_dead_letter | error | error | Có | 1 | 0 |

## 6. Phân tích lỗi

1. Retry hoặc tool failure: nếu tool trả về chuỗi có ERROR, evaluate_node đặt
evaluation_result là needs_retry. Graph đi qua retry_node để tăng attempt. Khi attempt
đạt max_attempts, request được đưa vào dead_letter.
2. Risky action without approval: các yêu cầu như refund/delete/send email được route
sang risky. Graph bắt buộc đi qua approval trước khi gọi tool, tránh tự ý thực hiện
hành động có tác dụng phụ.

## 7. Persistence / recovery

Project đã hỗ trợ MemorySaver mặc định và SQLite checkpointer khi cấu hình
checkpointer: sqlite. Mỗi scenario có thread_id riêng, giúp LangGraph lưu lịch sử state
theo từng lần chạy.

## 8. Extension work

Đã triển khai SQLite checkpointer và report tự động từ metrics.

## 9. Kế hoạch cải thiện

Nếu có thêm một ngày, ưu tiên sản xuất hóa phần evaluate_node bằng LLM-as-judge,
thêm test cho hidden scenario, và bổ sung giao diện HITL thật để người dùng
duyệt/từ chối hành động rủi ro.
