#!/usr/bin/env python3
"""
FindIT web UI: a minimal local browser interface over findit.py.
No external dependencies (uses Python's built-in http.server).
Usage: python3 findit_web.py   then open http://127.0.0.1:8000
"""

import os
import sys
import json
import threading
import webbrowser
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv

import findit  # reuse the CLI's search/rerank functions unchanged

# ── Configuration ────────────────────────────────────────────────────────────
HOST = "127.0.0.1"  # localhost only; never expose private results on the network
PORT = int(os.environ.get("FINDIT_PORT", "8000"))
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# ─────────────────────────────────────────────────────────────────────────────


def _join_names(names: list) -> str:
    """Human-readable join: ["Box"] -> "Box"; ["Box","Confluence"] -> "Box and
    Confluence"; three -> "Google Drive, Box, and Confluence"."""
    if len(names) <= 1:
        return names[0] if names else "no sources"
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


# Load credentials/settings the same way the CLI does (a shell var still wins).
_env_path = os.path.join(_SCRIPT_DIR, ".env")
load_dotenv(_env_path)
findit._restrict(_env_path, 0o600)

_SOURCES = findit.configured_sources()
if not any(_SOURCES.values()):
    print("No sources configured. Set up at least one of: drive_credentials.json "
          "(Google Drive), box_credentials.json (Box), confluence_credentials.json "
          "(Confluence). See the README.")
    sys.exit(1)

_USE_LLM = findit.gateway_configured()

# Authenticate the configured sources once at startup and reuse across requests.
_enabled = [label for key, label in
            (("drive", "Google Drive"), ("box", "Box"), ("confluence", "Confluence"))
            if _SOURCES[key]]
_SEARCH_LABEL = _join_names(_enabled)  # configured sources, for the page text
print(f"Authenticating with {', '.join(_enabled)}...")
_drive_service = findit.get_drive_service() if _SOURCES["drive"] else None
_box_client = findit.get_box_client() if _SOURCES["box"] else None
_confluence_config = findit.get_confluence_config() if _SOURCES["confluence"] else None
if not _USE_LLM:
    print("No gateway key configured; results will be returned raw (no AI ranking).")

# Single-user local tool: run one search at a time to keep the reused clients safe.
_search_lock = threading.Lock()


