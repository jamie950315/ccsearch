# ccsearch

This file is the project briefing for assistants. It covers architecture, working rules, and the last verified deployment state. Re-check live state before relying on the snapshot because services and untracked configuration can change independently of Git.

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
- `fetch`: direct URL fetch and extraction

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

### Search Engines

- `brave`, the Brave side of `both`, and `llm-context` prefer `BRAVE_SEARCH_API_KEY`, round-robin any extra Search keys, and fall back to `BRAVE_API_KEY` only when no Search key is set
- `perplexity` uses OpenRouter
- `both` runs Brave and Perplexity concurrently and preserves partial failures

All Brave attempts, including retries, share a cross-process limiter. Each Brave key has its own window, capped at 50 RPS per key, across the local CLI, HTTP API, and MCP services. Extra keys are selected round-robin for each live request. The limiter cannot account for other devices using the same Brave subscription.

Search-style engines normalize output for downstream agents: cleaned text, `hostname`, `rank`, host summaries, optional `host_filtering`, optional `result_limiting`, `cache_status`, and `duration_ms`.

### Fetch Engine

`fetch` uses a layered flow:

1. `_simple_fetch`
2. Cloudflare detection
3. SPA shell detection
4. optional FlareSolverr fallback

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

### Batch Execution

Batch execution lives in the shared core, not the API layer.

- bounded parallelism via `max_workers`
- isolation of runtime failures and normally validated per-request errors
- duplicate request suppression within the batch
- stable output ordering
- per-batch summary fields such as `success_count`, `error_count`, `duration_ms`, and `deduped_count`

The batch dedupe fingerprint includes engine, normalized query, offset, cache settings, flaresolverr, host filters, and result limit.

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
- `batch`
- `engines`
- `diagnostics`

Keep the MCP server thin and forward into shared execution logic.

## Development

- Install dependencies with `pip install -r requirements.txt`
- Copy `config.ini.example` to `config.ini`
- Use `./ccsearch.py --help` for CLI flags
- The standard requirements currently install `fastembed` for semantic cache and `curl_cffi` for direct-fetch TLS impersonation. The code degrades gracefully if either is unavailable.
- The standard requirements include `markitdown[pdf]`; additional MarkItDown extras are optional for Office formats.

## Verified Deployment

Snapshot verified on 2026-09-01, with the Pi5 FlareSolverr binding re-checked on 2026-09-03 and Brave Search key round-robin verified on 2026-09-03. The primary deployment is A1-JP, an Ubuntu 24.04 ARM64 Oracle A1 instance in Osaka. A1-US is the first application rollback host: its code, untracked configuration, stopped containers, data, and migration backup remain available, while its API, MCP, cache timer, and Cloudflare connector are disabled and inactive. The Raspberry Pi 5 checkout is a second cold standby: its API and MCP services remain disabled and inactive. Do not start either standby unless the user explicitly chooses to fail over.

| Component | Live state | Binding / public route |
| --- | --- | --- |
| HTTP API | `ccsearch-api.service`, enabled and running as `ubuntu` on A1-JP | `0.0.0.0:8888`, `https://ccsearch.0ruka.dev` |
| MCP | `ccsearch-mcp.service`, enabled and running as `ubuntu` on A1-JP | `0.0.0.0:8890`, `https://ccsearch-mcp.0ruka.dev` |
| Cache cleanup | `ccsearch-cache-prune.timer`, enabled and active | Hourly; deletes result files beginning on day 91 |
| FlareSolverr | Docker container `flaresolverr`, image `ghcr.io/flaresolverr/flaresolverr:latest` | `127.0.0.1:8191` only |
| Public ingress | `cloudflared-a1.service`, enabled and running on A1-JP | `/etc/cloudflared/a1-services.yml` maps the two public hostnames above to localhost |

The A1-JP API and MCP units live in `/etc/systemd/system/`, use `WorkingDirectory=/home/ubuntu/ccsearch`, load `/home/ubuntu/ccsearch/.env` with systemd `EnvironmentFile=`, and restart automatically. Their source unit files are not checked in. The cache-maintenance service and timer are reproducible under `systemd/`.

The HTTP service runs Flask's built-in server directly. It is systemd-managed but is not yet a production WSGI/ASGI deployment; replacement remains in `TODO.md`.

FlareSolverr has no authentication and is bound to localhost only. The checked-in compose file publishes `127.0.0.1:8191:8191`. A1-JP, the A1-US rollback copy, and Pi5 all have that loopback binding. On 2026-09-03, Pi5's running `flaresolverr` container was listening on `127.0.0.1:8191` only; its ccsearch API and MCP services remained disabled. Do not publish port `8191` on all interfaces if compose is recreated.

### Current Non-secret Runtime Configuration

The live, untracked `config.ini` differs from `config.ini.example`:

