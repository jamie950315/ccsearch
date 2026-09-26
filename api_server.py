#!/usr/bin/env python3
"""
ccsearch HTTP API Server

Exposes ccsearch functionality over HTTP with API key authentication.
"""
import os
import sys
import functools
import secrets
from flask import Flask, request, jsonify

# Import ccsearch functions
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ccsearch import (
    load_config,
    load_api_key,
    execute_batch,
    execute_query,
    get_diagnostics,
    list_engines,
    normalize_claims,
    validate_query,
    validate_execution_options,
    VALID_ENGINES,
    DEFAULT_CACHE_TTL_MINUTES,
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_key")
API_KEY = load_api_key(KEY_FILE, create_if_missing=True)
print(f"[ccsearch-api] API key loaded from {'environment' if os.environ.get('CCSEARCH_API_KEY') else KEY_FILE}; authentication enabled")


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def require_api_key(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(key.encode("utf-8"), API_KEY.encode("utf-8")):
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
      - engine (str, required): brave | perplexity | both | fetch | llm-context | perplexity-verify
      - claims (list[str], perplexity-verify): claims to check instead of a newline-separated query
      - cache (bool, optional): enable caching (default: false)
      - cache_ttl (int, optional): cache TTL in minutes (default/max: 129600, 90 days)
      - max_cache_age (int, optional): ignore cache entries older than this many minutes
      - semantic_cache (bool, optional): enable semantic cache (default: false)
      - semantic_threshold (float, optional): cosine similarity threshold (default: 0.9)
      - offset (int, optional): pagination offset (brave/both)
      - result_limit (int, optional): results for brave/both/llm-context (default: 8)
      - freshness, country, search_lang (str, optional): Brave filters for brave/both/llm-context
      - snippet_limit (int, optional): snippets per llm-context result (default: 5)
      - flaresolverr (bool, optional): force FlareSolverr for fetch engine
      - format (str, fetch): text (default, content only) or chunks (chunks only)
      - focus (str, fetch) / focus_k (int, default 5): return only the most relevant passages
      - max_chars (int, fetch): truncate content and report truncated/total_chars
      - max_replies (int, fetch): forum replies for Discourse/Reddit/V2EX (default: 30)
      - verbose (bool, optional): include hashes, offsets, section paths, outbound links, raw ages
      - include_hosts (list[str] or comma-separated str, optional): host allow-list for brave/both/llm-context
      - exclude_hosts (list[str] or comma-separated str, optional): host deny-list for brave/both/llm-context
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Bad Request", "message": "JSON object body required"}), 400

    query = data.get("query", "")
    engine = data.get("engine", "")
    if not isinstance(engine, str):
        return jsonify({"error": "Bad Request", "message": "'query' and 'engine' must be strings"}), 400
    engine = engine.strip().lower()
    if engine == "perplexity-verify" and data.get("claims") is not None:
        try:
            query = "\n".join(normalize_claims(data.get("claims")))
        except ValueError as e:
            return jsonify({"error": "Bad Request", "message": str(e)}), 400
    if not isinstance(query, str):
        return jsonify({"error": "Bad Request", "message": "'query' and 'engine' must be strings"}), 400
    query = query.strip()

    if not query:
        return jsonify({"error": "Bad Request", "message": "'query' is required"}), 400

    if engine not in VALID_ENGINES:
        return jsonify({
            "error": "Bad Request",
            "message": f"'engine' must be one of: {', '.join(VALID_ENGINES)}"
        }), 400

    use_cache = data.get("cache", False)
    cache_ttl = data.get("cache_ttl", DEFAULT_CACHE_TTL_MINUTES)
    use_semantic = data.get("semantic_cache", False)
    semantic_threshold = data.get("semantic_threshold", 0.9)
    offset = data.get("offset")
    result_limit = data.get("result_limit")
    force_flaresolverr = data.get("flaresolverr", False)
    include_hosts = data.get("include_hosts")
    exclude_hosts = data.get("exclude_hosts")
    # Newer options are forwarded only when present so older clients see no change.
    extra_options = {
        name: data[name]
        for name in ("max_cache_age", "freshness", "country", "search_lang", "snippet_limit",
                     "format", "verbose", "focus", "focus_k", "max_chars", "max_replies")
        if name in data and data[name] is not None
    }

    config = load_config(CONFIG_PATH)

    validation_error = validate_query(query, engine)
    if validation_error:
        return jsonify({"error": "Bad Request", "message": validation_error}), 400

    option_error = validate_execution_options(
        engine,
        offset=offset,
        cache_ttl=cache_ttl,
        semantic_threshold=semantic_threshold,
        flaresolverr=force_flaresolverr,
        include_hosts=include_hosts,
        exclude_hosts=exclude_hosts,
        result_limit=result_limit,
        cache=use_cache,
        semantic_cache=use_semantic,
        **extra_options,
    )
    if option_error:
        return jsonify({"error": "Bad Request", "message": option_error}), 400

    try:
        result = execute_query(
            query,
            engine,
            config,
            offset=offset,
            cache=use_cache,
            cache_ttl=cache_ttl,
            semantic_cache=use_semantic,
            semantic_threshold=semantic_threshold,
            flaresolverr=force_flaresolverr,
            include_hosts=include_hosts,
            exclude_hosts=exclude_hosts,
            result_limit=result_limit,
            **extra_options,
        )
        if isinstance(result, dict) and result.get("error"):
            return jsonify(result), 424 if engine == "fetch" else 500
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400
    except RuntimeError as e:
        app.logger.exception("Search execution failed")
        return jsonify({"error": "Server Error", "message": str(e)}), 500
    except Exception as e:
        app.logger.exception("Unexpected search failure")
        return jsonify({"error": "Search Failed", "message": str(e)}), 500


@app.route("/batch", methods=["POST"])
@require_api_key
def batch():
    """Execute multiple requests in one HTTP round-trip.

    Items with ``url`` are fetched; items with ``query`` are searched with
    ``engine`` (default brave); ``op`` ("search" or "fetch") overrides the
    inference. ``defaults.engine`` applies to search items only.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Bad Request", "message": "JSON object body required"}), 400

    requests_payload = data.get("requests")
    defaults = data.get("defaults", {})
    max_workers = data.get("max_workers")
    dedupe_results = data.get("dedupe_results", True)
    config = load_config(CONFIG_PATH)

    try:
        result = execute_batch(requests_payload, config, defaults=defaults, max_workers=max_workers, dedupe_results=dedupe_results)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400
    except Exception as e:
        app.logger.exception("Unexpected batch failure")
        return jsonify({"error": "Batch Failed", "message": str(e)}), 500


@app.route("/engines", methods=["GET"])
@require_api_key
def engines():
    """List available search engines and their requirements."""
    config = load_config(CONFIG_PATH)
    return jsonify({"engines": list_engines(), "diagnostics": get_diagnostics(config, include_engines=False)})


@app.route("/diagnostics", methods=["GET"])
@require_api_key
def diagnostics():
    """Return runtime diagnostics without exposing secret values."""
    config = load_config(CONFIG_PATH)
    return jsonify(get_diagnostics(config, include_quota=True))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("CCSEARCH_PORT", 8888))
    print(f"[ccsearch-api] Starting on port {port}")
    app.run(host="0.0.0.0", port=port)
