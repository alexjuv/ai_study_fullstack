"""Run: python -m uvicorn app:app --host 127.0.0.1 --port 8000."""
import asyncio
import json
import logging
import math
import mimetypes
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from string import Template
from typing import Literal
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator, ValidationError, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from referencing import Registry
from referencing.exceptions import Unresolvable

ROOT = Path(__file__).resolve().parent
mimetypes.add_type("text/javascript", ".mjs")
load_dotenv(ROOT / ".env")
LOG = logging.getLogger("uvicorn.error")


class APIError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status

    def body(self):
        return {"error": {"code": self.code, "message": self.message}}


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class PromptRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: str
    variables: dict[str, str] = Field(default_factory=dict)


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    messages: list[Message] = Field(default_factory=list, max_length=100)
    prompt: PromptRef | None = None
    stream: bool = False
    response_format: dict | None = None
    max_tokens: int = Field(default=1024, ge=1, le=16384)

    @model_validator(mode="after")
    def validate_input(self):
        if bool(self.messages) == bool(self.prompt):
            raise ValueError("Provide exactly one of messages or prompt")
        fmt = self.response_format
        if fmt is not None:
            if fmt.get("type") == "json_object":
                if set(fmt) != {"type"}:
                    raise ValueError("json_object accepts only type")
            elif fmt.get("type") == "json_schema":
                spec = fmt.get("json_schema", {})
                if not isinstance(spec, dict) or not isinstance(spec.get("schema"), dict):
                    raise ValueError("json_schema.schema must be an object")
                if not isinstance(spec.get("name"), str) or not spec["name"]:
                    raise ValueError("json_schema.name is required")
                # Local validation must never fetch caller-provided remote schemas.
                def check_refs(node):
                    if isinstance(node, dict):
                        for key, value in node.items():
                            if key in {"$ref", "$dynamicRef"} and (not isinstance(value, str) or not value.startswith("#")):
                                raise ValueError("Only local JSON Schema references are supported")
                            check_refs(value)
                    elif isinstance(node, list):
                        for value in node:
                            check_refs(value)
                check_refs(spec["schema"])
                try:
                    Draft202012Validator.check_schema(spec["schema"])
                except SchemaError as exc:
                    raise ValueError("Invalid JSON Schema") from exc
            else:
                raise ValueError("response_format type must be json_object or json_schema")
        return self


def output_schema(fmt):
    return fmt["json_schema"]["schema"] if fmt["type"] == "json_schema" else {"type": "object"}


def reject_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


class ResponsesAdapter:
    path = "/responses"

    def payload(self, req, messages):
        body = {"model": req.model, "input": messages, "stream": True,
                "max_output_tokens": req.max_tokens}
        if req.response_format:
            fmt = req.response_format
            body["text"] = {"format": {"type": "json_schema", "name": "result",
                "schema": output_schema(fmt), "strict": True} if fmt["type"] == "json_schema"
                else {"type": "json_object"}}
            if fmt["type"] == "json_schema":
                body["text"]["format"]["name"] = fmt["json_schema"]["name"]
        return body

    def event(self, event):
        kind = event.get("type")
        if kind in {"error", "response.failed", "response.incomplete"} or event.get("error"):
            raise APIError("UPSTREAM_ERROR", "Upstream generation failed or was incomplete", 502)
        response = event.get("response", {})
        return (event.get("delta", "") if kind == "response.output_text.delta" else "",
                response.get("usage") or {}, kind == "response.completed")


class MessagesAdapter:
    path = "/messages"

    def payload(self, req, messages):
        body = {"model": req.model, "messages": [m for m in messages if m["role"] != "system"],
                "max_tokens": req.max_tokens, "stream": True}
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        if system:
            body["system"] = system
        if not body["messages"]:
            raise APIError("INVALID_REQUEST", "Messages protocol needs a user or assistant message")
        if req.response_format:
            body["output_config"] = {"format": {"type": "json_schema", "schema": output_schema(req.response_format)}}
        return body

    def event(self, event):
        kind = event.get("type")
        if kind == "error" or event.get("error"):
            raise APIError("UPSTREAM_ERROR", "Upstream generation failed", 502)
        delta = event.get("delta", {})
        if delta.get("stop_reason") in {"max_tokens", "refusal"}:
            raise APIError("UPSTREAM_ERROR", "Upstream output was incomplete or refused", 502)
        text = delta.get("text", "") if delta.get("type") == "text_delta" else ""
        usage = event.get("message", {}).get("usage") or event.get("usage") or {}
        return text, usage, kind == "message_stop"


MODELS = {"z-ai/glm-5.2:free": ResponsesAdapter(), "minimax/minimax-m3:free": MessagesAdapter()}


