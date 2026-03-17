#!/usr/bin/env python3
"""
ccsearch HTTP API Server

Exposes ccsearch functionality over HTTP with API key authentication.
"""
import os
import sys
import secrets
import functools
from flask import Flask, request, jsonify

# Import ccsearch functions
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("CCSEARCH_API_KEY", "")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

if not API_KEY:
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_key")
    if os.path.exists(key_file):
        with open(key_file, "r") as f:
            API_KEY = f.read().strip()
    else:
        API_KEY = secrets.token_urlsafe(32)
        with open(key_file, "w") as f:
            f.write(API_KEY)
        os.chmod(key_file, 0o600)
        print(f"[ccsearch-api] Generated new API key, saved to {key_file}")

print(f"[ccsearch-api] API Key: {API_KEY}")


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def require_api_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(key, API_KEY):
            return jsonify({"error": "Unauthorized", "message": "Invalid or missing X-API-Key header"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "ccsearch-api"})


@app.route("/search", methods=["POST"])
@require_api_key
def search():
    """
    Main search endpoint.

    JSON body:
      - query (str, required): search query or URL (for fetch engine)
      - engine (str, required): brave | perplexity | both | fetch | llm-context
      - cache (bool, optional): enable caching (default: false)
      - cache_ttl (int, optional): cache TTL in minutes (default: 10)
      - semantic_cache (bool, optional): enable semantic cache (default: false)
      - semantic_threshold (float, optional): cosine similarity threshold (default: 0.9)
      - offset (int, optional): pagination offset (brave only)
      - flaresolverr (bool, optional): force FlareSolverr for fetch engine
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Bad Request", "message": "JSON body required"}), 400

    query = data.get("query", "").strip()
    engine = data.get("engine", "").strip().lower()

    if not query:
        return jsonify({"error": "Bad Request", "message": "'query' is required"}), 400

    valid_engines = ["brave", "perplexity", "both", "fetch", "llm-context"]
    if engine not in valid_engines:
        return jsonify({
            "error": "Bad Request",
            "message": f"'engine' must be one of: {', '.join(valid_engines)}"
        }), 400

    use_cache = data.get("cache", False)
    cache_ttl = data.get("cache_ttl", 10)
    use_semantic = data.get("semantic_cache", False)
    semantic_threshold = data.get("semantic_threshold", 0.9)
    offset = data.get("offset")
    force_flaresolverr = data.get("flaresolverr", False)

    config = load_config(CONFIG_PATH)

    try:
        result = None

        # 1. Try exact cache
        if use_cache or use_semantic:
            result = read_from_cache(query, engine, offset, cache_ttl)
            if result:
                result["_from_cache"] = True
                if use_semantic and engine != "fetch":
                    cache_key = get_cache_key(query, engine, offset)
                    index = _load_semantic_index()
                    key = cache_key.replace(".json", "")
                    if key not in index:
                        update_semantic_index(query, engine, offset, cache_key)

        # 2. Try semantic cache
        if not result and use_semantic and engine != "fetch":
            result, sim = read_from_semantic_cache(
                query, engine, offset, cache_ttl, semantic_threshold
            )
            if result:
                result["_from_cache"] = True
                result["_semantic_similarity"] = sim

        # 3. Perform actual search
        if not result:
            if engine == "brave":
                api_key = os.environ.get("BRAVE_API_KEY")
                if not api_key:
                    return jsonify({"error": "Server Error", "message": "BRAVE_API_KEY not configured"}), 500
                result = perform_brave_search(query, api_key, config, offset=offset)

            elif engine == "perplexity":
                api_key = os.environ.get("OPENROUTER_API_KEY")
                if not api_key:
                    return jsonify({"error": "Server Error", "message": "OPENROUTER_API_KEY not configured"}), 500
                result = perform_perplexity_search(query, api_key, config)

            elif engine == "both":
                brave_key = os.environ.get("BRAVE_API_KEY")
                openrouter_key = os.environ.get("OPENROUTER_API_KEY")
                if not brave_key or not openrouter_key:
                    return jsonify({"error": "Server Error", "message": "BRAVE_API_KEY and OPENROUTER_API_KEY required"}), 500
                result = perform_both_search(query, brave_key, openrouter_key, config, offset=offset)

            elif engine == "llm-context":
                api_key = os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY")
                if not api_key:
                    return jsonify({"error": "Server Error", "message": "BRAVE_SEARCH_API_KEY not configured"}), 500
                result = perform_llm_context_search(query, api_key, config)

            elif engine == "fetch":
                if not query.startswith("http"):
                    return jsonify({"error": "Bad Request", "message": "For fetch engine, query must be a valid URL"}), 400
                if force_flaresolverr:
                    config.set('Fetch', 'flaresolverr_mode', 'always')
                result = perform_fetch(query, config)

            # Write to cache
            if use_cache or use_semantic:
                cache_key = get_cache_key(query, engine, offset)
                write_to_cache(query, engine, offset, result)
                if use_semantic and engine != "fetch":
                    update_semantic_index(query, engine, offset, cache_key)

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": "Search Failed", "message": str(e)}), 500


@app.route("/engines", methods=["GET"])
@require_api_key
def engines():
    """List available search engines and their requirements."""
    return jsonify({
        "engines": [
            {"name": "brave", "description": "Brave Web Search", "requires": "BRAVE_API_KEY"},
            {"name": "perplexity", "description": "Perplexity via OpenRouter", "requires": "OPENROUTER_API_KEY"},
            {"name": "both", "description": "Brave + Perplexity combined", "requires": "BRAVE_API_KEY + OPENROUTER_API_KEY"},
            {"name": "llm-context", "description": "Brave LLM Context API (smart chunks)", "requires": "BRAVE_API_KEY"},
            {"name": "fetch", "description": "Fetch and extract text from a URL", "requires": None},
        ]
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("CCSEARCH_PORT", 8888))
    print(f"[ccsearch-api] Starting on port {port}")
    app.run(host="0.0.0.0", port=port)
