# 内部课程图谱服务

内部服务将数据面与控制面分开。解析器把 Document Package v1 目录发布到
共享 `/exchange` 卷，调用方只提交 `file://` URI、规范包指纹和服务端 Profile
名称。Hyper-Extract 不启动 Docling，任务请求也不接受 API Key。

## 发布输入

先写入 `/exchange/packages/.staging-*`，完成校验后原子重命名为
`/exchange/packages/<name>.hepkg`。API 与 Worker 必须把同一卷挂载到相同路径。

```bash
curl http://he-api:8000/v1/contracts/document-package/v1

curl -X POST http://he-api:8000/v1/document-packages/validate \
  -H 'Content-Type: application/json' \
  -d '{"contract_version":"1.0","package_uri":"file:///exchange/packages/course.hepkg/","sha256":"<规范指纹>"}'
```

## 创建与观察任务

```bash
curl -X POST http://he-api:8000/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: course-2026-001' \
  -d '{
    "input":{"type":"document_package","contract_version":"1.0","package_uri":"file:///exchange/packages/course.hepkg/","package_format":"directory","sha256":"<规范指纹>"},
    "pipeline":{"name":"course_graph","profile":{"name":"course_knowledge_graph","version":"1"}},
    "execution":{"model_profile":"minimax-course-default","context_policy":"auto","priority":"normal"}
  }'
```

轮询 `GET /v1/runs/{run_id}` 可查看阶段、最近 checkpoint 事件、尝试次数、取消
状态、可恢复性和产物链接。创建响应不确定时必须复用相同 Idempotency Key；
同一个 Key 对应不同请求会被拒绝。

取消与恢复分别使用 `POST /v1/runs/{run_id}/cancel` 和
`POST /v1/runs/{run_id}/resume`。取消只发生在 checkpoint 安全点，恢复沿用同一
逻辑任务和已有 `.he-run` 文件，不重复处理已完成块。

## 消费产物

调用方只消费 `GET /v1/runs/{run_id}/artifacts` 声明的文件。成功发布必须同时
存在 `artifact-manifest.json` 与最后写入的 `_SUCCESS`；导入 Course Graph 前应
逐项验证 SHA-256。成功任务的必需产物包括 Course Graph、运行摘要、质量报告、
性能报告和成本报告。`performance-report.json` 分开记录当前进程墙钟时间与累计
模型等待时间；恢复任务不会把零调用的恢复耗时误报成完整模型耗时。

如需估算金额，由部署方配置每百万 Token 单价：

```bash
HYPER_EXTRACT_INPUT_COST_PER_MILLION=1.0
HYPER_EXTRACT_OUTPUT_COST_PER_MILLION=4.0
HYPER_EXTRACT_COST_CURRENCY=CNY
```

未配置单价时，`cost-report.json` 仍会输出 Token，但状态为 `unpriced`，金额为
`null`；HE 不会猜测供应商实时价格。

首期部署支持共享卷 `file://` 文档包。HTTP 与对象存储后续作为来源适配器，
只负责物化同一份不可变 Document Package，不改变任务 API 与图谱协议。
