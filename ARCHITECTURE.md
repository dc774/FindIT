# FindIT: Architecture Documentation

## What it does
CLI tool: user types a plain English query, FindIT searches Google Drive, Box, and Confluence in parallel and returns the most relevant documents with links and source attribution.

## Usage
```bash
python findit.py "find me the DMARC spreadsheet"
```

## File layout
```
FindIT/
├── findit.py                            # The core tool: CLI + all search/rerank functions
├── findit_web.py                        # Optional local browser UI (stdlib http.server; imports findit.py)
├── requirements.txt                     # Python dependencies (pinned)
├── README.md, ARCHITECTURE.md, LICENSE
├── .env.example                         # Template for .env (committed)
├── *_credentials.example.json           # Templates for the Drive / Box / Confluence credentials (committed)
├── approved_sources.example.json        # Template for the data-scoping config (committed)
│
│   Local only, gitignored, supplied by each user:
├── .env                                 # Cornell AI Gateway base URL + key
├── drive_credentials.json               # Google OAuth client credentials (from Google Cloud Console)
├── box_credentials.json                 # Box OAuth client credentials {"client_id": "...", "client_secret": "..."}
├── confluence_credentials.json          # Confluence personal access token + base_url
└── approved_sources.json                # Which sources may be sent to AI re-ranking

~/.findit/
├── token.json                           # Cached Google OAuth token (created on first run, auto-refreshed)
├── box_token.*                          # Cached Box OAuth token (shelve file; created on first run)
└── audit.log                            # Per-search JSONL audit log (no file contents)
```

## Flow
1. **Auth**: On first run, opens browser for both Google and Box OAuth. Tokens cached to `~/.findit/`. Subsequent runs use cached tokens (auto-refreshed when expired). Box uses a one-shot local server on port 8081 to capture the OAuth redirect.
2. **Parallel search**: Google Drive, Box, and Confluence are searched concurrently via `ThreadPoolExecutor` (3 workers). Drive uses `fullText contains`, Box uses content search, and Confluence uses the REST search API `GET /rest/api/search` with CQL `(text ~ "<word>" or ...) and type = page` (pages only), authenticated with a Bearer personal access token, capturing each hit's excerpt. Multi-word queries match any of the words (OR) on Drive and Confluence, so the candidate pool is intentionally wide and the reranker narrows it (Box content search is already loose). Each source returns up to `MAX_RESULTS` (default 50) files. A `source` field ("Google Drive", "Box", or "Confluence") is attached to each result. If any single source errors, it returns an empty list and the run continues with the others.
3. **Data-scoping gate + Claude re-rank**: Before anything leaves the boundary, a default-deny scoping gate (`scope_and_rerank`) checks each result's source/location against `approved_sources.json`; only approved results are passed to Claude Haiku, which returns a ranked JSON array of the top `TOP_N` (default 8) results with a relevance reason for each. Out-of-scope results are dropped and logged. With no gateway key, `--no-llm`, or no approved sources, FindIT skips this step and returns raw results with no external call. See "Data-scoping gate" below.
4. **Output**: Ranked results show name, source, relevance reason, and a metadata line: file type, size, modified date, owner, and location. The CLI also prints the link; the web UI links the title instead (no raw URL line). Owner is the Drive file owner / Box `owned_by` / Confluence page creator; location is the Box parent folder or Confluence space. File type and size apply to Drive and Box files only (Confluence pages omit them).

## Data-scoping gate
FindIT's only outbound transmission is the re-rank call to the Cornell AI Gateway. A default-deny gate sits at that boundary so content only leaves from locations a human has explicitly approved.

- **Config**: `approved_sources.json` (per-user, gitignored; `approved_sources.example.json` is the committed template). Each source is either `"approved": "all"` (whole source cleared) or a list of approved location ids (Drive folder ids, Box folder ids, Confluence space keys). A source that is absent, set to an empty list, or a missing/invalid file is **denied**. A brand-new source or connector is therefore excluded until someone reviews and lists it.
- **Enforcement**: `scope_and_rerank()` is the single function that may call the model. It runs `enforce_scope()` first; only approved results reach `_rerank_with_claude()`. Out-of-scope results are dropped from the output and recorded as flagged exceptions, so there is no path to the external API that skips the gate.
- **Matching granularity**: a result's *immediate* location (the file's parent folder, or the page's space). A file in a subfolder of an approved Drive folder is not auto-covered unless that subfolder is listed or the whole source is approved. Recursive ancestor matching and query-time scoping (CQL `space in`, Box `ancestor_folder_ids`) are possible future optimizations.
- **Audit log**: every search appends one JSONL line to `~/.findit/audit.log` (mode 0600): timestamp, query, mode, per-source counts, and per-document references as a hash of the link plus scope ids. File names, excerpts, and other content are never logged, so the log does not become a second copy of sensitive data.
- **Raw fallback**: `--no-llm`, a missing gateway key, or no approved sources all return raw search results with no external call; the gate is moot because nothing is transmitted.

## Configuration constants (top of findit.py)
| Constant | Default | Purpose |
|---|---|---|
| `CREDENTIALS_FILE` | `<script dir>/drive_credentials.json` | Path to Google OAuth credentials (relative to script) |
| `BOX_CREDENTIALS_FILE` | `<script dir>/box_credentials.json` | Path to Box OAuth credentials (relative to script) |
| `CONFLUENCE_CREDENTIALS_FILE` | `<script dir>/confluence_credentials.json` | Path to Confluence config: token + base_url (relative to script) |
| `TOKEN_DIR` | `~/.findit` | Where to cache OAuth tokens |
| `TOKEN_FILE` | `~/.findit/token.json` | Cached Google OAuth token |
| `BOX_TOKEN_FILE` | `~/.findit/box_token` (shelve adds an extension) | Cached Box OAuth token |
| `MAX_RESULTS` | 50 | How many results to fetch per source |
| `TOP_N` | 8 | How many results to show after re-ranking |
| `CLAUDE_MODEL` | `claude-haiku-4-5` | Claude model for re-ranking (bare ID; the Cornell AI Gateway 403s on date-suffixed IDs and aliases) |

