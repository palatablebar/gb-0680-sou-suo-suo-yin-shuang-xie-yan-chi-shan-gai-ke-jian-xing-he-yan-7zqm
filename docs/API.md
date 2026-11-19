# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

归窗：`/api/runs/<id>/windows` 返回该批次的窗口清单与自洽校验（`duplicate_documents`、`duplicate_events`、`mismatched`、`unwindowed`）。`/api/windows` 返回跨批次窗口台账，事件按 `event_id`、文档按 `doc_id` 全局去重，先到窗口拥有，后到窗口不重复计数，`conflicting_events`/`conflicting_documents` 列出跨窗冲突。两处均以 `rule` 标注口径；页面顶部“核验窗口”面板与归档 CSV 的 `window` 行使用同一份 `app/windows.summarize`，编号、时区、边界和统计一致。归档 CSV 的 `accepted` 行逐篇带 `window`、`window_settlement_at`、`window_basis`、`window_event_windows`、`window_cross_day`，可直接追回归窗依据。
