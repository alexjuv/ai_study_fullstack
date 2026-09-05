import {readGeneration} from './stream.mjs';

const $ = id => document.getElementById(id);
let models = [], prompts = {}, controller = null, configured = false;
const formatJSON = value => JSON.stringify(value, null, 2);
const ms = value => value == null ? '—' : `${Math.round(value).toLocaleString()} ms`;

async function getJSON(path, options) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`服务返回了无法解析的响应（HTTP ${response.status}）。请检查服务日志。`); }
  if (!response.ok) {
    const details = data.error?.details?.map(item => `${item.loc.join('.')}：${item.message}`).join('\n');
    throw new Error(`${data.error?.code || `HTTP ${response.status}`}：${data.error?.message || '请求失败'}${details ? `\n${details}` : ''}`);
  }
  return data;
}

function fillOptions(id, items) {
  $(id).replaceChildren(...items.map(([value, label]) => new Option(label, value)));
}

function requestBody() {
  const body = {model: $('model').value, stream: $('delivery').value === 'stream', max_tokens: Number($('max-tokens').value)};
  if ($('source').value === 'template') {
    const variables = JSON.parse($('variables').value);
    if (!variables || Array.isArray(variables) || typeof variables !== 'object' || Object.values(variables).some(value => typeof value !== 'string')) throw new Error('模板变量必须是 JSON 对象，且每个值都应为字符串。');
    body.prompt = {name: $('template').value, version: $('version').value, variables};
  } else {
    if (!$('message').value.trim()) throw new Error('请先输入一个问题。');
    body.messages = [{role: 'user', content: $('message').value}];
  }
  const format = $('format').value;
  if (format === 'json_object') body.response_format = {type: format};
  if (format === 'json_schema') {
    const schema = JSON.parse($('schema').value);
    if (!schema || typeof schema !== 'object' || Array.isArray(schema)) throw new Error('JSON Schema 必须是 JSON 对象。');
    body.response_format = {type: format, json_schema: {name: 'answer', schema}};
  }
  return body;
}

function preview() {
  const template = $('source').value === 'template';
  $('custom-fields').hidden = template;
  $('message').required = !template;
  $('template-fields').hidden = !template;
  $('schema-fields').hidden = $('format').value !== 'json_schema';
  $('stream-note').textContent = $('delivery').value !== 'stream' ? '等待生成完成，一次显示完整回答。' : $('format').value === 'text' ? '文本增量会实时显示；完成后返回用量与延迟。' : '结构化输出会先收齐并校验，再显示完整 JSON。';
  $('protocol').textContent = `上游协议：${models.find(model => model.model === $('model').value)?.protocol || '—'}`;
  $('template-preview').textContent = prompts[$('template').value]?.[$('version').value] || '';
  try { $('request-preview').textContent = formatJSON(requestBody()); }
  catch (error) { $('request-preview').textContent = `请修正输入：${error.message}`; }
}

function versions() {
  fillOptions('version', Object.keys(prompts[$('template').value] || {}).map(version => [version, version]));
  preview();
}

async function refreshMetrics() {
  $('refresh-metrics').disabled = true;
  try {
    const {calls} = await getJSON('/metrics');
    $('history-body').replaceChildren(...calls.slice(-8).reverse().map(call => {
      const row = document.createElement('tr');
      const usage = Object.keys(call.usage_raw || {}).length ? (call.usage?.total_tokens ?? '—') : '—';
      for (const value of [call.request_id.slice(0, 10), call.model || '—', call.status === 'ok' ? '完成' : call.status === 'cancelled' ? '已取消' : call.error_code || '失败', usage, ms(call.latency_ms)]) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      row.firstChild.title = call.request_id;
      return row;
    }));
    $('history-state').textContent = calls.length ? `已更新 · 展示最近 ${Math.min(8, calls.length)} 条 / 服务保留 ${calls.length} 条` : '还没有调用记录。发送第一个请求后，记录会出现在这里。';
  } catch (error) { $('history-state').textContent = `记录加载失败：${error.message} 点击「刷新记录」重试。`; }
  finally { $('refresh-metrics').disabled = false; }
}

