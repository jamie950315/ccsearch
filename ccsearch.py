#!/usr/bin/env python3
"""
ccsearch - A CLI Web Search Utility for LLMs and Human users.
Supports Brave Search API and Perplexity (via OpenRouter).
"""
import os
import sys
import json
import time
import argparse
import configparser
import requests
import hashlib
import concurrent.futures
from bs4 import BeautifulSoup

def load_config(config_file):
    config = configparser.ConfigParser()
    # Default settings
    config['Brave'] = {
        'requests_per_second': '1',
        'count': '10',
        'safesearch': 'moderate',
        'freshness': '',
        'max_retries': '2'
    }
    config['Perplexity'] = {
        'model': 'perplexity/sonar',
        'citations': 'true',
        'temperature': '0.1',
        'max_tokens': '1024',
        'max_retries': '2'
    }
    config['Fetch'] = {
        'flaresolverr_url': '',
        'flaresolverr_timeout': '60000',
        'flaresolverr_mode': 'fallback'
    }

    if os.path.exists(config_file):
        config.read(config_file)
    return config

def get_cache_dir():
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "ccsearch")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

def get_cache_key(query, engine, offset):
    key_string = f"{query}_{engine}_{offset}"
    return hashlib.md5(key_string.encode('utf-8')).hexdigest() + ".json"

def read_from_cache(query, engine, offset, ttl_minutes):
    cache_file = os.path.join(get_cache_dir(), get_cache_key(query, engine, offset))
    if not os.path.exists(cache_file):
        return None

    file_age = time.time() - os.path.getmtime(cache_file)
    if file_age > (ttl_minutes * 60):
        return None # Cache expired

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None # Return None if cache file is corrupted

def write_to_cache(query, engine, offset, result):
    cache_file = os.path.join(get_cache_dir(), get_cache_key(query, engine, offset))
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to write to cache: {e}\n")

# ---------------------------------------------------------------------------
# Semantic cache (optional — requires fastembed)
# ---------------------------------------------------------------------------
_embedding_model = None

def _get_embedding_model():
    """Lazily load the fastembed TextEmbedding model. Returns None if unavailable."""
    global _embedding_model
    if _embedding_model is None:
        try:
            from fastembed import TextEmbedding
            sys.stderr.write("[ccsearch] Loading embedding model (BAAI/bge-small-en-v1.5)...\n")
            _embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        except ImportError:
            sys.stderr.write("Warning: fastembed not installed — semantic cache disabled. Run: pip install fastembed\n")
            _embedding_model = False  # sentinel: don't retry import
    return _embedding_model if _embedding_model is not False else None

def _compute_embedding(text):
    """Return embedding as list[float], or None if fastembed unavailable."""
    model = _get_embedding_model()
    if model is None:
        return None
    try:
        return next(model.embed([text])).tolist()
    except Exception as e:
        sys.stderr.write(f"Warning: embedding failed: {e}\n")
        return None

