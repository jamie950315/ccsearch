#!/usr/bin/env python3
"""
ccsearch - A CLI Web Search Utility for LLMs and Human users.
Supports Brave Search API and Perplexity (via OpenRouter).
"""
import os
import sys
import json
import html as html_lib
import time
import re
import io
import argparse
import configparser
import importlib.util
import requests
import hashlib
import tempfile
import concurrent.futures
import threading
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup, NavigableString, Tag
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI=True
except ImportError:
    HAS_CURL_CFFI=False

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
    config['LLMContext'] = {
        'count': '20',
        'maximum_number_of_tokens': '8192',
        'maximum_number_of_urls': '20',
        'context_threshold_mode': 'balanced',
        'freshness': '',
        'max_retries': '2'
    }
    config['Fetch'] = {
        'flaresolverr_url': '',
        'flaresolverr_timeout': '60000',
        'flaresolverr_mode': 'fallback'
    }
    config['Batch'] = {
        'max_workers': '4'
    }

    if os.path.exists(config_file):
        config.read(config_file)
    return config

def load_api_key(api_key_file, env_var="CCSEARCH_API_KEY", create_if_missing=False):
    """Load a shared API key from env or disk, optionally generating it on first run."""
    api_key = os.environ.get(env_var, "").strip()
    if api_key:
        return api_key

    if os.path.exists(api_key_file):
        with open(api_key_file, "r", encoding="utf-8") as f:
            return f.read().strip()

    if not create_if_missing:
        return ""

    import secrets
    api_key = secrets.token_urlsafe(32)
    with open(api_key_file, "w", encoding="utf-8") as f:
        f.write(api_key)
    os.chmod(api_key_file, 0o600)
    return api_key

def mask_secret(secret, prefix=4, suffix=4):
    """Return a masked representation of a secret for safe logging."""
    if not secret:
        return ""
    if len(secret) <= prefix + suffix:
        return "*" * len(secret)
    return f"{secret[:prefix]}...{secret[-suffix:]}"

def get_cache_dir():
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "ccsearch")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

TRACKING_QUERY_PREFIXES=("utm_",)
TRACKING_QUERY_KEYS={
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "ref_src",
    "ref_url",
    "igshid",
    "si",
}

OPTIONAL_DEPENDENCIES={
    "curl_cffi": "TLS impersonation for fetch",
    "fastembed": "semantic cache embeddings",
    "markitdown": "binary document to Markdown conversion",
    "mcp": "MCP server runtime",
}

VALID_ENGINES=("brave", "perplexity", "both", "fetch", "llm-context")
_cache_lock = threading.Lock()
_semantic_index_lock = threading.Lock()

ENGINE_DETAILS={
    "brave": {
        "description": "Brave Web Search",
        "requires": "BRAVE_API_KEY",
        "category": "search",
        "supports_offset": True,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": True,
        "supports_result_limit": True,
    },
    "perplexity": {
        "description": "Perplexity via OpenRouter",
        "requires": "OPENROUTER_API_KEY",
        "category": "answer",
        "supports_offset": False,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": False,
        "supports_result_limit": False,
    },
    "both": {
        "description": "Brave + Perplexity combined",
        "requires": "BRAVE_API_KEY + OPENROUTER_API_KEY",
        "category": "hybrid",
        "supports_offset": True,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": True,
        "supports_result_limit": True,
    },
    "llm-context": {
        "description": "Brave LLM Context API (smart chunks)",
        "requires": "BRAVE_SEARCH_API_KEY or BRAVE_API_KEY",
        "category": "context",
        "supports_offset": False,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": True,
        "supports_result_limit": True,
    },
    "fetch": {
        "description": "Fetch and extract text from a URL",
        "requires": None,
        "category": "fetch",
        "supports_offset": False,
        "supports_semantic_cache": False,
        "supports_flaresolverr": True,
        "supports_host_filter": False,
        "supports_result_limit": False,
    },
}

HOST_FILTER_ENGINES={"brave", "both", "llm-context"}
RESULT_LIMIT_ENGINES={"brave", "both", "llm-context"}

def normalize_fetch_cache_url(url):
    """Normalize fetch URLs so cache hits survive tracking params and query-order changes."""
    parsed=urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url

    scheme=parsed.scheme.lower()
    hostname=(parsed.hostname or "").lower()
    port=parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc=f"{hostname}:{port}"
    else:
        netloc=hostname

    path=parsed.path or "/"
    if path != "/":
        path=re.sub(r"/{2,}", "/", path).rstrip("/") or "/"

    filtered_params=[]
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower_key=key.lower()
        if lower_key.startswith(TRACKING_QUERY_PREFIXES) or lower_key in TRACKING_QUERY_KEYS:
            continue
        filtered_params.append((key, value))
    filtered_params.sort()
    query=urlencode(filtered_params, doseq=True)

    return urlunparse((scheme, netloc, path, "", query, ""))

def normalize_cache_query(query, engine):
    """Normalize cache input on a per-engine basis."""
    if engine == "fetch":
        return normalize_fetch_cache_url(query)
    return re.sub(r"\s+", " ", str(query)).strip()

def get_cache_key(query, engine, offset):
    normalized_query = normalize_cache_query(query, engine)
    key_string = f"{normalized_query}_{engine}_{offset}"
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
            result=json.load(f)
            if engine == "fetch" and isinstance(result, dict):
                result["url"]=query
            return result
    except Exception:
        return None # Return None if cache file is corrupted

def write_to_cache(query, engine, offset, result):
    cache_file = os.path.join(get_cache_dir(), get_cache_key(query, engine, offset))
    try:
        with _cache_lock:
            target_dir = os.path.dirname(cache_file) or get_cache_dir()
            fd, temp_path = tempfile.mkstemp(prefix="cache-", suffix=".json", dir=target_dir)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False)
                os.replace(temp_path, cache_file)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
    except Exception as e:
        sys.stderr.write(f"Warning: Failed to write to cache: {e}\n")

