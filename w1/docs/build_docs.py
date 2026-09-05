"""Bundle the frozen Archify HTML viewers into one offline document."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGES = [('overview', '架构总览'), ('ordinary', '普通调用'), ('streaming', '流式调用'), ('structured', '结构化输出'), ('retry', '重试与结束'), ('contract', '详细契约')]

def build():
    sources = {name: (ROOT / f'{name}.html').read_text(encoding='utf-8') for name, _ in PAGES}
    for name, _ in PAGES[:-1]:
        assert '<svg' in sources[name] and json.loads((ROOT / f'{name}.receipt.json').read_text(encoding='utf-8-sig'))['ok']
    nav = ''.join(f'<button type="button" role="tab" id="tab-{name}" data-page="{name}" aria-controls="panel" aria-selected="false">{label}</button>' for name, label in PAGES)
    data = json.dumps(sources, ensure_ascii=False).replace('</', '<\\/')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>W1 · API 架构与时序</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#132334;font-family:system-ui,"Microsoft YaHei",sans-serif}header{padding:15px 24px 0;background:#fff;border-bottom:1px solid #d9e2e9}header .line{display:flex;align-items:center;gap:20px;justify-content:space-between}strong{font-weight:650}header small{color:#64748b}nav{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}button{font:inherit;color:#516376;background:transparent;border:0;border-bottom:3px solid transparent;padding:10px 16px;cursor:pointer}button[aria-selected=true]{color:#096968;border-color:#096968;background:#eef8f7}button:focus-visible,a:focus-visible{outline:3px solid #3ba8bc;outline-offset:2px}iframe{display:block;width:100%;height:calc(100dvh - 107px);min-height:600px;border:0}#notes{max-width:1100px;margin:32px auto;padding:0 28px 50px;line-height:1.8}#notes[hidden],iframe[hidden]{display:none}h1{font-size:36px;letter-spacing:-1px}h2{margin-top:36px;font-size:23px}.eyebrow{font-size:12px;letter-spacing:1px;color:#267c78}table{width:100%;border-collapse:collapse;background:#fff;font-size:14px}th,td{text-align:left;padding:13px 16px;border:1px solid #dfe6ec;vertical-align:top;overflow-wrap:anywhere}th{background:#eef4f5}a{color:#187874}p{color:#405367}@media(max-width:700px){header{padding:12px 12px 0}header small{display:none}button{padding:9px 10px;font-size:14px}iframe{height:900px}#notes{padding:0 16px}table{font-size:12px}td,th{padding:8px}}
</style><header><div class="line"><strong>W1 / API 层架构与时序</strong><small>依据 app.py · 单文件离线阅读 · 图内可缩放、搜索与导出</small></div><nav role="tablist" aria-label="图表章节">NAV</nav></header><main id="panel" role="tabpanel"><iframe id="diagram" title="API 架构图"></iframe><div id="notes" hidden></div></main><script id="sources" type="application/json">DATA</script><script>
const pages=JSON.parse(document.querySelector('#sources').textContent), frame=document.querySelector('#diagram'), notes=document.querySelector('#notes'), buttons=[...document.querySelectorAll('[data-page]')];
function select(id){if(!Object.hasOwn(pages,id))id='overview';buttons.forEach(b=>{const active=b.dataset.page===id;b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1});document.querySelector('#panel').setAttribute('aria-labelledby','tab-'+id);frame.hidden=id==='contract';notes.hidden=id!=='contract';if(id==='contract'){notes.innerHTML=pages[id]}else{frame.title=document.querySelector('#tab-'+id).textContent;frame.style.height='';frame.srcdoc=pages[id]}history.replaceState(null,'','#'+id)}
function fitFrame(){if(frame.hidden)return;const doc=frame.contentDocument;if(doc)frame.style.height=Math.max(doc.documentElement.scrollHeight,innerHeight-document.querySelector('header').offsetHeight)+'px'}
frame.onload=()=>frame.contentDocument.fonts.ready.then(fitFrame);window.addEventListener('resize',()=>{frame.style.height='';requestAnimationFrame(fitFrame)});
buttons.forEach((b,i)=>{b.onclick=()=>select(b.dataset.page);b.onkeydown=e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();const next=buttons[(i+(e.key==='ArrowRight'?1:buttons.length-1))%buttons.length];select(next.dataset.page);next.focus()}}});window.onhashchange=()=>select(location.hash.slice(1));select(location.hash.slice(1));
</script></html>'''.replace('NAV', nav).replace('DATA', data)
    assert all(f'id="tab-{name}"' in document for name, _ in PAGES)
    (ROOT / 'architecture.html').write_text(document, encoding='utf-8')

if __name__ == '__main__':
    build()
    print('Bundled 5 checked diagrams and detailed API contract: architecture.html')
