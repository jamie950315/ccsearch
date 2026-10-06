# ccsearch

This file is the project briefing for assistants. It covers architecture and working rules. The repository is public, so this file must stay generic: no host names, domains, IP addresses, filesystem paths, service inventories, or operational history for a specific deployment.

Deployment-specific notes live in `DEPLOYMENT.local.md`, which is ignored by Git. If it exists, read it before any deployment or operations work, keep it updated after deployments, and never commit it or copy its contents into tracked files. Re-check live state before relying on it.

## Project Shape

`ccsearch` is a Python search and fetch utility with three user-facing entry points that share one execution core:

- `ccsearch.py`: CLI, and the source of truth for validation, engine dispatch, caching, fetch extraction, result shaping, batching, diagnostics, and output
- `api_server.py`: thin Flask HTTP wrapper. Keep validation and execution in the shared core.
- `mcp_server.py`: thin FastMCP wrapper. Its `search` tool covers search engines only; URL retrieval uses the separate `fetch` tool.

Supported engines:

- `brave`: Brave Web Search
- `perplexity`: Perplexity via OpenRouter
- `both`: Brave and Perplexity combined
- `llm-context`: Brave LLM Context API
- `fetch`: URL fetch and extraction with a fallback chain
- `perplexity-verify`: claim-by-claim verification via Perplexity (MCP exposes it as the `verify` tool)

Shared capabilities:

- query and option validation
- exact cache and semantic cache
- host filtering and result limiting for search-style engines
- batch execution with deduplication
- runtime diagnostics and engine capability reporting

Other files:

- `test_ccsearch.py` is the unit and regression suite. It does not replace live HTTP, MCP transport, systemd, Docker, or tunnel smoke tests.
- `skills/SKILL.md` is the generic self-hosted HTTP skill template. `skills/claude_dot_ai_Specific_SKILL.md` is the Claude.ai-oriented variant. Keep both synchronized with public API behavior while preserving their intentional setup differences.

## Architecture

### Shared Execution

Use and update shared helpers in `ccsearch.py` instead of re-implementing behavior in CLI, HTTP API, or MCP.

Cache freshness defaults to 90 days and cannot exceed 90 days. A shorter caller-provided `cache_ttl` still expires a result earlier. Result files become unreadable after 90 days and are physically deleted beginning on day 91; `--prune-cache` and `ccsearch-cache-prune.timer` enforce cleanup, including semantic-index orphan removal.

Automatic cleanup shares an hourly completion timestamp across local processes in the existing cache operations lock file; explicit pruning bypasses that schedule. HTML response extraction reads metadata before pruning and reuses one parsed DOM. Default text shaping does not scrub unused chunks. Rebuild section metadata after removing injected headings, and keep hashes and offsets consistent with the returned text.

### Search Engines

- `brave`, the Brave side of `both`, and `llm-context` prefer `BRAVE_SEARCH_API_KEY`, round-robin however many extra Search keys are configured, and fall back to `BRAVE_API_KEY` only when no Search key is set
- `perplexity` uses OpenRouter
- `both` runs Brave and Perplexity concurrently and preserves partial failures

All Brave attempts, including retries, share a cross-process limiter. Each Brave key has its own window, capped at 50 RPS per key, across the local CLI, HTTP API, and MCP services. Extra keys are selected round-robin for each live request, using however many keys are configured rather than a fixed count of three. The limiter cannot account for other devices using the same Brave subscription.

Search-style engines normalize output for downstream agents: cleaned text, `hostname`, `rank`, `published_at` (`YYYY-MM-DD` or null), host summaries, optional `host_filtering`, `result_limiting` (default limit 8 for brave/both/llm-context), `snippet_limiting` (llm-context default 5), optional `freshness_filtering`, `injection_suspected` where AI-directed text was removed, `cache_status`, `cached_at`, and `duration_ms`. `freshness`, `country`, and `search_lang` pass through to Brave and are part of the cache key.

Response shaping (`shape_search_result`, `shape_fetch_result`) runs after cache lookup, so cached payloads stay complete and shaping changes apply to old entries too.

### Fetch Engine

