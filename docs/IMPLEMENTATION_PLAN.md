---
title: "FindIT: Security and Implementation Plan"
author: "David Cutri"
date: "November 2026"
---

*FindIT began as the subject of a course project on strategic AI implementation (Part Four: Design a Strategic AI Implementation Plan). The original plan, written in June 2026, proposed a set of security and governance controls before most of them existed. This version reports what has since been built and verified, what remains open, and what must happen before FindIT is used by anyone other than its developer.*

# Summary

FindIT lets a person search Google Drive, Box, and Confluence at once in plain English. It runs the searches in parallel under the user's own accounts, then asks Claude to re-rank the merged results and explain in one line why each top result matches.

The original plan identified one central risk: institutional document metadata leaving Cornell through the re-ranking call, with no one having decided that it should. It also recorded two critical findings from a security audit that assumed public release: OAuth secrets bundled in distributed packages, and a single-identity design in which everyone would search as the developer.

**Status as of November 2026:**

| Area | Original plan (June 2026) | Status now |
|---|---|---|
| Data-scoping gate before the AI step | Proposed | **Built and live-tested.** Default-deny; nothing is sent unless its source or location is explicitly approved. |
| Single, unbypassable outbound boundary | Proposed | **Built.** One function may call the model, and it applies the gate first. |
| Audit log without a second copy of data | Proposed | **Built.** Local log of each search with hashed document references; no file names or contents. |
| `--no-llm` fallback | Proposed | **Built.** Also the automatic behavior when no key or no approved sources exist. |
| Critical: secrets in distributed packages | Open | **Resolved by design.** No secrets in the repository; every user supplies their own credentials. Verified by scanning the full git history. |
| Critical: no per-user isolation | Open | **Resolved by design.** Each user runs their own copy and signs in as themselves. |
| Exception routing to a security liaison | Proposed | **Not built.** Exceptions are flagged in the local log only. Required before team use. |
| Technology Risk Assessment (TRA) | Required before team use | **Not started.** FindIT is currently used only by its developer. |

FindIT is published as open source at <https://github.com/dc774/FindIT> under the MIT license.

# What FindIT does, and exactly what leaves the machine

1. **Search.** The query is sent to each configured source (Google Drive, Box, Confluence) through that source's own API, using the user's own credentials. Each source is optional. FindIT only ever sees what the user could already open.
2. **Gate.** Every result is checked against `approved_sources.json`. A source that is not listed, or a result whose folder or Confluence space is not on its source's list, is excluded.
3. **Re-rank.** Approved results are sent to Claude (Claude Haiku by default) through the Cornell AI Gateway or, if the user chooses, their own Anthropic API key.
4. **Display.** Ranked results are shown in the terminal or a local web page.

The re-ranking request is FindIT's **only** outbound transmission besides the searches themselves. It contains:

- the search query;
- each approved result's file name and last-modified date;
- for Confluence pages only, a search excerpt of up to 200 characters.

It does **not** contain full documents, file contents beyond those excerpts, owners, folder names, or links. FindIT has no write access to any source and takes no action other than returning a list to the user.

# Values and classification

**Responsible/ethical classification: Yellow.** The central value constraint is data stewardship under Cornell Policy 5.10, together with the accountability expected by Cornell's TRA process. The core tension is between usefulness and data minimization: re-ranking works better with more context, while every piece of context sent outside the machine is a stewardship decision. The likely failure is not one dramatic breach but a quiet, compounding one: routine metadata and excerpts leaving the institution with no one having consciously accepted that risk.

The controls below are designed so that this decision is always made deliberately, by a person, before any data is sent, and so that the tool still works fully, without AI, when that decision has not been made.

# Security controls

Each control below names where it lives in the code, so it can be checked directly.

