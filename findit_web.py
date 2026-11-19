#!/usr/bin/env python3
"""
FindIT web UI: a minimal local browser interface over findit.py.
No external dependencies (uses Python's built-in http.server).
Usage: python3 findit_web.py   then open http://127.0.0.1:8000
"""

import os
import re
import sys
import json
import base64
import threading
import webbrowser
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

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

_USE_LLM = findit.llm_mode() is not None

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
    print(f"AI ranking off ({findit.llm_off_reason()}); results will be returned raw.")

# Single-user local tool: run one search at a time to keep the reused clients safe.
_search_lock = threading.Lock()


def search_and_rank(query: str) -> dict:
    """Search the configured sources, merge, and (if an API or gateway key is set) re-rank.
    Returns {"results": ranked list or raw merged results, "ranked": whether Claude
    ranked them, "counts": results found per source, "note": how they were produced}."""
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
    counts = {label: len(results[label]) for label in futures}
    out = {"results": files, "ranked": False, "counts": counts}
    if not files:
        out["note"] = "No results found."
    elif not _USE_LLM:
        findit.log_search(query, mode="raw")
        out["note"] = f"Raw results; AI ranking off ({findit.llm_off_reason()})."
    else:
        result = findit.scope_and_rerank(query, files)
        out["results"] = result.ranked
        if not result.configured:
            out["note"] = "Raw results; no approved sources configured (approved_sources.json)."
        elif result.sent_to_llm:
            out["ranked"] = True
            out["note"] = f"Ranked by Claude ({findit.claude_model()}) from approved sources."
        else:
            out["note"] = "No results were within approved scope for this query."
    return out


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FindIT</title>
<meta name="referrer" content="no-referrer">
__FONTS_LINK__
<style>
  :root {
    /* Every color on this page is from the Cornell brand palette
       (brand.cornell.edu/design-center/colors); text colors are ones it approves on white. */
    --red: #B31B1B;      /* Carnelian */
    --text: #222222;     /* Dark gray: all text, and the dark top-bar block / button hover */
    --link: #006699;     /* Link blue */
    --focus: #006699;    /* Link blue: search border and keyboard focus */
    --line: #A2998B;     /* Dark warm gray: borders and dividers */
    --bg: #FFFFFF;       /* White */
    /* Cornell typography (brand.cornell.edu/design-center/typography): Palatino is the
       primary serif; Freight Sans Pro is the sans-serif, used when an Adobe Fonts project
       is configured (FINDIT_ADOBE_FONTS_ID), otherwise a close system sans-serif. */
    --serif: Palatino, "Palatino Linotype", "Book Antiqua", Georgia, serif;
    --sans: "freight-sans-pro", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    /* Source colors from the Cornell brand palette (brand.cornell.edu/design-center/colors):
       squares use the graphic accents; label text uses the variants approved for small text on white. */
    --drive-mark: #6EB43F; --drive-text: #4B7B2B;
    --box-mark: #006699; --box-text: #006699;
    --confluence-mark: #F8981D; --confluence-text: #222222;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  input, button { font-family: inherit; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font-family: var(--sans); }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .wrap { max-width: 1200px; margin: 0 auto; padding: 0 20px; }

  .topbar { background: var(--red); color: var(--bg); font-size: 12px; font-weight: 700;
            letter-spacing: .08em; text-transform: uppercase; }
  .topbar .wrap { display: flex; justify-content: space-between; align-items: stretch; gap: 16px; }
  .topbar .note { background: var(--text); padding: 11px 20px; margin-left: -20px;
                  text-transform: none; letter-spacing: 0; font-weight: 500; font-size: 13px; }
  .topbar .sources { padding: 11px 0; text-align: right; }

  header.site { border-bottom: 1px solid var(--line); }
  header.site .wrap { display: flex; align-items: center; gap: 18px; padding-top: 14px; padding-bottom: 14px; flex-wrap: wrap; }
  .brand { display: flex; align-items: center; gap: 12px; color: var(--text); text-decoration: none; }
  .brand img { width: 64px; height: 64px; }
  .wordmark { font-family: var(--serif); font-size: 34px; }
  .tagline { font-family: var(--serif); font-size: 20px; color: var(--text); margin-left: auto; }

  form.search { display: flex; margin: 30px 0 0; }
  form.search input { flex: 1; min-width: 0; font-size: 26px; padding: 14px 20px; color: var(--text);
                      border: 2px solid var(--focus); border-right: none; border-radius: 3px 0 0 3px; }
  form.search input:focus { outline: 2px solid var(--focus); outline-offset: 2px; }
  form.search button { width: 84px; border: none; background: var(--red); color: var(--bg); cursor: pointer;
                       border-radius: 0 3px 3px 0; display: grid; place-items: center; }
  form.search button:hover { background: var(--text); }
  form.search button:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
  form.search button:disabled { opacity: .7; cursor: default; }

  .tabs { display: flex; flex-wrap: wrap; gap: 12px 36px; margin-top: 34px; border-bottom: 1px solid var(--line); }
  .tabs button { background: none; border: none; padding: 0 0 10px; font: inherit; font-size: 12px; font-weight: 700;
                 letter-spacing: .04em; text-transform: uppercase; color: var(--text); cursor: pointer;
                 border-bottom: 2px solid transparent; margin-bottom: -1px; white-space: nowrap; }
  .tabs button[aria-selected="true"] { color: var(--text); border-bottom-color: var(--red); }
  .tabs button:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
  .status { color: var(--text); font-size: 14px; min-height: 20px; margin: 16px 0 0; }

  .layout { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 56px; margin: 18px 0 60px; }
  .result { padding: 24px 0 26px; border-bottom: 1px solid var(--line); }
  .result h3 { margin: 0 0 10px; font-size: 22px; font-weight: 400; line-height: 1.3; }
  .result h3 a { color: var(--link); text-decoration: none; }
  .result h3 a:hover { text-decoration: underline; }
  .result h3 b { font-weight: 700; }
  .meta { font-size: 12px; color: var(--text); margin-bottom: 6px; }
  .meta .src { color: var(--text); font-weight: 700; font-size: 11px; letter-spacing: .07em; text-transform: uppercase; }
  .meta .src::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 2px;
                       background: var(--mark, currentColor); margin-right: 6px; vertical-align: 0; }
  .meta .src-drive { color: var(--drive-text); --mark: var(--drive-mark); }
  .meta .src-box { color: var(--box-text); --mark: var(--box-mark); }
  .meta .src-confluence { color: var(--confluence-text); --mark: var(--confluence-mark); }
  .snippet { margin: 0; font-size: 16px; line-height: 1.75; color: var(--text); }

  .box { position: relative; border: 1px solid var(--line); padding: 22px 26px 24px; margin-bottom: 30px; overflow: hidden; }
  .box.flag::before { content: ""; position: absolute; top: 0; left: 0; border-style: solid;
                      border-width: 26px 26px 0 0; border-color: var(--red) transparent transparent transparent; }
  .box h2 { font-family: var(--serif); font-weight: 400; font-size: 26px; margin: 0 0 12px; }
  .box ul { margin: 0; padding-left: 18px; font-size: 14px; line-height: 1.6; }
  .box p { margin: 0 0 10px; font-size: 14px; line-height: 1.6; }

  @media (max-width: 900px) {
    .layout { grid-template-columns: 1fr; gap: 20px; }
    .tagline { margin-left: 0; width: 100%; font-size: 17px; }
  }
  @media (max-width: 600px) {
    .topbar .note { display: none; }
    .topbar .sources { text-align: left; }
    .wordmark { font-size: 28px; }
    .brand img { width: 48px; height: 48px; }
    form.search input { font-size: 19px; padding: 12px 14px; }
    form.search button { width: 60px; }
    .tabs { gap: 12px 22px; }
    .result h3 { font-size: 19px; }
  }
