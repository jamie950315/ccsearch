# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Overview

`ccsearch` is a Python search and fetch utility with three user-facing entry points that share the same execution core:

- `ccsearch.py`: CLI
- `api_server.py`: Flask HTTP API
- `mcp_server.py`: MCP server

Supported engines:

- `brave`: Brave Web Search
- `perplexity`: Perplexity via OpenRouter
- `both`: Brave + Perplexity combined
- `llm-context`: Brave LLM Context API
- `fetch`: direct URL fetch and extraction

The project now exposes a shared execution layer with:

- query validation
- option validation
- exact cache + semantic cache
- host filtering for search-style engines
- result limiting for search-style engines
- batch execution with deduplication
- runtime diagnostics and engine capability reporting

## Development

- Install dependencies with `pip install -r requirements.txt`
- Copy `config.ini.example` to `config.ini`
- Use `./ccsearch.py --help` for CLI flags
- The standard requirements currently install `fastembed` for semantic cache and `curl_cffi` for direct-fetch TLS impersonation. The code degrades gracefully if either is unavailable.
- Optional extra: `markitdown[...]` for PDF / Office conversion in `fetch`.

## Testing And Verification

Before claiming work is complete, run the actual checks used in this repo:

- Syntax/build check:
  - `python3 -m py_compile ccsearch.py api_server.py mcp_server.py test_ccsearch.py`
- Test suite:
  - `python3 -m unittest -v test_ccsearch.py`

When relevant, also run the tool for real:

- CLI search:
  - `python3 ccsearch.py "OpenAI Responses API" -e brave --format json`
- CLI fetch:
  - `python3 ccsearch.py "https://example.com" -e fetch --format json`
- API server:
  - `CCSEARCH_PORT=18991 python3 api_server.py`
  - `curl -sS http://127.0.0.1:18991/health`
  - `curl -sS http://127.0.0.1:18991/diagnostics -H "X-API-Key: <key>"`

## Architecture

### Shared Execution

`ccsearch.py` is the source of truth for:

- engine dispatch
- cache handling
- semantic cache lookup and backfill
- fetch extraction
- batch execution
- diagnostics
- engine capability reporting

Use and update shared helpers instead of re-implementing behavior in CLI, HTTP API, or MCP.

Cache freshness defaults to 90 days and cannot exceed 90 days. A shorter caller-provided `cache_ttl` still expires a result earlier. Result files become unreadable after 90 days and are physically deleted beginning on day 91; `--prune-cache` and `ccsearch-cache-prune.timer` enforce cleanup, including semantic-index orphan removal.

### Search Engines

- `brave`, the Brave side of `both`, and `llm-context` prefer `BRAVE_SEARCH_API_KEY` and fall back to `BRAVE_API_KEY` only when the Search key is unset
- `perplexity` uses OpenRouter
- `both` runs Brave and Perplexity concurrently and preserves partial failures

All Brave attempts, including retries, share a cross-process limiter capped at 50 RPS across the local CLI, HTTP API, and MCP services. The limiter cannot account for other devices using the same Brave subscription.

Search-style engines normalize output for downstream agents:

- cleaned text
- `hostname`
- `rank`
- host summaries
- optional `host_filtering`
- optional `result_limiting`
- `cache_status`
- `duration_ms`

### Fetch Engine

`fetch` uses a layered flow:

1. `_simple_fetch`
2. Cloudflare detection
3. SPA shell detection
4. optional FlareSolverr fallback

When available, `_simple_fetch` uses `curl_cffi` with Chrome impersonation. Otherwise it falls back to `requests`.

`fetch` also supports:

- non-HTML text decoding
- binary document conversion through optional MarkItDown integration
- JSON-LD and social metadata extraction
- structured `chunks`
- code/list/table preservation
- outbound link extraction
- X/Twitter routing through the fxtwitter API

### Batch Execution

Batch execution is implemented in shared core logic, not in the API layer.

Features:

- bounded parallelism via `max_workers`
- isolation of runtime failures and normally validated per-request errors
- duplicate request suppression within the batch
- stable output ordering
- per-batch summary fields such as `success_count`, `error_count`, `duration_ms`, `deduped_count`

The batch dedupe fingerprint includes execution-shaping options such as:

- engine
- normalized query
- offset
- cache settings
- flaresolverr
- host filters
- result limit

## HTTP API Server

`api_server.py` is a Flask app that exposes:

- `GET /health`
- `POST /search`
- `POST /batch`
- `GET /engines`
- `GET /diagnostics`

Authentication:

- all endpoints except `/health` require `X-API-Key`
- the key is loaded from `CCSEARCH_API_KEY` or `.api_key`

Do not duplicate validation logic here; call the shared helpers from `ccsearch.py`.

## MCP Server

`mcp_server.py` exposes FastMCP tools over both SSE and Streamable HTTP.

Current tools:

- `search`
- `fetch`
- `batch`
- `engines`
- `diagnostics`

The MCP server should stay thin and forward into shared execution logic.

## Current Pi 5 Runtime

The dated, live-verified deployment snapshot and operational commands are in `AGENTS.md`. Re-check systemd, Docker, listeners, and Cloudflare configuration before treating it as current.

- `ccsearch-api.service`: `/usr/bin/python3 api_server.py`, port `8888`, HTTP `X-API-Key` authentication except `/health`.
- `ccsearch-mcp.service`: `/usr/bin/python3 mcp_server.py`, port `8890`, path-prefix authentication, SSE and Streamable HTTP.
- `ccsearch-cache-prune.timer`: hourly cache cleanup for entries that have reached day 91.
- Both units run as `jamie` from `/home/jamie/ccsearch` and systemd loads `.env` for them. The Python code does not load dotenv files for manual runs.
- FlareSolverr runs in Docker on port `8191`; `cloudflared.service` publishes the API and MCP hostnames.
- The HTTP process still uses Flask's built-in server, not a production WSGI/ASGI server.

Never print authenticated MCP URLs or raw MCP access logs: the API key is part of the path and can appear in journals and proxy logs.

## Environment Variables

- `BRAVE_API_KEY`
- `BRAVE_SEARCH_API_KEY`
- `OPENROUTER_API_KEY`
- `CCSEARCH_API_KEY`
- optional port overrides:
  - `CCSEARCH_PORT`
  - `CCSEARCH_MCP_PORT`

All Brave-backed engines prefer `BRAVE_SEARCH_API_KEY` and fall back to `BRAVE_API_KEY`. `both` additionally requires `OPENROUTER_API_KEY`.
