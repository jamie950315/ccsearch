@CLAUDE.md

# Repository and Deployment Operations

This file records repository-specific working rules and the verified deployment state. `CLAUDE.md` describes the code architecture in more detail. Re-check live state before relying on this snapshot because services and untracked configuration can change independently of Git.

## Project Shape

- `ccsearch.py` is the shared source of truth for validation, engine dispatch, caching, fetch extraction, result shaping, batching, diagnostics, and CLI output.
- `api_server.py` is a thin Flask HTTP wrapper. Keep validation and execution behavior in the shared core.
- `mcp_server.py` is a thin FastMCP wrapper. Its `search` tool covers search engines only; URL retrieval uses the separate `fetch` tool.
- `test_ccsearch.py` is the unit and regression suite. It does not replace live HTTP, MCP transport, systemd, Docker, or tunnel smoke tests.
- `skills/SKILL.md` is the generic self-hosted HTTP skill template. `skills/claude_dot_ai_Specific_SKILL.md` is the Claude.ai-oriented variant. Keep both synchronized with public API behavior while preserving their intentional setup differences.

## Verified Deployment

Snapshot verified on 2026-09-01. The primary deployment is A1-JP, an Ubuntu 24.04 ARM64 Oracle A1 instance in Osaka. A1-US is the first application rollback host: its code, untracked configuration, stopped containers, data, and migration backup remain available, while its API, MCP, cache timer, and Cloudflare connector are disabled and inactive. The Raspberry Pi 5 checkout is a second cold standby with its API and MCP services disabled and inactive. Do not start either standby unless the user explicitly chooses to fail over.

| Component | Live state | Binding / public route |
| --- | --- | --- |
| HTTP API | `ccsearch-api.service`, enabled and running as `ubuntu` on A1-JP | `0.0.0.0:8888`, `https://ccsearch.0ruka.dev` |
| MCP | `ccsearch-mcp.service`, enabled and running as `ubuntu` on A1-JP | `0.0.0.0:8890`, `https://ccsearch-mcp.0ruka.dev` |
| Cache cleanup | `ccsearch-cache-prune.timer`, enabled and active | Hourly; deletes result files beginning on day 91 |
| FlareSolverr | Docker container `flaresolverr`, image `ghcr.io/flaresolverr/flaresolverr:latest` | `127.0.0.1:8191` only |
| Public ingress | `cloudflared-a1.service`, enabled and running on A1-JP | `/etc/cloudflared/a1-services.yml` maps the two public hostnames above to localhost |

The A1-JP API and MCP units live in `/etc/systemd/system/`, use `WorkingDirectory=/home/ubuntu/ccsearch`, load `/home/ubuntu/ccsearch/.env` with systemd `EnvironmentFile=`, and restart automatically. Their source unit files are not checked in. The cache-maintenance service and timer are reproducible under `systemd/`.

The HTTP service runs Flask's built-in server directly. It is systemd-managed but is not yet a production WSGI/ASGI deployment; replacement remains in `TODO.md`.

FlareSolverr has no authentication and is intentionally bound to localhost only on A1-JP. The checked-in compose file now enforces that loopback binding. A1-US keeps the same corrected compose file for rollback. The Pi cold standby predates this hardening and must not become the public DNS target without re-checking its listener and firewall state.

### Current Non-secret Runtime Configuration

The live, untracked `config.ini` differs from `config.ini.example`:

- Brave: one cross-process 50 RPS limit shared by Web Search, LLM Context, CLI, HTTP, MCP, batch workers, and retries on A1-JP; 20 results, safesearch off, 2 retries. Traffic from other hosts using the same subscription is not visible to this limiter.
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
- Every Brave-backed engine prefers `BRAVE_SEARCH_API_KEY`; `BRAVE_API_KEY` is used only as a compatibility fallback when the Search key is unset. `both` additionally requires `OPENROUTER_API_KEY`.
- `CCSEARCH_API_KEY` from the environment takes precedence over `.api_key`. Both servers read the key at process startup, so changing it requires service restarts.
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
- HTTP API: use Flask tests plus live `/health`, authenticated `/diagnostics`, and the affected endpoint when safe.
- MCP: exercise the affected tool and at least one real SSE or Streamable HTTP initialization when transport/auth code changes.
- Fetch: test direct HTML extraction and, when relevant, the running FlareSolverr fallback.
- Fetch results containing `error` are failures even when transport metadata is present. Preserve ordinary HTTP errors, verify binary conversion with a real document, and reject empty browser-rendered content.
- Deployment: check systemd state, listeners, redacted recent logs, Docker state, and public Cloudflare routes.

## Documentation Sync Rules

- Public CLI, response, engine, or option changes: update `README.md`, `CLAUDE.md`, both files under `skills/`, and tests together.
- Deployment changes: update this live snapshot and the deployment sections in `README.md`; never place credentials or tunnel IDs in tracked files.
- Cache freshness defaults to and is capped at 90 days. Files are unreadable after 90 days and deleted beginning on day 91 by the hourly timer or the next cleanup pass; keep exact and semantic cache behavior synchronized.
- `offset` is supported by `brave` and `both`.
- HTTP non-health endpoints use `X-API-Key`; MCP uses the key as a path prefix.
- The HTTP API maps validation failures to 400, authentication failures to 401, completed fetch error payloads to 424 while preserving their metadata through Cloudflare, and unexpected server failures to 500.
- Keep `README.md` in English unless the user explicitly requests another language.