</style>
</head>
<body>
<div class="topbar"><div class="wrap">
  <span class="note">Searches only what you can already access</span>
  <span class="sources">__SOURCE_LIST__</span>
</div></div>
<header class="site"><div class="wrap">
  <a class="brand" href="/">__LOGO_IMG__<span class="wordmark">FindIT</span></a>
  <span class="tagline">Search __SEARCH_SOURCES__ in plain English</span>
</div></header>
<div class="wrap">
  <form id="f" class="search" role="search">
    <label for="q" class="sr-only">Search</label>
    <input id="q" type="search" placeholder="e.g. onboarding checklist" autocomplete="off" autofocus>
    <button id="btn" type="submit" aria-label="Search">
      <svg width="30" height="30" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><circle cx="10" cy="10" r="6.5"/><line x1="15" y1="15" x2="21" y2="21"/></svg>
    </button>
  </form>
  <div class="tabs" id="tabs" role="tablist" aria-label="Filter by source" hidden></div>
  <div class="status" id="status" role="status"></div>
  <div class="layout">
    <main><div id="results"></div></main>
    <aside>
      <section class="box flag">
        <h2>Search tips</h2>
        <ul>
          <li>Use keywords, not full sentences.</li>
          <li>Skip filler words like "document", "spreadsheet", "I need", or "please find".</li>
          <li>Fewer, more specific words rank better. A single distinctive term often works best.</li>
        </ul>
      </section>
      <section class="box" id="about" hidden>
        <h2>About these results</h2>
        <div id="aboutBody"></div>
      </section>
    </aside>
  </div>