- Brave: three Search API keys on A1-JP, round-robin per live request, with a per-key cross-process limiter shared by Web Search, LLM Context, CLI, HTTP, MCP, batch workers, and retries; 20 results, safesearch off, 2 retries. Traffic from other hosts using the same subscriptions is not visible to this limiter.
- Perplexity: `perplexity/sonar-pro-search`, citations on, temperature 0.1, 16,384 max tokens, 2 retries.
- LLM Context: 30 results, 16,384 max tokens, 20 URLs, lenient threshold, 2 retries.
- Fetch: `http://localhost:8191/v1`, 60-second FlareSolverr timeout, fallback mode.
- Batch: effective default is 4 workers (the code default is used when `[Batch]` is absent).
- `curl_cffi`, `fastembed`, `markitdown[pdf]`, and `mcp` are installed; all search engine credentials are present in the service environment.

Treat these as a dated operational snapshot, not portable defaults. `config.ini.example` remains the conservative setup template.

## Secrets and Configuration

- Never print, commit, or paste values from `.env`, `.api_key`, `config.ini`, Cloudflare credentials, or authenticated MCP URLs.
- `.env`, `.api_key`, and `config.ini` must remain mode `0600`; all three are ignored by Git and may contain deployment-specific values.
- The Python programs do not load dotenv files themselves. systemd loads `.env`; for manual runs, export the variables in the shell first.
- Every Brave-backed engine prefers `BRAVE_SEARCH_API_KEY`, plus optional `BRAVE_SEARCH_API_KEY_2`, `BRAVE_SEARCH_API_KEY_3`, or `BRAVE_SEARCH_API_KEYS`. `BRAVE_API_KEY` is used only as a compatibility fallback when no Search key is set. `both` additionally requires `OPENROUTER_API_KEY`.
- `CCSEARCH_API_KEY` from the environment takes precedence over `.api_key`. Both servers read the key at process startup, so changing it requires service restarts.
- Optional port overrides: `CCSEARCH_PORT` and `CCSEARCH_MCP_PORT`.
- MCP authentication embeds the shared key in the URL path. Uvicorn, systemd journal, proxies, and client logs can record that path. Do not show raw MCP access logs; redact the first path segment. AI assistants must never rotate the shared key autonomously. If the key is exposed in output during coding, stop reproducing it, redact it from subsequent output, notify the user, and ask whether they want it rotated. Rotate it only after explicit user approval.

## Canonical Development and Deployment Workflow

- Treat the Mac working copy as the canonical authoring workspace. A1-JP,
  A1-US, and Pi5 are deployment targets, not normal code-editing locations.
- Before making changes, inspect the Mac working tree and preserve any existing
  unexplained work. Never overwrite or mix unrelated modifications.
- Implement and test changes on the Mac working copy when its required
  dependencies are available. Use the A1-JP virtual environment for additional
  Linux or ARM64 verification when needed.
- Commit approved changes on the Mac and push them to `origin/main` before
  deploying them.
- Deployment hosts use the public HTTPS GitHub remote for fetches. Only the Mac
  working copy pushes releases; never copy the Mac GitHub private key to A1-JP,
  A1-US, or Pi5.
- Deploy production changes by fast-forwarding A1-JP to the exact verified
  commit. Preserve its untracked `.env`, `.api_key`, and `config.ini`.
- Restart only the services affected by the change. Documentation-only changes
  must not restart services.
- Verify the affected CLI, HTTP API, MCP tools, Docker services, systemd units,
  and public Cloudflare endpoints in proportion to the change.
- After A1-JP passes production verification, fast-forward A1-US and Pi5 to the
  same commit for rollback readiness. Do not start their ccsearch API, MCP,
  cache, or public connector services.
- Finish by confirming that Mac, GitHub, A1-JP, A1-US, and Pi5 reference the
  same commit and that no unexpected tracked changes remain.
- If an emergency edit is ever made directly on a deployment host, copy it
  back into the Mac working copy, test it, commit it, and resynchronize every
  deployment target before considering the work complete.

## Operations

Read-only status checks:

```bash
systemctl status ccsearch-api.service ccsearch-mcp.service cloudflared.service
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
python3 -m unittest -v test_ccsearch.py
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
- Deployment: check systemd state, listeners, redacted recent logs, Docker state, and public Cloudflare routes.

## Documentation Sync Rules

- Public CLI, response, engine, or option changes: update `README.md`, both files under `skills/`, and tests together.
- Deployment changes: update this live snapshot and the deployment sections in `README.md`; never place credentials or tunnel IDs in tracked files.
- Cache freshness defaults to and is capped at 90 days. Files are unreadable after 90 days and deleted beginning on day 91 by the hourly timer or the next cleanup pass; keep exact and semantic cache behavior synchronized.
- `offset` is supported by `brave` and `both`.
- HTTP non-health endpoints use `X-API-Key`; MCP uses the key as a path prefix.
- The HTTP API maps validation failures to 400, authentication failures to 401, completed fetch error payloads to 424 while preserving their metadata through Cloudflare, and unexpected server failures to 500.
- Keep `README.md` in English unless the user explicitly requests another language.
