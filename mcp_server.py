#!/usr/bin/env python3
"""
ccsearch MCP Server

Exposes ccsearch functionality as MCP tools over SSE/Streamable HTTP.
Runs alongside the existing Flask HTTP API without modifying it.
"""
import os
import sys
from typing import Literal
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.applications import Starlette

# Ensure ccsearch module is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from ccsearch import (
    load_config,
    perform_brave_search,
    perform_perplexity_search,
    perform_both_search,
    perform_llm_context_search,
    perform_fetch,
    read_from_cache,
    write_to_cache,
    get_cache_key,
    read_from_semantic_cache,
    update_semantic_index,
    _load_semantic_index,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
PORT=int(os.environ.get("CCSEARCH_MCP_PORT", 8890))

# API key auth (shared with Flask HTTP API)
API_KEY=os.environ.get("CCSEARCH_API_KEY", "")
if not API_KEY:
    key_file=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_key")
    if os.path.exists(key_file):
        with open(key_file) as f:
            API_KEY=f.read().strip()

mcp=FastMCP(
    name="ccsearch",
    instructions="Web search, URL fetching, and LLM-optimized context retrieval via Brave Search, Perplexity, and direct fetch.",
    host="0.0.0.0",
    port=PORT,
    log_level="INFO",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _try_cache(query: str, engine: str, offset: int|None, cache: bool, cache_ttl: int, semantic_cache: bool, semantic_threshold: float):
    """Attempt exact + semantic cache lookup. Returns (result, hit) tuple."""
    result=None
    if cache or semantic_cache:
        result=read_from_cache(query, engine, offset, cache_ttl)
        if result:
            result["_from_cache"]=True
            if semantic_cache and engine!="fetch":
                cache_key=get_cache_key(query, engine, offset)
                index=_load_semantic_index()
                key=cache_key.replace(".json", "")
                if key not in index:
                    update_semantic_index(query, engine, offset, cache_key)
            return result, True

    if not result and semantic_cache and engine!="fetch":
        result, sim=read_from_semantic_cache(query, engine, offset, cache_ttl, semantic_threshold)
        if result:
            result["_from_cache"]=True
            result["_semantic_similarity"]=sim
            return result, True

    return None, False

def _write_cache(query: str, engine: str, offset: int|None, result: dict, cache: bool, semantic_cache: bool):
    """Write result to cache if caching is enabled."""
    if cache or semantic_cache:
        cache_key=get_cache_key(query, engine, offset)
        write_to_cache(query, engine, offset, result)
        if semantic_cache and engine!="fetch":
            update_semantic_index(query, engine, offset, cache_key)

# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------
EngineType=Literal["brave", "perplexity", "both", "llm-context"]

@mcp.tool()
def search(
    query: str,
    engine: EngineType="brave",
    offset: int|None=None,
    cache: bool=False,
    cache_ttl: int=10,
    semantic_cache: bool=False,
    semantic_threshold: float=0.9,
) -> dict:
    """Search the web using various engines.

    Args:
        query: Search query string (keep it short, 1-6 words for best results)
        engine: Search engine to use — brave (fast links/snippets), perplexity (AI-synthesized answer), both (concurrent brave+perplexity), llm-context (pre-extracted smart chunks for LLMs)
        offset: Pagination offset (brave engine only)
        cache: Enable server-side result caching
        cache_ttl: Cache time-to-live in minutes
        semantic_cache: Enable semantic similarity cache matching
        semantic_threshold: Cosine similarity threshold for semantic cache (0.0-1.0)
    """
    config=load_config(CONFIG_PATH)

    # Cache lookup
    result, hit=_try_cache(query, engine, offset, cache, cache_ttl, semantic_cache, semantic_threshold)
    if hit:
        return result

    # Perform search
    if engine=="brave":
        api_key=os.environ.get("BRAVE_API_KEY")
        if not api_key:
            return {"error": "BRAVE_API_KEY not configured"}
        result=perform_brave_search(query, api_key, config, offset=offset)

    elif engine=="perplexity":
        api_key=os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return {"error": "OPENROUTER_API_KEY not configured"}
        result=perform_perplexity_search(query, api_key, config)

    elif engine=="both":
        brave_key=os.environ.get("BRAVE_API_KEY")
        openrouter_key=os.environ.get("OPENROUTER_API_KEY")
        if not brave_key or not openrouter_key:
            return {"error": "BRAVE_API_KEY and OPENROUTER_API_KEY required"}
        result=perform_both_search(query, brave_key, openrouter_key, config, offset=offset)

    elif engine=="llm-context":
        api_key=os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY")
        if not api_key:
            return {"error": "BRAVE_SEARCH_API_KEY not configured"}
        result=perform_llm_context_search(query, api_key, config)

    _write_cache(query, engine, offset, result, cache, semantic_cache)
    return result


@mcp.tool()
def fetch(
    url: str,
    flaresolverr: bool=False,
    cache: bool=False,
    cache_ttl: int=10,
) -> dict:
    """Fetch and extract text content from a URL.

    Args:
        url: The URL to fetch (must start with http:// or https://)
        flaresolverr: Force FlareSolverr headless browser for Cloudflare-protected or SPA pages
        cache: Enable server-side result caching
        cache_ttl: Cache time-to-live in minutes
    """
    if not url.startswith("http"):
        return {"error": "url must start with http:// or https://"}

    config=load_config(CONFIG_PATH)

    # Cache lookup
    if cache:
        result=read_from_cache(url, "fetch", None, cache_ttl)
        if result:
            result["_from_cache"]=True
            return result

    if flaresolverr:
        config.set('Fetch', 'flaresolverr_mode', 'always')

    result=perform_fetch(url, config)

    if cache:
        write_to_cache(url, "fetch", None, result)

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__=="__main__":
    import asyncio
    import uvicorn

    async def unauthorized(request: Request):
        return JSONResponse({"error": "Unauthorized", "message": "Invalid or missing API key in path"}, status_code=401)

    async def main():
        # Mount MCP SSE app under /<API_KEY>/ prefix
        # mount_path makes SSE return correct /KEY/messages/ endpoint to clients
        inner=mcp.sse_app()  # no mount_path — Starlette Mount sets root_path automatically
        app=Starlette(routes=[
            Mount(f"/{API_KEY}", app=inner),
            Route("/{path:path}", unauthorized),
        ])

        print(f"[ccsearch-mcp] Starting MCP server on port {PORT} (path auth: {'enabled' if API_KEY else 'DISABLED'})")
        print(f"[ccsearch-mcp] SSE endpoint: /<key>/sse")
        config=uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
        server=uvicorn.Server(config)
        await server.serve()

    asyncio.run(main())
