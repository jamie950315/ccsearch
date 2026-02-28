# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
This is a Python CLI web search utility (`ccsearch`) designed to provide search capabilities to LLMs and human users via Brave Data for Search API and Perplexity (through OpenRouter).

## Development
- **Dependencies**: Install requirements using `pip install -r requirements.txt`. (Requires `requests` and Python 3).
- **Configuration**: Copy `config.ini.example` to `config.ini` to configure rate bounds, max results, and the Perplexity model string.
- **Executing**: Run `./ccsearch.py --help` to see options.

## Testing & Usage Commands
- Run a Brave search (JSON output): `./ccsearch.py "Claude 3.5 Sonnet" -e brave --format json`
- Run a Perplexity query (Text output): `./ccsearch.py "What is Claude Code?" -e perplexity --format text`
- *Note:* Requires `BRAVE_API_KEY` or `OPENROUTER_API_KEY` to be set in the environment.

## Architecture
The script `ccsearch.py` handles CLI parsing through `argparse` and loads defaults from `config.ini`. It makes synchronous HTTP requests using the `requests` library. If an API key is missing, it will gracefully exit with status code 1 and prompt the user (or the invoking LLM tool) to provide the appropriate key.
