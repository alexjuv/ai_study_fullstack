"""Offline acceptance: python verify.py; real providers: python verify.py --live."""
import argparse
import asyncio
import json
import os
import socket
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import uvicorn

from app import MODELS, ROOT, create_app

SCHEMA = {"type": "json_schema", "json_schema": {"name": "answer", "schema": {
    "type": "object", "properties": {"answer": {"type": "string"}},
    "required": ["answer"], "additionalProperties": False}}}


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events):
        self.events = events
        self.finished = False

    async def __aiter__(self):
        yield b": heartbeat\r\n\r\n"
        for event in self.events:
            await asyncio.sleep(0.02)
            if event.get("type") in {"response.completed", "message_stop"}:
                self.finished = True
            yield ("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode()


class Provider:
    def __init__(self):
        self.calls = []
        self.failures = []
        self.text = '{"answer":"你好"}'
        self.truncate = False
        self.bad_json = False
        self.event_error = False

    def __call__(self, request):
        self.calls.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            if isinstance(failure, httpx.Response):
                return failure
            return httpx.Response(failure)
        if self.bad_json:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: nope\n\n")
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer offline-test"
        assert body["stream"] is True
        if request.url.path.endswith("/responses"):
            assert "input" in body and "messages" not in body and "max_output_tokens" in body
            events = [{"type": "response.output_text.delta", "delta": part} for part in (self.text[:5], self.text[5:])]
            events.append({"type": "response.completed", "response": {"usage": {"input_tokens": 10,
                "output_tokens": 5, "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 2}}}})
        else:
            assert request.url.path == "/api/v1/messages"
            assert "messages" in body and "input" not in body and "max_tokens" in body
            assert all(m["role"] != "system" for m in body["messages"])
            events = [{"type": "message_start", "message": {"usage": {"input_tokens": 7,
                "output_tokens": 1, "cache_read_input_tokens": None, "cache_creation_input_tokens": None,
                "output_tokens_details": None}}}]
            events.extend({"type": "content_block_delta", "delta": {"type": "text_delta", "text": part}}
                          for part in (self.text[:5], self.text[5:]))
            events.extend([{"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                            "usage": {"output_tokens": 5, "cache_read_input_tokens": 2,
                                      "cache_creation_input_tokens": 1, "output_tokens_details": {"thinking_tokens": 2}}},
                           {"type": "message_stop"}])
        if self.truncate:
            events.pop()
        if self.event_error:
            events = [{"type": "error", "error": {"message": "private upstream detail"}}]
        self.last_stream = EventStream(events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=self.last_stream)


def request_body(model, **extras):
    return {"model": model, "messages": [{"role": "user", "content": 'Return JSON with answer equal to "hello".'}], **extras}


async def offline():
    provider = Provider()
    evidence = ROOT / ".tmp" / "verify-metrics.jsonl"
    evidence.parent.mkdir(exist_ok=True)
    evidence.write_text("", encoding="utf-8")
    app = create_app(httpx.MockTransport(provider), api_key="offline-test", rpm=100,
                     retry_base=0.01, metrics_path=evidence)
    passed = []
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for path in ("/", "/architecture"):
            page = await client.get(path)
            assert page.status_code == 200 and "text/html" in page.headers["content-type"], path
            assert "<html" in page.text.lower(), path
        for path in ("/static/app.js", "/static/stream.mjs"):
            asset = await client.get(path)
            assert asset.status_code == 200 and "javascript" in asset.headers["content-type"], path
        for path in ("/.env", "/static/.env", "/docs/.env", "/static/../.env"):
            assert (await client.get(path)).status_code == 404, path
        passed.append("Demo and architecture HTML routes; configuration files are not exposed")
        for model in MODELS:
            response = await client.post("/generate", json=request_body(model))
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["text"] == provider.text
            assert data["usage"]["total_tokens"] == 15
            assert 0 < data["metrics"]["ttft_ms"] < data["metrics"]["latency_ms"]
            response = await client.post("/generate", json=request_body(model, stream=True))
            assert response.status_code == 200 and "text/event-stream" in response.headers["content-type"]
            assert response.text.count("event: delta") == 2 and "event: done" in response.text
            for fmt in ({"type": "json_object"}, SCHEMA):
                for streaming in (False, True):
                    response = await client.post("/generate", json=request_body(model, response_format=fmt, stream=streaming))
                    assert response.status_code == 200, response.text
                    if streaming:
                        assert response.text.count("event: delta") == 1 and "event: done" in response.text
                    else:
                        assert response.json()["json"] == {"answer": "你好"}
                    payload = json.loads(provider.calls[-1].content)
                    assert ("text" if model.startswith("z-ai/") else "output_config") in payload
            for version in ("v1", "v2"):
                response = await client.post("/generate", json={"model": model,
                    "prompt": {"name": "explain", "version": version, "variables": {"topic": "SSE"}}})
                assert response.status_code == 200
                payload = json.loads(provider.calls[-1].content)
                assert "SSE" in str(payload) and "$topic" not in str(payload)
            passed.append(f"{model}: route, SSE, JSON/schema, prompt versions, usage, TTFT")

        model = next(iter(MODELS))
        provider.failures = [503, 429, 500]
        with patch("app.asyncio.sleep", new_callable=AsyncMock) as sleep:
            response = await client.post("/generate", json=request_body(model))
            assert response.status_code == 200 and response.json()["metrics"]["attempts"] == 4
            assert [call.args[0] for call in sleep.call_args_list[:3]] == [0.01, 0.02, 0.04]
        start = len(provider.calls)
        provider.failures = [503] * 4
        response = await client.post("/generate", json=request_body(model))
        assert response.status_code == 502 and len(provider.calls) - start == 4
        provider.failures = [httpx.ConnectError("offline")]
        response = await client.post("/generate", json=request_body(model))
        assert response.status_code == 200 and response.json()["metrics"]["attempts"] == 2
        start = len(provider.calls)
        provider.failures = [401]
        response = await client.post("/generate", json=request_body(model))
        assert response.status_code == 502 and len(provider.calls) - start == 1
        assert response.json()["error"]["code"] == "UPSTREAM_AUTH_ERROR"
        for failure in (httpx.Response(429, headers={"Retry-After": "5"}, json={}),
                        httpx.Response(429, json={"error": {"metadata": {"retry_after_seconds": 5}}})):
            provider.failures = [failure]
            with patch("app.asyncio.sleep", new_callable=AsyncMock) as sleep:
                response = await client.post("/generate", json=request_body(model))
                assert response.status_code == 200 and sleep.call_args_list[0].args == (5.0,)
        passed.append("Retries: exponential 0.01/0.02/0.04, max 3 retries, network retry, no auth retry")

        for model in MODELS:
            for attr, code in (("truncate", "UPSTREAM_TRUNCATED"), ("bad_json", "UPSTREAM_PROTOCOL_ERROR"), ("event_error", "UPSTREAM_ERROR")):
                setattr(provider, attr, True)
                response = await client.post("/generate", json=request_body(model))
                assert response.status_code == 502 and response.json()["error"]["code"] == code
                response = await client.post("/generate", json=request_body(model, stream=True))
                assert "event: error" in response.text and "event: done" not in response.text
                setattr(provider, attr, False)
            for bad in ('not JSON', '{"answer":123}', 'NaN'):
                provider.text = bad
                response = await client.post("/generate", json=request_body(model, response_format=SCHEMA))
                assert response.status_code == 502 and response.json()["error"]["code"] == "INVALID_STRUCTURED_OUTPUT"
                response = await client.post("/generate", json=request_body(model, response_format=SCHEMA, stream=True))
                assert "event: error" in response.text and "event: delta" not in response.text
        provider.text = '{"answer":"你好"}'
        passed.append("Both protocols: truncated stream, malformed SSE, upstream error, invalid JSON/schema handled")

        for body, status, code in [
            (request_body("missing"), 400, "UNKNOWN_MODEL"),
            ({"model": model}, 422, "INVALID_REQUEST"),
            (request_body(model, max_tokens=0), 422, "INVALID_REQUEST"),
            (request_body(model, response_format={"type": "bad"}), 422, "INVALID_REQUEST"),
            (request_body(model, response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": {"$ref": "https://example.com"}}}), 422, "INVALID_REQUEST"),
            ({"model": model, "prompt": {"name": "explain", "version": "missing"}}, 404, "PROMPT_NOT_FOUND"),
            ({"model": model, "prompt": {"name": "explain", "version": "v1"}}, 400, "INVALID_PROMPT_VARIABLES"),
        ]:
            response = await client.post("/generate", json=body)
            assert response.status_code == status and response.json()["error"]["code"] == code, response.text
        calls = (await client.get("/metrics")).json()["calls"]
        assert any(c.get("error_code") == "UPSTREAM_AUTH_ERROR" for c in calls)
        assert all("latency_ms" in c and "request_id" in c for c in calls)
        assert any(c["usage"].get("cached_input_tokens") == 3 for c in calls)
        assert any(c["usage"].get("cache_creation_input_tokens") == 1 for c in calls)
        assert any(c["usage"].get("reasoning_tokens") == 2 for c in calls)
        assert "offline-test" not in evidence.read_text(encoding="utf-8")
        passed.append("Validation, unified error codes, success/failure metrics and token categories")

    limited = create_app(httpx.MockTransport(provider), api_key="offline-test", rpm=1, metrics_path=evidence)
    async with limited.router.lifespan_context(limited), httpx.AsyncClient(transport=httpx.ASGITransport(limited), base_url="http://test") as client:
        first, second = MODELS
        assert (await client.post("/generate", json=request_body(first))).status_code == 200
        response = await client.post("/generate", json=request_body(first))
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        assert (await client.post("/generate", json=request_body(second))).status_code == 200
        monotonic = time.monotonic
        with patch("app.time.monotonic", side_effect=lambda: monotonic() + 61):
            assert (await client.post("/generate", json=request_body(first))).status_code == 200
    passed.append("Per-model independent 429 limits and sliding-window expiry")
    # ASGITransport buffers responses; a socket check proves deltas arrive before completion.
    socket_app = create_app(httpx.MockTransport(provider), api_key="offline-test", metrics_path=evidence)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(socket_app, log_level="critical"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Smoke server did not start")
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{listener.getsockname()[1]}") as client:
                for model in MODELS:
                    seen_delta = False
                    async with client.stream("POST", "/generate", json=request_body(model, stream=True)) as response:
                        assert response.status_code == 200
                        async for line in response.aiter_lines():
                            if line == "event: delta" and not seen_delta:
                                assert not provider.last_stream.finished, "SSE was buffered until completion"
                                seen_delta = True
                    assert seen_delta
        finally:
            server.should_exit = True
            await task
    passed.append("Real localhost HTTP: both protocols deliver first SSE delta before upstream completion")
    return {"mode": "offline", "status": "passed", "checks": passed,
            "metrics_file": str(evidence), "note": "Mock upstream evidence only; real provider acceptance requires --live."}


async def live():
    if not os.getenv("OPENROUTER_API_KEY"):
        return {"mode": "live", "status": "blocked", "reason": "Configure OPENROUTER_API_KEY in w1/.env or environment"}
    app = create_app(rpm=100)
    checks = []
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test", timeout=180) as client:
        for model in MODELS:
            cases = {"text": request_body(model), "SSE": request_body(model, stream=True),
                     "json_object": request_body(model, response_format={"type": "json_object"}),
                     "json_schema": request_body(model, response_format=SCHEMA),
                     "prompt": {"model": model, "prompt": {"name": "explain", "version": "v1", "variables": {"topic": "SSE"}}}}
            for name, body in cases.items():
                response = await client.post("/generate", json=body)
                ok = response.status_code == 200
                if body.get("stream"):
                    ok = ok and "event: done" in response.text and "event: delta" in response.text and "event: error" not in response.text
                checks.append({"model": model, "case": name, "passed": ok,
                               "status_code": response.status_code, "response": response.text})
                print(f'{model} {name}: {"PASS" if ok else "FAIL"}', flush=True)
    return {"mode": "live", "status": "passed" if all(c["passed"] for c in checks) else "failed", "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(live() if args.live else offline())
    output = json.dumps(report, indent=2, ensure_ascii=False)
    print(output)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    raise SystemExit(0 if report["status"] == "passed" else 2)
