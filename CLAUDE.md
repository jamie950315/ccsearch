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
- Optional extras:
  - `fastembed` for semantic cache
  - `markitdown[...]` for PDF / Office conversion in `fetch`
  - `curl_cffi` for better anti-bot handling in direct fetches

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

### Search Engines

- `brave` and `llm-context` use Brave APIs
- `perplexity` uses OpenRouter
- `both` runs Brave and Perplexity concurrently and preserves partial failures

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
- per-request isolation
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

## Current Runtime Assumptions

- HTTP API default port: `8888`
- MCP default port: `8890`
- HTTP API auth: `X-API-Key`
- MCP auth: path-based key prefix

## Environment Variables

- `BRAVE_API_KEY`
- `BRAVE_SEARCH_API_KEY`
- `OPENROUTER_API_KEY`
- `CCSEARCH_API_KEY`
- optional port overrides:
  - `CCSEARCH_PORT`
  - `CCSEARCH_MCP_PORT`

`llm-context` prefers `BRAVE_SEARCH_API_KEY` and falls back to `BRAVE_API_KEY`.