def _cosine_sim(a, b):
    """Pure-Python cosine similarity between two equal-length float lists."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0

def _semantic_index_path():
    return os.path.join(get_cache_dir(), "semantic_index.json")

def _load_semantic_index():
    path = _semantic_index_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_semantic_index(index):
    try:
        with open(_semantic_index_path(), "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write(f"Warning: could not save semantic index: {e}\n")

def read_from_semantic_cache(query, engine, offset, ttl_minutes, threshold):
    """Return (cached_result, similarity) or (None, 0.0) when no semantic match found."""
    index = _load_semantic_index()
    if not index:
        return None, 0.0

    q_emb = _compute_embedding(query)
    if q_emb is None:
        return None, 0.0

    best_key, best_sim = None, -1.0
    cache_dir = get_cache_dir()
    for key, meta in index.items():
        if meta.get("engine") != engine or meta.get("offset") != offset:
            continue
        cache_file = os.path.join(cache_dir, key + ".json")
        if not os.path.exists(cache_file):
            continue
        if time.time() - os.path.getmtime(cache_file) > ttl_minutes * 60:
            continue
        emb = meta.get("embedding")
        if not emb:
            continue
        sim = _cosine_sim(q_emb, emb)
        if sim > best_sim:
            best_sim, best_key = sim, key

    if best_key and best_sim >= threshold:
        cache_file = os.path.join(cache_dir, best_key + ".json")
        try:
            with open(cache_file, encoding="utf-8") as f:
                result = json.load(f)
            return result, round(best_sim, 4)
        except Exception:
            pass

    return None, 0.0

def update_semantic_index(query, engine, offset, cache_key_filename):
    """Compute and store the query embedding in the semantic index."""
    emb = _compute_embedding(query)
    if emb is None:
        return
    key = cache_key_filename.replace(".json", "")
    index = _load_semantic_index()
    index[key] = {"query": query, "engine": engine, "offset": offset, "embedding": emb}
    _save_semantic_index(index)

# ---------------------------------------------------------------------------

def retry_request(method, url, max_retries, **kwargs):
    """Request wrapper with a simple Exponential Backoff mechanism"""
    for attempt in range(max_retries + 1):
        try:
            if method.upper() == 'GET':
                response = requests.get(url, **kwargs)
            else:
                response = requests.post(url, **kwargs)
            response.raise_for_status()
            return response
        except (requests.exceptions.RequestException) as e:
            # Avoid retrying standard HTTP 4xx client errors (unless it's 429 Too Many Requests)
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    raise e
            if attempt < max_retries:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s...
                continue
            raise e

def perform_brave_search(query, api_key, config, offset=None):
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key
    }
    count = config.getint('Brave', 'count', fallback=10)
    params = {"q": query, "count": count}

    safesearch = config.get('Brave', 'safesearch', fallback='moderate').lower()
    if safesearch in ['off', 'moderate', 'strict']:
        params['safesearch'] = safesearch

    freshness = config.get('Brave', 'freshness', fallback='').lower()
    if freshness in ['pd', 'pw', 'pm', 'py']:
        params['freshness'] = freshness

    if offset is not None:
        params['offset'] = offset

    # Handle rate limiting
    rps = config.getfloat('Brave', 'requests_per_second', fallback=1.0)
    if rps > 0:
        time.sleep(1.0 / rps)

    max_retries = config.getint('Brave', 'max_retries', fallback=2)
    response = retry_request('GET', url, max_retries, headers=headers, params=params, timeout=(10, 30))
    data = response.json()

    results = []
    if 'web' in data and 'results' in data['web']:
        for item in data['web']['results']:
            results.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "description": item.get("description")
            })
    return {"engine": "brave", "query": query, "results": results}

def perform_perplexity_search(query, api_key, config):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/anthropics/claude-code",
        "X-Title": "ccsearch"
    }

    model = config.get('Perplexity', 'model', fallback='perplexity/sonar')
    include_citations = config.getboolean('Perplexity', 'citations', fallback=True)
    temperature = config.getfloat('Perplexity', 'temperature', fallback=0.1)
    max_tokens = config.getint('Perplexity', 'max_tokens', fallback=1024)

    system_prompt = "You are a helpful search assistant. Please provide accurate answers and cite your sources."
    if include_citations:
         system_prompt += " Include markdown citations [1], [2] referencing the URLs you used."

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query}
        ],
        "temperature": temperature,
        "max_tokens": max_tokens
    }

    max_retries = config.getint('Perplexity', 'max_retries', fallback=2)
    response = retry_request('POST', url, max_retries, headers=headers, json=payload, timeout=(10, 60))
    data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        content = "No response content found."

    return {
        "engine": "perplexity",
        "model": model,
        "query": query,
        "answer": content
    }

def perform_both_search(query, brave_api_key, perplexity_api_key, config, offset=None):
    """Run both Brave and Perplexity searches concurrently and merge results"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_brave = executor.submit(perform_brave_search, query, brave_api_key, config, offset)
        future_perplexity = executor.submit(perform_perplexity_search, query, perplexity_api_key, config)

        try:
            brave_result = future_brave.result()
        except Exception as e:
            sys.stderr.write(f"Warning: Brave search failed during merged request: {e}\n")
            brave_result = {"engine": "brave", "query": query, "results": [], "error": str(e)}

        try:
            perplexity_result = future_perplexity.result()
        except Exception as e:
            sys.stderr.write(f"Warning: Perplexity search failed during merged request: {e}\n")
            perplexity_result = {"engine": "perplexity", "model": config.get('Perplexity', 'model', fallback='perplexity/sonar'), "query": query, "answer": "", "error": str(e)}

    return {
        "engine": "both",
        "query": query,
        "brave_results": brave_result.get("results", []),
        "perplexity_answer": perplexity_result.get("answer", "")
    }