`fetch` uses a layered flow (`fetch_with_fallbacks` → `perform_fetch` → `_perform_direct_fetch`):

1. site API for recognized URLs: X/Twitter (fxtwitter), Discourse (`/t/{id}.json`, `/raw/{id}`), Reddit (`.json`), V2EX (API v1); blocked JSON APIs retry through FlareSolverr
2. `_simple_fetch`
3. Cloudflare detection (the `challenge-platform` beacon alone is not a challenge on successful pages with text)
4. SPA shell detection
5. optional FlareSolverr fallback
6. `[Fetch] extended_fallbacks`: Brave LLM Context passages whose URL matches the page, then the newest Wayback snapshot. These run only for blocked/unreachable pages, never for 404/410.

Every fetch result carries `attempts`, `served_from`, `ok`, `fetched_at`, and `content_date`. By default the shaped output returns `content` only; `format="chunks"` returns chunks only, `focus`/`focus_k` select BM25-ranked passages, `max_chars` truncates, and `verbose` restores hashes, offsets, section paths, outbound links, and transport headers.

When available, `_simple_fetch` uses `curl_cffi` with Chrome impersonation. Otherwise it falls back to `requests`.

`fetch` also supports:

- non-HTML text decoding
- PDF conversion through the standard MarkItDown dependency and optional Office-format extras
- JSON-LD and social metadata extraction
- structured `chunks`
- code/list/table preservation
- outbound link extraction
- X/Twitter routing through the fxtwitter API
- structured failures for non-success HTTP responses, unavailable document converters, and browser-rendered pages with no extractable content
- preservation of FlareSolverr final URL, HTTP status, and content type; ordinary 404 responses and known binary URLs are not hidden by HTML fallback

Known Akamai interstitials trigger browser fallback; unresolved challenges and
denial pages fail even with HTTP 200. LLM Context matching preserves topic IDs
and page numbers, ignoring known tracking parameters. Legacy wrong-page and
Akamai denial cache entries are bypassed. Browser rendering does not guarantee access.

Navigation pruning preserves sidebar-named layout wrappers containing substantial
article/main paragraph text. Linked teaser cards remain removable navigation;
an article's linked tags must not cause its enclosing layout to lose the body.

### Batch Execution

Batch execution lives in the shared core, not the API layer.

- bounded parallelism via `max_workers`
- isolation of runtime failures and normally validated per-request errors
- duplicate request suppression within the batch
- stable output ordering
- per-batch summary fields such as `success_count`, `error_count`, `duration_ms`, and `deduped_count`

Dispatch: `url` → fetch, `query` → search engine (entry engine, else default engine, else brave), explicit `op` wins; `url`+`query` without `op` is a per-item error. Batch defaults fill only options that apply to each item's engine (`OPTION_ENGINES`).

The batch dedupe fingerprint includes engine, normalized query, and every execution option. After execution, repeated search-result URLs across items become `{"ref", "see_index", "rank"}` references (`dedupe_results`), counted in `deduped_result_count`; `deduped_count` is requests plus results.

### HTTP API Server

`api_server.py` exposes:

- `GET /health`
- `POST /search`
- `POST /batch`
- `GET /engines`
- `GET /diagnostics`

All endpoints except `/health` require `X-API-Key`. The key is loaded from `CCSEARCH_API_KEY` or `.api_key`. Do not duplicate validation logic here; call the shared helpers from `ccsearch.py`.

### MCP Server

`mcp_server.py` exposes FastMCP tools over both SSE and Streamable HTTP:

- `search`
- `fetch`
- `verify`
- `batch`
- `engines`
- `diagnostics`

Tool descriptions and server instructions document engine selection, batch dispatch rules with an example, and defaults. Keep them synchronized with README and both skill files.

Keep the MCP server thin and forward into shared execution logic.

## Development

- Install dependencies with `pip install -r requirements.txt`
- Copy `config.ini.example` to `config.ini`
- Use `./ccsearch.py --help` for CLI flags
- The standard requirements currently install `fastembed` for semantic cache and `curl_cffi` for direct-fetch TLS impersonation. The code degrades gracefully if either is unavailable.
- The standard requirements include `markitdown[pdf]`; additional MarkItDown extras are optional for Office formats.

