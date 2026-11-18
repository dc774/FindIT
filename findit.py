#!/usr/bin/env python3
"""
FindIT CLI: Natural language document retrieval for Google Drive, Box, and Confluence.
Usage: python findit.py "find me the DMARC spreadsheet"
"""

import os
import sys
import glob
import json
import html
import secrets
import hashlib
import datetime
import anthropic
import concurrent.futures
import webbrowser
import http.server
import urllib.parse
import requests
from collections import namedtuple
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from box_sdk_gen import BoxClient, BoxOAuth, OAuthConfig, GetAuthorizeUrlOptions
from box_sdk_gen.box.token_storage import FileWithInMemoryCacheTokenStorage
from box_sdk_gen.managers.search import SearchForContentContentTypes, SearchForContentType

# ── Configuration ────────────────────────────────────────────────────────────
_SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE     = os.path.join(_SCRIPT_DIR, "drive_credentials.json")
BOX_CREDENTIALS_FILE = os.path.join(_SCRIPT_DIR, "box_credentials.json")
CONFLUENCE_CREDENTIALS_FILE = os.path.join(_SCRIPT_DIR, "confluence_credentials.json")
APPROVED_SOURCES_FILE = os.path.join(_SCRIPT_DIR, "approved_sources.json")
TOKEN_DIR            = os.path.expanduser("~/.findit")
TOKEN_FILE           = os.path.join(TOKEN_DIR, "token.json")
BOX_TOKEN_FILE       = os.path.join(TOKEN_DIR, "box_token")  # shelve adds its own extension
AUDIT_LOG            = os.path.join(TOKEN_DIR, "audit.log")

# Maps the human-readable "source" on each result to its approved_sources.json key.
SOURCE_KEYS = {"Google Drive": "google_drive", "Box": "box", "Confluence": "confluence"}

DRIVE_SCOPES   = ["https://www.googleapis.com/auth/drive.readonly"]
MAX_RESULTS    = 50   # results to fetch per source before Claude re-ranks
TOP_N          = 8    # how many results to show after re-ranking
CLAUDE_MODEL   = "claude-haiku-4-5"  # fast + cheap for re-ranking; bare ID (the Cornell AI Gateway rejects date-suffixed IDs and aliases with a 403)
# ─────────────────────────────────────────────────────────────────────────────


def _restrict(path: str, mode: int):
    """Best-effort tighten permissions on a secret file or dir. No-op if the path
    is missing or the platform does not support chmod (e.g. Windows)."""
    try:
        if os.path.exists(path):
            os.chmod(path, mode)
    except OSError:
        pass


def _secure_token_dir():
    """Create ~/.findit if needed and restrict it to the owner (0700)."""
    os.makedirs(TOKEN_DIR, exist_ok=True)
    _restrict(TOKEN_DIR, 0o700)


def _secure_box_token():
    """Restrict the cached Box token file(s) to the owner (0600)."""
    for path in glob.glob(BOX_TOKEN_FILE + "*"):
        _restrict(path, 0o600)


def get_drive_service():
    """Authenticate with Google Drive, caching token in ~/.findit/token.json."""
    creds = None

    _secure_token_dir()
    _restrict(CREDENTIALS_FILE, 0o600)

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, DRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
        _restrict(TOKEN_FILE, 0o600)

    return build("drive", "v3", credentials=creds)