def backfill_semantic_index(query, engine, offset):
    """Ensure an exact cache hit can still be reused by future semantic lookups."""
    cache_key = get_cache_key(query, engine, offset)
    key = cache_key.replace(".json", "")
    with _semantic_index_lock:
        index = _load_semantic_index()
        if key in index:
            return
    update_semantic_index(query, engine, offset, cache_key)

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
        target_path = _semantic_index_path()
        target_dir = os.path.dirname(target_path) or get_cache_dir()
        os.makedirs(target_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix="semantic-index-", suffix=".json", dir=target_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False)
            os.replace(temp_path, target_path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
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
    with _semantic_index_lock:
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

def _normalize_inline_spacing(text):
    """Collapse awkward whitespace around inline punctuation."""
    normalized=str(text or "").strip()
    if not normalized:
        return normalized
    return re.sub(r"\s+([,.;:!?])", r"\1", normalized)

def _clean_api_text(text, preserve_newlines=False):
    """Normalize API-returned text by stripping markup and decoding entities."""
    if text is None:
        return None
    separator='\n' if preserve_newlines else ' '
    plain=BeautifulSoup(str(text), 'html.parser').get_text(separator=separator, strip=True)
    plain=html_lib.unescape(plain)
    if preserve_newlines:
        return _normalize_block_text(plain)
    return _normalize_inline_spacing(re.sub(r"\s+", " ", plain))

def _normalize_result_url(url):
    """Normalize result URLs for deduplication without changing returned values."""
    if not url:
        return None
    return normalize_fetch_cache_url(url)

def _dedupe_result_items(items):
    """Deduplicate result items by normalized URL while preserving order."""
    deduped=[]
    seen_urls=set()
    for item in items:
        normalized=_normalize_result_url(item.get("url"))
        if normalized and normalized in seen_urls:
            continue
        if normalized:
            seen_urls.add(normalized)
        deduped.append(item)
    return deduped

def _annotate_rank(items):
    """Attach 1-based rank to result items after deduplication."""
    ranked=[]
    for idx, item in enumerate(items, 1):
        enriched=dict(item)
        enriched["rank"]=idx
        ranked.append(enriched)
    return ranked

def _collect_hostnames(items, field="hostname", limit=20):
    """Return a stable, deduplicated list of hostnames from structured items."""
    hosts=[]
    seen=set()
    for item in items or []:
        raw_value=item.get(field) if isinstance(item, dict) else None
        if field == "url" and raw_value:
            host=_normalize_hostname(urlparse(str(raw_value)).hostname)
        else:
            host=_normalize_hostname(raw_value)
        if not host or host in seen:
            continue
        seen.add(host)
        hosts.append(host)
        if len(hosts) >= limit:
            break
    return hosts

def _normalize_host_filters(hosts):
    """Normalize host filter input from CLI/API/MCP forms into a deduplicated list."""
    if hosts in (None, "", []):
        return []

    if isinstance(hosts, str):
        raw_values=re.split(r"[\s,]+", hosts)
    elif isinstance(hosts, (list, tuple, set)):
        raw_values=[]
        for value in hosts:
            raw_values.extend(re.split(r"[\s,]+", str(value)))
    else:
        raise ValueError("Host filters must be provided as a string or list of strings.")

    normalized=[]
    seen=set()
    for raw in raw_values:
        value=(raw or "").strip()
        if not value:
            continue
        parsed=urlparse(value if "://" in value else f"https://{value}")
        host=_normalize_hostname(parsed.hostname)
        if not host:
            raise ValueError(f"Invalid host filter value: {value}")
        if host in seen:
            continue
        seen.add(host)
        normalized.append(host)
    return normalized

def _result_item_hostname(item):
    """Resolve hostname from a structured result item."""
    if not isinstance(item, dict):
        return None
    hostname=_normalize_hostname(item.get("hostname"))
    if hostname:
        return hostname
    return _normalize_hostname(urlparse(item.get("url") or "").hostname)

def _filter_result_items_by_host(items, include_hosts=None, exclude_hosts=None):
    """Filter result items by normalized hostnames while preserving order."""
    include_set=set(include_hosts or [])
    exclude_set=set(exclude_hosts or [])
    filtered=[]
    removed=0
    for item in items or []:
        host=_result_item_hostname(item)
        if include_set and host not in include_set:
            removed+=1
            continue
        if host and host in exclude_set:
            removed+=1
            continue
        filtered.append(dict(item) if isinstance(item, dict) else item)
    return filtered, removed

def _apply_host_filters(result, engine, include_hosts=None, exclude_hosts=None):
    """Apply host filters to search-style result payloads after cache/engine execution."""
    if not include_hosts and not exclude_hosts:
        return result
    if not isinstance(result, dict):
        return result

    filtered=dict(result)
    host_filtering={
        "include_hosts": list(include_hosts or []),
        "exclude_hosts": list(exclude_hosts or []),
        "removed_results": 0,
    }

    if engine == "brave":
        items, removed=_filter_result_items_by_host(filtered.get("results", []), include_hosts, exclude_hosts)
        filtered["results"]=_annotate_rank(items)
        filtered["result_count"]=len(filtered["results"])
        filtered["result_hosts"]=_collect_hostnames(filtered["results"])
        filtered["result_host_count"]=len(filtered["result_hosts"])
        host_filtering["removed_results"]=removed
    elif engine == "llm-context":
        items, removed=_filter_result_items_by_host(filtered.get("results", []), include_hosts, exclude_hosts)
        filtered["results"]=_annotate_rank(items)
        filtered["result_count"]=len(filtered["results"])
        kept_urls={item.get("url") for item in filtered["results"] if isinstance(item, dict) and item.get("url")}
        filtered["sources"]={
            url: meta for url, meta in (filtered.get("sources") or {}).items()
            if url in kept_urls
        }
        filtered["source_count"]=len(filtered["sources"])
        filtered["result_hosts"]=_collect_hostnames(filtered["results"])
        filtered["result_host_count"]=len(filtered["result_hosts"])
        host_filtering["removed_results"]=removed
    elif engine == "both":
        items, removed=_filter_result_items_by_host(filtered.get("brave_results", []), include_hosts, exclude_hosts)
        filtered["brave_results"]=_annotate_rank(items)
        filtered["brave_result_count"]=len(filtered["brave_results"])
        filtered["brave_result_hosts"]=_collect_hostnames(filtered["brave_results"])
        filtered["brave_result_host_count"]=len(filtered["brave_result_hosts"])
        host_filtering["removed_results"]=removed
    else:
        return filtered

    filtered["host_filtering"]=host_filtering
    return filtered

def _apply_result_limit(result, engine, result_limit=None):
    """Trim search-style result payloads to a top-N result set."""
    if result_limit is None:
        return result
    if not isinstance(result, dict):
        return result

    limited=dict(result)
    result_limiting={
        "limit": result_limit,
        "removed_results": 0,
    }

    if engine == "brave":
        items=list(limited.get("results", []))
        trimmed=items[:result_limit]
        result_limiting["removed_results"]=max(0, len(items) - len(trimmed))
        limited["results"]=_annotate_rank(trimmed)
        limited["result_count"]=len(limited["results"])
        limited["result_hosts"]=_collect_hostnames(limited["results"])
        limited["result_host_count"]=len(limited["result_hosts"])
    elif engine == "llm-context":
        items=list(limited.get("results", []))
        trimmed=items[:result_limit]
        result_limiting["removed_results"]=max(0, len(items) - len(trimmed))
        limited["results"]=_annotate_rank(trimmed)
        limited["result_count"]=len(limited["results"])
        kept_urls={item.get("url") for item in limited["results"] if isinstance(item, dict) and item.get("url")}
        limited["sources"]={
            url: meta for url, meta in (limited.get("sources") or {}).items()
            if url in kept_urls
        }
        limited["source_count"]=len(limited["sources"])
        limited["result_hosts"]=_collect_hostnames(limited["results"])
        limited["result_host_count"]=len(limited["result_hosts"])
    elif engine == "both":
        items=list(limited.get("brave_results", []))
        trimmed=items[:result_limit]
        result_limiting["removed_results"]=max(0, len(items) - len(trimmed))
        limited["brave_results"]=_annotate_rank(trimmed)
        limited["brave_result_count"]=len(limited["brave_results"])
        limited["brave_result_hosts"]=_collect_hostnames(limited["brave_results"])
        limited["brave_result_host_count"]=len(limited["brave_result_hosts"])
    else:
        return limited

    limited["result_limiting"]=result_limiting
    return limited

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
            result_url=item.get("url")
            results.append({
                "title": _clean_api_text(item.get("title")),
                "url": result_url,
                "description": _clean_api_text(item.get("description")),
                "hostname": urlparse(result_url).hostname if result_url else None,
            })
    results=_annotate_rank(_dedupe_result_items(results))
    result={"engine": "brave", "query": query, "offset": offset, "result_count": len(results), "results": results}
    hosts=_collect_hostnames(results)
    if hosts:
        result["result_hosts"]=hosts
        result["result_host_count"]=len(hosts)
    return result

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
    content = html_lib.unescape(content)
    citations = _extract_perplexity_citations(data)

    result = {
        "engine": "perplexity",
        "model": model,
        "query": query,
        "answer": content
    }
    if citations:
        result["citations"] = citations
        hosts=_collect_hostnames(citations, field="url")
        if hosts:
            result["citation_hosts"]=hosts
            result["citation_host_count"]=len(hosts)
    return result

def _extract_perplexity_citations(data):
    """Normalize citation-like payloads from OpenRouter/Perplexity responses."""
    citations=[]
    seen=set()
    for field in ("citations", "references", "sources"):
        raw=data.get(field)
        if not raw:
            continue
        entries=raw if isinstance(raw, list) else [raw]
        for entry in entries:
            if isinstance(entry, str):
                url=entry.strip()
                title=None
            elif isinstance(entry, dict):
                url=(
                    entry.get("url")
                    or entry.get("link")
                    or entry.get("source")
                    or entry.get("uri")
                    or ""
                )
                title=entry.get("title") or entry.get("name")
                url=str(url).strip()
                title=str(title).strip() if title else None
            else:
                continue
            if not url:
                continue
            key=_normalize_result_url(url) or url
            if key in seen:
                continue
            seen.add(key)
            citation={"url": url}
            if title:
                citation["title"]=title
            citations.append(citation)
    return citations

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

    brave_results=brave_result.get("results", [])
    result = {
        "engine": "both",
        "query": query,
        "offset": offset,
        "brave_result_count": len(brave_results),
        "brave_results": brave_results,
        "perplexity_answer": perplexity_result.get("answer", "")
    }
    if perplexity_result.get("citations"):
        result["perplexity_citations"] = perplexity_result["citations"]
    if brave_result.get("error"):
        result["brave_error"] = brave_result["error"]
    if perplexity_result.get("error"):
        result["perplexity_error"] = perplexity_result["error"]
    brave_hosts=_collect_hostnames(brave_results)
    if brave_hosts:
        result["brave_result_hosts"]=brave_hosts
        result["brave_result_host_count"]=len(brave_hosts)
    citation_hosts=_collect_hostnames(result.get("perplexity_citations", []), field="url")
    if citation_hosts:
        result["perplexity_citation_hosts"]=citation_hosts
        result["perplexity_citation_host_count"]=len(citation_hosts)
    result["has_partial_failure"] = bool(result.get("brave_error") or result.get("perplexity_error"))
    return result

def perform_llm_context_search(query, api_key, config):
    url = "https://api.search.brave.com/res/v1/llm/context"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key
    }

    count = config.getint('LLMContext', 'count', fallback=20)
    max_tokens = config.getint('LLMContext', 'maximum_number_of_tokens', fallback=8192)
    max_urls = config.getint('LLMContext', 'maximum_number_of_urls', fallback=20)
    threshold_mode = config.get('LLMContext', 'context_threshold_mode', fallback='balanced').lower()

    params = {
        "q": query,
        "count": count,
        "maximum_number_of_tokens": max_tokens,
        "maximum_number_of_urls": max_urls,
    }

    if threshold_mode in ['strict', 'balanced', 'lenient', 'disabled']:
        params['context_threshold_mode'] = threshold_mode

    freshness = config.get('LLMContext', 'freshness', fallback='').lower()
    if freshness in ['pd', 'pw', 'pm', 'py']:
        params['freshness'] = freshness

    # Reuse Brave rate limiting since it's the same API key / subscription
    rps = config.getfloat('Brave', 'requests_per_second', fallback=1.0)
    if rps > 0:
        time.sleep(1.0 / rps)

    max_retries = config.getint('LLMContext', 'max_retries', fallback=2)
    response = retry_request('GET', url, max_retries, headers=headers, params=params, timeout=(10, 30))
    data = response.json()

    grounding = data.get("grounding", {})
    sources = data.get("sources", {})

    results = []
    for item in grounding.get("generic", []):
        result_url=item.get("url")
        source_meta=sources.get(result_url, {}) if result_url else {}
        results.append({
            "url": result_url,
            "title": _clean_api_text(item.get("title")),
            "hostname": source_meta.get("hostname") or (urlparse(result_url).hostname if result_url else None),
            "age": source_meta.get("age"),
            "snippets": [
                cleaned
                for snippet in item.get("snippets", [])
                for cleaned in [_clean_api_text(snippet, preserve_newlines=True)]
                if cleaned
            ]
        })

    results=_annotate_rank(_dedupe_result_items(results))
    result={
        "engine": "llm-context",
        "query": query,
        "result_count": len(results),
        "source_count": len(sources),
        "results": results,
        "sources": sources,
    }
    hosts=_collect_hostnames(results)
    if hosts:
        result["result_hosts"]=hosts
        result["result_host_count"]=len(hosts)
    return result

FETCH_HEADERS={
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "max-age=0",
    "Sec-Ch-Ua": '"Chromium";v="146", "Google Chrome";v="146", "Not:A-Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.google.com/",
}

CLOUDFLARE_INDICATORS=[
    "Checking your browser",
    "cf-browser-verification",
    "challenge-platform"
]

MARKITDOWN_MIME_TO_EXTENSIONS={
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/epub+zip": ".epub",
}

MARKITDOWN_EXTENSIONS=set(MARKITDOWN_MIME_TO_EXTENSIONS.values())

_SPA_MOUNT_POINTS=[
    'id="root"', 'id="app"', 'id="__next"', 'id="__nuxt"',
    'id="___gatsby"', 'id="svelte"', 'id="ember-application"',
    'id="react-root"', 'id="react-app"',
]

_NOISE_HINTS={
    "cookie",
    "consent",
    "newsletter",
    "subscribe",
    "subscription",
    "promo",
    "popup",
    "modal",
    "banner",
    "advert",
    "ads",
    "social-share",
    "share-bar",
    "breadcrumb",
}

_CONTENT_HINTS={
    "article",
    "content",
    "post",
    "story",
    "entry",
    "main",
    "body",
    "page",
    "text",
}

def _detect_spa_shell(raw_html, clean_text_len):
    """Detect if page is a JS-heavy SPA shell that needs headless rendering.
    Checks for empty SPA mount points and script-heavy pages with little text."""
    html=raw_html if isinstance(raw_html, str) else raw_html.decode('utf-8', errors='ignore')
    html_lower=html.lower()
    script_count=html_lower.count('<script')
    has_mount_point=False
    for mount in _SPA_MOUNT_POINTS:
        if mount in html_lower:
            has_mount_point=True
            if clean_text_len < 500:
                return True, f"SPA mount point ({mount}) with only {clean_text_len} chars"
            break

    semantic_content_hint=any(tag in html_lower for tag in ("<main", "<article", "<p", "<h1", "<h2", "<section"))
    if clean_text_len < 50:
        if script_count >= 3:
            return True, f"{script_count} script tags but only {clean_text_len} chars of text"
        if clean_text_len == 0 and has_mount_point:
            return True, "empty SPA mount point"
        if clean_text_len == 0 and not semantic_content_hint:
            return True, "empty body shell"
        return False, ""
    if script_count > 5 and clean_text_len < 200:
        return True, f"{script_count} script tags but only {clean_text_len} chars of text"
    return False, ""

def _clean_html(html):
    """Parse HTML and extract clean text content. Returns (title, cleanText)."""
    title, cleanText, _ = _extract_html_content(html)
    return title, cleanText