The Claude re-ranking step runs through the Cornell AI Gateway, not the public Anthropic API. The gateway speaks the native Anthropic `/v1/messages` API, so the `anthropic` SDK targets it by setting `base_url` to the gateway endpoint and passing the gateway key as `auth_token` (which sends `Authorization: Bearer`, the gateway's expected scheme). These are not constants: `main()` (and `findit_web.py`) call `load_dotenv(<script dir>/.env)` at startup to read `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` from the project `.env`, If either is unset, `gateway_configured()` returns false and FindIT skips re-ranking and returns raw results with no external call. `load_dotenv` does not override an already-set shell variable, so exported values take precedence over `.env`. The client deliberately does not pass `api_key`, so no `x-api-key` header is sent (passing both would make the gateway reject the request).

## Dependencies
```
google-auth
google-auth-oauthlib
google-api-python-client
box_sdk_gen
anthropic
requests
python-dotenv
```
These are listed in `requirements.txt`. Install once with `python3 -m pip install -r requirements.txt`, using the same `python3` that runs the tool. A virtualenv is optional (for isolation), not required.

## Google Cloud setup
- Project: FindIT
- App name: FindIT CLI
- Auth type: OAuth 2.0 (per-user, not service account)
- Audience: Internal (Cornell Google Workspace only)
- Scope: `drive.readonly`
- Credentials file: downloaded from Google Cloud Console

## Box app setup
- App type: Custom App with Standard OAuth 2.0
- Scopes: Read all files and folders stored in Box
- Redirect URI: `http://localhost:8081` (used for local OAuth capture)
- Credentials file: `box_credentials.json` with `client_id` and `client_secret`

## Confluence setup
- Instance: Cornell self-hosted Confluence Server/Data Center (`https://confluence.cornell.edu`)
- Auth: Personal Access Token (PAT), sent as `Authorization: Bearer <token>`. No browser OAuth and no token caching. The PAT is read straight from the config file each run.
- Endpoint: `GET /rest/api/search` with CQL (returns an `excerpt` per hit, used by the reranker)
- CQL: `(text ~ "<word>" or ...) and type = page` (pages only, matches any query word; excludes blog posts, comments, attachments)
- Access is read-only (content search), but the PAT itself is unscoped: a Confluence Server/DC personal access token carries the user's full permissions (read **and** write), so it is a high-value secret even though FindIT only reads. Use the shortest expiry the platform allows, rotate it periodically, and revoke it immediately if the machine is ever compromised. To re-issue or revoke: Confluence profile → Personal Access Tokens.
- Config file: `confluence_credentials.json` with `token` and `base_url`. This is per-user and never committed: each person creates their own token and copies `confluence_credentials.example.json` to it, so everyone searches Confluence as themselves rather than as the owner.

## Distribution (GitHub, bring-your-own-credentials)
FindIT is distributed as source on GitHub. There is no packaging step and no secrets in the repository: each user clones or downloads the code and supplies all of their own credentials. Nothing is shared between users, and no real credential file is ever committed.

What is in the repo: the code (`findit.py`, `findit_web.py`), `requirements.txt`, `README.md`, this file, `LICENSE` (MIT), and credential *templates only* (`.env.example`, `drive_credentials.example.json`, `box_credentials.example.json`, `confluence_credentials.example.json`). `.gitignore` excludes every real secret (`.env`, the three credential JSONs, cached tokens, `~/.findit`) and local tooling (`.claude/`, which can hold a gateway key).

What each user supplies locally (never shipped): their own Google OAuth client (`drive_credentials.json`), their own Box app credentials (`box_credentials.json`), their own Confluence PAT (`confluence_credentials.json`), and their own Cornell AI Gateway key (`.env`). Each source is optional; a user configures only the sources they want and searches only those (the gateway key is optional too, see the re-rank step). Setup is in the README.

Because each user registers their own Google and Box apps and signs in as themselves, FindIT runs entirely as that user against only the data they can already access. There are no shared OAuth client secrets to leak, and per-user isolation is automatic (each person runs their own copy). This is the key difference from a hosted service, which would have to hold every user's tokens server-side and authenticate them individually.

Before making the repository public: confirm `git status` shows no real credential files staged, enable GitHub secret scanning and push protection, and verify no secret ever entered the history.

## Browser UI (findit_web.py)
Optional thin web layer over the same core. No external dependencies (Python's built-in `http.server`). It imports `findit.py`, authenticates once at startup (reusing cached tokens), and serves two routes: `GET /` returns a single self-contained HTML page (inline CSS/JS), and `GET /search?q=...` runs the same parallel search plus Claude re-rank and returns JSON. The page renders results as clickable cards, building DOM nodes with `textContent` to avoid injection. Binds to `127.0.0.1` only (localhost), default port 8000 (`FINDIT_PORT` to override). A single lock serializes searches so the reused clients are used one request at a time.

## Notes
- Confluence is implemented as a third parallel source (pages-only CQL search).
- Multi-source merging is implemented: Claude re-ranks across all sources in one pass.
