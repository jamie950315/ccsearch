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

The `fetch` engine uses a layered approach: `_simple_fetch` (bare `requests.get`) → Cloudflare detection (`_detect_cloudflare`) → optional `_flaresolverr_fetch` fallback. The orchestrator `perform_fetch` reads `[Fetch]` config to decide the execution strategy (`fallback`, `always`, or `never`). FlareSolverr communication is a simple `requests.post()` to its HTTP API — no extra dependencies required.
