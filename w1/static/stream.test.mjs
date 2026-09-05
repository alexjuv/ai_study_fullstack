// Run: node w1/static/stream.test.mjs (offline; no model requests).
import assert from 'node:assert/strict';
import {readGeneration} from './stream.mjs';

function response(text) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({start(controller) {
    // Split inside UTF-8 characters and CRLF delimiters as a network can.
    for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
    controller.close();
  }}), {headers: {'Content-Type': 'text/event-stream'}});
}
let output = '';
const result = await readGeneration(response(': heartbeat\r\n\r\nevent: delta\r\ndata: {"text":"你好"}\r\n\r\nevent: done\r\ndata: {"text":"你好",\r\ndata: "metrics":{}}\r\n\r\n'), text => output += text);
assert.equal(output, '你好');
assert.equal(result.text, '你好');
await assert.rejects(readGeneration(response('event: delta\ndata: {"text":"partial"}\n\n'), () => {}), /未收到完成事件/);
await assert.rejects(readGeneration(response('event: error\ndata: {"error":{"code":"TEST_ERROR","message":"failed"}}\n\n'), () => {}), /TEST_ERROR/);
await assert.rejects(readGeneration(response('event: done\ndata: {"text":"x","metrics":{}}'), () => {}), /未收到完成事件/);
await assert.rejects(readGeneration(response('event: done\ndata: {}\n\n'), () => {}), /完成事件格式无效/);
await assert.rejects(readGeneration(new Response('{}'), () => {}), /SSE/);
console.log('PASS: SSE UTF-8 chunks, CRLF, multiline, done, error, truncated and malformed responses');
