#!/usr/bin/env python3
"""Small read-only dashboard for generic harness runs."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = "generic-harness/v2"
MAX_LOG_BYTES = 120_000
MAX_LOG_LINES = 400
MAX_LOG_FILES = 20
DASHBOARD_HEALTH = b"ok-tui-v2\n"
STATE_FIELDS = (
    "schema_version",
    "run_id",
    "status",
    "phase",
    "request",
    "workspace",
    "candidate_workspace",
    "coordinator_agent",
    "coordinator_detection",
    "worker_agent",
    "reviewer_agent",
    "review_index",
    "max_reviews",
    "active_agent",
    "artifact_id",
    "last_error",
    "created_at",
    "updated_at",
    "finished_at",
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_ -]?key|authorization|password|secret|credential)\s*[:=]\s*)\S+"
)
BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
SECRET_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,})\b"
)
CONTROL_CODES = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|.)|"
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def redact(text: str) -> str:
    text = SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    text = BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    return SECRET_TOKEN.sub("[REDACTED]", text)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def clean_dialogue_text(text: str) -> str:
    """Strip terminal controls and redact likely credentials from log text."""
    text = CONTROL_CODES.sub("", str(text).replace("\r\n", "\n").replace("\r", "\n"))
    return redact(text.replace("\nHARNESS_ROLE_COMPLETE", "").strip())


def tail_text(path: Path) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - MAX_LOG_BYTES))
            text = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return clean_dialogue_text("\n".join(text.splitlines()[-MAX_LOG_LINES:]))


def file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return 0.0


def run_log_paths(run_dir: Path) -> list[Path]:
    fixed = [path for path in (run_dir / "harness.log", run_dir / "events.jsonl") if path.is_file()]
    role_logs = list(run_dir.glob("iterations/*/worker.log"))
    role_logs.extend(run_dir.glob("iterations/*/verification.log"))
    role_logs.extend(run_dir.glob("reviews/*/reviewer.log"))
    role_logs = sorted(role_logs, key=file_mtime, reverse=True)[: max(0, MAX_LOG_FILES - len(fixed))]
    return sorted({*fixed, *role_logs}, key=lambda path: (file_mtime(path), str(path)))


def log_entry(path: Path, run_dir: Path) -> dict[str, Any]:
    return {
        "path": str(path.relative_to(run_dir)),
        "text": tail_text(path),
        "updated_at": file_mtime(path),
    }


def load_run(run_dir: Path) -> dict[str, Any] | None:
    state_path = run_dir / "state.json"
    state = read_json(state_path)
    if not state or state.get("schema_version") != SCHEMA_VERSION:
        return None
    record = {field: redact_value(state.get(field)) for field in STATE_FIELDS}
    record["run_id"] = str(state.get("run_id") or run_dir.name)
    record["report_available"] = (run_dir / "FINAL_REPORT.md").is_file()
    record["_sort"] = timestamp(state.get("created_at")) or file_mtime(state_path)
    record["_run_dir"] = run_dir
    return record


def status_payload(root: Path = ROOT) -> dict[str, Any]:
    root = Path(root)
    runs_dir = root / "runs"
    records: list[dict[str, Any]] = []
    if runs_dir.is_dir():
        for run_dir in runs_dir.iterdir():
            if run_dir.is_dir():
                record = load_run(run_dir)
                if record:
                    records.append(record)
    records.sort(key=lambda item: (item["_sort"], item["run_id"]), reverse=True)

    runs = []
    for record in records:
        summary = {key: value for key, value in record.items() if not key.startswith("_")}
        runs.append(summary)

    current = None
    if records:
        current = dict(runs[0])
        run_dir = records[0]["_run_dir"]
        current["logs"] = [log_entry(path, run_dir) for path in run_log_paths(run_dir)]
        current["final_report"] = (
            str(run_dir / "FINAL_REPORT.md") if current["report_available"] else None
        )
    return {"current": current, "runs": runs, "server_time": time.time()}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#101827"><title>Generic Task Harness Status</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#151d31;--line:#2b3854;--text:#edf2ff;--muted:#aab6d1;--blue:#72a7ff;--green:#64dfa7;--red:#ff8290}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1050px;margin:auto;padding:18px}h1,h2,h3{margin:.2em 0}.head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.languages{display:flex;gap:6px}.languages button{color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 9px;cursor:pointer}.languages button[aria-pressed="true"]{border-color:var(--blue);color:var(--blue)}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px;margin-top:14px}.card{grid-column:span 12;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}.third{grid-column:span 4}.badge{display:inline-block;border-radius:99px;padding:3px 9px;background:#263555}.running,.planning,.reviewing,.repairing,.promoting{background:#17406d}.complete,.pass{background:#174735}.failed,.incomplete,.paused{background:#63313b}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#090e1a;border:1px solid var(--line);border-radius:10px;padding:12px;max-height:420px;overflow:auto}.request{font-size:17px}.history{display:grid;gap:8px}.run{padding:9px;border:1px solid var(--line);border-radius:10px}@media(max-width:700px){.third{grid-column:span 12}main{padding:10px}.head{display:block}.languages{margin-top:10px}}
</style></head><body><main>
<div class="head"><div><h1 id="title"></h1><div id="subtitle" class="muted"></div></div>
<div class="languages" aria-label="Language"><button type="button" data-lang="zh" onclick="setLanguage('zh')">中文</button><button type="button" data-lang="en" onclick="setLanguage('en')">English</button></div></div>
<div id="error"></div><div id="app"></div>
<script>
const messages={
  en:{title:'Generic Task Harness',pageTitle:'Generic Task Harness Status',subtitle:'Read-only local status · refreshes every 2 seconds',empty:'No Harness run records yet.',noOutput:'No output yet',waiting:'Waiting',coordinator:'Coordinator',worker:'Worker',reviewer:'Reviewer',active:'Active agent',reviewRound:'Review',history:'Run history',readError:'Could not read status: '},
  zh:{title:'通用任务 Harness',pageTitle:'通用任务 Harness 状态',subtitle:'只读本地状态 · 每 2 秒自动刷新',empty:'尚无通用任务运行记录。',noOutput:'暂无输出',waiting:'等待',coordinator:'协调角色',worker:'执行角色',reviewer:'审计角色',active:'当前活动角色',reviewRound:'审计',history:'运行历史',readError:'无法读取状态：'}
};
let lastPayload=null;
let lang=(localStorage.getItem('harness-lang')||navigator.language||'en').toLowerCase().startsWith('zh')?'zh':'en';
const t=function(key){return messages[lang][key]};
const esc=function(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})};
const cls=function(v){return String(v||'').toLowerCase().replace(/[^a-z_]/g,'')};
function setLanguage(next){
  lang=next==='zh'?'zh':'en';localStorage.setItem('harness-lang',lang);document.documentElement.lang=lang==='zh'?'zh-CN':'en';document.title=t('pageTitle');document.querySelector('#title').textContent=t('title');document.querySelector('#subtitle').textContent=t('subtitle');document.querySelectorAll('[data-lang]').forEach(function(button){button.setAttribute('aria-pressed',String(button.dataset.lang===lang))});if(lastPayload)render(lastPayload)
}
function render(d){
  lastPayload=d;
  const c=d.current;
  if(!c){document.querySelector('#app').innerHTML='<div class="card">'+t('empty')+'</div>';return}
  const logs=(c.logs||[]).map(function(x){return '<div class="card"><h3>'+esc(x.path)+'</h3><pre>'+esc(x.text||t('noOutput'))+'</pre></div>'}).join('');
  const history=(d.runs||[]).map(function(x){return '<div class="run"><span class="badge '+cls(x.status)+'">'+esc(x.status)+'</span> <b>'+esc(x.run_id)+'</b><div class="muted">'+esc(x.phase||'')+' · '+esc(x.request||'')+'</div></div>'}).join('');
  const active=c.active_agent&&typeof c.active_agent==='object'?[c.active_agent.profile,c.active_agent.role,c.active_agent.pid&&('PID '+c.active_agent.pid)].filter(Boolean).join(' · '):(c.active_agent||'—');
  document.querySelector('#app').innerHTML=
    '<section class="grid"><div class="card"><span class="badge '+cls(c.status)+'">'+esc(c.status)+'</span><h2>'+esc(c.phase||t('waiting'))+'</h2><div class="request">'+esc(c.request||'')+'</div>'+(c.last_error?'<p class="failed">'+esc(c.last_error)+'</p>':'')+'</div>'+
    '<div class="card third"><div class="muted">'+t('coordinator')+'</div><b>'+esc(c.coordinator_agent||'—')+'</b></div>'+
    '<div class="card third"><div class="muted">'+t('worker')+'</div><b>'+esc(c.worker_agent||'—')+'</b></div>'+
    '<div class="card third"><div class="muted">'+t('reviewer')+'</div><b>'+esc(c.reviewer_agent||'—')+'</b></div>'+
    '<div class="card"><div class="muted">'+t('active')+'</div><b>'+esc(active)+'</b><div class="muted">Workspace: '+esc(c.workspace||'—')+' · '+t('reviewRound')+' '+esc((c.review_index||0)+1)+' / '+esc(c.max_reviews||0)+'</div></div>'+
    logs+
    '<div class="card"><h3>'+t('history')+'</h3><div class="history">'+history+'</div></div></section>';
}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error(await r.text());render(await r.json());document.querySelector('#error').textContent=''}catch(e){document.querySelector('#error').textContent=t('readError')+e.message}}
setLanguage(lang);refresh();setInterval(refresh,2000);
</script></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "GenericHarnessStatus/1"

    @property
    def dashboard_root(self) -> Path:
        return Path(getattr(self.server, "dashboard_root", ROOT))

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_bytes(DASHBOARD_HEALTH, "text/plain; charset=utf-8")
        elif path == "/api/status":
            body = json.dumps(status_payload(self.dashboard_root), ensure_ascii=False).encode()
            self.send_bytes(body, "application/json; charset=utf-8")
        elif path == "/":
            self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
        else:
            self.send_bytes(b"Not found.\n", "text/plain; charset=utf-8", 404)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def make_server(
    root: Path = ROOT, host: str = "127.0.0.1", port: int = 8787
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("The unauthenticated dashboard may only listen on localhost; use an SSH tunnel for remote access.")
    server = ThreadingHTTPServer((host, port), Handler)
    server.dashboard_root = Path(root)  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    try:
        server = make_server(ROOT, args.host, args.port)
    except ValueError as error:
        parser.error(str(error))
    print(f"Harness status dashboard: http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