async def sse_events(response):
    """Parse SSE frames, including comments, CRLF and multiline data fields."""
    data = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                raw = "\n".join(data)
                data.clear()
                if raw != "[DONE]":
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError("SSE data must be an object")
                    yield event
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        raise APIError("UPSTREAM_PROTOCOL_ERROR", "Unterminated SSE frame", 502)


def usage_counts(raw, adapter):
    input_tokens = raw.get("input_tokens") or 0
    output_tokens = raw.get("output_tokens") or 0
    cached = (raw.get("input_tokens_details") or {}).get("cached_tokens") or 0
    cache_write = raw.get("cache_creation_input_tokens") or 0
    if isinstance(adapter, MessagesAdapter):
        cached = raw.get("cache_read_input_tokens") or 0
        input_tokens += cached + cache_write
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens, "cached_input_tokens": cached,
            "cache_creation_input_tokens": cache_write,
            "reasoning_tokens": (raw.get("output_tokens_details") or {}).get("reasoning_tokens")
            or (raw.get("output_tokens_details") or {}).get("thinking_tokens") or 0}


def sse(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(transport=None, *, api_key=None, rpm=None, retry_base=None, metrics_path=None):
    key = os.getenv("OPENROUTER_API_KEY", "") if api_key is None else api_key
    limit = int(os.getenv("REQUESTS_PER_MINUTE", "20")) if rpm is None else rpm
    base = float(os.getenv("RETRY_BASE_SECONDS", "0.5")) if retry_base is None else retry_base
    if limit < 1 or base < 0:
        raise ValueError("REQUESTS_PER_MINUTE must be positive; RETRY_BASE_SECONDS must be nonnegative")
    log_path = ROOT / "metrics.jsonl" if metrics_path is None else Path(metrics_path)
    prompts = json.loads((ROOT / "prompts.json").read_text(encoding="utf-8"))
    # ponytail: single-process sliding windows; use Redis for multiple workers/hosts.
    windows = defaultdict(deque)
    recent = deque(maxlen=100)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", transport=transport,
                                     timeout=httpx.Timeout(120, connect=15)) as client:
            app.state.client = client
            yield

    app = FastAPI(title="W1 Unified LLM Service", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.get("/", include_in_schema=False)
    async def demo():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/architecture", include_in_schema=False)
    async def architecture():
        return FileResponse(ROOT / "docs" / "architecture.html")

    def record(state):
        state["latency_ms"] = round((time.perf_counter() - state.pop("started")) * 1000, 3)
        recent.append(state.copy())
        LOG.info("llm_call %s", json.dumps(state, ensure_ascii=False))
        try:
            with log_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(state, ensure_ascii=False) + "\n")
        except OSError:
            LOG.exception("Cannot persist metrics; record remains in server log and /metrics")

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse(exc.body(), status_code=exc.status,
                            headers={"Retry-After": "60"} if exc.status == 429 else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if request.url.path == "/generate":
            record({"request_id": uuid4().hex, "model": None, "started": time.perf_counter(),
                    "ttft_ms": None, "attempts": 0, "status": "error", "usage": {},
                    "usage_raw": {}, "error_code": "INVALID_REQUEST"})
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Invalid request fields",
            "details": [{"loc": e["loc"], "message": e["msg"]} for e in exc.errors()]}}, status_code=422)

    @app.get("/health")
    async def health():
        return {"status": "ok", "configured": bool(key)}

    @app.get("/models")
    async def models():
        return {"models": [{"model": name, "protocol": adapter.path[1:]} for name, adapter in MODELS.items()]}

    @app.get("/prompts")
    async def list_prompts():
        return prompts

    @app.get("/metrics")
    async def metrics():
        return {"calls": list(recent)}

    async def open_upstream(req, adapter, messages, state):
        body = adapter.payload(req, messages)
        for attempt in range(4):  # Initial call + at most three retries.
            state["attempts"] = attempt + 1
            retry_after = 0
            try:
                request = app.state.client.build_request("POST", adapter.path, json=body,
                    headers={"Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"})
                response = await app.state.client.send(request, stream=True)
            except httpx.TransportError as exc:
                error = APIError("UPSTREAM_TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "UPSTREAM_UNAVAILABLE",
                                 "Cannot connect to upstream", 504 if isinstance(exc, httpx.TimeoutException) else 502)
            else:
                if response.status_code == 200:
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        await response.aclose()
                        raise APIError("UPSTREAM_PROTOCOL_ERROR", "Expected an SSE response", 502)
                    return response
                status = response.status_code
                if status == 429 or status >= 500:
                    try:
                        await response.aread()
                        metadata = response.json().get("error", {}).get("metadata", {})
                        retry_after = float(response.headers.get("retry-after") or metadata.get("retry_after_seconds") or 0)
                        if not math.isfinite(retry_after) or retry_after < 0:
                            retry_after = 0
                        retry_after = min(retry_after, 60)
                    except (ValueError, TypeError, AttributeError, httpx.TransportError):
                        retry_after = 0
                await response.aclose()
                error = APIError("UPSTREAM_RATE_LIMITED" if status == 429 else "UPSTREAM_AUTH_ERROR" if status in {401, 403}
                                 else "UPSTREAM_ERROR", f"Upstream returned HTTP {status}", 503 if status == 429 else 502)
                if status != 429 and status < 500:
                    raise error
            if attempt == 3:
                raise error
            await asyncio.sleep(max(base * 2 ** attempt, retry_after))

    @app.post("/generate")
    async def generate(req: GenerateRequest, request: Request):
        state = {"request_id": uuid4().hex, "model": req.model, "stream": req.stream,
                 "started": time.perf_counter(), "ttft_ms": None, "attempts": 0,
                 "status": "error", "usage": {}, "usage_raw": {}}
        try:
            adapter = MODELS.get(req.model)
            if adapter is None:
                raise APIError("UNKNOWN_MODEL", "Use a model listed in /models")
            messages = [m.model_dump() for m in req.messages]
            if req.prompt:
                ref = req.prompt
                if ref.name not in prompts or ref.version not in prompts[ref.name]:
                    raise APIError("PROMPT_NOT_FOUND", "Unknown prompt name or version", 404)
                try:
                    content = Template(prompts[ref.name][ref.version]).substitute(ref.variables)
                except (KeyError, ValueError) as exc:
                    raise APIError("INVALID_PROMPT_VARIABLES", "Missing or invalid template variables") from exc
                messages = [{"role": "user", "content": content}]
            if req.response_format:
                messages = [{"role": "system", "content": "Return only valid JSON matching this schema: "
                    + json.dumps(output_schema(req.response_format))}] + messages
            if not key:
                raise APIError("NOT_CONFIGURED", "Set OPENROUTER_API_KEY in w1/.env", 503)
            now = time.monotonic()
            window = windows[req.model]
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= limit:
                raise APIError("RATE_LIMITED", "Per-model request limit exceeded", 429)
            window.append(now)
            upstream = await open_upstream(req, adapter, messages, state)
        except APIError as exc:
            state["error_code"] = exc.code
            record(state)
            raise

        async def chunks():
            parts, raw_usage, complete = [], {}, False
            result = None
            failure = None
            try:
                async for event in sse_events(upstream):
                    text, usage, done = adapter.event(event)
                    raw_usage.update(usage)
                    state["usage_raw"] = raw_usage.copy()
                    state["usage"] = usage_counts(raw_usage, adapter)
                    if text:
                        if state["ttft_ms"] is None:
                            state["ttft_ms"] = round((time.perf_counter() - state["started"]) * 1000, 3)
                        parts.append(text)
                        if not req.response_format:
                            yield {"type": "delta", "text": text}
                    if done:
                        complete = True
                        break
                if not complete:
                    raise APIError("UPSTREAM_TRUNCATED", "Stream ended without a completion event", 502)
                content = "".join(parts)
                if not content:
                    raise APIError("EMPTY_OUTPUT", "Upstream returned no text", 502)
                parsed = None
                if req.response_format:
                    try:
                        parsed = json.loads(content, parse_constant=reject_constant)
                        Draft202012Validator(output_schema(req.response_format), registry=Registry()).validate(parsed)
                    except (ValueError, ValidationError, Unresolvable) as exc:
                        raise APIError("INVALID_STRUCTURED_OUTPUT", "Output does not match requested JSON format", 502) from exc
                state["status"] = "ok"
                result = {"request_id": state["request_id"], "model": req.model, "text": content,
                          "json": parsed, "usage": state["usage"], "usage_available": bool(raw_usage)}
            except APIError as exc:
                failure = exc
            except httpx.TransportError:
                failure = APIError("UPSTREAM_INTERRUPTED", "Upstream connection interrupted", 502)
            except (ValueError, KeyError, TypeError, AttributeError):
                failure = APIError("UPSTREAM_PROTOCOL_ERROR", "Malformed upstream response", 502)
            finally:
                if failure:
                    state["error_code"] = failure.code
                elif state["status"] != "ok":
                    state["status"] = "cancelled"
                await upstream.aclose()
                record(state)
            if failure:
                raise failure
            result["metrics"] = {k: state[k] for k in ("latency_ms", "ttft_ms", "attempts")}
            if req.response_format:
                yield {"type": "delta", "text": result["text"]}
            yield {"type": "done", **result}

        if req.stream:
            async def stream_body():
                try:
                    async for chunk in chunks():
                        yield sse(chunk.pop("type"), chunk)
                except APIError as exc:
                    yield sse("error", exc.body())
            return StreamingResponse(stream_body(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        async for chunk in chunks():
            if chunk["type"] == "done":
                chunk.pop("type")
                return chunk

    return app


app = create_app()
