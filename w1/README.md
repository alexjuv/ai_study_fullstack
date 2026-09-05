# 第一周作业：LLM 统一模型调用服务

Python 3.12 + FastAPI + httpx。统一入口 `POST /generate` 按 `model` 选择适配器：

| model | 上游协议与地址 |
| --- | --- |
| `z-ai/glm-5.2:free` | Responses：`https://openrouter.ai/api/v1/responses` |
| `minimax/minimax-m3:free` | Anthropic Messages：`https://openrouter.ai/api/v1/messages` |

请求体、系统提示、输出约束、流事件和 Token 统计分别转换，不是两个模型共用 Chat Completions 的伪适配器。

## 安装与启动（Windows PowerShell）

在仓库根目录执行；当前工作区已创建 `w1/.venv` 并安装依赖。新机器可运行：

```powershell
cd w1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

将自己的密钥填入 `.env` 的 `OPENROUTER_API_KEY=` 后面。已有 `.env` 时不要重复复制；它已被忽略，不应提交密钥。

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

无需激活也能使用虚拟环境；若需要激活：`.\.venv\Scripts\Activate.ps1`。不要用多个 worker，当前限流窗口位于单进程内存。

打开 <http://127.0.0.1:8000/> 使用中文 API 演示网页；<http://127.0.0.1:8000/architecture> 查看详细架构和调用时序图；<http://127.0.0.1:8000/docs> 保留自动生成的 API 文档。

演示网页可选择模型、普通或流式输出、JSON 格式以及模板版本，查看请求、返回结果、Token 和延迟。网页与 API 共用一个服务，无需额外安装前端依赖；密钥始终保留在服务端 `.env`。生成调用会使用真实上游模型，免费共享池繁忙时会显示上游限流错误。

`GET /health` 显示密钥是否配置；`GET /models`、`GET /prompts` 可查看模型与模板。

## curl 示例

以下示例适用于 Bash；Windows 使用 `curl.exe`，并把 JSON 保存为 UTF-8 文件，通过 `--data-binary @request.json` 提交，避免不同 PowerShell 版本的引号处理差异。

普通调用（替换 `model` 即可使用另一种协议）：

```bash
curl http://127.0.0.1:8000/generate -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2:free","messages":[{"role":"user","content":"用一句话介绍自己"}]}'
```

流式调用：

```bash
curl -N http://127.0.0.1:8000/generate -H 'Content-Type: application/json' \
  -d '{"model":"minimax/minimax-m3:free","stream":true,"messages":[{"role":"user","content":"解释什么是 SSE"}]}'
```

SSE 使用 `event: delta` 传递文本增量，`event: done` 传递完整文本、Token 和延迟；中途出错为 `event: error`，不发送 done。客户端必须检查终止事件，不能仅凭 HTTP 200 判断成功。

JSON 输出：

```bash
curl http://127.0.0.1:8000/generate -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2:free","response_format":{"type":"json_object"},"messages":[{"role":"user","content":"返回 JSON，包含城市和天气"}]}'
```

JSON Schema 输出：

```bash
curl http://127.0.0.1:8000/generate -H 'Content-Type: application/json' \
  -d '{"model":"minimax/minimax-m3:free","response_format":{"type":"json_schema","json_schema":{"name":"answer","schema":{"type":"object","properties":{"answer":{"type":"string"}},"required":["answer"],"additionalProperties":false}}},"messages":[{"role":"user","content":"解释什么是适配器模式"}]}'
```

Responses 将格式转换为 `text.format`；Messages 转换为 `output_config.format`，其中 json_object 用 object schema 表示。服务还会解析 JSON 并以 Draft 2020-12 校验，不合法则返回 `INVALID_STRUCTURED_OUTPUT`。只允许本地 schema 引用，禁止远程抓取。结构化 + stream 模式会先收齐并校验，再发一个 delta 和 done，避免向客户端输出未通过校验的 JSON；普通流式模式实时转发增量。

模板引用：

```bash
curl http://127.0.0.1:8000/generate -H 'Content-Type: application/json' \
  -d '{"model":"z-ai/glm-5.2:free","prompt":{"name":"explain","version":"v2","variables":{"topic":"指数退避"}}}'