FETCH_HEADERS={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5"
}

CLOUDFLARE_INDICATORS=[
    "Checking your browser",
    "cf-browser-verification",
    "challenge-platform"
]

def _clean_html(html):
    """Parse HTML and extract clean text content. Returns (title, cleanText)."""
    soup=BeautifulSoup(html, 'html.parser')
    title=soup.title.string.strip() if soup.title and soup.title.string else "No Title"
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.extract()
    text=soup.get_text(separator='\n')
    lines=(line.strip() for line in text.splitlines())
    chunks=(phrase.strip() for line in lines for phrase in line.split("  "))
    cleanText='\n'.join(chunk for chunk in chunks if chunk)
    return title, cleanText

def _detect_cloudflare(response):
    """Check if an HTTP response contains Cloudflare challenge indicators."""
    responseText=response.text
    if '<title>Just a moment...</title>' in responseText:
        return True
    for indicator in CLOUDFLARE_INDICATORS:
        if indicator in responseText:
            return True
    if len(response.content)<1024 and response.headers.get('cf-ray'):
        return True
    return False

def _simple_fetch(url, maxRetries=2):
    """Fetch a webpage using bare requests.get()."""
    return retry_request('GET', url, maxRetries, headers=FETCH_HEADERS, timeout=(10, 30))

def _flaresolverr_fetch(url, flaresolverrUrl, timeout=60000):
    """Fetch a webpage through FlareSolverr proxy."""
    payload={"cmd": "request.get", "url": url, "maxTimeout": timeout}
    httpTimeout=(10, timeout/1000+10)
    response=requests.post(flaresolverrUrl, json=payload, timeout=httpTimeout)
    data=response.json()
    if data.get("status")!="ok":
        raise Exception(f"FlareSolverr error: {data.get('message', 'Unknown error')}")
    return data["solution"]["response"]