## Deployment Model

A deployment typically runs `api_server.py` and `mcp_server.py` as separate long-running services (for example systemd units that load `.env` with `EnvironmentFile=` and restart automatically), the checked-in `ccsearch-cache-prune.timer` under `systemd/`, a loopback-only FlareSolverr container from `docker-compose.yml`, and a reverse proxy or tunnel for public HTTPS routes.

- Bind FlareSolverr to `127.0.0.1` only; it has no authentication. Do not publish port `8191` on all interfaces if compose is recreated.
- Keep the API and MCP ports unreachable from the Internet except through the proxy or tunnel.
- The HTTP service runs Flask's built-in server directly. It is not yet a production WSGI/ASGI deployment; replacement remains in `TODO.md`.
- MCP 1.26 emits a Pydantic `IncompleteFieldDefinitionWarning` at startup; both transports work despite it.
- A live `config.ini` may differ from `config.ini.example`; record deployment-specific values in `DEPLOYMENT.local.md`, not here. `config.ini.example` remains the conservative setup template.

## Secrets and Configuration

- Never print, commit, or paste values from `.env`, `.api_key`, `config.ini`, Cloudflare credentials, or authenticated MCP URLs.
- `.env`, `.api_key`, and `config.ini` must remain mode `0600`; all three are ignored by Git and may contain deployment-specific values.
- The Python programs do not load dotenv files themselves. systemd loads `.env`; for manual runs, export the variables in the shell first.
- Every Brave-backed engine prefers `BRAVE_SEARCH_API_KEY`, plus any number of extra keys from `BRAVE_SEARCH_API_KEY_2`, `BRAVE_SEARCH_API_KEY_3`, `BRAVE_SEARCH_API_KEY_4`, later numbered variables, a comma-separated `BRAVE_SEARCH_API_KEY`, or `BRAVE_SEARCH_API_KEYS`. `BRAVE_API_KEY` is used only as a compatibility fallback when no Search key is set. `both` additionally requires `OPENROUTER_API_KEY`.
- `CCSEARCH_API_KEY` from the environment takes precedence over `.api_key`. Both servers read the key at process startup, so changing it requires service restarts.
- Optional port overrides: `CCSEARCH_PORT` and `CCSEARCH_MCP_PORT`.
- MCP authentication embeds the shared key in the URL path. Uvicorn, systemd journal, proxies, and client logs can record that path. Do not show raw MCP access logs; redact the first path segment. AI assistants must never rotate the shared key autonomously. If the key is exposed in output during coding, stop reproducing it, redact it from subsequent output, notify the user, and ask whether they want it rotated. Rotate it only after explicit user approval.

## Canonical Development and Deployment Workflow

- Treat the local working copy that pushes to GitHub as the canonical authoring
  workspace. Deployment hosts are targets, not normal code-editing locations.
- Before making changes, inspect the working tree and preserve any existing
  unexplained work. Never overwrite or mix unrelated modifications.
- Implement and test changes on the authoring copy when its required
  dependencies are available; use a deployment host's environment for extra
  platform verification when needed.
- Commit approved changes and push them to `origin/main` before deploying them.
- Deployment hosts fetch over the public HTTPS remote. Never copy the authoring
  machine's GitHub private key to a deployment host.
- Deploy production changes by fast-forwarding the production checkout to the
  exact verified commit. Preserve its untracked `.env`, `.api_key`, and
  `config.ini`.
- Restart only the services affected by the change. Documentation-only changes
  must not restart services.
- Verify the affected CLI, HTTP API, MCP tools, Docker services, service units,
  and public routes in proportion to the change.
- After production passes verification, fast-forward any standby hosts to the
  same commit for rollback readiness without starting their services.
- Finish by confirming that every checkout references the same commit and that
  no unexpected tracked changes remain.
- If an emergency edit is ever made directly on a deployment host, copy it back
  into the authoring copy, test it, commit it, and resynchronize every
  deployment target before considering the work complete.

## Operations

Read-only status checks:

```bash
systemctl status ccsearch-api.service ccsearch-mcp.service
docker ps --filter name=flaresolverr
ss -ltnp | rg ':(8888|8890|8191)\b'
curl -fsS http://127.0.0.1:8888/health
```

After code, environment, or untracked configuration changes, restart only the affected service and then inspect its status and redacted logs:

```bash
sudo systemctl restart ccsearch-api.service ccsearch-mcp.service
systemctl --no-pager --full status ccsearch-api.service ccsearch-mcp.service
systemctl --no-pager status ccsearch-cache-prune.timer
```

Do not restart live services for documentation-only changes. Do not run or share an unfiltered `journalctl` dump for MCP because request paths contain the API key.

## Required Verification

Before declaring a change complete:

```bash
python3 -m py_compile ccsearch.py api_server.py mcp_server.py test_ccsearch.py
python3 -m unittest discover -v
python3 ccsearch.py --doctor --format json
python3 ccsearch.py --list-engines --format json
```

Also run checks proportional to the changed surface:

- Core or CLI: execute the affected CLI path with representative valid and invalid input.
  - `python3 ccsearch.py "OpenAI Responses API" -e brave --format json`
  - `python3 ccsearch.py "https://example.com" -e fetch --format json`
- HTTP API: use Flask tests plus live `/health`, authenticated `/diagnostics`, and the affected endpoint when safe.
- MCP: exercise the affected tool and at least one real SSE or Streamable HTTP initialization when transport/auth code changes.
- Fetch: test direct HTML extraction and, when relevant, the running FlareSolverr fallback.
- Fetch results containing `error` are failures even when transport metadata is present. Preserve ordinary HTTP errors, verify binary conversion with a real document, and reject empty browser-rendered content.
- Fetch fallback: check `served_from` and `attempts` on a Cloudflare-protected page, a Discourse topic (for example linux.do), and a 404 page (must not fall back).
- Deployment: check service state, listeners, redacted recent logs, Docker state, and public routes.

## Documentation Sync Rules

- Public CLI, response, engine, or option changes: update `README.md`, both files under `skills/`, and tests together.
- Deployment changes: update `DEPLOYMENT.local.md`. Never place credentials, tunnel IDs, host names, domains, IP addresses, or deployment paths in tracked files.
- Cache freshness defaults to and is capped at 90 days. Files are unreadable after 90 days and deleted beginning on day 91 by the hourly timer or the next cleanup pass; keep exact and semantic cache behavior synchronized.
- `offset` is supported by `brave` and `both`.
- HTTP non-health endpoints use `X-API-Key`; MCP uses the key as a path prefix.
- The HTTP API maps validation failures to 400, authentication failures to 401, completed fetch error payloads to 424 while preserving their metadata through Cloudflare, and unexpected server failures to 500.
- Keep `README.md` in English unless the user explicitly requests another language.

## Review hardening (2026-09-08)

- Strict option types and parsed HTTP(S) URLs are validated before cache/network work; bad batch items remain isolated. CLI failures, including partial/batch failures, exit nonzero.
- Never cache failed/partial results; legacy failed entries are bypassed. Fetch keys preserve path semantics and repeated-value order. Semantic index mutations use a cross-process lock; model initialization is synchronized and computation failures surface.
- Brave keys wait outside the shared limiter lock. MCP blocking tools run in the bounded Starlette thread pool. Uvicorn access logging is disabled; upstream proxy/client logs can still expose authenticated URLs.
- Empty authentication files fail closed; concurrent first starts atomically publish one complete 0600 key. Existing unreadable configuration fails explicitly.
- Missing provider answers/error envelopes fail rather than becoming successful empty responses. `both` keeps partial output but sets top-level `error` if both engines fail.
- Fetch fallback is limited to expected transport/browser errors, not programming exceptions. Unresolved challenges and empty document conversions fail. MarkItDown uses its single file conversion API; MIME types take precedence over dynamic URL suffixes.
- Review regression suites are `test_review_*.py`, and the agent-improvement suite is `test_agent_improvements.py`; use `python3 -m unittest discover -v` to include them along with the original suite. Do not globally clear inherited provider credentials. Key-selection and missing-key tests use explicit fixtures; the loopback MCP server keeps its disposable authentication key.
