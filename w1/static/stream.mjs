// Consume complete SSE frames; EOF alone never means successful generation.
export async function readGeneration(response, onDelta) {
  if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) {
    throw new Error('服务未返回预期的 SSE 数据，请检查服务日志。');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value, {stream: !done});
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const lines = frame.split(/\r?\n/);
        const kind = lines.find(line => line.startsWith('event:'))?.slice(6).trim();
        const raw = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).replace(/^ /, '')).join('\n');
        if (!raw) continue;
        const data = JSON.parse(raw);
        if (kind === 'error') throw new Error(`${data.error?.code || 'STREAM_ERROR'}：${data.error?.message || '生成失败'}`);
        if (kind === 'delta') {
          if (typeof data.text !== 'string') throw new Error('流事件缺少文本。');
          onDelta(data.text);
        }
        if (kind === 'done') {
          if (typeof data.text !== 'string' || !data.metrics) throw new Error('完成事件格式无效。');
          return data;
        }
      }
      if (done) throw new Error('连接已结束，但未收到完成事件。已有内容可能不完整，请重新发送。');
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