```

模板存储在 `prompts.json`，用标准库 `string.Template` 替换 `$topic`。新增模板或版本后重启服务，保留旧版本内容以确保旧引用行为稳定。请求必须提供 messages 或 prompt 之一；缺失变量、模板、版本都会返回明确错误。

观测记录：

```bash
curl http://127.0.0.1:8000/metrics
```

每次调用记录 request_id、模型、成功/失败、重试次数、总延迟 `latency_ms`、首个文本增量延迟 `ttft_ms`（无文本为 null），以及输入/输出/总 Token、缓存读取/写入 Token、推理 Token。分类字段是总量的子集，不应再次相加。Messages 的 input_tokens 不含缓存，本服务合并缓存后得到统一输入总数；Responses 的缓存 Token 已包含在输入总数。

原始上游统计保存在 `usage_raw`，不推算缺失的推理 Token；未返回的分类数字为 0，`usage_available=false` 表示完全没有上游 usage，不能把它视为实际零消耗。非流式下也请求上游 SSE，以测量首个文本增量；总延迟包括排队重试等待与收齐输出。记录写入 `metrics.jsonl` 和服务日志，`/metrics` 只保留最近 100 次、重启清空；日志不记录密钥、提示词和模型正文。

## 重试、错误与限流

- 首次尝试之外最多重试 3 次，默认等待 0.5、1、2 秒。若上游给出数字形式的 Retry-After 或 OpenRouter 的 retry_after_seconds，取较长等待时间（上游提示最多采用 60 秒）。重试上游 429、5xx 和建连/请求阶段的传输异常；401/403、其他 4xx 不重试。
- 开始读取响应流后不重试，避免重复文本和重复计费；中途断流、错误帧、未完成输出均报错。
- 每个模型独立使用 60 秒滑动窗口，默认每分钟 20 次逻辑请求；内部重试不重复占用本地额度。超限返回 HTTP 429、`RATE_LIMITED` 和保守的 `Retry-After: 60`。
- 上游 429 重试耗尽映射为 HTTP 503 / `UPSTREAM_RATE_LIMITED`，区别于本地限流。密钥缺失为 503 / `NOT_CONFIGURED`，未知模型 400 / `UNKNOWN_MODEL`，字段错误 422 / `INVALID_REQUEST`，上游协议/结构化/断流错误一般为 502。
- `.env` 可设置 `REQUESTS_PER_MINUTE`、`RETRY_BASE_SECONDS`，修改后重启。

此作业服务绑定本机，不含用户鉴权。限流和模板采用单进程本地存储；部署为多实例时再引入共享存储和访问控制。

## 验证与验收证据

在 `w1` 目录：

```powershell
.\.venv\Scripts\python.exe verify.py --report verification-offline.json
.\.venv\Scripts\python.exe verify.py --live --report verification-live.json
node static/stream.test.mjs
```

离线验证使用 httpx MockTransport 模拟两种上游协议，经过应用入口，覆盖全部六大功能、协议请求形状、缓存/推理分类及 null 统计字段、指数退避与上游等待提示、重试耗尽、错误不重试、模型独立限流和窗口过期、错误流与结构化输出失败。另启动临时本机 HTTP 服务，验证两个协议的首个 delta 确实在上游完成之前到达。无需密钥或外网。

在线验证会实际调用指定的两个免费模型，分别检查普通调用、SSE、JSON 对象、JSON Schema 和模板引用（共 10 次逻辑调用）；输出保存模型原始业务响应供验收。重试/限流的故障注入证据以离线脚本为准，避免人为耗尽真实配额。成功退出码为 0，失败或缺少密钥为 2。

当前证据见 `verification-offline.json` 和 `verification-live.json`。两个模型均已完成真实调用；MiniMax 的五项在线检查已通过，GLM 的普通、SSE 和 JSON 对象检查已通过，JSON Schema 与模板检查遇到免费共享池上游 429，定向复验结果记录在在线报告中。此前失败保留在 previous_attempts，不把模拟结果记作真实调用成功。若提供方拒绝结构化格式，服务会报告上游错误，不会偷偷换模型或降级为仅提示词约束。

## 文件与协议参考

- `app.py`：统一入口、两个适配器、模板、SSE、观测、重试和限流。
- `static/`：原生 HTML/CSS/JavaScript 演示网页。
- `docs/architecture.html`：可独立打开的 API 架构与时序图。
- `docs/*.architecture.json`、`docs/*.sequence.json`：五张图的可维护源文件；各自的 HTML 与 receipt.json 保存生成结果和校验记录。修改单图后，用 `python docs/build_docs.py` 重新打包总入口。
- `verify.py`：离线/在线验收脚本。
- `prompts.json`：可版本引用的提示词存储。
- `requirements.txt`：依赖；`.venv`：本地独立环境。
- 官方协议：[Responses](https://openrouter.ai/docs/api/api-reference/responses/create-responses)、[Messages](https://openrouter.ai/docs/api/api-reference/anthropic-messages/create-messages?explorer=true)、[结构化输出](https://openrouter.ai/docs/guides/features/structured-outputs)。
- 指定模型：[GLM 5.2 free](https://openrouter.ai/z-ai/glm-5.2:free)、[MiniMax M3 free](https://openrouter.ai/minimax/minimax-m3:free)。免费配额和参数支持由上游提供方决定。