def perform_fetch(url, config):
    """Fetch and extract clean text from a webpage with optional FlareSolverr fallback."""
    flaresolverrUrl=config.get('Fetch', 'flaresolverr_url', fallback='').strip()
    flaresolverrTimeout=config.getint('Fetch', 'flaresolverr_timeout', fallback=60000)
    flaresolverrMode=config.get('Fetch', 'flaresolverr_mode', fallback='fallback').strip().lower()
    maxRetries=config.getint('Brave', 'max_retries', fallback=2)
    useAlways=flaresolverrMode=="always" and flaresolverrUrl
    canFallback=flaresolverrMode=="fallback" and flaresolverrUrl

    # Always mode: skip simple fetch, go directly to FlareSolverr
    if useAlways:
        try:
            sys.stderr.write("[ccsearch] Using FlareSolverr (always mode)...\n")
            html=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
            sys.stderr.write("[ccsearch] FlareSolverr solved challenge successfully.\n")
            title, cleanText=_clean_html(html)
            return {"engine": "fetch", "url": url, "title": title, "content": cleanText, "fetched_via": "flaresolverr"}
        except Exception as e:
            return {"engine": "fetch", "url": url, "error": f"FlareSolverr failed: {e}"}

    # Try simple fetch first
    simpleFetchErr=None
    response=None
    try:
        response=_simple_fetch(url, maxRetries)
    except Exception as e:
        simpleFetchErr=e

    # Simple fetch succeeded — check for Cloudflare challenge
    if response is not None:
        if canFallback and _detect_cloudflare(response):
            sys.stderr.write("[ccsearch] Cloudflare detected, falling back to FlareSolverr...\n")
            try:
                html=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
                sys.stderr.write("[ccsearch] FlareSolverr solved challenge successfully.\n")
                title, cleanText=_clean_html(html)
                return {"engine": "fetch", "url": url, "title": title, "content": cleanText, "fetched_via": "flaresolverr"}
            except Exception as flareErr:
                return {"engine": "fetch", "url": url, "error": f"Cloudflare detected. Direct fetch blocked | FlareSolverr also failed: {flareErr}"}
        # No Cloudflare or no fallback configured — use direct response
        title, cleanText=_clean_html(response.content)
        return {"engine": "fetch", "url": url, "title": title, "content": cleanText, "fetched_via": "direct"}

    # Simple fetch failed — try FlareSolverr fallback
    if canFallback:
        sys.stderr.write(f"[ccsearch] Direct fetch failed ({simpleFetchErr}), falling back to FlareSolverr...\n")
        try:
            html=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
            sys.stderr.write("[ccsearch] FlareSolverr solved challenge successfully.\n")
            title, cleanText=_clean_html(html)
            return {"engine": "fetch", "url": url, "title": title, "content": cleanText, "fetched_via": "flaresolverr"}
        except Exception as flareErr:
            return {"engine": "fetch", "url": url, "error": f"Direct fetch failed: {simpleFetchErr} | FlareSolverr also failed: {flareErr}"}

    return {"engine": "fetch", "url": url, "error": str(simpleFetchErr)}