async function connect() {
  $('refresh').disabled = true;
  try {
    const [health, catalog, templates] = await Promise.all([getJSON('/health'), getJSON('/models'), getJSON('/prompts')]);
    models = catalog.models; prompts = templates; configured = health.configured;
    const selectedModel = $('model').value;
    fillOptions('model', models.map(item => [item.model, item.model]));
    if (models.some(item => item.model === selectedModel)) $('model').value = selectedModel;
    fillOptions('template', Object.keys(prompts).map(name => [name, name]));
    versions();
    $('health').textContent = configured ? '服务在线 · 密钥已配置' : '服务在线 · 请在 w1/.env 配置密钥后重启';
    $('health').classList.toggle('ready', configured);
    $('controls').disabled = !!controller;
    $('submit').disabled = !!controller || !configured || !models.length;
  } catch (error) {
    configured = false;
    $('health').textContent = `连接失败：${error.message}`;
    $('health').classList.remove('ready');
    $('submit').disabled = true;
  } finally { $('refresh').disabled = false; }
}

$('request-form').addEventListener('input', preview);
$('request-form').addEventListener('change', preview);
$('template').addEventListener('change', versions);
$('refresh').addEventListener('click', connect);
$('refresh-metrics').addEventListener('click', refreshMetrics);
$('cancel').addEventListener('click', () => controller?.abort());
$('request-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (controller) return;
  $('error').hidden = true;
  let body;
  try { body = requestBody(); }
  catch (error) { $('status').textContent = '输入有误 · 未发送'; $('error').textContent = `请修正输入：${error.message}`; $('error').hidden = false; return; }
  controller = new AbortController();
  $('controls').disabled = true; $('submit').disabled = true; $('refresh').disabled = true; $('cancel').hidden = false;
  $('empty').hidden = true; $('output').hidden = false; $('output').textContent = '';
  $('status').textContent = '正在请求…';
  $('raw-response').textContent = '等待完成事件…';
  for (const id of ['tokens', 'latency', 'ttft', 'attempts']) $(id).textContent = '—';
  $('usage-note').textContent = '正在等待服务端统计；上游繁忙时可能需要重试。';
  try {
    const response = await fetch('/generate', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal: controller.signal});
    if (!response.ok) {
      const data = await response.json();
      throw new Error(`${data.error?.code || response.status}：${data.error?.message || '请求失败'}${data.error?.details ? `\n${formatJSON(data.error.details)}` : ''}`);
    }
    const result = body.stream ? await readGeneration(response, text => {
      $('status').textContent = '正在生成…'; $('output').textContent += text;
    }) : await response.json();
    $('output').textContent = result.json == null ? result.text : formatJSON(result.json);
    $('raw-response').textContent = formatJSON(result);
    $('tokens').textContent = result.usage_available ? (result.usage?.total_tokens ?? '—').toLocaleString() : '—';
    $('latency').textContent = ms(result.metrics?.latency_ms);
    $('ttft').textContent = ms(result.metrics?.ttft_ms);
    $('attempts').textContent = result.metrics?.attempts ?? '—';
    $('usage-note').textContent = result.usage_available ? `输入 ${result.usage.input_tokens} / 输出 ${result.usage.output_tokens} Token · 分类明细见完整响应` : '上游未提供用量，不能将其视为零消耗。';
    $('status').textContent = '已完成';
  } catch (error) {
    const cancelled = error.name === 'AbortError';
    $('status').textContent = cancelled ? '已取消' : '调用失败';
    $('error').textContent = cancelled ? '已停止等待，已有内容可能不完整；上游可能已经产生用量。' : `${error.message}\n可检查输入或稍后重新发送；更多信息见服务日志。`;
    $('error').hidden = false;
    $('raw-response').textContent = '本次未收到完整结果。';
    $('usage-note').textContent = '本次未获得完整统计，可刷新最近调用查看服务端记录。';
  } finally {
    controller = null;
    $('controls').disabled = false; $('submit').disabled = !configured || !models.length; $('refresh').disabled = false; $('cancel').hidden = true;
    refreshMetrics();
  }
});
connect();
refreshMetrics();
