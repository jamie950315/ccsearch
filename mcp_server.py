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
    load_api_key,
    execute_batch,
    execute_query,
    get_diagnostics,
    list_engines,
    mask_secret,
    validate_query,
    validate_execution_options,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
PORT=int(os.environ.get("CCSEARCH_MCP_PORT", 8890))

# API key auth (shared with Flask HTTP API)
KEY_FILE=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_key")
API_KEY=load_api_key(KEY_FILE, create_if_missing=True)

mcp=FastMCP(
    name="ccsearch",
    instructions="Web search, URL fetching, and LLM-optimized context retrieval via Brave Search, Perplexity, and direct fetch.",
    host="0.0.0.0",
    port=PORT,
    log_level="INFO",
)

# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------
EngineType=Literal["brave", "perplexity", "both", "llm-context"]

@mcp.tool()
def search(
    query: str,
    engine: EngineType="brave",
    offset: int|None=None,
    result_limit: int|None=None,
    cache: bool=False,
    cache_ttl: int=10,
    semantic_cache: bool=False,
    semantic_threshold: float=0.9,
    include_hosts: str|None=None,
    exclude_hosts: str|None=None,
) -> dict:
    """Search the web using various engines.

    Args:
        query: Search query string (keep it short, 1-6 words for best results)
        engine: Search engine to use — brave (fast links/snippets), perplexity (AI-synthesized answer), both (concurrent brave+perplexity), llm-context (pre-extracted smart chunks for LLMs)
        offset: Pagination offset (brave engine only)
        result_limit: Trim returned results for brave/both/llm-context
        cache: Enable server-side result caching
        cache_ttl: Cache time-to-live in minutes
        semantic_cache: Enable semantic similarity cache matching
        semantic_threshold: Cosine similarity threshold for semantic cache (0.0-1.0)
        include_hosts: Comma-separated host allow-list for brave/both/llm-context
        exclude_hosts: Comma-separated host deny-list for brave/both/llm-context
    """
    config=load_config(CONFIG_PATH)
    validation_error=validate_query(query, engine)
    if validation_error:
        return {"error": validation_error}
    option_error=validate_execution_options(
        engine,
        offset=offset,
        cache_ttl=cache_ttl,
        semantic_threshold=semantic_threshold,
        include_hosts=include_hosts,
        exclude_hosts=exclude_hosts,
        result_limit=result_limit,
    )
    if option_error:
        return {"error": option_error}
    try:
        return execute_query(
            query,
            engine,
            config,
            offset=offset,
            cache=cache,
            cache_ttl=cache_ttl,
            semantic_cache=semantic_cache,
            semantic_threshold=semantic_threshold,
            include_hosts=include_hosts,
            exclude_hosts=exclude_hosts,
            result_limit=result_limit,
        )
    except (ValueError, RuntimeError) as e:
        return {"error": str(e)}


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
    config=load_config(CONFIG_PATH)
    validation_error=validate_query(url, "fetch")
    if validation_error:
        return {"error": validation_error}
    option_error=validate_execution_options(
        "fetch",
        cache_ttl=cache_ttl,
        flaresolverr=flaresolverr,
    )
    if option_error:
        return {"error": option_error}
    try:
        return execute_query(
            url,
            "fetch",
            config,
            cache=cache,
            cache_ttl=cache_ttl,
            flaresolverr=flaresolverr,
        )
    except (ValueError, RuntimeError) as e:
        return {"error": str(e)}

@mcp.tool()
def engines() -> dict:
    """List available search and fetch engines."""
    config=load_config(CONFIG_PATH)
    return {"engines": list_engines(), "diagnostics": get_diagnostics(config, include_engines=False)}


@mcp.tool()
def diagnostics() -> dict:
    """Return runtime diagnostics without exposing secret values."""
    config=load_config(CONFIG_PATH)
    return get_diagnostics(config)


@mcp.tool()
def batch(
    requests: list[dict],
    engine: str|None=None,
    cache: bool=False,
    cache_ttl: int=10,
    semantic_cache: bool=False,
    semantic_threshold: float=0.9,
    offset: int|None=None,
    result_limit: int|None=None,
    flaresolverr: bool=False,
    include_hosts: str|None=None,
    exclude_hosts: str|None=None,
    max_workers: int|None=None,
) -> dict:
    """Execute multiple search/fetch requests in one call.

    Each request object may provide:
    - query or url
    - engine
    - offset
    - result_limit
    - cache
    - cache_ttl
    - semantic_cache
    - semantic_threshold
    - flaresolverr
    - include_hosts
    - exclude_hosts
    """
    config=load_config(CONFIG_PATH)
    defaults={
        "cache": cache,
        "cache_ttl": cache_ttl,
        "semantic_cache": semantic_cache,
        "semantic_threshold": semantic_threshold,
        "offset": offset,
        "result_limit": result_limit,
        "flaresolverr": flaresolverr,
        "include_hosts": include_hosts,
        "exclude_hosts": exclude_hosts,
    }
    if engine:
        defaults["engine"]=engine
    try:
        return execute_batch(requests, config, defaults=defaults, max_workers=max_workers)
    except ValueError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__=="__main__":
    import asyncio
    import uvicorn

    async def unauthorized(request: Request):
        return JSONResponse({"error": "Unauthorized", "message": "Invalid or missing API key in path"}, status_code=401)

    async def main():
        # Build combined app: SSE (/sse + /messages/) + Streamable HTTP (/mcp)
        sse_inner=mcp.sse_app()
        http_inner=mcp.streamable_http_app()
        combined=Starlette(routes=list(sse_inner.routes)+list(http_inner.routes))

        # lifespan must be on the outermost app for uvicorn to trigger it
        app=Starlette(
            routes=[
                Mount(f"/{API_KEY}", app=combined),
                Route("/{path:path}", unauthorized, methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS"]),
            ],
            lifespan=lambda app: mcp.session_manager.run(),
        )

        print(f"[ccsearch-mcp] Starting MCP server on port {PORT} (path auth: {'enabled' if API_KEY else 'DISABLED'})")
        print(f"[ccsearch-mcp] API key source: {'environment' if os.environ.get('CCSEARCH_API_KEY') else KEY_FILE} ({mask_secret(API_KEY)})")
        print(f"[ccsearch-mcp] SSE: /<key>/sse | Streamable HTTP: /<key>/mcp")
        config=uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
        server=uvicorn.Server(config)
        await server.serve()

    asyncio.run(main())