def _extract_html_content(html, base_url=None):
    """Parse HTML and return (title, content, chunks) with basic structure preserved."""
    soup=BeautifulSoup(html, 'html.parser')
    title=_extract_html_title(soup)

    if soup.body:
        root=BeautifulSoup(str(soup.body), 'html.parser')
    else:
        root=BeautifulSoup(str(soup), 'html.parser')

    _prune_html_noise(root)
    root=_select_content_root(root)
    blocks=_extract_content_blocks(root, base_url=base_url)
    if not blocks:
        text=root.get_text(separator='\n')
        lines=(line.strip() for line in text.splitlines())
        text_chunks=(phrase.strip() for line in lines for phrase in line.split("  "))
        cleanText='\n'.join(chunk for chunk in text_chunks if chunk)
        blocks=_chunk_text_content(cleanText)
    cleanText='\n'.join(block["text"] for block in blocks)
    return title, cleanText, blocks

def _prune_html_noise(root):
    """Remove common non-content elements before text extraction."""
    for tag in root(["script", "style", "nav", "footer", "header", "noscript", "aside", "form", "svg"]):
        tag.extract()

    for hidden in root.select("[aria-hidden='true']"):
        hidden.extract()

    for tag in root.find_all(True):
        style=(tag.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            tag.extract()
            continue

        hint_parts=[
            tag.get("id", ""),
            " ".join(tag.get("class", [])) if tag.get("class") else "",
            tag.get("role", ""),
            tag.get("aria-label", ""),
            tag.get("data-testid", ""),
        ]
        hint_blob=" ".join(part for part in hint_parts if part).lower()
        if not hint_blob:
            continue
        if not any(hint in hint_blob for hint in _NOISE_HINTS):
            continue
        if len(tag.get_text(" ", strip=True)) <= 1200:
            tag.extract()

def _node_hint_blob(node):
    """Return a normalized hint string built from common DOM attributes."""
    hint_parts=[
        node.get("id", ""),
        " ".join(node.get("class", [])) if node.get("class") else "",
        node.get("role", ""),
        node.get("aria-label", ""),
        node.get("data-testid", ""),
    ]
    return " ".join(part for part in hint_parts if part).lower()

def _score_content_candidate(node):
    """Score a node for article-likeness using text size, density, and semantic hints."""
    text=node.get_text(" ", strip=True)
    text_len=len(text)
    if text_len < 80:
        return float("-inf")

    link_text_len=sum(len(link.get_text(" ", strip=True)) for link in node.find_all("a"))
    link_density=(link_text_len / text_len) if text_len else 0.0
    paragraph_count=len(node.find_all("p"))
    heading_count=len(node.find_all(["h1", "h2", "h3"]))
    punctuation_hits=len(re.findall(r"[.!?,:;]", text))
    hint_blob=_node_hint_blob(node)

    score=float(text_len)
    score+=min(paragraph_count * 140, 700)
    score+=min(heading_count * 90, 180)
    score+=min(punctuation_hits * 8, 240)

    if node.name in {"article", "main"}:
        score+=260
    if node.get("role") == "main":
        score+=220
    if any(hint in hint_blob for hint in _CONTENT_HINTS):
        score+=180
    if any(hint in hint_blob for hint in _NOISE_HINTS):
        score-=450

    score-=link_density * text_len * 1.4
    return score

def _select_content_root(root):
    """Pick the most article-like subtree from a cleaned HTML body."""
    body=root.body or root
    candidates=[body]
    candidates.extend(body.select("main, article, [role='main'], section, div"))

    best_node=body
    best_score=_score_content_candidate(body)
    for candidate in candidates:
        score=_score_content_candidate(candidate)
        if score > best_score:
            best_node=candidate
            best_score=score
    return best_node

def _normalize_block_text(text):
    """Normalize a block of text while preserving intentional line breaks."""
    lines=[]
    for raw_line in text.splitlines():
        line=re.sub(r"\s+", " ", raw_line).strip()
        line=_normalize_inline_spacing(line)
        if line:
            lines.append(line)
    return "\n".join(lines)

def _extract_code_text(node):
    """Extract code block text while preserving intentional line breaks and indentation."""
    raw_text=node.get_text("\n")
    raw_text=raw_text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return raw_text.rstrip()

def _detect_code_language(node):
    """Best-effort language detection from common code block class names."""
    candidates=[node]
    parent=getattr(node, "parent", None)
    if isinstance(parent, Tag):
        candidates.append(parent)
    nested_code=node.find("code") if isinstance(node, Tag) and (node.name or "").lower() == "pre" else None
    if isinstance(nested_code, Tag):
        candidates.insert(0, nested_code)
    for candidate in candidates:
        classes=candidate.get("class", []) if isinstance(candidate, Tag) else []
        for class_name in classes:
            lowered=str(class_name).strip().lower()
            if not lowered:
                continue
            for prefix in ("language-", "lang-", "highlight-source-"):
                if lowered.startswith(prefix) and len(lowered) > len(prefix):
                    return lowered[len(prefix):]
            if lowered.startswith("brush:"):
                return lowered.split(":", 1)[1].split(";", 1)[0].strip() or None
    return None

def _serialize_code_block(node):
    """Serialize a code/pre node into fenced Markdown plus metadata."""
    text=_extract_code_text(node)
    if not text:
        return "", None
    language=_detect_code_language(node)
    fence=f"```{language}" if language else "```"
    return f"{fence}\n{text}\n```", language

def _normalize_hostname(hostname):
    """Normalize hostnames for lightweight same-site comparisons."""
    normalized=(hostname or "").strip().lower().rstrip(".")
    if normalized.startswith("www."):
        normalized=normalized[4:]
    return normalized or None

def _hosts_match(left, right):
    """Return True when two hostnames should be treated as the same site."""
    return bool(left and right and _normalize_hostname(left) == _normalize_hostname(right))

def _extract_links_from_node(node, base_url=None, limit=8):
    """Extract a small, deduplicated set of HTTP links from a node subtree."""
    links=[]
    seen=set()
    base_host=_normalize_hostname(urlparse(base_url).hostname) if base_url else None
    for anchor in node.find_all("a", href=True):
        href=(anchor.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:", "tel:")):
            continue
        absolute=urljoin(base_url, href) if base_url else href
        parsed=urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        text=_normalize_block_text(anchor.get_text(" ", strip=True)) or absolute
        link_host=_normalize_hostname(parsed.hostname)
        links.append({
            "text": text,
            "url": absolute,
            "hostname": link_host,
            "is_same_host": _hosts_match(base_host, link_host),
        })
        if len(links) >= limit:
            break
    return links

def _aggregate_chunk_links(chunks, limit=25):
    """Aggregate unique outbound links across chunks for page-level navigation."""
    aggregated=[]
    seen=set()
    for chunk in chunks or []:
        for link in chunk.get("links", []) or []:
            url=link.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            aggregated.append({
                "text": link.get("text") or url,
                "url": url,
                "hostname": link.get("hostname"),
                "is_same_host": bool(link.get("is_same_host")),
                "chunk_index": chunk.get("index"),
            })
            if len(aggregated) >= limit:
                return aggregated
    return aggregated

def _count_list_items(node):
    """Count direct list items for a UL/OL node."""
    return len(node.find_all("li", recursive=False))

def _table_structure(node):
    """Return lightweight structural metadata for a table node."""
    rows=[]
    header_row=None
    for tr in node.find_all("tr"):
        cells=tr.find_all(["th", "td"])
        row=[_normalize_block_text(cell.get_text(" ", strip=True)).replace("\n", " ") for cell in cells]
        if any(cell for cell in row):
            rows.append(row)
    if not rows:
        return {"table_row_count": 0, "table_column_count": 0, "table_headers": []}
    first_row_has_header=bool(node.find("thead")) or bool(node.find("th"))
    if first_row_has_header:
        header_row=rows[0]
    body_rows=rows[1:] if header_row else rows
    max_cols=max(len(row) for row in rows)
    return {
        "table_row_count": len(body_rows),
        "table_column_count": max_cols,
        "table_headers": header_row or [],
    }

def _chunk_text_content(text, default_type="paragraph"):
    """Split freeform text into lightweight chunk objects."""
    normalized=text.strip()
    if not normalized:
        return []
    parts=[part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()]
    if len(parts) == 1 and "\n" in normalized:
        parts=[line.strip() for line in normalized.splitlines() if line.strip()]
    chunks=[
        {"index": idx + 1, "type": default_type, "text": part}
        for idx, part in enumerate(parts)
    ]
    return _annotate_chunks(chunks)

def _extract_content_blocks(root, base_url=None):
    """Extract structured content blocks from a cleaned HTML subtree."""
    blocks=[]
    _collect_content_blocks(root.body or root, blocks, base_url=base_url)
    deduped=[]
    seen_texts=set()
    for block in blocks:
        text=block["text"].strip()
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        deduped.append(block)
    for idx, block in enumerate(deduped, 1):
        block["index"]=idx
    return _annotate_chunks(deduped)

def _collect_content_blocks(node, blocks, base_url=None):
    """Walk a DOM subtree and append structured blocks."""
    if not isinstance(node, Tag):
        return

    for child in node.children:
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue

        name=(child.name or "").lower()
        if name in {"script", "style", "noscript"}:
            continue
        if name in {"ul", "ol"}:
            text=_serialize_list(child)
            if text:
                block={
                    "type": "list",
                    "text": text,
                    "list_item_count": _count_list_items(child),
                    "list_ordered": name == "ol",
                }
                links=_extract_links_from_node(child, base_url=base_url)
                if links:
                    block["links"]=links
                blocks.append(block)
            continue
        if name == "table":
            text=_serialize_table(child)
            if text:
                block={"type": "table", "text": text}
                block.update(_table_structure(child))
                links=_extract_links_from_node(child, base_url=base_url)
                if links:
                    block["links"]=links
                blocks.append(block)
            continue
        if name in {"pre", "code"}:
            if name == "code" and isinstance(child.parent, Tag) and (child.parent.name or "").lower() == "pre":
                continue
            text, language=_serialize_code_block(child)
            if text:
                block={"type": "code", "text": text, "code_line_count": max(1, len(text.splitlines()) - 2)}
                if language:
                    block["code_language"]=language
                blocks.append(block)
            continue
        if name in {"blockquote"}:
            text=_normalize_block_text(child.get_text("\n", strip=True))
            if text:
                quoted="\n".join(f"> {line}" for line in text.splitlines())
                block={"type": "blockquote", "text": quoted}
                links=_extract_links_from_node(child, base_url=base_url)
                if links:
                    block["links"]=links
                blocks.append(block)
            continue
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text=_normalize_block_text(child.get_text(" ", strip=True))
            if text:
                block={"type": "heading", "text": text, "heading_level": int(name[1])}
                links=_extract_links_from_node(child, base_url=base_url)
                if links:
                    block["links"]=links
                blocks.append(block)
            continue
        if name in {"p"}:
            text=_normalize_block_text(child.get_text(" ", strip=True))
            if text:
                block={"type": "paragraph", "text": text}
                links=_extract_links_from_node(child, base_url=base_url)
                if links:
                    block["links"]=links
                blocks.append(block)
            continue

        if list(child.find_all(["p", "ul", "ol", "table", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"], recursive=False)):
            _collect_content_blocks(child, blocks, base_url=base_url)
            continue

        text=_normalize_block_text(child.get_text(" ", strip=True))
        if text and len(text) >= 40:
            block={"type": "paragraph", "text": text}
            links=_extract_links_from_node(child, base_url=base_url)
            if links:
                block["links"]=links
            blocks.append(block)

def _serialize_list(node, depth=0):
    """Serialize a UL/OL subtree into Markdown-like list text."""
    lines=[]
    ordered=(node.name or "").lower() == "ol"
    for idx, item in enumerate(node.find_all("li", recursive=False), 1):
        nested_lists=item.find_all(["ul", "ol"], recursive=False)
        item_clone=BeautifulSoup(str(item), "html.parser").find("li")
        if item_clone:
            for nested in item_clone.find_all(["ul", "ol"], recursive=False):
                nested.extract()
            item_text=_normalize_block_text(item_clone.get_text(" ", strip=True))
        else:
            item_text=""
        prefix=f"{idx}. " if ordered else "- "
        if item_text:
            lines.append(("  " * depth) + prefix + item_text)
        for nested in nested_lists:
            nested_text=_serialize_list(nested, depth + 1)
            if nested_text:
                lines.append(nested_text)
    return "\n".join(line for line in lines if line.strip())

def _serialize_table(node):
    """Serialize a HTML table into Markdown-like text."""
    rows=[]
    for tr in node.find_all("tr"):
        cells=tr.find_all(["th", "td"])
        row=[_normalize_block_text(cell.get_text(" ", strip=True)).replace("\n", " ") for cell in cells]
        if any(cell for cell in row):
            rows.append(row)
    if not rows:
        return ""

    first_row_has_header=bool(node.find("thead")) or bool(node.find("th"))
    max_cols=max(len(row) for row in rows)
    normalized=[row + [""] * (max_cols - len(row)) for row in rows]

    if first_row_has_header and len(normalized) >= 1:
        header=normalized[0]
        body=normalized[1:]
        lines=[
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        lines.extend("| " + " | ".join(row) + " |" for row in body)
        return "\n".join(lines)

    return "\n".join(" | ".join(row) for row in normalized)

def _annotate_chunks(chunks):
    """Add stable metadata to chunk objects while preserving existing fields."""
    if not chunks:
        return []

    annotated=[]
    current_section=None
    section_stack=[]
    total=len(chunks)
    offset=0
    for idx, chunk in enumerate(chunks, 1):
        annotated_chunk=dict(chunk)
        text=annotated_chunk.get("text", "")
        if annotated_chunk.get("type") == "heading":
            heading_level=max(1, int(annotated_chunk.get("heading_level", 1)))
            section_stack=section_stack[:heading_level - 1]
            if text:
                section_stack.append(text)
            current_section=text or current_section

        annotated_chunk["section_title"]=current_section
        annotated_chunk["section_path"]=list(section_stack)
        annotated_chunk["section_path_text"]=" > ".join(section_stack) if section_stack else None
        annotated_chunk["section_depth"]=len(section_stack)
        annotated_chunk["char_count"]=len(text)
        annotated_chunk["word_count"]=len(re.findall(r"\S+", text))
        if "links" in annotated_chunk:
            annotated_chunk["link_count"]=len(annotated_chunk["links"])
            annotated_chunk["internal_link_count"]=sum(1 for link in annotated_chunk["links"] if link.get("is_same_host"))
            annotated_chunk["external_link_count"]=sum(1 for link in annotated_chunk["links"] if not link.get("is_same_host"))
        annotated_chunk["relative_position"]=round(idx / total, 4)
        annotated_chunk["char_start"]=offset
        annotated_chunk["char_end"]=offset + len(text)
        annotated_chunk["text_sha256"]=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
        chunk_identity="|".join((
            annotated_chunk.get("type", ""),
            annotated_chunk.get("section_path_text") or "",
            str(annotated_chunk["char_start"]),
            str(annotated_chunk["char_end"]),
            text,
        ))
        annotated_chunk["chunk_id"]=hashlib.sha256(chunk_identity.encode("utf-8")).hexdigest()[:16]
        annotated.append(annotated_chunk)
        offset=annotated_chunk["char_end"] + 1
    return annotated

def _looks_like_html_payload(content):
    """Heuristically detect HTML when the server sends an unhelpful MIME type."""
    if isinstance(content, bytes):
        sample=content[:2048].decode("utf-8", errors="ignore").lower()
    else:
        sample=str(content)[:2048].lower()
    return any(marker in sample for marker in (
        "<!doctype html",
        "<html",
        "<head",
        "<body",
        "<article",
        "<main",
        "<meta ",
    ))

def _find_meta_content(soup, attr_name, values):
    """Return the first non-empty meta content whose attribute matches one of the values."""
    expected={value.lower() for value in values}
    for tag in soup.find_all("meta"):
        attr_value=tag.get(attr_name)
        if not attr_value or attr_value.strip().lower() not in expected:
            continue
        content=(tag.get("content") or "").strip()
        if content:
            return content
    return None

def _extract_html_title(soup):
    """Extract the most useful page title from standard HTML or social metadata."""
    if soup.title and soup.title.string and soup.title.string.strip():
        return soup.title.string.strip()
    return (
        _find_meta_content(soup, "property", {"og:title"})
        or _find_meta_content(soup, "name", {"twitter:title"})
        or _extract_json_ld_metadata(soup).get("title")
        or "No Title"
    )

def _extract_html_metadata(html, base_url=None):
    """Extract stable page metadata that is useful for downstream agents."""
    soup=BeautifulSoup(html, 'html.parser')
    metadata={}

    html_tag=soup.find("html")
    if html_tag:
        lang=(html_tag.get("lang") or html_tag.get("xml:lang") or "").strip()
        if lang:
            metadata["lang"]=lang

    for link in soup.find_all("link"):
        rel_values=link.get("rel") or []
        if not isinstance(rel_values, (list, tuple, set)):
            rel_values=[rel_values]
        rel_values={str(value).strip().lower() for value in rel_values if value}
        if "canonical" not in rel_values:
            continue
        href=(link.get("href") or "").strip()
        if href:
            metadata["canonical_url"]=urljoin(base_url, href) if base_url else href
            break

    description=(
        _find_meta_content(soup, "name", {"description", "twitter:description"})
        or _find_meta_content(soup, "property", {"og:description"})
    )
    if description:
        metadata["description"]=description

    author=(
        _find_meta_content(soup, "name", {"author", "parsely-author"})
        or _find_meta_content(soup, "property", {"article:author", "og:author"})
        or _find_meta_content(soup, "itemprop", {"author"})
    )
    if author:
        metadata["author"]=author

    published_at=(
        _find_meta_content(soup, "property", {"article:published_time", "og:published_time"})
        or _find_meta_content(soup, "name", {"pubdate", "publishdate", "date", "dc.date"})
        or _find_meta_content(soup, "itemprop", {"datepublished", "datecreated"})
    )
    if published_at:
        metadata["published_at"]=published_at

    json_ld_metadata=_extract_json_ld_metadata(soup, base_url=base_url)
    for key in ("canonical_url", "lang", "description", "author", "published_at"):
        if key not in metadata and json_ld_metadata.get(key):
            metadata[key]=json_ld_metadata[key]

    return metadata

def _extract_json_ld_metadata(soup, base_url=None):
    """Extract page metadata from JSON-LD article/news schemas when present."""
    metadata={}

    for item in _iter_json_ld_objects(soup):
        if "lang" not in metadata:
            lang=_coerce_json_ld_string(item.get("inLanguage"))
            if lang:
                metadata["lang"]=lang

        if "canonical_url" not in metadata:
            canonical=_extract_json_ld_url(item.get("mainEntityOfPage")) or _extract_json_ld_url(item.get("url"))
            if canonical:
                metadata["canonical_url"]=urljoin(base_url, canonical) if base_url else canonical

        if "description" not in metadata:
            description=_coerce_json_ld_string(item.get("description"))
            if description:
                metadata["description"]=description

        if "author" not in metadata:
            author=_extract_json_ld_author(item.get("author"))
            if author:
                metadata["author"]=author

        if "published_at" not in metadata:
            published_at=_coerce_json_ld_string(item.get("datePublished")) or _coerce_json_ld_string(item.get("dateCreated"))
            if published_at:
                metadata["published_at"]=published_at

        if "title" not in metadata:
            title=_coerce_json_ld_string(item.get("headline")) or _coerce_json_ld_string(item.get("name"))
            if title:
                metadata["title"]=title

    return metadata

def _iter_json_ld_objects(soup):
    """Yield JSON-LD objects from script tags, flattening lists and @graph blocks."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw=(script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            payload=json.loads(raw)
        except json.JSONDecodeError:
            continue

        stack=payload if isinstance(payload, list) else [payload]
        while stack:
            item=stack.pop(0)
            if isinstance(item, list):
                stack[:0]=item
                continue
            if not isinstance(item, dict):
                continue
            graph=item.get("@graph")
            if isinstance(graph, list):
                stack[:0]=graph
            yield item

def _coerce_json_ld_string(value):
    """Return a readable string from a JSON-LD scalar or object."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("name", "@id", "url", "text"):
            nested=value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None

def _extract_json_ld_url(value):
    """Extract a URL-like field from common JSON-LD shapes."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("@id", "url"):
            nested=value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None

def _extract_json_ld_author(value):
    """Extract author names from JSON-LD author fields."""
    if isinstance(value, list):
        names=[_extract_json_ld_author(item) for item in value]
        names=[name for name in names if name]
        return ", ".join(names) if names else None
    if isinstance(value, dict):
        name=_coerce_json_ld_string(value.get("name"))
        if name:
            return name
    return _coerce_json_ld_string(value)

def _normalize_content_type(response):
    """Return normalized content type without charset suffix, or None."""
    raw=response.headers.get("Content-Type", "") if response is not None else ""
    content_type=raw.split(";", 1)[0].strip().lower()
    return content_type or None

def _normalize_final_url(response, fallback_url):
    """Return the final response URL after redirects, or the original URL."""
    final_url=getattr(response, "url", None) if response is not None else None
    if isinstance(final_url, str) and final_url.strip():
        return final_url
    return fallback_url

def _content_length_bytes(response):
    """Return content length in bytes using the payload when available."""
    if response is None:
        return None
    content=getattr(response, "content", None)
    if content is not None:
        try:
            return len(content)
        except TypeError:
            pass
    header=response.headers.get("Content-Length") if response.headers else None
    if not header:
        return None
    try:
        return int(header)
    except (TypeError, ValueError):
        return None

def _header_value(response, header_name):
    """Return a stripped header value when present."""
    if response is None or not getattr(response, "headers", None):
        return None
    value=response.headers.get(header_name)
    if value is None:
        return None
    value=str(value).strip()
    return value or None

def _filename_from_content_disposition(content_disposition):
    """Extract filename from a Content-Disposition header when present."""
    if not content_disposition:
        return None
    utf8_match=re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.IGNORECASE)
    if utf8_match:
        from urllib.parse import unquote
        return unquote(utf8_match.group(1)).strip('"')
    basic_match=re.search(r'filename="?([^";]+)"?', content_disposition, flags=re.IGNORECASE)
    if basic_match:
        return basic_match.group(1).strip()
    return None

def _guess_extension(url, content_type=None):
    """Guess a useful file extension from the URL path or content type."""
    path=urlparse(url).path
    ext=os.path.splitext(path)[1].lower()
    if ext:
        return ext
    return MARKITDOWN_MIME_TO_EXTENSIONS.get(content_type, "")

def _is_html_content_type(content_type):
    return content_type in {"text/html", "application/xhtml+xml"}

def _is_text_content_type(content_type):
    if not content_type:
        return False
    if content_type.startswith("text/"):
        return True
    return content_type in {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-javascript",
        "application/ld+json",
    }

def _title_from_url(url, fallback="No Title"):
    """Infer a readable title from the URL path."""
    path=urlparse(url).path.rstrip("/")
    if not path:
        return fallback
    name=os.path.basename(path)
    return name or fallback

def _resolve_filename(response, url):
    """Resolve a stable filename from headers or URL."""
    content_disposition=response.headers.get("Content-Disposition", "") if response is not None else ""
    filename=_filename_from_content_disposition(content_disposition)
    if filename:
        return filename
    final_url=_normalize_final_url(response, url)
    fallback=_title_from_url(final_url, fallback="")
    return fallback or None

def _decode_text_response(response, final_url):
    """Decode a non-HTML text response without running it through BeautifulSoup."""
    try:
        text=response.content.decode(response.encoding or "utf-8", errors="replace")
    except Exception:
        text=getattr(response, "text", "")
    return _title_from_url(final_url), text.strip()

def _build_fetch_result(url, fetched_via, response=None, title=None, content=None, error=None, converted_via=None, metadata=None, chunks=None):
    """Build a consistent fetch result payload with transport metadata."""
    final_url=_normalize_final_url(response, url)
    parsed_final=urlparse(final_url)
    filename=_resolve_filename(response, url)
    result={
        "engine": "fetch",
        "url": url,
        "final_url": final_url,
        "status_code": getattr(response, "status_code", None) if response is not None else None,
        "content_type": _normalize_content_type(response),
        "content_length": _content_length_bytes(response),
        "fetched_via": fetched_via,
        "hostname": parsed_final.hostname,
    }
    for field, header_name in (("etag", "ETag"), ("last_modified", "Last-Modified")):
        header_value=_header_value(response, header_name)
        if header_value:
            result[field]=header_value
    if filename:
        result["filename"]=filename
    if title is not None:
        result["title"]=title
    if content is not None:
        result["content"]=content
        result["content_sha256"]=hashlib.sha256(content.encode("utf-8")).hexdigest()
        result["content_word_count"]=len(re.findall(r"\S+", content))
    if error is not None:
        result["error"]=error
    if converted_via is not None:
        result["converted_via"]=converted_via
    if metadata:
        result.update({key: value for key, value in metadata.items() if value is not None})
    if chunks:
        result["chunks"]=chunks
        result["chunk_count"]=len(chunks)
        outbound_links=_aggregate_chunk_links(chunks)
        if outbound_links:
            result["outbound_links"]=outbound_links
            result["outbound_link_count"]=len(outbound_links)
            result["internal_outbound_link_count"]=sum(1 for link in outbound_links if link.get("is_same_host"))
            result["external_outbound_link_count"]=sum(1 for link in outbound_links if not link.get("is_same_host"))
            hosts=[link.get("hostname") for link in outbound_links if link.get("hostname")]
            if hosts:
                result["outbound_hosts"]=sorted(dict.fromkeys(hosts))
    return result

def _convert_with_markitdown(content_bytes, url, content_type=None):
    """Convert a binary document to Markdown using MarkItDown when available."""
    try:
        from markitdown import MarkItDown
    except ImportError:
        return None, "Binary document detected but markitdown is not installed."

    extension=_guess_extension(url, content_type) or ".bin"
    file_name=_title_from_url(url, fallback="document") or "document"
    if not file_name.endswith(extension):
        file_name=f"{file_name}{extension}"

    md=MarkItDown(enable_plugins=False)

    if hasattr(md, "convert_stream"):
        stream=io.BytesIO(content_bytes)
        stream.name=file_name
        try:
            result=md.convert_stream(stream)
            text=getattr(result, "text_content", None) or str(result)
            return text.strip(), None
        except TypeError:
            # Fall back to the file-path API for older/newer signatures we don't know.
            pass

    with tempfile.NamedTemporaryFile(suffix=extension, delete=False) as tmp:
        tmp.write(content_bytes)
        tmp_path=tmp.name
    try:
        result=md.convert(tmp_path)
        text=getattr(result, "text_content", None) or str(result)
        return text.strip(), None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

def _convert_binary_response(url, response):
    """Convert supported binary documents to Markdown, when appropriate."""
    content_type=_normalize_content_type(response)
    extension=_guess_extension(_normalize_final_url(response, url), content_type)
    if not content_type and extension not in MARKITDOWN_EXTENSIONS:
        return None
    if content_type and content_type not in MARKITDOWN_MIME_TO_EXTENSIONS and extension not in MARKITDOWN_EXTENSIONS:
        return None

    content, error=_convert_with_markitdown(response.content, _normalize_final_url(response, url), content_type)
    if error:
        return _build_fetch_result(url, "direct", response=response, error=error)
    chunks=_chunk_text_content(content, default_type="markdown")
    return _build_fetch_result(
        url,
        "direct",
        response=response,
        title=_resolve_filename(response, url) or _title_from_url(_normalize_final_url(response, url)),
        content=content,
        converted_via="markitdown",
        chunks=chunks,
    )

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
    """Fetch a webpage. Uses curl_cffi for TLS impersonation when available, otherwise requests.Session."""
    if HAS_CURL_CFFI:
        for attempt in range(maxRetries+1):
            try:
                session=cffi_requests.Session(impersonate="chrome")
                response=session.get(url, headers=FETCH_HEADERS, timeout=30)
                response.raise_for_status()
                return response
            except Exception as e:
                status=getattr(getattr(e, 'response', None), 'status_code', None)
                if status and 400<=status<500 and status!=429:
                    raise
                if attempt<maxRetries:
                    time.sleep(2**attempt)
                    continue
                raise
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

_TWITTER_HOSTS={'twitter.com','www.twitter.com','mobile.twitter.com','x.com','www.x.com','mobile.x.com','api.fxtwitter.com','fxtwitter.com','vxtwitter.com','fixvx.com'}
_TWITTER_NON_USER_PATHS={'home','explore','search','notifications','messages','settings','i','tos','privacy','hashtag','intent','share','login','compose','who_to_follow','lists'}
_TWITTER_HANDLE_RE=re.compile(r'^[A-Za-z0-9_]{1,15}$')

def _is_twitter_url(url):
    """Check if URL is a Twitter/X link and return (screen_name, tweet_id) or None."""
    from urllib.parse import urlparse
    parsed=urlparse(url)
    if not parsed.hostname or parsed.hostname.lower() not in _TWITTER_HOSTS:
        return None
    segments=[s for s in parsed.path.strip('/').split('/') if s]
    if not segments:
        return None
    user=segments[0]
    if user.lower() in _TWITTER_NON_USER_PATHS:
        return None
    if not _TWITTER_HANDLE_RE.match(user):
        return None
    # If path has /status/ segment, require a valid numeric ID
    if len(segments)>=2 and segments[1].lower()=='status':
        if len(segments)>=3 and segments[2].isdigit():
            return (user, segments[2])
        return None
    return (user, None)

def _safe_int(val, default=0):
    """Safely cast a value to int, returning default on failure."""
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def _format_tweet(tweet):
    """Format a fxtwitter tweet JSON object into readable text."""
    author=tweet.get('author', {})
    parts=[
        f"@{author.get('screen_name', '?')} ({author.get('name', '?')})",
    ]
    if author.get('description'):
        parts.append(f"  Bio: {author['description']}")
    author_stats=[]
    if author.get('followers') is not None:
        author_stats.append(f"Followers: {_safe_int(author.get('followers')):,}")
    if author.get('following') is not None:
        author_stats.append(f"Following: {_safe_int(author.get('following')):,}")
    if author_stats:
        parts.append(f"  {' | '.join(author_stats)}")
    parts.extend([
        "",
        f"  {tweet.get('text', '')}",
        "",
        f"  Date: {tweet.get('created_at', '?')}",
        f"  Likes: {_safe_int(tweet.get('likes')):,}  Retweets: {_safe_int(tweet.get('retweets')):,}  Replies: {_safe_int(tweet.get('replies')):,}",
    ])
    views=_safe_int(tweet.get('views'))
    if views:
        parts.append(f"  Views: {views:,}")
    if tweet.get('replying_to'):
        parts.append(f"  Replying to: @{tweet['replying_to']}")
    media=tweet.get('media', {})
    for mtype in ('photos', 'videos'):
        for item in media.get(mtype, []):
            parts.append(f"  [{mtype[:-1].title()}] {item.get('url', '')}")
    if tweet.get('quote'):
        q=tweet['quote']
        qa=q.get('author', {})
        parts.extend(["", f"  Quoted @{qa.get('screen_name','?')}: {q.get('text', '')}"])
    return '\n'.join(parts)

def _format_twitter_user(user):
    """Format a fxtwitter user JSON object into readable text."""
    parts=[
        f"@{user.get('screen_name', '?')} ({user.get('name', '?')})",
        f"  {user.get('description', '')}",
        "",
        f"  Followers: {_safe_int(user.get('followers')):,}  Following: {_safe_int(user.get('following')):,}",
        f"  Tweets: {_safe_int(user.get('tweets')):,}  Likes: {_safe_int(user.get('likes')):,}",
        f"  Joined: {user.get('joined', '?')}",
    ]
    if user.get('location'):
        parts.append(f"  Location: {user['location']}")
    if user.get('website', {}).get('display_url'):
        parts.append(f"  Website: {user['website']['display_url']}")
    return '\n'.join(parts)

def _fetch_twitter(url, parsed):
    """Fetch Twitter/X content via fxtwitter API. Returns result dict or None on failure."""
    user, tweet_id=parsed
    if tweet_id:
        api_url=f"https://api.fxtwitter.com/{user}/status/{tweet_id}"
    else:
        api_url=f"https://api.fxtwitter.com/{user}"
    sys.stderr.write(f"[ccsearch] Twitter/X URL detected, using fxtwitter API: {api_url}\n")
    try:
        resp=requests.get(api_url, timeout=15)
        data=resp.json()
    except Exception as e:
        sys.stderr.write(f"[ccsearch] fxtwitter API request failed: {e}\n")
        return None
    if data.get('code') != 200:
        sys.stderr.write(f"[ccsearch] fxtwitter API error: {data.get('message', 'Unknown')}\n")
        return None
    if tweet_id and data.get('tweet'):
        t=data['tweet']
        title=f"@{t.get('author',{}).get('screen_name','?')}: {t.get('text','')[:80]}"
        content=_format_tweet(t)
        return _build_fetch_result(url, "fxtwitter", title=title, content=content, chunks=_chunk_text_content(content, default_type="social"))
    elif not tweet_id and data.get('user'):
        u=data['user']
        title=f"@{u.get('screen_name','?')} — Twitter/X Profile"
        content=_format_twitter_user(u)
        return _build_fetch_result(url, "fxtwitter", title=title, content=content, chunks=_chunk_text_content(content, default_type="social"))
    return None

def perform_fetch(url, config):
    """Fetch and extract clean text from a webpage with optional FlareSolverr fallback."""
    # Intercept Twitter/X URLs and use fxtwitter API
    parsed=_is_twitter_url(url)
    if parsed:
        result=_fetch_twitter(url, parsed)
        if result:
            return result
        sys.stderr.write("[ccsearch] fxtwitter API failed, falling back to normal fetch...\n")
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
            title, cleanText, chunks=_extract_html_content(html, base_url=url)
            metadata=_extract_html_metadata(html, base_url=url)
            return _build_fetch_result(url, "flaresolverr", title=title, content=cleanText, metadata=metadata, chunks=chunks)
        except Exception as e:
            return _build_fetch_result(url, "flaresolverr", error=f"FlareSolverr failed: {e}")

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
                final_url=_normalize_final_url(response, url)
                title, cleanText, chunks=_extract_html_content(html, base_url=final_url)
                metadata=_extract_html_metadata(html, base_url=final_url)
                return _build_fetch_result(url, "flaresolverr", response=response, title=title, content=cleanText, metadata=metadata, chunks=chunks)
            except Exception as flareErr:
                return _build_fetch_result(url, "direct", response=response, error=f"Cloudflare detected. Direct fetch blocked | FlareSolverr also failed: {flareErr}")
        converted_result=_convert_binary_response(url, response)
        if converted_result:
            return converted_result
        contentType=_normalize_content_type(response)
        looksLikeHtml=_is_html_content_type(contentType) or _looks_like_html_payload(response.content)
        metadata={}
        chunks=None
        if _is_text_content_type(contentType) and not looksLikeHtml:
            title, cleanText=_decode_text_response(response, _normalize_final_url(response, url))
            chunks=_chunk_text_content(cleanText)
        else:
            final_url=_normalize_final_url(response, url)
            title, cleanText, chunks=_extract_html_content(response.content, base_url=final_url)
            metadata=_extract_html_metadata(response.content, base_url=final_url)
        # Detect JS-heavy SPA shells and auto-fallback to FlareSolverr (HTML only)
        isHtml=looksLikeHtml or contentType is None
        isSpa=False
        spaReason=""
        if isHtml and response.status_code==200:
            isSpa, spaReason=_detect_spa_shell(response.content, len(cleanText))
        if canFallback and isSpa:
            sys.stderr.write(f"[ccsearch] SPA shell detected ({spaReason}), falling back to FlareSolverr...\n")
            try:
                html=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
                sys.stderr.write("[ccsearch] FlareSolverr rendered page successfully.\n")
                final_url=_normalize_final_url(response, url)
                fTitle, fCleanText, fChunks=_extract_html_content(html, base_url=final_url)
                if len(fCleanText)>len(cleanText):
                    rendered_metadata=_extract_html_metadata(html, base_url=final_url)
                    return _build_fetch_result(url, "flaresolverr", response=response, title=fTitle, content=fCleanText, metadata=rendered_metadata, chunks=fChunks)
                sys.stderr.write("[ccsearch] FlareSolverr result not better, using direct response.\n")
            except Exception as flareErr:
                sys.stderr.write(f"[ccsearch] FlareSolverr fallback failed: {flareErr}\n")
        return _build_fetch_result(url, "direct", response=response, title=title, content=cleanText, metadata=metadata, chunks=chunks)

    # Simple fetch failed — try FlareSolverr fallback
    if canFallback:
        sys.stderr.write(f"[ccsearch] Direct fetch failed ({simpleFetchErr}), falling back to FlareSolverr...\n")
        try:
            html=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
            sys.stderr.write("[ccsearch] FlareSolverr solved challenge successfully.\n")
            title, cleanText, chunks=_extract_html_content(html, base_url=url)
            metadata=_extract_html_metadata(html, base_url=url)
            return _build_fetch_result(url, "flaresolverr", title=title, content=cleanText, metadata=metadata, chunks=chunks)
        except Exception as flareErr:
            return _build_fetch_result(url, "direct", error=f"Direct fetch failed: {simpleFetchErr} | FlareSolverr also failed: {flareErr}")

    return _build_fetch_result(url, "direct", error=str(simpleFetchErr))

def list_engines():
    """Return machine-readable engine metadata for CLI, API, and MCP layers."""
    return [
        {
            "name": name,
            **details,
            "required_env_vars": _engine_required_env_vars(name),
            "configured": _is_engine_configured(name),
            "configured_via": _engine_configured_via(name),
        }
        for name, details in ENGINE_DETAILS.items()
    ]

def _engine_required_env_vars(engine):
    """Return environment variables that can satisfy an engine."""
    requirements={
        "brave": ["BRAVE_API_KEY"],
        "perplexity": ["OPENROUTER_API_KEY"],
        "both": ["BRAVE_API_KEY", "OPENROUTER_API_KEY"],
        "llm-context": ["BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"],
        "fetch": [],
    }
    return requirements.get(engine, [])

def _engine_configured_via(engine):
    """Return the active environment variable(s) satisfying an engine."""
    if engine == "fetch":
        return "built-in"
    required=_engine_required_env_vars(engine)
    if engine == "both":
        if all(os.environ.get(name) for name in required):
            return " + ".join(required)
        return None
    for name in required:
        if os.environ.get(name):
            return name
    return None

def _is_engine_configured(engine):
    """Return whether the engine is runnable with the current environment."""
    return _engine_configured_via(engine) is not None

def get_diagnostics(config=None, include_engines=True):
    """Return runtime diagnostics without exposing secret values."""
    fetch_config=config or load_config(os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini"))
    dependencies={}
    for module_name, purpose in OPTIONAL_DEPENDENCIES.items():
        installed=HAS_CURL_CFFI if module_name == "curl_cffi" else importlib.util.find_spec(module_name) is not None
        dependencies[module_name]={
            "installed": bool(installed),
            "purpose": purpose,
        }
    diagnostics = {
        "cache_dir": get_cache_dir(),
        "dependencies": dependencies,
        "environment": {
            "BRAVE_API_KEY": bool(os.environ.get("BRAVE_API_KEY")),
            "BRAVE_SEARCH_API_KEY": bool(os.environ.get("BRAVE_SEARCH_API_KEY")),
            "OPENROUTER_API_KEY": bool(os.environ.get("OPENROUTER_API_KEY")),
            "CCSEARCH_API_KEY": bool(os.environ.get("CCSEARCH_API_KEY")),
        },
        "fetch": {
            "flaresolverr_configured": bool(fetch_config.get("Fetch", "flaresolverr_url", fallback="").strip()),
            "flaresolverr_mode": fetch_config.get("Fetch", "flaresolverr_mode", fallback="fallback").strip().lower(),
        },
        "batch": {
            "max_workers": fetch_config.getint("Batch", "max_workers", fallback=4),
        },
    }
    if include_engines:
        diagnostics["engines"] = list_engines()
    return diagnostics

def validate_query(query, engine):
    """Validate the query shape for a given engine. Returns an error message or None."""
    if not query or not str(query).strip():
        return "'query' is required"
    if engine == "fetch" and not str(query).startswith("http"):
        return "For fetch engine, query must be a valid HTTP or HTTPS URL."
    return None

def validate_execution_options(engine, offset=None, cache_ttl=10, semantic_threshold=0.9, flaresolverr=False, include_hosts=None, exclude_hosts=None, result_limit=None):
    """Validate shared execution options. Returns an error message or None."""
    if offset is not None and engine not in {"brave", "both"}:
        return "The 'offset' option is only supported for brave and both engines."
    if offset is not None and offset < 0:
        return "'offset' must be greater than or equal to 0."
    if flaresolverr and engine != "fetch":
        return "The 'flaresolverr' option is only supported for the fetch engine."
    if cache_ttl <= 0:
        return "'cache_ttl' must be greater than 0."
    if not 0.0 <= semantic_threshold <= 1.0:
        return "'semantic_threshold' must be between 0.0 and 1.0."
    if result_limit is not None:
        try:
            result_limit=int(result_limit)
        except (TypeError, ValueError):
            return "'result_limit' must be an integer."
        if result_limit < 1:
            return "'result_limit' must be greater than or equal to 1."
    try:
        normalized_include=_normalize_host_filters(include_hosts)
        normalized_exclude=_normalize_host_filters(exclude_hosts)
    except ValueError as e:
        return str(e)
    if (normalized_include or normalized_exclude) and engine not in HOST_FILTER_ENGINES:
        return "Host filters are only supported for brave, both, and llm-context engines."
    overlap=set(normalized_include) & set(normalized_exclude)
    if overlap:
        return f"Host filters overlap between include_hosts and exclude_hosts: {', '.join(sorted(overlap))}"
    if result_limit is not None and engine not in RESULT_LIMIT_ENGINES:
        return "Result limiting is only supported for brave, both, and llm-context engines."
    return None

def _resolve_batch_max_workers(config, max_workers=None):
    """Resolve and validate effective batch concurrency."""
    if max_workers is None:
        max_workers = config.getint("Batch", "max_workers", fallback=4)
    try:
        max_workers = int(max_workers)
    except (TypeError, ValueError) as e:
        raise ValueError("'max_workers' must be a positive integer.") from e
    if max_workers <= 0:
        raise ValueError("'max_workers' must be a positive integer.")
    return max_workers

def _batch_request_fingerprint(query, engine, offset, cache, cache_ttl, semantic_cache, semantic_threshold, flaresolverr, include_hosts=None, exclude_hosts=None, result_limit=None):
    """Build a stable fingerprint for duplicate suppression within a batch."""
    normalized_query = normalize_cache_query(query, engine)
    return (
        engine,
        normalized_query,
        offset,
        bool(cache),
        int(cache_ttl),
        bool(semantic_cache),
        round(float(semantic_threshold), 6),
        bool(flaresolverr),
        tuple(_normalize_host_filters(include_hosts)),
        tuple(_normalize_host_filters(exclude_hosts)),
        None if result_limit is None else int(result_limit),
    )

def _coerce_batch_query(entry):
    """Extract a query string from a batch entry using query or url fields."""
    if not isinstance(entry, dict):
        return None
    query=entry.get("query")
    if query is None and "url" in entry:
        query=entry.get("url")
    if query is None:
        return None
    query=str(query).strip()
    return query or None

def load_batch_requests(batch_file):
    """Load batch requests from a JSON array/object or JSONL file."""
    with open(batch_file, "r", encoding="utf-8") as f:
        raw=f.read()
    stripped=raw.strip()
    if not stripped:
        raise ValueError("Batch file is empty.")

    treat_as_jsonl=batch_file.lower().endswith(".jsonl")
    if treat_as_jsonl:
        payload=None
    else:
        try:
            payload=json.loads(stripped)
        except json.JSONDecodeError:
            payload=None

    if payload is None:
        entries=[]
        for line_no, line in enumerate(raw.splitlines(), 1):
            line=line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON on line {line_no}: {e.msg}") from e
        if not entries:
            raise ValueError("Batch file is empty.")
        payload=entries

    if isinstance(payload, dict):
        if "requests" not in payload:
            raise ValueError("Batch JSON object must contain a 'requests' field.")
        requests_payload=payload["requests"]
        defaults=payload.get("defaults", {})
    else:
        requests_payload=payload
        defaults={}

    if not isinstance(requests_payload, list) or not requests_payload:
        raise ValueError("'requests' must be a non-empty list.")
    if defaults and not isinstance(defaults, dict):
        raise ValueError("'defaults' must be an object when provided.")
    return requests_payload, defaults

def execute_batch(requests_payload, config, defaults=None, max_workers=None):
    """Execute a batch of heterogeneous requests while isolating per-item failures."""
    if not isinstance(requests_payload, list) or not requests_payload:
        raise ValueError("'requests' must be a non-empty list.")
    defaults=defaults or {}
    if not isinstance(defaults, dict):
        raise ValueError("'defaults' must be an object when provided.")
    resolved_max_workers = min(len(requests_payload), _resolve_batch_max_workers(config, max_workers=max_workers))

    def build_entry(idx, entry):
        if not isinstance(entry, dict):
            return None, {"index": idx, "error": "Each batch request must be an object."}

        request_data = {
            "index": idx,
            "engine": str(entry.get("engine", defaults.get("engine", "brave"))).strip().lower(),
            "query": _coerce_batch_query(entry),
            "offset": entry.get("offset", defaults.get("offset")),
            "cache": entry.get("cache", defaults.get("cache", False)),
            "cache_ttl": entry.get("cache_ttl", defaults.get("cache_ttl", 10)),
            "semantic_cache": entry.get("semantic_cache", defaults.get("semantic_cache", False)),
            "semantic_threshold": entry.get("semantic_threshold", defaults.get("semantic_threshold", 0.9)),
            "flaresolverr": entry.get("flaresolverr", defaults.get("flaresolverr", False)),
            "include_hosts": entry.get("include_hosts", defaults.get("include_hosts")),
            "exclude_hosts": entry.get("exclude_hosts", defaults.get("exclude_hosts")),
            "result_limit": entry.get("result_limit", defaults.get("result_limit")),
        }

        engine=request_data["engine"]
        query=request_data["query"]
        if engine not in VALID_ENGINES:
            return None, {"index": idx, "engine": engine, "query": query, "error": f"Unsupported engine: {engine}"}
        validation_error=validate_query(query, engine)
        if validation_error:
            return None, {"index": idx, "engine": engine, "query": query, "error": validation_error}
        option_error=validate_execution_options(
            engine,
            offset=request_data["offset"],
            cache_ttl=request_data["cache_ttl"],
            semantic_threshold=request_data["semantic_threshold"],
            flaresolverr=request_data["flaresolverr"],
            include_hosts=request_data["include_hosts"],
            exclude_hosts=request_data["exclude_hosts"],
            result_limit=request_data["result_limit"],
        )
        if option_error:
            return None, {"index": idx, "engine": engine, "query": query, "error": option_error}

        request_data["fingerprint"] = _batch_request_fingerprint(
            query,
            engine,
            request_data["offset"],
            request_data["cache"],
            request_data["cache_ttl"],
            request_data["semantic_cache"],
            request_data["semantic_threshold"],
            request_data["flaresolverr"],
            request_data["include_hosts"],
            request_data["exclude_hosts"],
            request_data["result_limit"],
        )
        return request_data, None

    def run_request(request_data):
        started = time.time()
        try:
            result=execute_query(
                request_data["query"],
                request_data["engine"],
                config,
                offset=request_data["offset"],
                cache=request_data["cache"],
                cache_ttl=request_data["cache_ttl"],
                semantic_cache=request_data["semantic_cache"],
                semantic_threshold=request_data["semantic_threshold"],
                flaresolverr=request_data["flaresolverr"],
                include_hosts=request_data["include_hosts"],
                exclude_hosts=request_data["exclude_hosts"],
                result_limit=request_data["result_limit"],
            )
            if isinstance(result, dict):
                result=dict(result)
                result["index"]=request_data["index"]
                result.setdefault("duration_ms", round((time.time() - started) * 1000, 2))
            return result
        except Exception as e:
            return {
                "index": request_data["index"],
                "engine": request_data["engine"],
                "query": request_data["query"],
                "error": str(e),
                "duration_ms": round((time.time() - started) * 1000, 2),
            }

    started = time.time()
    results=[None] * len(requests_payload)
    future_map={}
    fingerprint_map={}
    with concurrent.futures.ThreadPoolExecutor(max_workers=resolved_max_workers) as executor:
        for idx, entry in enumerate(requests_payload, 1):
            request_data, error_result = build_entry(idx, entry)
            if error_result is not None:
                results[idx - 1] = error_result
                continue
            fingerprint = request_data["fingerprint"]
            if fingerprint in fingerprint_map:
                fingerprint_map[fingerprint]["indexes"].append(idx)
                continue
            future = executor.submit(run_request, request_data)
            fingerprint_map[fingerprint] = {"future": future, "indexes": [idx]}
            future_map[future] = fingerprint
        for future in concurrent.futures.as_completed(future_map):
            fingerprint = future_map[future]
            base_result = future.result()
            indexes = fingerprint_map[fingerprint]["indexes"]
            first_index = indexes[0]
            results[first_index - 1] = base_result
            for duplicate_index in indexes[1:]:
                if isinstance(base_result, dict):
                    duplicate_result = dict(base_result)
                    duplicate_result["index"] = duplicate_index
                    duplicate_result["duration_ms"] = 0.0
                    duplicate_result["_batch_deduped"] = True
                    duplicate_result["_batch_deduped_from"] = first_index
                    results[duplicate_index - 1] = duplicate_result
                else:
                    results[duplicate_index - 1] = {
                        "index": duplicate_index,
                        "error": "Batch deduplication requires object results.",
                    }
    error_count=sum(1 for item in results if isinstance(item, dict) and item.get("error"))
    deduped_count=sum(1 for item in results if isinstance(item, dict) and item.get("_batch_deduped"))
    engine_counts={}
    for item in results:
        if isinstance(item, dict) and item.get("engine"):
            engine_counts[item["engine"]] = engine_counts.get(item["engine"], 0) + 1
    return {
        "results": results,
        "count": len(results),
        "error_count": error_count,
        "success_count": len(results) - error_count,
        "has_errors": bool(error_count),
        "duration_ms": round((time.time() - started) * 1000, 2),
        "max_workers": resolved_max_workers,
        "deduped_count": deduped_count,
        "engine_counts": engine_counts,
    }

def _exact_cache_lookup(query, engine, offset, cache_ttl, use_semantic):
    """Attempt exact cache lookup and semantic-index backfill when needed."""
    result = read_from_cache(query, engine, offset, cache_ttl)
    if result:
        result["_from_cache"] = True
        if use_semantic and engine != "fetch":
            backfill_semantic_index(query, engine, offset)
    return result

def _semantic_cache_lookup(query, engine, offset, cache_ttl, semantic_threshold):
    """Attempt semantic cache lookup for non-fetch engines."""
    if engine == "fetch":
        return None
    result, sim = read_from_semantic_cache(query, engine, offset, cache_ttl, semantic_threshold)
    if result:
        result["_from_cache"] = True
        result["_semantic_similarity"] = sim
    return result

def execute_engine(query, engine, config, offset=None, flaresolverr=False):
    """Run a single engine without any cache handling."""
    if engine not in VALID_ENGINES:
        raise ValueError(f"Unsupported engine: {engine}")

    error = validate_query(query, engine)
    if error:
        raise ValueError(error)

    if engine == "brave":
        api_key = os.environ.get("BRAVE_API_KEY")
        if not api_key:
            raise RuntimeError("BRAVE_API_KEY environment variable not found.")
        return perform_brave_search(query, api_key, config, offset=offset)

    if engine == "perplexity":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY environment variable not found.")
        return perform_perplexity_search(query, api_key, config)

    if engine == "both":
        brave_api_key = os.environ.get("BRAVE_API_KEY")
        perplexity_api_key = os.environ.get("OPENROUTER_API_KEY")
        if not brave_api_key or not perplexity_api_key:
            raise RuntimeError("Both BRAVE_API_KEY and OPENROUTER_API_KEY are required for 'both' engine.")
        return perform_both_search(query, brave_api_key, perplexity_api_key, config, offset=offset)

    if engine == "llm-context":
        api_key = os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY")
        if not api_key:
            raise RuntimeError("BRAVE_SEARCH_API_KEY (or BRAVE_API_KEY) environment variable not found.")
        return perform_llm_context_search(query, api_key, config)

    fetch_config = config
    if flaresolverr:
        fetch_config = configparser.ConfigParser()
        fetch_config.read_dict({section: dict(config[section]) for section in config.sections()})
        fetch_config.set('Fetch', 'flaresolverr_mode', 'always')
    return perform_fetch(query, fetch_config)

def execute_query(query, engine, config, offset=None, cache=False, cache_ttl=10, semantic_cache=False, semantic_threshold=0.9, flaresolverr=False, include_hosts=None, exclude_hosts=None, result_limit=None):
    """Run a query through cache + engine execution and return a structured result."""
    started=time.time()
    normalized_include=_normalize_host_filters(include_hosts)
    normalized_exclude=_normalize_host_filters(exclude_hosts)
    option_error = validate_execution_options(
        engine,
        offset=offset,
        cache_ttl=cache_ttl,
        semantic_threshold=semantic_threshold,
        flaresolverr=flaresolverr,
        include_hosts=normalized_include,
        exclude_hosts=normalized_exclude,
        result_limit=result_limit,
    )
    if option_error:
        raise ValueError(option_error)
    normalized_result_limit=None if result_limit is None else int(result_limit)

    use_cache = cache or semantic_cache
    use_semantic = semantic_cache and engine != "fetch"
    cache_status="disabled"

    result = None
    if use_cache:
        result = _exact_cache_lookup(query, engine, offset, cache_ttl, use_semantic)
        if result:
            cache_status="exact"
    if not result and use_semantic:
        result = _semantic_cache_lookup(query, engine, offset, cache_ttl, semantic_threshold)
        if result:
            cache_status="semantic"
    if not result:
        result = execute_engine(query, engine, config, offset=offset, flaresolverr=flaresolverr)
        if use_cache:
            cache_status="miss"
        if use_cache:
            cache_key = get_cache_key(query, engine, offset)
            write_to_cache(query, engine, offset, result)
            if use_semantic:
                update_semantic_index(query, engine, offset, cache_key)
    if isinstance(result, dict):
        result=dict(result)
        result=_apply_host_filters(result, engine, include_hosts=normalized_include, exclude_hosts=normalized_exclude)
        result=_apply_result_limit(result, engine, result_limit=normalized_result_limit)
        result["cache_status"]=cache_status
        result["duration_ms"]=round((time.time() - started) * 1000, 2)
    return result

def main():
    parser = argparse.ArgumentParser(description="Web Search Utility for LLMs using Brave or Perplexity.")
    parser.add_argument("query", nargs="?", help="The search query, keyword, or URL (for fetch engine)")
    parser.add_argument("-e", "--engine", choices=list(VALID_ENGINES), required=False, help="Search engine to use (brave, perplexity, both, fetch, or llm-context)")
    parser.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini"), help="Path to config INI file")
    parser.add_argument("--batch-file", help="Path to a JSON/JSONL batch file containing multiple requests")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format: json or text")
    parser.add_argument("--offset", type=int, default=None, help="Pagination offset (for Brave search only)")
    parser.add_argument("--limit", type=int, default=None, help="Limit returned results for brave, both, and llm-context")
    parser.add_argument("--cache", action="store_true", help="Enable results caching")
    parser.add_argument("--cache-ttl", type=int, default=10, help="Cache Time-To-Live in minutes (default: 10)")
    parser.add_argument("--semantic-cache", action="store_true", help="Enable semantic similarity cache via fastembed (implies --cache)")
    parser.add_argument("--semantic-threshold", type=float, default=0.9, help="Cosine similarity threshold for semantic cache (default: 0.9)")
    parser.add_argument("--flaresolverr", action="store_true", help="Force FlareSolverr mode for fetch engine (overrides config flaresolverr_mode to 'always')")
    parser.add_argument("--list-engines", action="store_true", help="List available engines and their configured status")
    parser.add_argument("--doctor", action="store_true", help="Show runtime diagnostics and dependency status")
    parser.add_argument("--batch-workers", type=int, default=None, help="Maximum concurrent workers for --batch-file execution")
    parser.add_argument("--include-host", action="append", default=[], help="Restrict brave/both/llm-context results to these hostnames (repeatable or comma-separated)")
    parser.add_argument("--exclude-host", action="append", default=[], help="Exclude brave/both/llm-context results from these hostnames (repeatable or comma-separated)")

    args = parser.parse_args()
    config = load_config(args.config)

    try:
        if args.list_engines:
            payload={"engines": list_engines()}
            if args.format == "json":
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print("Available engines:\n")
                for engine in payload["engines"]:
                    configured="configured" if engine["configured"] else "not configured"
                    print(f"- {engine['name']} ({engine['category']}, {configured})")
                    if engine.get("requires"):
                        print(f"  requires: {engine['requires']}")
                    if engine.get("configured_via"):
                        print(f"  configured_via: {engine['configured_via']}")
                    supports=[]
                    if engine.get("supports_offset"):
                        supports.append("offset")
                    if engine.get("supports_semantic_cache"):
                        supports.append("semantic-cache")
                    if engine.get("supports_flaresolverr"):
                        supports.append("flaresolverr")
                    if engine.get("supports_host_filter"):
                        supports.append("host-filter")
                    if engine.get("supports_result_limit"):
                        supports.append("result-limit")
                    if supports:
                        print(f"  supports: {', '.join(supports)}")
                    print()
            return

        if args.doctor:
            payload=get_diagnostics(config)
            if args.format == "json":
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print("ccsearch diagnostics\n")
                print("Environment:")
                for key, present in payload["environment"].items():
                    print(f"- {key}: {'set' if present else 'missing'}")
                print("\nDependencies:")
                for name, info in payload["dependencies"].items():
                    print(f"- {name}: {'installed' if info['installed'] else 'missing'} ({info['purpose']})")
                print("\nFetch:")
                print(f"- flaresolverr_configured: {payload['fetch']['flaresolverr_configured']}")
                print(f"- flaresolverr_mode: {payload['fetch']['flaresolverr_mode']}")
                print("\nBatch:")
                print(f"- max_workers: {payload['batch']['max_workers']}")
                print(f"- cache_dir: {payload['cache_dir']}")
            return

        if args.batch_file:
            batch_requests, file_defaults = load_batch_requests(args.batch_file)
            cli_defaults = {
                "engine": args.engine or file_defaults.get("engine", "brave"),
                "cache": args.cache,
                "cache_ttl": args.cache_ttl,
                "semantic_cache": args.semantic_cache,
                "semantic_threshold": args.semantic_threshold,
                "offset": args.offset,
                "result_limit": args.limit,
                "flaresolverr": args.flaresolverr,
                "include_hosts": args.include_host,
                "exclude_hosts": args.exclude_host,
            }
            merged_defaults = dict(file_defaults)
            cli_default_values = {
                "engine": "brave",
                "cache": False,
                "cache_ttl": 10,
                "semantic_cache": False,
                "semantic_threshold": 0.9,
                "offset": None,
                "result_limit": None,
                "flaresolverr": False,
                "include_hosts": None,
                "exclude_hosts": None,
            }
            for key, value in cli_defaults.items():
                default_value = cli_default_values[key]
                if value != default_value or key not in merged_defaults:
                    merged_defaults[key] = value
            batch_result = execute_batch(batch_requests, config, defaults=merged_defaults, max_workers=args.batch_workers)
            if args.format == "json":
                print(json.dumps(batch_result, indent=2, ensure_ascii=False))
            else:
                print(
                    f"Batch completed: {batch_result['count']} request(s), "
                    f"{batch_result['success_count']} success(es), {batch_result['error_count']} error(s), "
                    f"{batch_result['duration_ms']}ms total with {batch_result['max_workers']} worker(s)"
                )
                if batch_result.get("deduped_count"):
                    print(f"Deduplicated requests: {batch_result['deduped_count']}")
                print()
                for item in batch_result["results"]:
                    print(f"=== Request {item.get('index', '?')} ===")
                    if item.get("error"):
                        print(f"Error: {item['error']}\n")
                        continue
                    print(f"Engine: {item.get('engine')}")
                    print(f"Query: {item.get('query') or item.get('url')}")
                    if item.get("engine") == "fetch":
                        print(f"Title: {item.get('title')}")
                        if item.get("final_url"):
                            print(f"Final URL: {item['final_url']}")
                        snippet=(item.get("content") or "").splitlines()
                        if snippet:
                            print(snippet[0])
                    elif item.get("engine") == "perplexity":
                        print((item.get("answer") or "").splitlines()[0] if item.get("answer") else "")
                    elif item.get("engine") == "both":
                        print((item.get("perplexity_answer") or "").splitlines()[0] if item.get("perplexity_answer") else "")
                    else:
                        first=(item.get("results") or [{}])[0]
                        if first.get("title"):
                            print(first["title"])
                    print()
            return

        if not args.query:
            parser.error("the following arguments are required: query")
        if not args.engine:
            parser.error("the following arguments are required: -e/--engine")

        if args.flaresolverr and args.engine == "fetch" and not config.get('Fetch', 'flaresolverr_url', fallback='').strip():
            sys.stderr.write("WARNING: --flaresolverr flag set but no flaresolverr_url configured in config.ini.\n")

        result = execute_query(
            args.query,
            args.engine,
            config,
            offset=args.offset,
            cache=args.cache,
            cache_ttl=args.cache_ttl,
            semantic_cache=args.semantic_cache,
            semantic_threshold=args.semantic_threshold,
            flaresolverr=args.flaresolverr,
            include_hosts=args.include_host,
            exclude_hosts=args.exclude_host,
            result_limit=args.limit,
        )

        if args.format == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            if result.get("_from_cache"):
                print(f"[Returning Cached Result - {args.cache_ttl}min TTL]\n")
            if result.get("cache_status"):
                print(f"cache_status: {result['cache_status']}")
            if result.get("duration_ms") is not None:
                print(f"duration_ms: {result['duration_ms']}")
            if result.get("host_filtering"):
                filtering=result["host_filtering"]
                print(
                    "host_filtering: "
                    f"include={filtering.get('include_hosts', [])} "
                    f"exclude={filtering.get('exclude_hosts', [])} "
                    f"removed={filtering.get('removed_results', 0)}"
                )
                print()
            if result.get("result_limiting"):
                limiting=result["result_limiting"]
                print(
                    "result_limiting: "
                    f"limit={limiting.get('limit')} "
                    f"removed={limiting.get('removed_results', 0)}"
                )
                print()

            if args.engine == "brave":
                print(f"Brave Search Results for: {args.query}")
                print(f"Results: {result.get('result_count', len(result['results']))}\n")
                for res in result["results"]:
                    hostname = f" [{res['hostname']}]" if res.get("hostname") else ""
                    print(f"{res.get('rank', '?')}. {res['title']}{hostname}\n   URL: {res['url']}\n   {res['description']}\n")
            elif args.engine == "perplexity":
                print(f"Perplexity Search Answer ({result.get('model', 'unknown')}):\n")
                print(result["answer"])
                if result.get("citations"):
                    print("\nCitations:")
                    for idx, citation in enumerate(result["citations"], 1):
                        label=citation.get("title") or citation["url"]
                        print(f"{idx}. {label}")
                        if citation.get("title"):
                            print(f"   URL: {citation['url']}")
            elif args.engine == "both":
                print(f"--- Synthesized Answer (Perplexity) ---\n")
                print(result["perplexity_answer"])
                if result.get("perplexity_citations"):
                    print("\nCitations:")
                    for idx, citation in enumerate(result["perplexity_citations"], 1):
                        label=citation.get("title") or citation["url"]
                        print(f"{idx}. {label}")
                        if citation.get("title"):
                            print(f"   URL: {citation['url']}")
                if result.get("perplexity_error"):
                    print(f"\n[Perplexity error: {result['perplexity_error']}]")
                print(f"\n\n--- Source Reference Links (Brave) ---\n")
                print(f"Results: {result.get('brave_result_count', len(result['brave_results']))}\n")
                for res in result["brave_results"]:
                    hostname = f" [{res['hostname']}]" if res.get("hostname") else ""
                    print(f"{res.get('rank', '?')}. {res['title']}{hostname}\n   URL: {res['url']}\n   {res['description']}\n")
                if result.get("brave_error"):
                    print(f"[Brave error: {result['brave_error']}]")
            elif args.engine == "llm-context":
                print(f"LLM Context Results for: {args.query}")
                print(f"Results: {result.get('result_count', len(result['results']))} | Sources: {result.get('source_count', len(result.get('sources', {})))}\n")
                for res in result["results"]:
                    hostname = f" [{res['hostname']}]" if res.get("hostname") else ""
                    print(f"{res.get('rank', '?')}. {res['title']}{hostname}")
                    print(f"   URL: {res['url']}")
                    if res.get("age") is not None:
                        print(f"   Age: {res['age']}")
                    for snippet in res.get("snippets", []):
                        print(f"   > {snippet}")
                    print()
            elif args.engine == "fetch":
                if "error" in result:
                    print(f"Error fetching URL: {result['error']}\n")
                else:
                    print(f"--- Fetched Content: {result['title']} ---\n")
                    print(f"URL: {result['url']}\n")
                    for key in ("final_url", "content_type", "status_code", "canonical_url", "author", "published_at"):
                        value = result.get(key)
                        if value is not None:
                            print(f"{key}: {value}")
                    if result.get("chunks"):
                        print(f"chunks: {len(result['chunks'])}")
                    print()
                    print(result["content"])

    except ValueError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
    except RuntimeError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)
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
