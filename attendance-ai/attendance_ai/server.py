"""내 PC에서만 동작하는 간단한 웹 화면.

기본으로 127.0.0.1(이 컴퓨터)에서만 접속을 받으므로, 올린 출입기록 파일은
외부 서버나 인터넷으로 전송되지 않는다.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import tempfile
import threading
import webbrowser
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from .cli import print_summary
from .config import Settings
from .engine import analyze
from .io_utils import load_confirmations, load_punches
from .outputs import FILES, write_outputs

MAX_BODY = 200 * 1024 * 1024
_JOBS: dict[str, Path] = {}
_ROOT = Path(tempfile.mkdtemp(prefix="attendance_ai_"))

_TYPES = {".csv": "text/csv; charset=utf-8", ".html": "text/html; charset=utf-8",
          ".json": "application/json; charset=utf-8"}


def _run_job(payload: dict) -> dict:
    job = secrets.token_urlsafe(12)
    d = _ROOT / job
    d.mkdir(parents=True)
    f = payload["file"]
    name = Path(f.get("name") or "upload.csv").name
    src = d / ("input" + (Path(name).suffix.lower() or ".csv"))
    src.write_bytes(base64.b64decode(f["b64"]))
    s = Settings()
    opts = payload.get("settings") or {}
    for key in ("auto_correct_threshold", "review_threshold", "duplicate_window_min", "new_shift_gap_hours",
                "max_interval_hours"):
        if opts.get(key) not in (None, ""):
            setattr(s, key, float(opts[key]))
    punches, rep = load_punches(src)
    rep.source = name
    confirmations = []
    c = payload.get("confirm")
    if c:
        cp = d / ("confirm" + (Path(c.get("name") or "c.csv").suffix.lower() or ".csv"))
        cp.write_bytes(base64.b64decode(c["b64"]))
        confirmations = load_confirmations(cp)
    result = analyze(punches, s, rep, confirmations)
    out = d / "result"
    write_outputs(result, out)
    _JOBS[job] = out
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_summary(result)
    return {"job": job, "summary": buf.getvalue(), "files": [{"name": k, "desc": v} for k, v in FILES.items()],
            "stats": result.stats}


class _Handler(BaseHTTPRequestHandler):
    server_version = "AttendanceAI/1.0"

    def log_message(self, fmt, *args):  # 개인정보가 담긴 경로를 콘솔에 남기지 않음
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            return self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "jobs" and parts[1] in _JOBS and parts[2] in FILES:
            p = _JOBS[parts[1]] / parts[2]
            extra = {} if p.suffix == ".html" else {"Content-Disposition": f"attachment; filename=\"{parts[2]}\""}
            return self._send(200, p.read_bytes(), _TYPES.get(p.suffix, "application/octet-stream"), extra)
        self._send(404, "찾을 수 없습니다".encode("utf-8"), "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/analyze":
            return self._send(404, b"{}", "application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._send(413, json.dumps({"error": "파일이 너무 크거나 비어 있습니다."}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        try:
            payload = json.loads(self.rfile.read(n))
            res = _run_job(payload)
            self._send(200, json.dumps(res, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        except SystemExit as e:  # 입력 파일 형식 오류 등 사용자에게 보여줄 메시지
            self._send(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        except Exception as e:  # pragma: no cover
            self._send(500, json.dumps({"error": f"분석 중 오류: {type(e).__name__}: {e}"}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    httpd = make_server(host, port)
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{httpd.server_address[1]}/"
    print(f"AI 근태 판정기 웹 화면: {url}")
    if host not in ("127.0.0.1", "localhost"):
        print("⚠ 이 컴퓨터 밖에서도 접속할 수 있는 주소로 열었습니다. 개인정보가 담긴 파일이므로 사내망에서만 사용하세요.")
    print("종료하려면 Ctrl+C")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        httpd.server_close()


_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 근태 판정기</title>
<style>
:root{--bg:#f6f6f3;--surface:#fff;--surface2:#f0f0ec;--text:#1c1c1e;--muted:#6a6a70;--border:#e2e2dc;--accent:#2f6fdb;--bad:#c62828}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#111113;--surface:#1b1b1e;--surface2:#232327;--text:#ececef;--muted:#9c9ca6;--border:#2f2f35;--accent:#6ea2ff;--bad:#ff6b6b}}
:root[data-theme="dark"]{--bg:#111113;--surface:#1b1b1e;--surface2:#232327;--text:#ececef;--muted:#9c9ca6;--border:#2f2f35;--accent:#6ea2ff;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic","Noto Sans KR",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 16px 48px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px;margin-top:16px}
.drop{border:2px dashed var(--border);border-radius:10px;padding:26px;text-align:center;color:var(--muted);cursor:pointer}
.drop.on{border-color:var(--accent);color:var(--text)}.row{display:flex;gap:12px;flex-wrap:wrap;margin-top:14px;align-items:flex-end}
label.f{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:4px}
input[type=number],input[type=file]{background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:6px 8px;font:inherit;max-width:100%}
input[type=number]{width:110px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 18px;font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.5;cursor:default}pre{white-space:pre-wrap;font:12.5px/1.5 ui-monospace,Menlo,Consolas,monospace;background:var(--surface2);padding:12px;border-radius:8px;overflow-x:auto}
.files a{display:block;color:var(--accent);margin:3px 0}.files span{color:var(--muted);font-size:12px;margin-left:6px}
iframe{width:100%;height:80vh;border:1px solid var(--border);border-radius:10px;background:var(--surface);margin-top:16px}
.err{color:var(--bad);margin-top:10px;white-space:pre-wrap}details summary{cursor:pointer;color:var(--muted);font-size:13px}
</style></head><body><div class="wrap">
<h1>AI 근태 판정기</h1>
<div class="sub">출입관리 단말기에서 내려받은 파일(CSV/XLSX)을 올리면, 출근·퇴근 메뉴를 잘못 누른 기록을 AI가 찾아 바로잡고 실제 지문 시각 기준으로 근로시간을 계산합니다. 파일은 이 컴퓨터 밖으로 나가지 않습니다.</div>
<div class="card">
  <div class="drop" id="drop">여기에 출입기록 파일을 끌어다 놓거나 클릭해서 선택하세요<br><b id="fname"></b></div>
  <input type="file" id="file" accept=".csv,.xlsx,.xlsm,.txt" hidden>
  <div class="row">
    <label class="f">담당자 확정 파일 (선택)<input type="file" id="confirm" accept=".csv,.xlsx"></label>
  </div>
  <details style="margin-top:14px"><summary>판정 정책 바꾸기</summary>
  <div class="row">
    <label class="f">자동 정정 최소 확신도<input type="number" id="auto" step="0.01" min="0.5" max="1" value="0.95"></label>
    <label class="f">검토 기준 확신도<input type="number" id="review" step="0.01" min="0.5" max="1" value="0.80"></label>
    <label class="f">중복 입력 묶음(분)<input type="number" id="dup" step="1" min="0" value="5"></label>
    <label class="f">새 근무 판단(퇴근 후 시간)<input type="number" id="gap" step="0.5" min="1" value="4"></label>
    <label class="f">한 구간 최대 시간<input type="number" id="maxiv" step="1" min="8" value="16"></label>
  </div></details>
  <div class="row"><button id="go" disabled>분석하기</button><span class="sub" id="status"></span></div>
  <div class="err" id="err"></div>
</div>
<div id="result" hidden>
  <div class="card"><b>결과 파일</b><div class="files" id="files"></div><pre id="summary"></pre></div>
  <iframe id="report" title="분석 보고서"></iframe>
</div>
</div>
<script>
const $=s=>document.querySelector(s); let chosen=null;
const pick=f=>{chosen=f; $('#fname').textContent=f?f.name:''; $('#go').disabled=!f;};
$('#drop').onclick=()=>$('#file').click(); $('#file').onchange=e=>pick(e.target.files[0]);
['dragover','dragenter'].forEach(t=>$('#drop').addEventListener(t,e=>{e.preventDefault();$('#drop').classList.add('on');}));
['dragleave','drop'].forEach(t=>$('#drop').addEventListener(t,e=>{e.preventDefault();$('#drop').classList.remove('on');}));
$('#drop').addEventListener('drop',e=>{if(e.dataTransfer.files[0]) pick(e.dataTransfer.files[0]);});
const b64=f=>new Promise((ok,no)=>{const r=new FileReader(); r.onload=()=>ok(r.result.split(',')[1]); r.onerror=no; r.readAsDataURL(f);});
$('#go').onclick=async()=>{
  $('#err').textContent=''; $('#go').disabled=true; $('#status').textContent='분석 중…';
  try{
    const body={file:{name:chosen.name,b64:await b64(chosen)},settings:{auto_correct_threshold:$('#auto').value,review_threshold:$('#review').value,
      duplicate_window_min:$('#dup').value,new_shift_gap_hours:$('#gap').value,max_interval_hours:$('#maxiv').value}};
    const c=$('#confirm').files[0]; if(c) body.confirm={name:c.name,b64:await b64(c)};
    const res=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await res.json(); if(!res.ok) throw new Error(j.error||res.statusText);
    $('#summary').textContent=j.summary; const fl=$('#files'); fl.textContent='';
    for(const f of j.files){const a=document.createElement('a'); a.href=`/jobs/${j.job}/${f.name}`; a.textContent=f.name;
      if(f.name.endsWith('.html')) a.target='_blank'; const s=document.createElement('span'); s.textContent=f.desc; a.append(s); fl.append(a);}
    $('#report').src=`/jobs/${j.job}/report.html`; $('#result').hidden=false; $('#status').textContent='완료';
  }catch(e){$('#err').textContent=e.message; $('#status').textContent='';}
  finally{$('#go').disabled=!chosen;}
};
</script></body></html>
"""