| Risk | Control | Where |
|---|---|---|
| Institutional data sent to the model without a decision | Default-deny data-scoping gate. Missing, empty, or invalid configuration blocks everything. A new source is excluded until someone lists it. | `enforce_scope()`, `load_approved_sources()` in `findit.py` |
| Gate bypassed by another code path | `scope_and_rerank()` is the only caller of the private `_rerank_with_claude()`, and it runs the gate first. | `findit.py` |
| No record of what was sent | Every search appends a line to `~/.findit/audit.log`: time, query, mode, counts, and per-result source, location id, and a hash of the link. Out-of-scope results are recorded as flagged. No names, excerpts, or contents. | `log_search()` |
| AI step unwanted or not cleared | `--no-llm`, no key, or no approved sources all return raw results with no outbound call. | `main()`, `llm_mode()` |
| Secrets committed or distributed | Repository contains templates only. `.gitignore` excludes every real credential file, the `.env` file, token caches, and local tool settings. Full git history scanned for the real secret values before publishing; none present. GitHub secret scanning and push protection enabled. | `.gitignore`, `*.example.json`, `.env.example` |
| Everyone searching as one person | Self-install, bring-your-own-credentials model. Each user registers their own Google and Box apps, uses their own Confluence token, and runs their own copy. | README setup |
| Credentials readable by other local users | Credential files, tokens, `.env`, and the audit log are set to owner-only (0600); the token directory to 0700. | `_restrict()`, `_secure_token_dir()` |
| Personal API key sent to the wrong server | A personal Anthropic key is used only when no custom base URL is set, so it can never be sent to the gateway. Exactly one credential is passed per request. | `llm_mode()`, `_rerank_with_claude()` |
| Query injection into source searches | Drive and Confluence query terms are escaped, not stripped or interpolated raw. | `_drive_escape()`, `_cql_escape()` |
| Forged Box sign-in redirect | Box OAuth sends a random `state` value and rejects a mismatched callback. | `get_box_client()` |
| Prompt injection through file names or excerpts | The model may only return ranking JSON. Each returned index is bounds-checked; malformed output stops the ranking step with an error rather than being acted on. The model's "reason" text is rendered as plain text (`textContent` in the web page, never HTML). Worst case: results are reordered or omitted. | `_rerank_with_claude()`, `findit_web.py` |
| Malicious links in results | The web page only renders `http`/`https` links; `javascript:` and `data:` links are neutralized. | `safeHref()` in `findit_web.py` |
| Web interface exposed on the network | The local server binds to `127.0.0.1` only. | `findit_web.py` |
| Over-broad source access | Google Drive uses the read-only scope. Box setup instructs read-only access. FindIT only issues search requests. | `DRIVE_SCOPES`, README |

# How this was verified

- **Live end-to-end test (June 24, 2026)** against real Google Drive, Box, and Confluence accounts. With no scoping configuration, every search returned raw results with no outbound call and was logged as default-deny. With only one Confluence space approved, exactly 108 results were excluded (50 Drive, 50 Box, 8 Confluence pages from other spaces), and only results from the approved space were ranked. The audit log was confirmed to contain no file names or contents and to be owner-only.
- **Targeted tests** for each later fix: adversarial input for the query escaping, introspection of the Box `state` parameter, and offline tests (fake keys, no network) confirming that each Claude configuration selects the right endpoint and sends exactly one credential.
- **Repository checks before publishing:** staged files reviewed by hand, every real credential value searched for in the full history (none found), and GitHub secret scanning and push protection turned on.

A formal follow-up security audit has not yet been run. It is listed below as a requirement before team use.

# Remaining risks and limits

These are known and deliberate, and each has a stated condition for revisiting it.

1. **Exceptions are logged, not routed.** Out-of-scope results are always excluded, so the gate prevents the exposure rather than merely detecting it. But the flagged record exists only in the user's local log; no one is notified. For a single user this is acceptable. Before team use, flagged exceptions must reach the unit security liaison.
2. **The Confluence token is broad.** A Confluence personal access token carries the user's full permissions, including write, even though FindIT only reads. The documentation tells users to use the shortest expiry available, rotate it, and revoke it if a machine is compromised.
3. **Scoping matches a result's immediate location.** A file in a subfolder of an approved Drive folder is not covered unless that subfolder is listed or the whole source is approved. This errs toward excluding too much, not too little. Approving a whole source avoids it; making folder approval cover subfolders is a planned improvement.
4. **The audit log contains the search query.** Queries are stored in plain text in the owner-only local log, since a query is needed to make the log useful for review.
5. **The personal API key option sends data outside Cornell's AI Gateway.** It exists so people outside Cornell can run the tool. The documentation directs anyone searching Cornell data to use the gateway.
6. **No rate limiting.** Search volume is bounded only by the user's own use.
7. **Prompt injection is constrained, not eliminated.** A crafted file name or excerpt could influence ranking. It cannot cause any action beyond the ranked list.
8. **No TRA yet.** FindIT is used only by its developer. It will not be offered to others before a TRA is completed.

# Governance

**Human oversight sits at the configuration layer, not the query layer.** A person decides, in `approved_sources.json`, which sources may ever be sent to the AI step: a whole system with one line, or optionally specific folders or Confluence spaces. Approval is never per document. Individual searches then need no approval, which keeps the tool fast without making the data decision implicit.