def main():
    parser = argparse.ArgumentParser(description="Web Search Utility for LLMs using Brave or Perplexity.")
    parser.add_argument("query", help="The search query, keyword, or URL (for fetch engine)")
    parser.add_argument("-e", "--engine", choices=["brave", "perplexity", "both", "fetch"], required=True, help="Search engine to use (brave, perplexity, both, or fetch)")
    parser.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini"), help="Path to config INI file")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format: json or text")
    parser.add_argument("--offset", type=int, default=None, help="Pagination offset (for Brave search only)")
    parser.add_argument("--cache", action="store_true", help="Enable results caching")
    parser.add_argument("--cache-ttl", type=int, default=10, help="Cache Time-To-Live in minutes (default: 10)")
    parser.add_argument("--semantic-cache", action="store_true", help="Enable semantic similarity cache via fastembed (implies --cache)")
    parser.add_argument("--semantic-threshold", type=float, default=0.9, help="Cosine similarity threshold for semantic cache (default: 0.9)")
    parser.add_argument("--flaresolverr", action="store_true", help="Force FlareSolverr mode for fetch engine (overrides config flaresolverr_mode to 'always')")

    args = parser.parse_args()
    config = load_config(args.config)

    try:
        result = None
        use_cache = args.cache or args.semantic_cache
        # Semantic cache only makes sense for text-query engines, not URL fetch
        use_semantic = args.semantic_cache and args.engine != "fetch"

        # 1. Try exact cache hit
        if use_cache:
            result = read_from_cache(args.query, args.engine, args.offset, args.cache_ttl)
            if result:
                result["_from_cache"] = True
                # Ensure the semantic index has an entry even for exact-cache hits,
                # so future paraphrased queries can still find it semantically.
                if use_semantic:
                    cache_key = get_cache_key(args.query, args.engine, args.offset)
                    index = _load_semantic_index()
                    key = cache_key.replace(".json", "")
                    if key not in index:
                        update_semantic_index(args.query, args.engine, args.offset, cache_key)

        # 2. Try semantic cache hit (on exact miss)
        if not result and use_semantic:
            result, sim = read_from_semantic_cache(
                args.query, args.engine, args.offset, args.cache_ttl, args.semantic_threshold
            )
            if result:
                result["_from_cache"] = True
                result["_semantic_similarity"] = sim

        if not result:
            if args.engine == "brave":
                api_key = os.environ.get("BRAVE_API_KEY")
                if not api_key:
                    sys.stderr.write("ERROR: BRAVE_API_KEY environment variable not found.\nPlease set it using: export BRAVE_API_KEY='your_key'\n")
                    sys.exit(1)
                result = perform_brave_search(args.query, api_key, config, offset=args.offset)

            elif args.engine == "perplexity":
                api_key = os.environ.get("OPENROUTER_API_KEY")
                if not api_key:
                    sys.stderr.write("ERROR: OPENROUTER_API_KEY environment variable not found.\nPlease set it using: export OPENROUTER_API_KEY='your_key'\n")
                    sys.exit(1)
                result = perform_perplexity_search(args.query, api_key, config)

            elif args.engine == "both":
                brave_api_key = os.environ.get("BRAVE_API_KEY")
                perplexity_api_key = os.environ.get("OPENROUTER_API_KEY")
                if not brave_api_key or not perplexity_api_key:
                    sys.stderr.write("ERROR: Both BRAVE_API_KEY and OPENROUTER_API_KEY are required for 'both' engine.\n")
                    sys.exit(1)
                result = perform_both_search(args.query, brave_api_key, perplexity_api_key, config, offset=args.offset)

            elif args.engine == "fetch":
                if not args.query.startswith("http"):
                    sys.stderr.write("ERROR: For 'fetch' engine, the query must be a valid HTTP or HTTPS URL.\n")
                    sys.exit(1)
                if args.flaresolverr:
                    config.set('Fetch', 'flaresolverr_mode', 'always')
                    if not config.get('Fetch', 'flaresolverr_url', fallback='').strip():
                        sys.stderr.write("WARNING: --flaresolverr flag set but no flaresolverr_url configured in config.ini.\n")
                result = perform_fetch(args.query, config)

            if use_cache:
                cache_key = get_cache_key(args.query, args.engine, args.offset)
                write_to_cache(args.query, args.engine, args.offset, result)
                if use_semantic:
                    update_semantic_index(args.query, args.engine, args.offset, cache_key)

        if args.format == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            if result.get("_from_cache"):
                print(f"[Returning Cached Result - {args.cache_ttl}min TTL]\n")

            if args.engine == "brave":
                print(f"Brave Search Results for: {args.query}\n")
                for idx, res in enumerate(result["results"], 1):
                    print(f"{idx}. {res['title']}\n   URL: {res['url']}\n   {res['description']}\n")
            elif args.engine == "perplexity":
                print(f"Perplexity Search Answer ({result.get('model', 'unknown')}):\n")
                print(result["answer"])
            elif args.engine == "both":
                print(f"--- Synthesized Answer (Perplexity) ---\n")
                print(result["perplexity_answer"])
                print(f"\n\n--- Source Reference Links (Brave) ---\n")
                for idx, res in enumerate(result["brave_results"], 1):
                    print(f"{idx}. {res['title']}\n   URL: {res['url']}\n   {res['description']}\n")
            elif args.engine == "fetch":
                if "error" in result:
                    print(f"Error fetching URL: {result['error']}\n")
                else:
                    print(f"--- Fetched Content: {result['title']} ---\n")
                    print(f"URL: {result['url']}\n")
                    print(result["content"])

    except requests.exceptions.HTTPError as e:
        sys.stderr.write(f"HTTP Error: {e}\n")
        # Attempt to print detailed error payload if available
        if getattr(e, 'response', None) is not None:
             sys.stderr.write(f"Response: {e.response.text}\n")
        sys.exit(1)
    except requests.exceptions.Timeout as e:
        sys.stderr.write(f"Timeout Error: Request took too long to respond.\n{e}\n")
        sys.exit(1)
    except Exception as e:
        sys.stderr.write(f"Unexpected error: {e}\n")
        sys.exit(1)

if __name__ == "__main__":
    main()