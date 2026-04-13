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
    mask_secret,
    execute_batch,
    execute_query,
    get_diagnostics,
    list_engines,
    validate_query,
    validate_execution_options,
    VALID_ENGINES,
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_key")
API_KEY = load_api_key(KEY_FILE, create_if_missing=True)
print(f"[ccsearch-api] API key loaded from {'environment' if os.environ.get('CCSEARCH_API_KEY') else KEY_FILE}: {mask_secret(API_KEY)}")


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
      - result_limit (int, optional): trim returned results for brave/both/llm-context
      - flaresolverr (bool, optional): force FlareSolverr for fetch engine
      - include_hosts (list[str] or comma-separated str, optional): host allow-list for brave/both/llm-context
      - exclude_hosts (list[str] or comma-separated str, optional): host deny-list for brave/both/llm-context
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Bad Request", "message": "JSON body required"}), 400

    query = data.get("query", "").strip()
    engine = data.get("engine", "").strip().lower()

    if not query:
        return jsonify({"error": "Bad Request", "message": "'query' is required"}), 400

    if engine not in VALID_ENGINES:
        return jsonify({
            "error": "Bad Request",
            "message": f"'engine' must be one of: {', '.join(VALID_ENGINES)}"
        }), 400

    use_cache = data.get("cache", False)
    cache_ttl = data.get("cache_ttl", 10)
    use_semantic = data.get("semantic_cache", False)
    semantic_threshold = data.get("semantic_threshold", 0.9)
    offset = data.get("offset")
    result_limit = data.get("result_limit")
    force_flaresolverr = data.get("flaresolverr", False)
    include_hosts = data.get("include_hosts")
    exclude_hosts = data.get("exclude_hosts")

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
        )
        return jsonify(result)

    except ValueError as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": "Server Error", "message": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "Search Failed", "message": str(e)}), 500


@app.route("/batch", methods=["POST"])
@require_api_key
def batch():
    """Execute multiple requests in one HTTP round-trip."""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Bad Request", "message": "JSON body required"}), 400

    requests_payload = data.get("requests")
    defaults = data.get("defaults", {})
    max_workers = data.get("max_workers")
    config = load_config(CONFIG_PATH)

    try:
        result = execute_batch(requests_payload, config, defaults=defaults, max_workers=max_workers)
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": "Bad Request", "message": str(e)}), 400
    except Exception as e:
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
    return jsonify(get_diagnostics(config))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("CCSEARCH_PORT", 8888))
    print(f"[ccsearch-api] Starting on port {port}")
    app.run(host="0.0.0.0", port=port)
