# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
This is a Python CLI web search utility (`ccsearch`) designed to provide search capabilities to LLMs and human users via Brave Search API (including Web Search and LLM Context endpoints) and Perplexity (through OpenRouter).

## Development
- **Dependencies**: Install requirements using `pip install -r requirements.txt`. (Requires `requests` and Python 3).
- **Configuration**: Copy `config.ini.example` to `config.ini` to configure rate bounds, max results, and the Perplexity model string.
- **Executing**: Run `./ccsearch.py --help` to see options.

## Testing & Usage Commands
- Run a Brave search (JSON output): `./ccsearch.py "Claude 3.5 Sonnet" -e brave --format json`
- Run a Perplexity query (Text output): `./ccsearch.py "What is Claude Code?" -e perplexity --format text`
- Run an LLM Context search (pre-extracted smart chunks): `./ccsearch.py "best practices for React hooks" -e llm-context --format json`
- Fetch a webpage: `./ccsearch.py "http://example.com" -e fetch --format json`
- Fetch with forced FlareSolverr: `./ccsearch.py "http://example.com" -e fetch --format json --flaresolverr`
- Run all unit tests: `python3 -m pytest test_ccsearch.py -v`
- *Note:* Requires `BRAVE_API_KEY` or `OPENROUTER_API_KEY` to be set in the environment. FlareSolverr requires `flaresolverr_url` to be set in `config.ini`.

## Architecture
The script `ccsearch.py` handles CLI parsing through `argparse` and loads defaults from `config.ini`. It makes synchronous HTTP requests using the `requests` library. If an API key is missing, it will gracefully exit with status code 1 and prompt the user (or the invoking LLM tool) to provide the appropriate key.

The `llm-context` engine calls Brave's LLM Context API (`/res/v1/llm/context`), which returns pre-extracted, relevance-scored web content (smart chunks) optimized for LLM consumption. It shares the same `BRAVE_API_KEY` and rate limiting as the `brave` engine. Configuration lives in the `[LLMContext]` section of `config.ini`.

The `fetch` engine uses a layered approach: `_simple_fetch` → Cloudflare detection (`_detect_cloudflare`) → SPA shell detection (`_detect_spa_shell`) → optional `_flaresolverr_fetch` fallback. When `curl_cffi` is installed, `_simple_fetch` uses Chrome TLS fingerprint impersonation (`impersonate="chrome"`) to bypass anti-bot detection on strict sites (Facebook, LinkedIn, Medium, etc.); otherwise it falls back to `requests.get`. Request headers match a real Chrome 146 browser (including `Sec-Ch-Ua`, `Sec-Fetch-*`, and a Google `Referer`). The orchestrator `perform_fetch` reads `[Fetch]` config to decide the execution strategy (`fallback`, `always`, or `never`). FlareSolverr communication is a simple `requests.post()` to its HTTP API — no extra dependencies required. For HTML responses with status 200, SPA shell detection checks for empty mount points (`id="root"`, `id="__next"`, etc.) and script-heavy pages with little text, automatically falling back to FlareSolverr for headless rendering when detected.

Twitter/X URLs (`x.com`, `twitter.com`) are automatically intercepted and routed through the [fxtwitter API](https://github.com/FixTweet/FixTweet) — no API key required. Tweet URLs return full text, author info, engagement metrics, and media links; profile URLs return user bio, follower counts, and metadata. If the fxtwitter API fails, the fetch falls back to the normal fetch path.

## HTTP API Server
`api_server.py` is a Flask-based HTTP API that exposes all ccsearch engines over the network. It runs on port 8888 (configurable via `CCSEARCH_PORT` env var) behind a Cloudflare Tunnel at `ccsearch.0ruka.dev`.

- **Authentication**: All endpoints except `/health` require an `X-API-Key` header. The key is loaded from the `CCSEARCH_API_KEY` env var or the `.api_key` file (auto-generated on first run).
- **Endpoints**: `POST /search` (main search), `GET /engines` (list engines), `GET /health` (health check).
- **Deployment**: Runs as a systemd service (`ccsearch-api.service`) with `Restart=always` for automatic recovery.
- Start/stop: `sudo systemctl start|stop|restart ccsearch-api`
- Logs: `journalctl -u ccsearch-api -f`

## MCP Server
`mcp_server.py` is a Model Context Protocol (MCP) server that exposes ccsearch as MCP tools over SSE transport. It runs independently alongside the Flask HTTP API, sharing the same `ccsearch.py` core logic.

- **Runtime**: System Python 3.13 (`/usr/bin/python3`) with `mcp` 1.26.0 (FastMCP)
- **Port**: 8890 (configurable via `CCSEARCH_MCP_PORT` env var)
- **Systemd**: `ccsearch-mcp.service`
- **Public endpoint**: `https://ccsearch-mcp.0ruka.dev/<CCSEARCH_API_KEY>/sse`
- **Authentication**: Path-based — the API key is embedded in the URL path prefix. Requests without a valid key get 401.
- **Tools exposed**: `search` (brave/perplexity/both/llm-context engines) and `fetch` (URL content extraction)
- **Architecture**: Starlette `Mount` places the FastMCP SSE app under `/<key>/`, so the SSE transport automatically returns the correct `/<key>/messages/` endpoint to clients. No middleware auth needed.