def get_box_client():
    """Authenticate with Box, caching token in ~/.findit/box_token.* (shelve format)."""
    _restrict(BOX_CREDENTIALS_FILE, 0o600)
    with open(BOX_CREDENTIALS_FILE) as f:
        creds = json.load(f)

    _secure_token_dir()

    storage = FileWithInMemoryCacheTokenStorage(BOX_TOKEN_FILE)
    config = OAuthConfig(
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        token_storage=storage,
    )
    oauth = BoxOAuth(config)

    if storage.get() is not None:
        _secure_box_token()
        return BoxClient(oauth)

    # First run: browser OAuth with local redirect capture.
    # A random state guards the one-shot callback against a forged/cross-site redirect.
    REDIRECT_URL = "http://localhost:8081"
    state = secrets.token_urlsafe(16)
    auth_url = oauth.get_authorize_url(
        options=GetAuthorizeUrlOptions(redirect_uri=REDIRECT_URL, state=state)
    )

    captured = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured["code"] = params.get("code", [None])[0]
            captured["state"] = params.get("state", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Box authorization complete. You may close this tab.")

        def log_message(self, *args):
            pass

    print("Opening Box authorization in your browser...")
    webbrowser.open(auth_url)
    http.server.HTTPServer(("localhost", 8081), _Handler).handle_request()

    if not captured.get("code"):
        print("Box authorization failed: no auth code received.")
        sys.exit(1)
    if captured.get("state") != state:
        print("Box authorization failed: state mismatch (possible CSRF). Please try again.")
        sys.exit(1)

    oauth.get_tokens_authorization_code_grant(captured["code"])
    _secure_box_token()
    return BoxClient(oauth)


def get_confluence_config() -> dict:
    """Load Confluence config (token + base_url) from confluence_credentials.json."""
    _restrict(CONFLUENCE_CREDENTIALS_FILE, 0o600)
    with open(CONFLUENCE_CREDENTIALS_FILE) as f:
        return json.load(f)


def configured_sources() -> dict:
    """Which sources the user has set up. A source is enabled if its credential
    file is present; FindIT searches only what is configured (bring-your-own-key)."""
    return {
        "drive": os.path.exists(CREDENTIALS_FILE),
        "box": os.path.exists(BOX_CREDENTIALS_FILE),
        "confluence": os.path.exists(CONFLUENCE_CREDENTIALS_FILE),
    }


def gateway_configured() -> bool:
    """True if the Cornell AI Gateway key is set. When absent, FindIT returns raw
    results without the Claude re-ranking step (nothing leaves the boundary)."""
    return bool(os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def load_approved_sources() -> dict:
    """Read approved_sources.json. Returns {} (deny-all) if it is missing or invalid."""
    _restrict(APPROVED_SOURCES_FILE, 0o600)
    try:
        with open(APPROVED_SOURCES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def enforce_scope(files: list[dict], approved: dict) -> tuple[list[dict], list[dict]]:
    """Split results into (allowed, blocked) against the approved-sources list.
    Default-deny: a source absent from the config, or a result whose location is
    not on its source's list, is blocked. An entry of "all" clears a whole source."""
    allowed, blocked = [], []
    for f in files:
        entry = approved.get(SOURCE_KEYS.get(f.get("source"), ""))
        ok = False
        if isinstance(entry, dict):
            app = entry.get("approved")
            if app == "all":
                ok = True
            elif isinstance(app, list):
                ok = any(sid in app for sid in f.get("scope_ids", []))
        (allowed if ok else blocked).append(f)
    return allowed, blocked


def _doc_ref(f: dict) -> str:
    """A stable, non-content identifier for a result (hash of its link)."""
    return hashlib.sha256((f.get("webViewLink") or "").encode("utf-8")).hexdigest()[:12]


def log_search(query: str, *, mode: str, allowed: list = None, blocked: list = None):
    """Append one JSONL audit line to ~/.findit/audit.log: the query, the mode, and
    document refs as hashes + scope ids only -- never file names, excerpts, or other
    content, so the log never becomes a second copy of sensitive data."""
    allowed = allowed or []
    blocked = blocked or []

    def refs(items):
        return [{"source": i.get("source", ""), "scope_ids": i.get("scope_ids", []),
                 "doc": _doc_ref(i)} for i in items]

    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "query": query,
        "mode": mode,
        "allowed_count": len(allowed),
        "blocked_count": len(blocked),
        "flagged": bool(blocked),
        "allowed": refs(allowed),
        "blocked": refs(blocked),
    }
    try:
        _secure_token_dir()
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _restrict(AUDIT_LOG, 0o600)
    except OSError:
        pass


_DRIVE_TYPES = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.folder": "Folder",
    "application/pdf": "PDF",
}


def _drive_filetype(mime: str) -> str:
    """Friendly file-type label from a Drive mimeType."""
    if mime in _DRIVE_TYPES:
        return _DRIVE_TYPES[mime]
    if "/" in mime:
        return mime.split("/")[-1].split(".")[-1].upper()[:12]
    return ""


def _drive_escape(term: str) -> str:
    """Escape a term for a Drive query string literal (backslash, then single quote),
    so quotes/backslashes in the query are searched literally, not stripped or breaking out."""
    return term.replace("\\", "\\\\").replace("'", "\\'")


def search_drive(service, query: str) -> list[dict]:
    """Search Google Drive using fullText search and return file metadata."""
    # Match any of the query words (OR) rather than the whole query as one unit,
    # so a multi-word query still surfaces files that mention some of the terms.
    # The reranker then picks the most relevant from the wider pool.
    terms = [_drive_escape(t) for t in query.split() if t.strip()]
    if terms:
        clause = "(" + " or ".join(f"fullText contains '{t}'" for t in terms) + ")"
    else:
        clause = f"fullText contains '{_drive_escape(query)}'"

    results = (
        service.files()
        .list(
            q=f"{clause} and trashed = false",
            pageSize=MAX_RESULTS,
            fields="files(id, name, mimeType, modifiedTime, webViewLink, owners(displayName), parents)",
        )
        .execute()
    )
    files = results.get("files", [])
    # Drop tracking params (e.g. ?usp=drivesdk) so links are clean and single-line.
    for f in files:
        link = f.get("webViewLink")
        if link:
            f["webViewLink"] = link.split("?", 1)[0]
        owners = f.get("owners") or []
        f["owner"] = owners[0].get("displayName", "") if owners else ""
        f["filetype"] = _drive_filetype(f.get("mimeType", ""))
        # Scope ids for the data-scoping gate: the file's immediate parent folder id(s).
        f["scope_ids"] = f.get("parents") or []
    return files


def search_box(client, query: str) -> list[dict]:
    """Search Box file content and return file metadata."""
    response = client.search.search_for_content(
        query=query,
        content_types=[SearchForContentContentTypes.FILE_CONTENT],
        type=SearchForContentType.FILE,
        limit=MAX_RESULTS,
        fields=["id", "name", "modified_at", "shared_link", "owned_by", "parent", "extension"],
    )
    files = []
    for item in (response.entries or []):
        link = (
            item.shared_link.url
            if item.shared_link
            else f"https://app.box.com/file/{item.id}"
        )
        owner = item.owned_by.name if getattr(item, "owned_by", None) else ""
        parent = getattr(item, "parent", None)
        location = parent.name if parent else ""
        parent_id = getattr(parent, "id", None) if parent else None
        ext = getattr(item, "extension", None)
        files.append({
            "name": item.name,
            "modifiedTime": str(item.modified_at) if item.modified_at else "unknown",
            "webViewLink": link,
            "source": "Box",
            "owner": owner,
            "location": location,
            "filetype": ext.upper() if ext else "",
            # Scope id for the data-scoping gate: the file's immediate parent folder id.
            "scope_ids": [parent_id] if parent_id else [],
        })
    return files


def _strip_highlights(text: str) -> str:
    """Remove Confluence search highlight markers and decode HTML entities."""
    text = (text or "").replace("@@@hl@@@", "").replace("@@@endhl@@@", "")
    return html.unescape(text).strip()


def _confluence_space_key(hit: dict) -> str:
    """Best-effort extract a hit's Confluence space key (the scope id for gating).
    Prefers the expanded space object; falls back to parsing the container URL."""
    space = (hit.get("content") or {}).get("space") or hit.get("space") or {}
    if space.get("key"):
        return space["key"]
    disp = (hit.get("resultGlobalContainer") or {}).get("displayUrl", "")
    for marker in ("/display/", "/spaces/"):
        if marker in disp:
            return disp.split(marker, 1)[1].split("/", 1)[0]
    return ""


def _cql_escape(term: str) -> str:
    """Escape a term for a CQL double-quoted string literal (backslash, then double
    quote), so quotes/backslashes are searched literally rather than stripped."""
    return term.replace("\\", "\\\\").replace('"', '\\"')


def search_confluence(config: dict, query: str) -> list[dict]:
    """Search Confluence pages via the REST search API (CQL), returning metadata plus an excerpt."""
    base_url = config["base_url"].rstrip("/")
    # Match any of the query words (OR) rather than all of them, widening the
    # candidate pool for the reranker.
    terms = [_cql_escape(t) for t in query.split() if t]
    if terms:
        text_clause = "(" + " or ".join(f'text ~ "{t}"' for t in terms) + ")"
    else:
        text_clause = f'text ~ "{_cql_escape(query)}"'
    cql = f"{text_clause} and type = page"

    response = requests.get(
        f"{base_url}/rest/api/search",
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Accept": "application/json",
        },
        params={"cql": cql, "limit": MAX_RESULTS, "expand": "content.history,content.space"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    files = []
    for hit in data.get("results", []):
        url = hit.get("url", "")
        history = (hit.get("content") or {}).get("history") or {}
        owner = (history.get("createdBy") or {}).get("displayName", "")
        location = (hit.get("resultGlobalContainer") or {}).get("title", "")
        space_key = _confluence_space_key(hit)
        files.append({
            "name": _strip_highlights(hit.get("title", "(untitled)")),
            "modifiedTime": hit.get("lastModified", "unknown"),
            "webViewLink": f"{base_url}{url}" if url else base_url,
            "source": "Confluence",
            "owner": owner,
            "location": location,
            "snippet": _strip_highlights(hit.get("excerpt", "")),
            # Scope id for the data-scoping gate: the page's space key.
            "scope_ids": [space_key] if space_key else [],
        })
    return files


def _rerank_with_claude(query: str, files: list[dict]) -> list[dict]:
    """Ask Claude to re-rank results by relevance. Returns the ranked file list.
    Private: the only caller is scope_and_rerank, which gates the input first so
    nothing reaches the external API without passing the data-scoping gate."""
    if not files:
        return []

    # Build a compact summary of files for Claude
    lines = []
    for i, f in enumerate(files):
        line = f"{i+1}. {f['name']} (modified: {f.get('modifiedTime', 'unknown')[:10]})"
        snippet = f.get("snippet")
        if snippet:
            line += f" | excerpt: {snippet[:200]}"
        lines.append(line)
    file_list = "\n".join(lines)

    prompt = f"""A user searched for: "{query}"

Your search returned these {len(files)} files:
{file_list}

Return ONLY a JSON array of the top {TOP_N} most relevant file numbers in order, with a short reason for each.
Format exactly like this, with no other text:
[{{"rank": 1, "file_number": 3, "reason": "..."}}, ...]"""

    # Route through the Cornell AI Gateway: base_url points at the gateway and
    # auth_token sends "Authorization: Bearer <key>" (the gateway's scheme). We do
    # not pass api_key, so no x-api-key header is sent and there is no risk of the
    # SDK sending both headers (which the gateway would reject). main()/findit_web.py
    # verify both env vars are set before any search runs.
    client = anthropic.Anthropic(
        base_url=os.environ["ANTHROPIC_BASE_URL"],
        auth_token=os.environ["ANTHROPIC_AUTH_TOKEN"],
    )
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    ranked = json.loads(raw)

    # Map back to full file objects
    output = []
    for item in ranked:
        idx = item["file_number"] - 1
        if 0 <= idx < len(files):
            f = files[idx].copy()
            f["reason"] = item["reason"]
            output.append(f)

    return output


RerankResult = namedtuple("RerankResult", ["ranked", "blocked", "configured", "sent_to_llm"])


def scope_and_rerank(query: str, files: list[dict]) -> RerankResult:
    """The single boundary where results may be sent to the external re-ranking API.
    Applies the default-deny data-scoping gate first and logs the search; only the
    approved results are passed to Claude. Out-of-scope results are dropped from the
    output and recorded as flagged exceptions. With no approved_sources.json (or an
    empty one), nothing is cleared, so nothing leaves the boundary (raw fallback)."""
    approved = load_approved_sources()
    if not approved:
        log_search(query, mode="raw_default_deny")
        return RerankResult(ranked=files, blocked=[], configured=False, sent_to_llm=False)

    allowed, blocked = enforce_scope(files, approved)
    log_search(query, mode="ranked", allowed=allowed, blocked=blocked)
    ranked = _rerank_with_claude(query, allowed) if allowed else []
    return RerankResult(ranked=ranked, blocked=blocked, configured=True, sent_to_llm=bool(allowed))


def print_results(query: str, results: list[dict]):
    """Print ranked results as plain terminal text."""
    print(f'\nFindIT results for: "{query}"\n')
    print("─" * 60)

    if not results:
        print("No matching documents found.")
        return

    for i, f in enumerate(results, 1):
        print(f"{i}. {f['name']}")
        print(f"   Source: {f.get('source', '')}")
        reason = f.get("reason")
        if reason:
            print(f"   {reason}")
        meta = []
        if f.get("filetype"):
            meta.append(f["filetype"])
        date = (f.get("modifiedTime") or "")[:10]
        if date and date != "unknown":
            meta.append(f"Modified {date}")
        if f.get("owner"):
            meta.append(f"Owner: {f['owner']}")
        if f.get("location"):
            meta.append(f"in {f['location']}")
        if meta:
            print(f"   {'  ·  '.join(meta)}")
        print(f"   {f.get('webViewLink', '(no link)')}")
        print()


def main():
    # --no-llm forces raw results (no external re-ranking call) even when a
    # gateway key is configured. Strip it out before assembling the query.
    raw_args = sys.argv[1:]
    no_llm = "--no-llm" in raw_args
    query_args = [a for a in raw_args if a != "--no-llm"]

    if not query_args:
        print("Usage: python3 findit.py [--no-llm] \"your search query\"")
        sys.exit(1)

    query = " ".join(query_args)

    # Load credentials/settings from the project .env (a shell-exported var wins,
    # since load_dotenv does not override an already-set environment variable).
    _env_path = os.path.join(_SCRIPT_DIR, ".env")
    load_dotenv(_env_path)
    _restrict(_env_path, 0o600)

    sources = configured_sources()
    if not any(sources.values()):
        print("No sources configured. Set up at least one of: drive_credentials.json "
              "(Google Drive), box_credentials.json (Box), confluence_credentials.json "
              "(Confluence). See the README.")
        sys.exit(1)

    use_llm = gateway_configured() and not no_llm

    enabled = [label for key, label in
               (("drive", "Google Drive"), ("box", "Box"), ("confluence", "Confluence"))
               if sources[key]]
    print(f"Searching {', '.join(enabled)} for: {query}")

    # Authenticate only the configured sources.
    drive_service = get_drive_service() if sources["drive"] else None
    box_client = get_box_client() if sources["box"] else None
    confluence_config = get_confluence_config() if sources["confluence"] else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        if drive_service is not None:
            futures["Google Drive"] = executor.submit(search_drive, drive_service, query)
        if box_client is not None:
            futures["Box"] = executor.submit(search_box, box_client, query)
        if confluence_config is not None:
            futures["Confluence"] = executor.submit(search_confluence, confluence_config, query)

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
        print("No results found.")
        sys.exit(0)

    counts = ", ".join(f"{len(results[label])} {label}" for label in futures)

    if use_llm:
        print(f"Found {len(files)} files ({counts}).")
        try:
            result = scope_and_rerank(query, files)
        except Exception as e:
            print(f"Ranking step failed: {e}")
            print("This usually means a Cornell AI Gateway problem (invalid key, wrong model, or a rate limit). Check ANTHROPIC_BASE_URL and ANTHROPIC_AUTH_TOKEN in your .env.")
            sys.exit(1)
        ranked = result.ranked
        if not result.configured:
            print("No approved sources configured (approved_sources.json); AI ranking off, showing raw results. See approved_sources.example.json.")
        else:
            if result.blocked:
                print(f"{len(result.blocked)} result(s) outside approved scope were excluded from ranking and logged.")
            if not ranked:
                print("No results were within approved scope for this query.")
    else:
        why = "--no-llm" if no_llm else "no gateway key configured"
        print(f"Found {len(files)} files ({counts}). AI ranking off ({why}); showing raw results.")
        log_search(query, mode="raw")
        ranked = files

    print_results(query, ranked)


if __name__ == "__main__":
    main()