def search_and_rank(query: str) -> list[dict]:
    """Search the configured sources, merge, and (if a gateway key is set) re-rank.
    Returns the ranked list, or raw merged results when AI ranking is off."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        if _drive_service is not None:
            futures["Google Drive"] = executor.submit(findit.search_drive, _drive_service, query)
        if _box_client is not None:
            futures["Box"] = executor.submit(findit.search_box, _box_client, query)
        if _confluence_config is not None:
            futures["Confluence"] = executor.submit(findit.search_confluence, _confluence_config, query)

        results = {}
        for label, fut in futures.items():
            try:
                results[label] = fut.result()
            except Exception as e:
                print(f"{label} search failed: {e}")
                results[label] = []

    drive_files = results.get("Google Drive", [])
    box_files = results.get("Box", [])
    confluence_files = results.get("Confluence", [])
    for f in drive_files:
        f["source"] = "Google Drive"

    files = drive_files + box_files + confluence_files
    if not files:
        return []
    if not _USE_LLM:
        findit.log_search(query, mode="raw")
        return files
    return findit.scope_and_rerank(query, files).ranked


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FindIT</title>
<style>
  :root { --bg:#f7f7f8; --card:#fff; --line:#e3e3e6; --muted:#6b7280; --accent:#2563eb; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:#111;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  .wrap { max-width:720px; margin:0 auto; padding:40px 20px 80px; }
  h1 { margin:0 0 4px; font-size:28px; }
  .sub { color:var(--muted); margin:0 0 16px; font-size:14px; }
  .tips { background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:10px 14px 10px 30px; margin:0 0 18px; color:#374151; font-size:13px; }
  .tips li { margin:3px 0; }
  form { display:flex; gap:8px; margin-bottom:8px; }
  input[type=text] { flex:1; padding:12px 14px; font-size:16px; border:1px solid var(--line);
                     border-radius:8px; background:var(--card); }
  input[type=text]:focus { outline:none; border-color:var(--accent); }
  button { padding:12px 18px; font-size:16px; border:none; border-radius:8px;
           background:var(--accent); color:#fff; cursor:pointer; }
  button:disabled { opacity:.6; cursor:default; }
  .status { color:var(--muted); font-size:14px; min-height:20px; margin:8px 0 16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px 16px; margin-bottom:12px; }
  .card a.title { font-size:16px; font-weight:600; color:var(--accent); text-decoration:none; }
  .card a.title:hover { text-decoration:underline; }
  .reason { color:#374151; font-size:14px; margin:6px 0 8px; }
  .badge { display:inline-block; font-size:12px; font-weight:600; padding:2px 8px;
           border-radius:999px; color:#fff; }
  .badge.drive { background:#16a34a; }
  .badge.box { background:#2563eb; }
  .badge.confluence { background:#7c3aed; }
  .rank { color:var(--muted); font-size:13px; margin-right:6px; }
  .meta { color:var(--muted); font-size:12px; margin-top:6px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>FindIT</h1>
  <p class="sub">Search __SEARCH_SOURCES__ in plain English.</p>
  <ul class="tips">
    <li>Use keywords, not full sentences.</li>
    <li>Skip filler words like "document", "spreadsheet", "I need", or "please find".</li>
    <li>Fewer, more specific words rank better. A single distinctive term often works best.</li>
  </ul>
  <form id="f">
    <input id="q" type="text" placeholder="e.g. onboarding checklist" autocomplete="off" autofocus>
    <button id="btn" type="submit">Search</button>
  </form>
  <div class="status" id="status"></div>
  <div id="results"></div>
</div>
<script>
  const form = document.getElementById('f');
  const input = document.getElementById('q');
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  const results = document.getElementById('results');

  function badgeClass(source){
    const s = (source || '').toLowerCase();
    if (s.indexOf('drive') !== -1) return 'drive';
    if (s.indexOf('box') !== -1) return 'box';
    if (s.indexOf('confluence') !== -1) return 'confluence';
    return '';
  }

  function safeHref(url){
    // Only allow http/https links; neutralize javascript:, data:, etc.
    try {
      const u = new URL(url, window.location.href);
      return (u.protocol === 'http:' || u.protocol === 'https:') ? url : '#';
    } catch (e) { return '#'; }
  }

  function render(items){
    results.innerHTML = '';
    items.forEach(function(item, i){
      const card = document.createElement('div');
      card.className = 'card';

      const head = document.createElement('div');
      const rank = document.createElement('span');
      rank.className = 'rank';
      rank.textContent = (i + 1) + '.';
      const a = document.createElement('a');
      a.className = 'title';
      a.href = safeHref(item.webViewLink);
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = item.name || '(untitled)';
      head.appendChild(rank);
      head.appendChild(a);

      const reason = document.createElement('div');
      reason.className = 'reason';
      reason.textContent = item.reason || '';

      const badge = document.createElement('span');
      badge.className = 'badge ' + badgeClass(item.source);
      badge.textContent = item.source || '';

      const meta = document.createElement('div');
      meta.className = 'meta';
      const date = (item.modifiedTime || '').slice(0, 10);
      const parts = [];
      if (item.filetype) parts.push(item.filetype);
      if (date && date !== 'unknown') parts.push('Modified ' + date);
      if (item.owner) parts.push(item.owner);
      if (item.location) parts.push('in ' + item.location);
      meta.textContent = parts.join('  ·  ');

      card.appendChild(head);
      card.appendChild(reason);
      card.appendChild(badge);
      card.appendChild(meta);
      results.appendChild(card);
    });
  }

  form.addEventListener('submit', async function(e){
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    btn.disabled = true;
    results.innerHTML = '';
    status.textContent = 'Searching __SEARCH_SOURCES__...';
    try {
      const r = await fetch('/search?q=' + encodeURIComponent(q));
      const data = await r.json();
      if (data.error) { status.textContent = 'Error: ' + data.error; return; }
      if (!data.results || !data.results.length) { status.textContent = 'No results found.'; return; }
      status.textContent = 'Top ' + data.results.length + ' results';
      render(data.results);
    } catch (err) {
      status.textContent = 'Request failed: ' + err;
    } finally {
      btn.disabled = false;
    }
  });
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__SEARCH_SOURCES__", _SEARCH_LABEL)  # configured-source list, computed at startup


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return
        if parsed.path == "/search":
            query = (parse_qs(parsed.query).get("q", [""])[0]).strip()
            if not query:
                self._send(400, "application/json", json.dumps({"error": "empty query"}).encode("utf-8"))
                return
            try:
                with _search_lock:
                    results = search_and_rank(query)
                payload = {"query": query, "results": results}
                self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode("utf-8"))
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    url = f"http://{HOST}:{PORT}"
    print(f"FindIT web UI running at {url}")
    print("Press Ctrl+C to stop.")
    if os.environ.get("FINDIT_NO_BROWSER") != "1":
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