</div>
<script>
  const form = document.getElementById('f');
  const input = document.getElementById('q');
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  const results = document.getElementById('results');
  const tabs = document.getElementById('tabs');
  const about = document.getElementById('about');
  const aboutBody = document.getElementById('aboutBody');
  const state = { items: [], terms: [], tab: 'all' };

  function safeHref(url){
    // Only allow http/https links; neutralize javascript:, data:, etc.
    try {
      const u = new URL(url, window.location.href);
      return (u.protocol === 'http:' || u.protocol === 'https:') ? url : '#';
    } catch (e) { return '#'; }
  }

  function queryTerms(q){
    const seen = new Set();
    return q.toLowerCase().split(/\s+/).filter(function(t){
      if (t.length < 2 || seen.has(t)) return false;
      seen.add(t); return true;
    });
  }

  function escapeRe(s){ return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  // Append text to el with query words in <b>. Builds text nodes only; never parses HTML.
  function highlight(el, text, terms){
    if (!terms.length) { el.appendChild(document.createTextNode(text)); return; }
    const re = new RegExp('(' + terms.map(escapeRe).join('|') + ')', 'gi');
    text.split(re).forEach(function(part, i){
      if (!part) return;
      if (i % 2 === 1) { const b = document.createElement('b'); b.textContent = part; el.appendChild(b); }
      else el.appendChild(document.createTextNode(part));
    });
  }

  // CSS class for a source's color in the results list; unknown sources get the default.
  function sourceClass(source){
    const s = (source || '').toLowerCase();
    if (s.indexOf('drive') !== -1) return 'src-drive';
    if (s.indexOf('box') !== -1) return 'src-box';
    if (s.indexOf('confluence') !== -1) return 'src-confluence';
    return '';
  }

  function fmtDate(iso){
    const d = new Date((iso || '').slice(0, 10) + 'T00:00:00');
    return isNaN(d) ? '' : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  }

  // WAI-ARIA tabs pattern: one Tab stop (roving tabindex), Left/Right/Home/End move
  // between tabs and select them, and the results area is the labelled tab panel.
  function selectTab(key){
    state.tab = key;
    renderTabs();
    render();
    const active = tabs.querySelector('[aria-selected="true"]');
    if (active) active.focus();
  }

  function renderTabs(){
    tabs.textContent = '';
    if (!state.items.length) {
      tabs.hidden = true;
      ['role', 'aria-labelledby'].forEach(function(a){ results.removeAttribute(a); });
      return;
    }
    const counts = {};
    state.items.forEach(function(it){ counts[it.source] = (counts[it.source] || 0) + 1; });
    const entries = [['all', 'All', state.items.length]].concat(
      Object.keys(counts).map(function(s){ return [s, s, counts[s]]; }));
    entries.forEach(function(e, i){
      const selected = state.tab === e[0];
      const b = document.createElement('button');
      b.type = 'button';
      b.id = 'tab-' + i;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(selected));
      b.setAttribute('aria-controls', 'results');
      b.tabIndex = selected ? 0 : -1;
      b.textContent = e[1] + ' (' + e[2] + ')';
      b.addEventListener('click', function(){ selectTab(e[0]); });
      b.addEventListener('keydown', function(ev){
        const last = entries.length - 1;
        const next = { ArrowRight: i === last ? 0 : i + 1, ArrowLeft: i === 0 ? last : i - 1,
                       Home: 0, End: last }[ev.key];
        if (next === undefined) return;
        ev.preventDefault();
        selectTab(entries[next][0]);
      });
      if (selected) results.setAttribute('aria-labelledby', b.id);
      tabs.appendChild(b);
    });
    results.setAttribute('role', 'tabpanel');
    tabs.hidden = false;
  }

  function render(){
    results.textContent = '';
    state.items.forEach(function(item, i){
      if (state.tab !== 'all' && item.source !== state.tab) return;
      const art = document.createElement('article');
      art.className = 'result';

      const h3 = document.createElement('h3');
      const a = document.createElement('a');
      a.href = safeHref(item.webViewLink);
      a.target = '_blank';
      a.rel = 'noopener';
      highlight(a, item.name || '(untitled)', state.terms);
      h3.appendChild(a);

      const meta = document.createElement('div');
      meta.className = 'meta';
      const src = document.createElement('span');
      src.className = 'src ' + sourceClass(item.source);
      src.textContent = item.source || '';
      meta.appendChild(src);
      const parts = [];
      if (item.filetype) parts.push('File format: ' + item.filetype);
      if (item.owner) parts.push(item.owner);
      if (item.location) parts.push('in ' + item.location);
      if (parts.length) meta.appendChild(document.createTextNode('  ·  ' + parts.join('  ·  ')));

      const p = document.createElement('p');
      p.className = 'snippet';
      const date = fmtDate(item.modifiedTime);
      if (date) {
        const ds = document.createElement('span');
        ds.className = 'date';
        ds.textContent = date + ' ';
        p.appendChild(ds);
        const dots = document.createElement('b');
        dots.textContent = '… ';
        p.appendChild(dots);
      }
      highlight(p, item.reason || item.snippet || '', state.terms);

      art.appendChild(h3);
      art.appendChild(meta);
      art.appendChild(p);
      results.appendChild(art);
    });
  }

  function renderAbout(data){
    aboutBody.textContent = '';
    const note = document.createElement('p');
    note.textContent = data.note || '';
    aboutBody.appendChild(note);
    const counts = data.counts || {};
    const names = Object.keys(counts);
    if (names.length) {
      const ul = document.createElement('ul');
      names.forEach(function(n){
        const li = document.createElement('li');
        li.textContent = n + ': ' + counts[n] + ' found';
        ul.appendChild(li);
      });
      aboutBody.appendChild(ul);
    }
    about.hidden = false;
  }

  form.addEventListener('submit', async function(e){
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    history.replaceState(null, '', '?q=' + encodeURIComponent(q));  // reload or bookmark re-runs it
    btn.disabled = true;
    state.items = []; state.tab = 'all'; state.terms = queryTerms(q);
    renderTabs(); render();
    about.hidden = true;
    status.textContent = 'Searching __SEARCH_SOURCES__...';
    try {
      const r = await fetch('/search?q=' + encodeURIComponent(q));
      const data = await r.json();
      if (data.error) { status.textContent = 'Error: ' + data.error; return; }
      state.items = data.results || [];
      renderAbout(data);
      if (!state.items.length) {
        status.textContent = data.ranked ? 'No relevant results found.' : (data.note || 'No results found.');
        return;
      }
      status.textContent = (data.ranked ? 'Top ' : '') + state.items.length + ' results';
      renderTabs(); render();
    } catch (err) {
      status.textContent = 'Request failed: ' + err;
    } finally {
      btn.disabled = false;
    }
  });

  // A search in the address bar (?q=..., e.g. from the command line) runs immediately.
  const initialQuery = new URLSearchParams(window.location.search).get('q');
  if (initialQuery) { input.value = initialQuery; form.requestSubmit(); }