**Today (single user).** The developer is both the person who configures scope and the person accountable for it, and reviews the local audit log.

**Before any team use**, the roles from Cornell's TRA model apply:

- the **developer** maintains the scoping, logging, and security controls and remediates any finding;
- the **unit security liaison** reviews flagged exceptions as they occur, not on a fixed schedule;
- the **unit head** accepts the residual risk through the TRA.

**Triggers, all event-based rather than calendar-based:**

- Any out-of-scope result reaching the AI step pauses that source pending review. (The gate currently prevents this by construction; the trigger remains as policy in case of a defect.)
- Any Critical or High audit finding blocks expansion until fixed and re-verified.
- Any new connector (for example Microsoft Teams or Jira) is excluded by default and stays excluded until its own scoping review is complete. Adding a connector is a governance event, not a code change alone.

# Rule of Two analysis

Meta's Agents Rule of Two holds that a system combining all three of the following capabilities needs human supervision rather than unchecked autonomy.

- **A. Processing untrusted input: present.** File names and excerpts are not pre-vetted and could attempt to steer the model. *Constraint:* the model's output is limited to bounds-checked ranking JSON and rendered as plain text.
- **B. Accessing sensitive data: present.** FindIT reads institutional content through each user's own OAuth grants and token. *Constraint:* access is per-user and read-only in practice, and the data-scoping gate decides what may leave the machine.
- **C. Communicating externally: present, narrowly.** The one outbound action is the re-ranking request. *Constraint:* it only runs on gate-approved results, and it can be switched off entirely.

Because FindIT has narrow forms of all three, it relies on a human-supervised checkpoint: the approved-sources configuration. The original plan proposed this checkpoint. It now exists in code, and the design makes it impossible to reach the model without passing it.

# Agentic boundary

FindIT is **not agentic** and is designed to stay that way. It performs exactly one AI-mediated step per query (re-ranking a fixed set of already-retrieved results) and returns a list to a person. It does not choose further searches, chain actions, or act on its own output, and it has no write access to any system.

It would become agentic if it acted on results, for example by summarizing and forwarding a document or updating a tracker. At that point all three Rule of Two capabilities would sit in one connected workflow, every state-changing action would need human approval, and the existing TRA scope (a read-only retrieval tool) would no longer apply; a new, fuller assessment would be required.

# Next steps

In order, before anyone other than the developer uses FindIT:

1. Complete a Technology Risk Assessment, with the unit head accepting the residual risk.
2. Route flagged exceptions to the unit security liaison instead of only the local log.
3. Run a formal follow-up security audit of the current code.
4. Replace the broad Confluence token with a narrower credential if Atlassian's permission model allows it.

Future connectors (Teams, Jira, others) each start excluded and require their own scoping review.

# Reflection

**What changed since the original plan.** The original plan named two open questions that had to be answered before any team pilot. The first was a security engineering question: whether the single-identity design could support genuine per-user isolation, and whether that would be a modest change or a substantial rebuild. The answer turned out to be neither. Instead of building a hosted service that would hold every user's tokens, FindIT moved to a self-install model in which each person runs their own copy with their own credentials. Per-user isolation became automatic, and both critical audit findings disappeared by design rather than by patching. The second question, data governance under Policy 5.10, is now narrower and better defined: the gate and audit log exist, so the TRA can review a concrete, working control rather than a proposal.

**What the framework contributed.** The most useful distinction remains the one between "is this data accessible" and "is using it this way permissible." The prototype worked long before the second question had an answer. Building the gate as the single path to the model, rather than as a setting a careful user could remember to turn on, is what turns that distinction into something the system enforces.

**What would be stressed at scale.** The approved-sources list, though not in the way the name might suggest. Approval is never per document. It is a decision about a whole system, or optionally about specific folders or Confluence spaces within it, recorded as one line per source (for example, `"confluence": {"approved": "all"}`). Once a source is approved, every document in it, including documents created later, can be ranked with no further approval.

Scale stresses this list in two ways. First, new connectors: when Microsoft Teams, Jira, or another system is added, it is excluded until someone adds its line. If adding connectors becomes routine, that decision could become a rubber stamp, but forgetting it fails safe: less is sent, not more. Second, folder-level approval: because a folder approval covers only files directly in that folder, an organization that scopes by folder rather than by whole source would have to list every relevant subfolder, which becomes tedious for deep folder trees. Approving whole sources avoids this, and making folder approval cover subfolders is a planned improvement.
