# Security

FindIT reads institutional documents, so it was designed around one question: what is allowed to leave the user's machine, and who decided? This page summarizes the answer. The full analysis, including how each control was verified and what remains open, is in the [Security and Implementation Plan](docs/IMPLEMENTATION_PLAN.md).

## What leaves the machine

Besides the searches themselves (sent to Google Drive, Box, and Confluence under the user's own accounts), FindIT makes one outbound call: the optional AI re-ranking request. It contains only the search query, each approved result's file name and modified date, and, for Confluence pages, an excerpt of up to 200 characters. Never full documents, owners, folders, or links.

One optional, data-free exception: if you set `FINDIT_ADOBE_FONTS_ID` to use Cornell's brand typeface, the web page loads font files from Adobe Fonts. That request carries no search terms or document data (the page sends no referrer). Without that setting, the page makes no outside requests.

## Key controls

- **Default-deny data scoping.** Nothing is sent to the model unless its source or location is listed in `approved_sources.json`. A missing or invalid file blocks everything; a new source is excluded until someone approves it.
- **One path to the model.** A single function may call Claude, and it applies the scoping gate first.
- **Works without AI.** With `--no-llm`, no key, or no approved sources, FindIT returns raw results and makes no outbound AI call.
- **Audit log without a second copy of the data.** Each search is logged locally with hashed document references, never names or contents.
- **No secrets in the repository.** Templates only. Each user supplies their own Google, Box, Confluence, and Claude credentials and runs their own copy, so everyone searches as themselves. Git history was scanned before publishing; GitHub secret scanning and push protection are on.
- **Read-only.** FindIT only searches. Google Drive uses the read-only scope, and FindIT has no code path that writes to any source.
- **Hardened inputs and outputs.** Search terms are escaped; Box sign-in uses an OAuth `state` check; model output is bounds-checked and rendered as plain text; the web page renders only `http`/`https` links, sends no referrer (so searches in its address bar never reach the sites you open), and listens on `127.0.0.1` only; credential and log files are owner-only.

## Known limits

Stated plainly so they can be weighed: flagged exceptions are recorded in the local log but not yet routed to anyone; a Confluence personal access token carries the user's full permissions (use a short expiry and rotate it); scoping matches a result's immediate folder or space; and the personal Anthropic API key option sends data outside Cornell's AI Gateway, so Cornell data should use the gateway. FindIT is used only by its developer, and a Cornell Technology Risk Assessment is required before any team use.

## Reporting a vulnerability

Please report security issues privately through this repository's **Security** tab ("Report a vulnerability") rather than in a public issue.
