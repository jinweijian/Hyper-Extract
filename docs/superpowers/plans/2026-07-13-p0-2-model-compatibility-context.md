# P0-2 Model Compatibility and Context Plan

**Goal:** 让课程管线同时兼容原生结构化输出与 MiniMax Thinking/普通文本 JSON，并在请求前完成总上下文预算与稳定错误分类。

## Task 1: Structured Output Invoker

- [ ] 用响应夹具覆盖 Pydantic 对象、dict、`<think>`、`reasoning_content`、JSON code fence、前后说明、截断 JSON。
- [ ] 实现 `native`、`tool`、`json_object`、`text_json` 和 `auto` 模式。
- [ ] `auto` 只在能力不支持时降级，不吞掉鉴权、限流、超时和上下文错误。
- [ ] 校验失败最多执行一次轻量 JSON 修复，并允许保存脱敏原始响应。
- [ ] 将课程节点、局部关系、去重、全局关系和社区摘要统一接入 Invoker。

## Task 2: Context Planner

- [ ] 总预算包含系统提示、Schema、全局大纲、当前目录、已知术语、正文和输出预留。
- [ ] 超预算时先压缩术语和非当前大纲，不按字符截断正文原子块。
- [ ] 无法容纳单个原子块时返回明确 `ContextBudgetError`。
- [ ] 运行指纹记录模型窗口、输出预留和编排策略版本。

## Task 3: Error Taxonomy

- [ ] 稳定分类鉴权、限流、超时、5xx、上下文超长、输出截断、输出校验和能力不支持。
- [ ] 429、超时、5xx 只重试同一内容边界。
- [ ] 只有上下文超长允许重新编排；普通网络错误不得触发切块。
- [ ] 最终错误保留阶段、类型和脱敏响应位置。

## Task 4: Real Compatibility Verification

- [ ] 使用 MiniMax 执行 Thinking/普通文本 JSON 或 Tool Calling 真实性测试。
- [ ] 使用硅基流动 DeepSeek 执行原生结构化横向对照。
- [ ] 两者输出通过同一 Course Graph Schema；不要求措辞逐字一致。
- [ ] 后续 `PMPBOK_CH_2` 默认使用 MiniMax，运行指纹记录模型、Base URL、输出模式和 Profile 哈希。