</script>
</body>
</html>
"""


def _fonts_link() -> str:
    """Stylesheet link for Cornell's Freight Sans Pro from Adobe Fonts, if the user set
    FINDIT_ADOBE_FONTS_ID (their Adobe Fonts web project ID). Empty otherwise, so by
    default the page makes no outside requests. The ID is validated before use."""
    kit = os.environ.get("FINDIT_ADOBE_FONTS_ID", "").strip()
    if not kit:
        return ""
    if not re.fullmatch(r"[a-z0-9]{5,16}", kit):
        print("Ignoring FINDIT_ADOBE_FONTS_ID: expected a short lowercase letters-and-digits project ID.")
        return ""
    return f'<link rel="stylesheet" href="https://use.typekit.net/{kit}.css">'


def _logo_img() -> str:
    """The FindIT logo as an inline <img> (data URI), read once from docs/logo.png.
    Inline so the page needs no extra route; empty if the file is missing."""
    try:
        with open(os.path.join(_SCRIPT_DIR, "docs", "logo.png"), "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
    except OSError:
        return ""
    return f'<img src="data:image/png;base64,{data}" alt="" width="64" height="64">'


# Fill in values computed at startup (configured sources, logo).
PAGE = (PAGE.replace("__SEARCH_SOURCES__", _SEARCH_LABEL)
            .replace("__SOURCE_LIST__", "  ·  ".join(_enabled))
            .replace("__LOGO_IMG__", _logo_img())
            .replace("__FONTS_LINK__", _fonts_link()))


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
                    payload = {"query": query, **search_and_rank(query)}
                self._send(200, "application/json", json.dumps(payload).encode("utf-8"))
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error": str(e)}).encode("utf-8"))
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    # An optional query on the command line (python3 findit_web.py "onboarding")
    # opens the browser with that search already running.
    query = " ".join(sys.argv[1:]).strip()
    url = f"http://{HOST}:{PORT}"
    open_url = f"{url}/?q={quote(query)}" if query else url
    server = ThreadingHTTPServer((HOST, PORT), Handler)  # bind before the browser asks for the page
    print(f"FindIT web UI running at {url}")
    print("Press Ctrl+C to stop.")
    if os.environ.get("FINDIT_NO_BROWSER") != "1":
        try:
            webbrowser.open(open_url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
