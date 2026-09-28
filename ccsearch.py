#!/usr/bin/env python3
"""Shared ccsearch core and CLI for search, context retrieval, and URL fetches.

Supports Brave, Perplexity via OpenRouter, the combined engine, Brave LLM
Context, and direct fetch with optional FlareSolverr fallback.
"""
import os
import sys
import json
import html as html_lib
import time
import re
import argparse
import configparser
import importlib.util
import requests
import hashlib
import tempfile
import concurrent.futures
import threading
import math
import warnings
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup, NavigableString, Tag
from bs4 import MarkupResemblesLocatorWarning
# Provider snippets are sometimes bare URLs; parsing them as markup is intended.
warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
try:
    import fcntl
except ImportError:  # pragma: no cover - the deployed Pi/Linux runtime provides fcntl
    fcntl=None
try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI=True
except ImportError:
    HAS_CURL_CFFI=False

FETCH_REQUEST_ERRORS=(requests.exceptions.RequestException,)
if HAS_CURL_CFFI:
    FETCH_REQUEST_ERRORS+=(cffi_requests.exceptions.RequestException,)

class FlareSolverrError(RuntimeError):
    """An explicit failure or malformed reply from the browser service."""

FETCH_BROWSER_ERRORS=FETCH_REQUEST_ERRORS+(FlareSolverrError,)

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
        'flaresolverr_mode': 'fallback',
        'extended_fallbacks': 'llm-context, archive'
    }
    config['Batch'] = {
        'max_workers': '4'
    }

    if os.path.exists(config_file):
        # ConfigParser.read silently skips unreadable files, hiding a broken
        # deployment behind defaults. An existing configuration must be readable.
        with open(config_file, encoding="utf-8") as config_stream:
            config.read_file(config_stream)
    return config

def load_api_key(api_key_file, env_var="CCSEARCH_API_KEY", create_if_missing=False):
    """Load a shared API key from env or disk, optionally generating it on first run."""
    api_key = os.environ.get(env_var, "").strip()
    if api_key:
        return api_key

    if os.path.exists(api_key_file):
        with open(api_key_file, "r", encoding="utf-8") as f:
            api_key = f.read().strip()
        if not api_key:
            raise RuntimeError("API key file is empty; refusing to start without authentication.")
        return api_key

    if not create_if_missing:
        return ""

    import secrets
    api_key = secrets.token_urlsafe(32)
    key_directory = os.path.dirname(os.path.abspath(api_key_file))
    fd, temporary_path = tempfile.mkstemp(prefix=".ccsearch-key-", dir=key_directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(api_key)
        try:
            # Publish a complete 0600 file without replacing another server's key.
            os.link(temporary_path, api_key_file)
        except FileExistsError:
            return load_api_key(api_key_file, env_var=env_var, create_if_missing=False)
    finally:
        os.unlink(temporary_path)
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

DEFAULT_CACHE_TTL_MINUTES=90 * 24 * 60
CACHE_MAX_READ_AGE_SECONDS=90 * 24 * 60 * 60
CACHE_DELETE_AGE_SECONDS=91 * 24 * 60 * 60
CACHE_CLEANUP_INTERVAL_SECONDS=60 * 60
BRAVE_SEARCH_MAX_RPS=50

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

VALID_ENGINES=("brave", "perplexity", "both", "fetch", "llm-context", "perplexity-verify")
SEARCH_ENGINES=("brave", "perplexity", "both", "llm-context", "perplexity-verify")

# Agent-oriented response defaults. Callers can override each one per request.
DEFAULT_RESULT_LIMITS={"brave": 8, "both": 8, "llm-context": 8}
DEFAULT_SNIPPET_LIMIT=5
DEFAULT_FOCUS_K=5
DEFAULT_MAX_REPLIES=30
MAX_VERIFY_CLAIMS=10
MAX_VERIFY_CLAIM_CHARS=500
FETCH_FORMATS=("text", "chunks")
FRESHNESS_WINDOWS_DAYS={"pd": 1, "pw": 7, "pm": 31, "py": 366}
_FRESHNESS_RANGE_RE=re.compile(r"^(\d{4}-\d{2}-\d{2})to(\d{4}-\d{2}-\d{2})$")
_COUNTRY_RE=re.compile(r"^(?:[A-Za-z]{2}|ALL|all)$")
_SEARCH_LANG_RE=re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?$")
SEARCH_OPTION_ENGINES={"brave", "both", "llm-context"}
SNIPPET_LIMIT_ENGINES={"llm-context"}
_cache_lock = threading.Lock()
_semantic_index_lock = threading.Lock()
_cache_cleanup_lock = threading.Lock()
_brave_rate_limit_thread_lock = threading.Lock()
_brave_key_rotation_thread_lock = threading.Lock()
_BRAVE_NUMBERED_KEY_RE = re.compile(r"^BRAVE_SEARCH_API_KEY_([1-9]\d*)$")
_last_cache_cleanup_at=0.0

ENGINE_DETAILS={
    "brave": {
        "description": "Brave Web Search",
        "use_for": "Find links and short summaries. Start research here.",
        "requires": "BRAVE_SEARCH_API_KEY or BRAVE_API_KEY",
        "category": "search",
        "supports_offset": True,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": True,
        "supports_result_limit": True,
        "supports_search_options": True,
        "default_result_limit": DEFAULT_RESULT_LIMITS["brave"],
    },
    "perplexity": {
        "description": "Perplexity via OpenRouter",
        "use_for": "Final cross-checking only; do not use it as the primary source.",
        "requires": "OPENROUTER_API_KEY",
        "category": "answer",
        "supports_offset": False,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": False,
        "supports_result_limit": False,
        "supports_search_options": False,
    },
    "both": {
        "description": "Brave + Perplexity combined",
        "use_for": "Links plus a synthesized answer in one call.",
        "requires": "(BRAVE_SEARCH_API_KEY or BRAVE_API_KEY) + OPENROUTER_API_KEY",
        "category": "hybrid",
        "supports_offset": True,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": True,
        "supports_result_limit": True,
        "supports_search_options": True,
        "default_result_limit": DEFAULT_RESULT_LIMITS["both"],
    },
    "llm-context": {
        "description": "Brave LLM Context API (smart chunks)",
        "use_for": "Read long documents as pre-extracted passages, or get content when the original site blocks fetch.",
        "requires": "BRAVE_SEARCH_API_KEY or BRAVE_API_KEY",
        "category": "context",
        "supports_offset": False,
        "supports_semantic_cache": True,
        "supports_flaresolverr": False,
        "supports_host_filter": True,
        "supports_result_limit": True,
        "supports_search_options": True,
        "default_result_limit": DEFAULT_RESULT_LIMITS["llm-context"],
        "default_snippet_limit": DEFAULT_SNIPPET_LIMIT,
    },
    "fetch": {
        "description": "Fetch and extract text from a URL",
        "use_for": "Read the original page. Blocked pages fall back to site APIs, LLM Context, and the Wayback Machine.",
        "requires": None,
        "category": "fetch",
        "supports_offset": False,
        "supports_semantic_cache": False,
        "supports_flaresolverr": True,
        "supports_host_filter": False,
        "supports_result_limit": False,
        "supports_search_options": False,
        "default_format": "text",
        "default_focus_k": DEFAULT_FOCUS_K,
        "default_max_replies": DEFAULT_MAX_REPLIES,
    },
    "perplexity-verify": {
        "description": "Claim-by-claim verification via Perplexity",
        "use_for": "Check a short list of final conclusions; each verdict cites fetchable URLs.",
        "requires": "OPENROUTER_API_KEY",
        "category": "verify",
        "supports_offset": False,
        "supports_semantic_cache": False,
        "supports_flaresolverr": False,
        "supports_host_filter": False,
        "supports_result_limit": False,
        "supports_search_options": False,
        "max_claims": MAX_VERIFY_CLAIMS,
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
    if ":" in hostname:
        hostname=f"[{hostname}]"
    port=parsed.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc=f"{hostname}:{port}"
    else:
        netloc=hostname

    # Path separators and params can identify distinct server resources.
    path=parsed.path or "/"
    if parsed.username is not None:
        netloc=parsed.netloc.rsplit("@", 1)[0] + "@" + netloc

    filtered_params=[]
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lower_key=key.lower()
        if lower_key.startswith(TRACKING_QUERY_PREFIXES) or lower_key in TRACKING_QUERY_KEYS:
            continue
        filtered_params.append((key, value))
    # Sort parameter names, but preserve repeated-value order (e.g. sort=a&sort=b).
    filtered_params.sort(key=lambda item: item[0])
    query=urlencode(filtered_params, doseq=True)

    return urlunparse((scheme, netloc, path, parsed.params, query, ""))

def normalize_cache_query(query, engine):
    """Normalize cache input on a per-engine basis."""
    if engine == "fetch":
        return normalize_fetch_cache_url(query)
    return re.sub(r"\s+", " ", str(query)).strip()

def _cache_variant_suffix(variant):
    """Serialize request options that change upstream results into the cache key."""
    if not variant:
        return ""
    cleaned={key: value for key, value in variant.items() if value not in (None, "", [], {})}
    if not cleaned:
        return ""
    return "_" + json.dumps(cleaned, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def get_cache_key(query, engine, offset, variant=None):
    normalized_query = normalize_cache_query(query, engine)
    # Requests without variant options keep their historical key.
    key_string = f"{normalized_query}_{engine}_{offset}{_cache_variant_suffix(variant)}"
    return hashlib.md5(key_string.encode('utf-8')).hexdigest() + ".json"

def _cache_file_path(query, engine, offset, variant=None):
    return os.path.join(get_cache_dir(), get_cache_key(query, engine, offset, variant))

def _iso_from_epoch(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _utc_now_iso():
    return _iso_from_epoch(time.time())

def _cache_result_filename(name):
    """Return whether a filename is one of ccsearch's hashed result files."""
    return bool(re.fullmatch(r"[0-9a-f]{32}\.json", str(name or "")))

@contextmanager
def _locked_runtime_file(path):
    """Open and exclusively lock a small cross-process runtime state file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd=os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def _cache_operations_lock_path():
    return os.path.join(get_cache_dir(), "cache_operations.lock")

def _cache_file_age(cache_file, now=None):
    current_time=time.time() if now is None else now
    return max(0.0, current_time - os.path.getmtime(cache_file))

def _delete_cache_file_if_retained_too_long(cache_file, now=None):
    """Delete one result file if it has reached the day-91 retention boundary."""
    current_time=time.time() if now is None else now
    with _cache_lock:
        try:
            with _locked_runtime_file(_cache_operations_lock_path()):
                if (
                    os.path.exists(cache_file)
                    and _cache_file_age(cache_file, current_time) >= CACHE_DELETE_AGE_SECONDS
                ):
                    os.unlink(cache_file)
                    return True
        except OSError:
            return False
    return False

def _prune_semantic_index_orphans():
    """Remove semantic entries whose corresponding result file no longer exists."""
    if not os.path.exists(_semantic_index_path()):
        return 0
    with _semantic_index_lock, _locked_runtime_file(_semantic_index_path() + ".lock"):
        index=_load_semantic_index()
        if not index:
            return 0
        cache_dir=get_cache_dir()
        retained={
            key: meta for key, meta in index.items()
            if os.path.exists(os.path.join(cache_dir, key + ".json"))
        }
        removed=len(index) - len(retained)
        if removed:
            _save_semantic_index(retained)
        return removed

def prune_cache(now=None, force=False):
    """Delete result files beginning on day 91 and prune semantic-index orphans.

    Normal calls scan at most once per hour per process. ``force=True`` is used
    by the maintenance CLI/timer and tests.
    """
    global _last_cache_cleanup_at
    current_time=time.time() if now is None else now
    with _cache_cleanup_lock:
        if not force and current_time - _last_cache_cleanup_at < CACHE_CLEANUP_INTERVAL_SECONDS:
            return {"deleted_files": 0, "pruned_index_entries": 0, "errors": 0, "skipped": True}
        _last_cache_cleanup_at=current_time

    cache_dir=get_cache_dir()
    deleted=0
    errors=0
    with _cache_lock:
        try:
            with _locked_runtime_file(_cache_operations_lock_path()):
                for entry in os.scandir(cache_dir):
                    if not entry.is_file(follow_symlinks=False) or not _cache_result_filename(entry.name):
                        continue
                    try:
                        if _cache_file_age(entry.path, current_time) >= CACHE_DELETE_AGE_SECONDS:
                            os.unlink(entry.path)
                            deleted+=1
                    except OSError:
                        errors+=1
        except OSError:
            errors+=1

    pruned=_prune_semantic_index_orphans()
    return {"deleted_files": deleted, "pruned_index_entries": pruned, "errors": errors, "skipped": False}

def read_from_cache(query, engine, offset, ttl_minutes, variant=None):
    prune_cache()
    cache_file = _cache_file_path(query, engine, offset, variant)
    if not os.path.exists(cache_file):
        return None

    try:
        file_age = _cache_file_age(cache_file)
    except OSError:
        return None
    effective_ttl_seconds=min(ttl_minutes * 60, CACHE_MAX_READ_AGE_SECONDS)
    if file_age >= CACHE_DELETE_AGE_SECONDS:
        _delete_cache_file_if_retained_too_long(cache_file)
        _prune_semantic_index_orphans()
        return None
    if file_age > effective_ttl_seconds:
        return None # Cache expired

    try:
        with open(cache_file, 'r', encoding='utf-8') as f:
            result=json.load(f)
            if not is_cacheable_result(result):
                return None
            if engine == "fetch" and isinstance(result, dict):
                result["url"]=query
            return result
    except FileNotFoundError:
        return None  # Another process may prune an expired file.
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"Warning: Failed to read cache: {exc}\n")
        return None

def is_cacheable_result(result):
    """Never persist or reuse failed or partially failed upstream responses."""
    return isinstance(result, dict) and not any(
        result.get(field) for field in ("error", "brave_error", "perplexity_error")
    )

def write_to_cache(query, engine, offset, result, variant=None):
    if not is_cacheable_result(result):
        return
    prune_cache()
    cache_file = _cache_file_path(query, engine, offset, variant)
    try:
        with _cache_lock:
            with _locked_runtime_file(_cache_operations_lock_path()):
                target_dir = os.path.dirname(cache_file) or get_cache_dir()
                fd, temp_path = tempfile.mkstemp(prefix="cache-", suffix=".json", dir=target_dir)
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False)
                    os.replace(temp_path, cache_file)
                finally:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
    except OSError as e:
        sys.stderr.write(f"Warning: Failed to write to cache: {e}\n")

def backfill_semantic_index(query, engine, offset, variant=None):
    """Ensure an exact cache hit can still be reused by future semantic lookups."""
    cache_key = get_cache_key(query, engine, offset, variant)
    key = cache_key.replace(".json", "")
    with _semantic_index_lock:
        index = _load_semantic_index()
        if key in index:
            return
    update_semantic_index(query, engine, offset, cache_key, variant=variant)

# ---------------------------------------------------------------------------
# Semantic cache (optional — requires fastembed)
# ---------------------------------------------------------------------------
_embedding_model = None
_embedding_model_lock = threading.Lock()

def _get_embedding_model():
    """Lazily load the fastembed TextEmbedding model. Returns None if unavailable."""
    global _embedding_model
    with _embedding_model_lock:
        if _embedding_model is None:
            try:
                from fastembed import TextEmbedding
            except ModuleNotFoundError as exc:
                if exc.name != "fastembed":
                    raise
                sys.stderr.write("Warning: fastembed not installed — semantic cache disabled. Run: pip install fastembed\n")
                _embedding_model = False  # sentinel: don't retry import
            else:
                sys.stderr.write("[ccsearch] Loading embedding model (BAAI/bge-small-en-v1.5)...\n")
                _embedding_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    return _embedding_model if _embedding_model is not False else None

def _compute_embedding(text):
    """Return embedding as list[float], or None if fastembed unavailable."""
    model = _get_embedding_model()
    if model is None:
        return None
    return next(model.embed([text])).tolist()

def _cosine_sim(a, b):
    """Pure-Python cosine similarity between two equal-length float lists."""
    if len(a) != len(b):
        raise ValueError("Semantic embedding dimensions do not match")
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
            index=json.load(f)
        if not isinstance(index, dict):
            raise ValueError("semantic index must be an object")
        return index
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"Warning: Failed to read semantic index: {exc}\n")
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
    except OSError as e:
        sys.stderr.write(f"Warning: could not save semantic index: {e}\n")

_NUMBER_TOKEN_RE=re.compile(r"\d+(?:[.,]\d+)*")

def _query_number_signature(query):
    """Return the numbers in a query; version/price/year changes must not share a cache entry."""
    return sorted(_NUMBER_TOKEN_RE.findall(str(query or "")))

def _semantic_variant(meta):
    variant=meta.get("variant") if isinstance(meta, dict) else None
    return variant if isinstance(variant, dict) else {}

def read_from_semantic_cache(query, engine, offset, ttl_minutes, threshold, variant=None):
    """Return (cached_result, similarity) or (None, 0.0) when no semantic match found.

    Candidates must share engine, offset, request variant, and every number in
    the query, so "Opus 5.5 pricing" cannot reuse "Opus 5 pricing".
    """
    prune_cache()
    index = _load_semantic_index()
    if not index:
        return None, 0.0

    q_emb = None

    best_key, best_sim = None, -1.0
    cache_dir = get_cache_dir()
    deleted_stale_entry=False
    for key, meta in index.items():
        if not _cache_result_filename(key + ".json") or not isinstance(meta, dict):
            raise ValueError("Invalid semantic index entry")
        if meta.get("engine") != engine or meta.get("offset") != offset:
            continue
        if _semantic_variant(meta) != {k: v for k, v in (variant or {}).items() if v not in (None, "", [], {})}:
            continue
        if isinstance(meta.get("query"), str) and _query_number_signature(meta["query"]) != _query_number_signature(query):
            continue
        cache_file = os.path.join(cache_dir, key + ".json")
        if not os.path.exists(cache_file):
            continue
        try:
            file_age=_cache_file_age(cache_file)
        except OSError:
            continue
        if file_age >= CACHE_DELETE_AGE_SECONDS:
            deleted_stale_entry = (
                _delete_cache_file_if_retained_too_long(cache_file)
                or deleted_stale_entry
            )
            continue
        if file_age > min(ttl_minutes * 60, CACHE_MAX_READ_AGE_SECONDS):
            continue
        emb = meta.get("embedding")
        if not emb:
            continue
        # Avoid loading the embedding runtime when no fresh candidate applies.
        if q_emb is None:
            q_emb = _compute_embedding(query)
            if q_emb is None:
                return None, 0.0
        sim = _cosine_sim(q_emb, emb)
        if sim > best_sim:
            best_sim, best_key = sim, key

    if deleted_stale_entry:
        _prune_semantic_index_orphans()

    if best_key and best_sim >= threshold:
        cache_file = os.path.join(cache_dir, best_key + ".json")
        try:
            with open(cache_file, encoding="utf-8") as f:
                result = json.load(f)
            if is_cacheable_result(result):
                try:
                    result["_cache_mtime"] = os.path.getmtime(cache_file)
                except OSError:
                    pass
                return result, round(best_sim, 4)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"Warning: Failed to read semantic cache result: {exc}\n")

    return None, 0.0

def update_semantic_index(query, engine, offset, cache_key_filename, variant=None):
    """Compute and store the query embedding in the semantic index."""
    emb = _compute_embedding(query)
    if emb is None:
        return
    key = cache_key_filename.replace(".json", "")
    entry = {"query": query, "engine": engine, "offset": offset, "embedding": emb}
    cleaned_variant = {k: v for k, v in (variant or {}).items() if v not in (None, "", [], {})}
    if cleaned_variant:
        entry["variant"] = cleaned_variant
    with _semantic_index_lock, _locked_runtime_file(_semantic_index_path() + ".lock"):
        index = _load_semantic_index()
        index[key] = entry
        _save_semantic_index(index)

# ---------------------------------------------------------------------------

def _brave_rate_limit_path():
    return os.path.join(get_cache_dir(), "brave_subscription_rate_limit.json")

def _brave_key_rotation_path():
    return os.path.join(get_cache_dir(), "brave_key_round_robin.json")

def _brave_key_fingerprint(api_key):
    """Return a non-secret identifier for one Brave subscription token."""
    token=str(api_key or "").strip()
    if not token:
        return "default"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

def _brave_rate_limit_windows(payload):
    """Normalize on-disk Brave limiter state into per-key timestamp windows."""
    if not isinstance(payload, dict):
        return {}
    windows=payload.get("windows")
    if isinstance(windows, dict):
        return dict(windows)
    timestamps=payload.get("timestamps")
    if isinstance(timestamps, list):
        return {"default": timestamps}
    return {}

def _brave_requests_per_second(config):
    """Return the configured per-key Brave rate capped at the Search plan's 50 RPS."""
    requested=config.getfloat('Brave', 'requests_per_second', fallback=1.0)
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError("Brave requests_per_second must be a finite positive number")
    return max(1, min(BRAVE_SEARCH_MAX_RPS, int(requested)))

def _wait_for_brave_rate_limit(config, key_fingerprint="default", now_fn=time.time, sleep_fn=time.sleep):
    """Acquire one Brave request slot for a single subscription key."""
    capacity=_brave_requests_per_second(config)
    fingerprint=str(key_fingerprint or "default")
    state_path=_brave_rate_limit_path()
    while True:
        with _brave_rate_limit_thread_lock:
            with _locked_runtime_file(state_path) as state_file:
                now=now_fn()
                state_file.seek(0)
                try:
                    payload=json.load(state_file)
                    windows=_brave_rate_limit_windows(payload)
                except json.JSONDecodeError as exc:
                    if state_file.tell():
                        sys.stderr.write(f"Warning: Resetting malformed Brave rate-limit state: {exc}\n")
                    windows={}
                timestamps=[
                    float(ts) for ts in windows.get(fingerprint, [])
                    if isinstance(ts, (int, float)) and 0 <= now - float(ts) < 1.0
                ]
                if len(timestamps) < capacity:
                    timestamps.append(now)
                    windows[fingerprint]=timestamps
                    state_file.seek(0)
                    state_file.truncate()
                    json.dump({"windows": windows}, state_file)
                    state_file.flush()
                    return
                sleep_for=max(0.001, timestamps[0] + 1.0 - now)
        # Never hold the shared lock while waiting: other keys have free slots.
        sleep_fn(sleep_for)

def _next_brave_key_index(key_count):
    """Return the next round-robin index shared by local CLI/API/MCP processes."""
    if key_count <= 1:
        return 0
    state_path=_brave_key_rotation_path()
    with _brave_key_rotation_thread_lock:
        with _locked_runtime_file(state_path) as state_file:
            state_file.seek(0)
            try:
                payload=json.load(state_file)
                counter=int(payload.get("counter", 0)) if isinstance(payload, dict) else 0
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                if state_file.tell():
                    sys.stderr.write(f"Warning: Resetting malformed Brave key-rotation state: {exc}\n")
                counter=0
            if counter < 0:
                counter=0
            index=counter % key_count
            state_file.seek(0)
            state_file.truncate()
            json.dump({"counter": counter + 1}, state_file)
            state_file.flush()
            return index

def retry_request(method, url, max_retries, before_attempt=None, **kwargs):
    """Request wrapper with a simple Exponential Backoff mechanism"""
    if type(max_retries) is not int or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer.")
    for attempt in range(max_retries + 1):
        try:
            if before_attempt is not None:
                before_attempt()
            if method.upper() == 'GET':
                response = requests.get(url, **kwargs)
            else:
                response = requests.post(url, **kwargs)
            response.raise_for_status()
            return response
        except (requests.exceptions.RequestException) as e:
            if isinstance(e, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                              requests.exceptions.MissingSchema, requests.exceptions.InvalidHeader)):
                raise
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

# ---------------------------------------------------------------------------
# Date normalization
# ---------------------------------------------------------------------------
_RELATIVE_AGE_RE=re.compile(
    r"^\s*(\d+)\s*(second|minute|min|hour|hr|day|week|month|year)s?\s+ago\s*$", re.IGNORECASE
)
_RELATIVE_AGE_DAYS={"second": 0, "minute": 0, "min": 0, "hour": 0, "hr": 0, "day": 1, "week": 7, "month": 30, "year": 365}
_TEXT_DATE_FORMATS=(
    "%B %d, %Y", "%b %d, %Y", "%A, %B %d, %Y", "%a, %B %d, %Y", "%A, %b %d, %Y",
    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y",
)

def _as_utc_date(dt):
    if dt.tzinfo is not None:
        dt=dt.astimezone(timezone.utc)
    return dt.date()

def _parse_single_date(value, reference=None):
    """Parse one date-like value into a ``datetime.date`` or return None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Unix timestamps (seconds or milliseconds).
        seconds=float(value) / 1000.0 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text=value.strip()
    if not text:
        return None
    if reference is None:
        ref=datetime.now(timezone.utc).date()
    elif isinstance(reference, datetime):
        ref=_as_utc_date(reference)
    else:
        ref=reference
    lowered=text.lower()
    if lowered in {"today", "just now"}:
        return ref
    if lowered == "yesterday":
        return ref - timedelta(days=1)
    relative=_RELATIVE_AGE_RE.match(text)
    if relative:
        amount=int(relative.group(1))
        unit=relative.group(2).lower()
        return ref - timedelta(days=amount * _RELATIVE_AGE_DAYS[unit])
    iso_candidate=text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return _as_utc_date(datetime.fromisoformat(iso_candidate))
    except ValueError:
        pass
    iso_prefix=re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso_prefix:
        try:
            return datetime(int(iso_prefix.group(1)), int(iso_prefix.group(2)), int(iso_prefix.group(3))).date()
        except ValueError:
            return None
    cleaned=re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text)
    for fmt in _TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    try:
        return _as_utc_date(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        return None

def normalize_published_at(value, reference=None):
    """Normalize one date value or a list of candidates to ``YYYY-MM-DD``.

    Absolute dates win over relative ages such as "3 days ago" because they do
    not drift when a result is served from cache.
    """
    candidates=value if isinstance(value, (list, tuple)) else [value]
    relative=None
    for candidate in candidates:
        if isinstance(candidate, str) and (_RELATIVE_AGE_RE.match(candidate.strip()) or candidate.strip().lower() in {"today", "yesterday", "just now"}):
            if relative is None:
                relative=_parse_single_date(candidate, reference)
            continue
        parsed=_parse_single_date(candidate, reference)
        if parsed:
            return parsed.isoformat()
    return relative.isoformat() if relative else None

def _freshness_bounds(freshness, today=None):
    """Return (earliest, latest) dates implied by a Brave freshness value."""
    if not freshness:
        return None, None
    today=today or datetime.now(timezone.utc).date()
    window=FRESHNESS_WINDOWS_DAYS.get(freshness)
    if window is not None:
        return today - timedelta(days=window), None
    matched=_FRESHNESS_RANGE_RE.match(freshness)
    if matched:
        return (datetime.strptime(matched.group(1), "%Y-%m-%d").date(),
                datetime.strptime(matched.group(2), "%Y-%m-%d").date())
    return None, None

# ---------------------------------------------------------------------------
# Prompt-injection detection
# ---------------------------------------------------------------------------
# Header-style blocks remove everything from the header to the end of the text unit.
_INJECTION_HEADER_PATTERNS=(
    ("uppercase-ai-instructions", re.compile(
        r"\[?\s*(?:CRITICAL |IMPORTANT |URGENT |SYSTEM |MANDATORY )?(?:INSTRUCTIONS?|NOTICE|MESSAGE|DIRECTIVE)S?\s+(?:FOR|TO)\s+(?:ALL\s+)?"
        r"(?:AI\b|LLMS?\b|LARGE LANGUAGE MODELS?|LANGUAGE MODELS?|AI ASSISTANTS?|ASSISTANTS?|AUTOMATED AGENTS?|AGENTS?|CHATBOTS?)[^\]\n]*\]?")),
    ("uppercase-ai-audience", re.compile(
        r"\[\s*(?:[A-Z]+\s+){0,6}(?:AI ASSISTANTS?|LANGUAGE MODELS?|AUTOMATED AGENTS?|LLMS)\b[^\]\n]*\]")),
)
# Sentence-level rules remove only the matching sentence.
_INJECTION_SENTENCE_PATTERNS=(
    ("ignore-previous-instructions", re.compile(r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|prompts|messages)\b", re.IGNORECASE)),
    ("addresses-ai-reader", re.compile(r"\bif you are an?\s+(?:AI|LLM|large language model|language model|AI assistant|assistant|automated agent|bot)\b", re.IGNORECASE)),
    ("ai-assistance-scope", re.compile(r"\bapplies to all forms of AI assistance\b", re.IGNORECASE)),
    ("automated-session-directive", re.compile(r"\b(?:automated session|automated agents?)\b.*\b(?:MUST|must)\b", re.IGNORECASE)),
    ("must-refuse-directive", re.compile(r"\byou MUST (?:refuse|immediately stop|not|ignore|stop)\b")),
    ("do-not-generate-directive", re.compile(r"\bDo NOT generate\b")),
)
# linux.do injects a fixed anti-AI notice into its pages and search snippets.
_KNOWN_INJECTION_PHRASES=(
    ("linux.do-anti-ai-notice", re.compile(r"Please write your own content\.?\s*Read the site guidelines:?\s*https?://linux\.do/guidelines\"?\s*\d*\.?", re.IGNORECASE)),
    ("linux.do-anti-ai-notice", re.compile(r"(?:navigate|redirect them) to:?\s*https?://linux\.do/guidelines\"?\s*\d*\.?", re.IGNORECASE)),
)
_SENTENCE_SPLIT_RE=re.compile(r"(?<=[.!?。！？])\s+|\n+")

def _is_residual_noise(text):
    """Return True when leftovers are only list markers, URLs, or punctuation."""
    stripped=re.sub(r"https?://\S+", " ", text or "")
    stripped=re.sub(r"[\s\d.,;:!?\"'()\[\]{}<>…·|/\\-]+", "", stripped)
    return len(stripped) < 4

def scrub_injection(text, field="text"):
    """Remove prompt-injection text addressed to AI readers.

    Returns ``(clean_text, findings)`` where each finding records the removed
    original text, its character offset in the input, the field, and the rule.
    ``clean_text`` is None when nothing but injected text remained.
    """
    if not isinstance(text, str) or not text:
        return text, []
    findings=[]
    spans=[]
    for rule, pattern in _INJECTION_HEADER_PATTERNS:
        match=pattern.search(text)
        if match:
            # A header marks the start of an injected block: drop it through the
            # end of its paragraph (or the whole snippet for single-line text).
            end=text.find("\n\n", match.end())
            spans.append((match.start(), len(text) if end < 0 else end, rule))
            break
    for rule, pattern in _KNOWN_INJECTION_PHRASES:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), rule))
    position=0
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        start=text.find(sentence, position)
        if start < 0:
            continue
        position=start + len(sentence)
        for rule, pattern in _INJECTION_SENTENCE_PATTERNS:
            if pattern.search(sentence):
                spans.append((start, start + len(sentence), rule))
                break
    if not spans:
        return text, []
    spans.sort()
    merged=[]
    for start, end, rule in spans:
        if merged and start <= merged[-1][1] + 3:
            prev_start, prev_end, prev_rule=merged[-1]
            merged[-1]=(prev_start, max(prev_end, end), prev_rule)
        else:
            merged.append((start, end, rule))
    pieces=[]
    cursor=0
    for start, end, rule in merged:
        pieces.append(text[cursor:start])
        findings.append({"field": field, "start": start, "text": text[start:end].strip(), "rule": rule})
        cursor=end
    pieces.append(text[cursor:])
    cleaned=re.sub(r"[ \t]{2,}", " ", "".join(pieces))
    cleaned=re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    cleaned=re.sub(r"(?:\s*[·|]\s*)+(?:\.\.\.|…)?$", "", cleaned).strip()
    if _is_residual_noise(cleaned):
        return None, findings
    return cleaned, findings

def _scrub_result_item(item, text_fields=("title", "description", "snippet"), list_fields=("snippets",)):
    """Scrub injection text from one structured search result in place."""
    findings=[]
    for field in text_fields:
        value=item.get(field)
        if isinstance(value, str) and value:
            cleaned, found=scrub_injection(value, field=field)
            if found:
                findings.extend(found)
                item[field]=cleaned or ""
    for field in list_fields:
        values=item.get(field)
        if not isinstance(values, list):
            continue
        kept=[]
        for idx, value in enumerate(values):
            if not isinstance(value, str):
                kept.append(value)
                continue
            cleaned, found=scrub_injection(value, field=f"{field}[{idx}]")
            findings.extend(found)
            if cleaned:
                kept.append(cleaned)
        item[field]=kept
    if findings:
        item["injection_suspected"]=item.get("injection_suspected", []) + findings
    return item

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
            if not isinstance(value, str):
                raise ValueError("Host filters must be provided as a string or list of strings.")
            raw_values.extend(re.split(r"[\s,]+", value))
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

def _brave_quota_path():
    return os.path.join(get_cache_dir(), "brave_quota.json")

def _parse_rate_limit_header(value):
    parts=[]
    for raw in str(value or "").split(","):
        raw=raw.strip()
        if not raw:
            continue
        try:
            parts.append(int(float(raw)))
        except ValueError:
            parts.append(None)
    return parts

def _brave_quota_windows(headers):
    """Translate Brave X-RateLimit-* headers into labeled windows."""
    if not headers:
        return []
    raw_limit=headers.get("X-RateLimit-Limit")
    if not isinstance(raw_limit, str):
        return []
    limits=_parse_rate_limit_header(raw_limit)
    if not any(isinstance(limit, int) for limit in limits):
        return []
    remaining=_parse_rate_limit_header(headers.get("X-RateLimit-Remaining"))
    resets=_parse_rate_limit_header(headers.get("X-RateLimit-Reset"))
    policy=[part.strip() for part in str(headers.get("X-RateLimit-Policy") or "").split(",") if part.strip()]
    windows=[]
    for idx, limit in enumerate(limits):
        window_seconds=None
        if idx < len(policy):
            matched=re.search(r"w=(\d+)", policy[idx])
            window_seconds=int(matched.group(1)) if matched else None
        windows.append({
            "window_seconds": window_seconds,
            "limit": limit,
            "remaining": remaining[idx] if idx < len(remaining) else None,
            "reset_seconds": resets[idx] if idx < len(resets) else None,
            # Brave reports 0 for windows without a quota on the plan.
            "unlimited": limit == 0,
        })
    return windows

def _record_brave_quota(api_key, headers):
    """Persist the most recent Brave rate-limit headers for diagnostics."""
    windows=_brave_quota_windows(headers)
    if not windows:
        return
    try:
        with _locked_runtime_file(_brave_quota_path()) as state_file:
            state_file.seek(0)
            try:
                payload=json.load(state_file)
                if not isinstance(payload, dict):
                    payload={}
            except json.JSONDecodeError:
                payload={}
            payload[_brave_key_fingerprint(api_key)]={"windows": windows, "observed_at": _utc_now_iso()}
            state_file.seek(0)
            state_file.truncate()
            json.dump(payload, state_file)
            state_file.flush()
    except OSError as exc:
        sys.stderr.write(f"Warning: could not record Brave quota headers: {exc}\n")

def _load_brave_quota():
    path=_brave_quota_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload=json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}

def _brave_request(url, api_key, config, section, params):
    """Run one rate-limited Brave request and record its quota headers."""
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key
    }
    max_retries = config.getint(section, 'max_retries', fallback=2)
    try:
        response = retry_request(
            'GET', url, max_retries,
            before_attempt=lambda: _wait_for_brave_rate_limit(config, key_fingerprint=_brave_key_fingerprint(api_key)),
            headers=headers, params=params, timeout=(10, 30),
        )
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None:
            _record_brave_quota(api_key, exc.response.headers)
        raise
    _record_brave_quota(api_key, getattr(response, "headers", None))
    return response

def _apply_search_options(params, config, section, search_options=None):
    """Merge per-request freshness/country/search_lang over config defaults."""
    options=search_options or {}
    freshness=options.get("freshness")
    if not freshness:
        freshness=config.get(section, 'freshness', fallback='').strip().lower()
    if freshness in FRESHNESS_WINDOWS_DAYS or _FRESHNESS_RANGE_RE.match(freshness or ""):
        params['freshness'] = freshness
    for name in ("country", "search_lang"):
        value=options.get(name)
        if value:
            params[name]=value
    return params

def perform_brave_search(query, api_key, config, offset=None, search_options=None):
    url = "https://api.search.brave.com/res/v1/web/search"
    count = config.getint('Brave', 'count', fallback=10)
    params = {"q": query, "count": count}

    safesearch = config.get('Brave', 'safesearch', fallback='moderate').lower()
    if safesearch not in ['off', 'moderate', 'strict']:
        raise ValueError("Brave safesearch must be off, moderate, or strict.")
    params['safesearch'] = safesearch
    _apply_search_options(params, config, 'Brave', search_options)

    if offset is not None:
        params['offset'] = offset

    response = _brave_request(url, api_key, config, 'Brave', params)
    data = response.json()
    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError("Brave returned an invalid or error response.")
    web = data.get('web', {})
    if not isinstance(web, dict) or not isinstance(web.get('results', []), list):
        raise RuntimeError("Brave returned invalid web results.")
    results = []
    if 'results' in web:
        for item in web['results']:
            if not isinstance(item, dict):
                raise RuntimeError("Brave returned an invalid result item.")
            result_url=item.get("url")
            results.append({
                "title": _clean_api_text(item.get("title")),
                "url": result_url,
                "description": _clean_api_text(item.get("description")),
                "hostname": urlparse(result_url).hostname if result_url else None,
                "published_at": normalize_published_at([item.get("page_age"), item.get("age")]),
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

    choices = data.get("choices") if isinstance(data, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Perplexity returned no valid answer content.")
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

VERIFY_VERDICTS=("supported", "contradicted", "not_found")

def normalize_claims(claims):
    """Normalize verify input (list or newline-separated string) into claims."""
    if isinstance(claims, str):
        raw=[line for line in claims.splitlines()]
    elif isinstance(claims, (list, tuple)):
        raw=list(claims)
    else:
        raise ValueError("'claims' must be a list of strings or a newline-separated string.")
    normalized=[]
    for claim in raw:
        if not isinstance(claim, str):
            raise ValueError("Each claim must be a string.")
        cleaned=re.sub(r"\s+", " ", re.sub(r"^\s*(?:\d+[.)]|[-*•])\s+", "", claim)).strip()
        if cleaned:
            normalized.append(cleaned)
    if not normalized:
        raise ValueError("At least one claim is required.")
    if len(normalized) > MAX_VERIFY_CLAIMS:
        raise ValueError(f"At most {MAX_VERIFY_CLAIMS} claims can be verified per request.")
    for claim in normalized:
        if len(claim) > MAX_VERIFY_CLAIM_CHARS:
            raise ValueError(f"Each claim must be at most {MAX_VERIFY_CLAIM_CHARS} characters.")
    return normalized

def _extract_json_object(text):
    """Return the first JSON object embedded in model output, or None."""
    if not isinstance(text, str):
        return None
    fenced=re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates=[fenced.group(1)] if fenced else []
    start=text.find("{")
    end=text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            parsed=json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None

def _resolve_verify_sources(raw_sources, citations):
    """Map model-cited sources onto real citation URLs returned by the provider."""
    citation_urls=[citation["url"] for citation in citations if citation.get("url")]
    known={_normalize_result_url(url): url for url in citation_urls}
    resolved=[]
    dropped=0
    for source in raw_sources if isinstance(raw_sources, list) else [raw_sources]:
        url=None
        if isinstance(source, bool):
            source=None
        if isinstance(source, (int, float)) or (isinstance(source, str) and re.fullmatch(r"\[?\d+\]?", source.strip())):
            number=int(str(source).strip("[] "))
            if 1 <= number <= len(citation_urls):
                url=citation_urls[number - 1]
        elif isinstance(source, str) and source.strip():
            url=known.get(_normalize_result_url(source.strip()))
        if url and url not in resolved:
            resolved.append(url)
        elif source not in (None, ""):
            dropped+=1
    return resolved, dropped

def perform_perplexity_verify(claims, api_key, config):
    """Verify claims one by one with Perplexity and return citation-backed verdicts."""
    claims=normalize_claims(claims)
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/anthropics/claude-code",
        "X-Title": "ccsearch"
    }
    model = config.get('Perplexity', 'model', fallback='perplexity/sonar')
    max_tokens = config.getint('Perplexity', 'max_tokens', fallback=1024)
    numbered="\n".join(f"{idx}. {claim}" for idx, claim in enumerate(claims, 1))
    system_prompt = (
        "You are a meticulous fact-checker. Search the web for current, authoritative sources "
        "(official documentation and primary sources first) and check each claim independently. "
        "Cite sources with numbered markers such as [1]. Reply with JSON only."
    )
    user_prompt = (
        "Check each numbered claim.\n\n"
        f"{numbered}\n\n"
        "Return exactly this JSON shape:\n"
        '{"results": [{"claim": <claim number>, "verdict": "supported" | "contradicted" | "not_found", '
        '"sources": [<citation numbers you relied on>], "note": "<one sentence; for contradicted claims state the correct value>"}]}\n'
        "Use not_found when the sources neither confirm nor refute the claim."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": max(max_tokens, 1024),
    }
    max_retries = config.getint('Perplexity', 'max_retries', fallback=2)
    response = retry_request('POST', url, max_retries, headers=headers, json=payload, timeout=(10, 120))
    data = response.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Perplexity returned no valid verification content.")
    citations = _extract_perplexity_citations(data)
    parsed = _extract_json_object(html_lib.unescape(content))
    if parsed is None or not isinstance(parsed.get("results"), list):
        raise RuntimeError("Perplexity returned verification output that is not the requested JSON.")

    by_number={}
    for entry in parsed["results"]:
        if not isinstance(entry, dict):
            continue
        number=entry.get("claim")
        if isinstance(number, str) and number.strip().isdigit():
            number=int(number.strip())
        if isinstance(number, int) and 1 <= number <= len(claims) and number not in by_number:
            by_number[number]=entry
    if not by_number and len(parsed["results"]) == len(claims):
        by_number={idx: entry for idx, entry in enumerate(parsed["results"], 1) if isinstance(entry, dict)}

    results=[]
    for idx, claim in enumerate(claims, 1):
        entry=by_number.get(idx)
        if entry is None:
            results.append({"claim": claim, "verdict": "not_found", "sources": [], "note": "The model returned no verdict for this claim."})
            continue
        verdict=str(entry.get("verdict") or "").strip().lower().replace(" ", "_")
        if verdict not in VERIFY_VERDICTS:
            verdict="not_found"
        sources, dropped=_resolve_verify_sources(entry.get("sources", []), citations)
        note=entry.get("note") if isinstance(entry.get("note"), str) else ""
        item={"claim": claim, "verdict": verdict, "sources": sources, "note": note.strip()}
        if verdict != "not_found" and not sources:
            # A verdict without a fetchable citation cannot be re-checked.
            item["source_backed"]=False
        if dropped:
            item["unverifiable_sources_dropped"]=dropped
        results.append(item)
    summary={verdict: sum(1 for item in results if item["verdict"] == verdict) for verdict in VERIFY_VERDICTS}
    result={
        "engine": "perplexity-verify",
        "model": model,
        "query": "\n".join(claims),
        "claims": claims,
        "results": results,
        "summary": summary,
    }
    if citations:
        result["citations"]=citations
    return result

def _extract_perplexity_citations(data):
    """Normalize citation-like payloads from OpenRouter/Perplexity responses."""
    citations=[]
    seen=set()
    message={}
    choices=data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        candidate=choices[0].get("message")
        if isinstance(candidate, dict):
            message=candidate

    payloads=[]
    for container in (data, message):
        for field in ("citations", "references", "sources"):
            raw=container.get(field)
            if raw:
                payloads.append(raw)
    annotations=message.get("annotations")
    if annotations:
        payloads.append(annotations)

    for raw in payloads:
        if not raw:
            continue
        entries=raw if isinstance(raw, list) else [raw]
        for entry in entries:
            if isinstance(entry, str):
                url=entry.strip()
                title=None
            elif isinstance(entry, dict):
                nested=entry.get("url_citation")
                if isinstance(nested, dict):
                    entry=nested
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

def perform_both_search(query, brave_api_key, perplexity_api_key, config, offset=None, search_options=None):
    """Run both Brave and Perplexity searches concurrently and merge results"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        future_brave = executor.submit(perform_brave_search, query, brave_api_key, config, offset, search_options)
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
    if result.get("brave_error") and result.get("perplexity_error"):
        result["error"] = "Both search engines failed."
    return result

def perform_llm_context_search(query, api_key, config, search_options=None):
    url = "https://api.search.brave.com/res/v1/llm/context"

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

    if threshold_mode not in ['strict', 'balanced', 'lenient', 'disabled']:
        raise ValueError("LLMContext context_threshold_mode must be strict, balanced, lenient, or disabled.")
    params['context_threshold_mode'] = threshold_mode
    _apply_search_options(params, config, 'LLMContext', search_options)

    response = _brave_request(url, api_key, config, 'LLMContext', params)
    data = response.json()

    if not isinstance(data, dict) or data.get("error"):
        raise RuntimeError("Brave LLM Context returned an invalid or error response.")
    grounding = data.get("grounding", {})
    sources = data.get("sources", {})
    if not isinstance(grounding, dict) or not isinstance(grounding.get("generic", []), list) or not isinstance(sources, dict):
        raise RuntimeError("Brave LLM Context returned invalid grounding or sources.")

    results = []
    for item in grounding.get("generic", []):
        if not isinstance(item, dict) or not isinstance(item.get("snippets", []), list):
            raise RuntimeError("Brave LLM Context returned an invalid result item.")
        result_url=item.get("url")
        source_meta=sources.get(result_url, {}) if result_url else {}
        if not isinstance(source_meta, dict):
            source_meta={}
        entry={
            "url": result_url,
            "title": _clean_api_text(item.get("title") or source_meta.get("title")),
            "hostname": source_meta.get("hostname") or (urlparse(result_url).hostname if result_url else None),
            "published_at": normalize_published_at(source_meta.get("age")),
            # Raw provider ages mix several formats; shaping keeps them only in verbose mode.
            "age": source_meta.get("age"),
            "snippets": [
                cleaned
                for snippet in item.get("snippets", [])
                for cleaned in [_clean_api_text(snippet, preserve_newlines=True)]
                if cleaned
            ]
        }
        short=_clean_api_text(source_meta.get("snippet"), preserve_newlines=True)
        if short:
            entry["snippet"]=short
        results.append(entry)

    results=_annotate_rank(_dedupe_result_items(results))
    result={
        "engine": "llm-context",
        "query": query,
        "result_count": len(results),
        "results": results,
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

# Documentation chrome: side navigation, tables of contents, breadcrumbs, pagers,
# author boxes, and call-to-action bands. Matched as whole hint tokens.
_NAVIGATION_WORDS=(
    "sidebar", "side-bar", "sidenav", "side-nav", "toc", "table-of-contents", "on-this-page", "breadcrumb",
    "breadcrumbs", "pagination", "pager", "prev-next", "post-nav", "post-navigation", "related-post",
    "related-posts", "related-articles", "author-bio", "author-box", "about-the-author", "cta",
    "call-to-action", "skip-link", "skip-to-content", "sr-only", "visually-hidden",
)
# Whole class/id tokens only: "docs-sidebar" or "toc-list" match, while utility
# variants such as "toc-visible:md:col-span-6" or "has-sidebar" do not.
_NAVIGATION_TOKEN_RE=re.compile(
    r"(?:[a-z0-9]+[-_])*(?:" + "|".join(re.escape(word) for word in _NAVIGATION_WORDS) + r")"
    r"(?:[-_](?:nav|navigation|menu|wrapper|container|list|links|block|area|section|box|panel|widget|left|right|root|inner|content))?"
)
_NAVIGATION_TOKEN_EXCLUDED_PREFIXES=("has-", "with-", "no-", "show-", "hide-", "is-", "toggle-", "open-")
_PREV_NEXT_RE=re.compile(
    r"^\W*(?:previous|prev|next|older|newer)(?:\s+(?:post|posts|article|blog|page|chapter|entry|story|lesson))?\W*$",
    re.IGNORECASE,
)

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
        if tag.parent is None:
            continue
        style=(tag.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            tag.extract()
            continue
        if _is_navigation_chrome(tag):
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

def _link_density(node, text_len=None):
    text_len=len(node.get_text(" ", strip=True)) if text_len is None else text_len
    if not text_len:
        return 0.0
    link_text_len=sum(len(link.get_text(" ", strip=True)) for link in node.find_all("a"))
    return link_text_len / text_len

def _is_navigation_chrome(tag):
    """Return True for sidebars, tables of contents, breadcrumbs, pagers, and CTAs."""
    if (tag.name or "").lower() in {"main", "article", "body", "html"}:
        return False
    if not _has_navigation_hint(tag):
        return False
    text_len=len(tag.get_text(" ", strip=True))
    # Small chrome goes unconditionally; large blocks must look like link lists.
    return text_len <= 1200 or _link_density(tag, text_len) >= 0.5

def _has_navigation_hint(tag):
    tokens=[str(tag.get("id") or "")] + [str(value) for value in (tag.get("class") or [])] + [str(tag.get("data-testid") or "")]
    for token in tokens:
        token=token.strip().lower()
        if not token or any(char in token for char in ":[]()@/!.#"):
            continue
        if token.startswith(_NAVIGATION_TOKEN_EXCLUDED_PREFIXES):
            continue
        if _NAVIGATION_TOKEN_RE.fullmatch(token):
            return True
    for label in (tag.get("aria-label"), tag.get("role")):
        normalized=re.sub(r"\s+", "-", str(label or "").strip().lower())
        if normalized and _NAVIGATION_TOKEN_RE.fullmatch(normalized):
            return True
    return False

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
    """Pick the most article-like subtree from a cleaned HTML body.

    A single substantial ``<main>``/``role=main`` landmark wins outright: docs
    sites often keep large navigation sidebars outside it that would otherwise
    out-score the article on raw text length.
    """
    body=root.body or root
    body_text_len=len(body.get_text(" ", strip=True))
    for selector, parent_names in (("main, [role='main']", ["main"]), ("article", ["article"])):
        landmarks={}
        for node in body.select(selector):
            if node.find_parent(parent_names):
                continue
            # Streaming SSR can duplicate the whole app; identical copies count once.
            landmarks.setdefault(node.get_text(" ", strip=True), node)
        if len(landmarks) != 1:
            continue
        landmark_text, landmark=next(iter(landmarks.items()))
        if len(landmark_text) >= 200 and len(landmark_text) >= body_text_len * 0.15:
            return landmark
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
        # Responsive layouts repeat headings with different whitespace or case.
        if (block.get("type") == "heading" and deduped and deduped[-1].get("type") == "heading"
                and re.sub(r"\s+", " ", deduped[-1]["text"]).casefold() == re.sub(r"\s+", " ", text).casefold()):
            continue
        if len(text) <= 60 and _PREV_NEXT_RE.match(text):
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

        # Page builders (Framer, Webflow) nest real paragraphs several wrappers deep.
        if child.find(["p", "ul", "ol", "table", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"]):
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

    modified_at=(
        _find_meta_content(soup, "property", {"article:modified_time", "og:updated_time"})
        or _find_meta_content(soup, "itemprop", {"datemodified"})
    )
    if modified_at:
        metadata["modified_at"]=modified_at

    json_ld_metadata=_extract_json_ld_metadata(soup, base_url=base_url)
    for key in ("canonical_url", "lang", "description", "author", "published_at", "modified_at"):
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

        if "modified_at" not in metadata:
            modified_at=_coerce_json_ld_string(item.get("dateModified"))
            if modified_at:
                metadata["modified_at"]=modified_at

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
    if content_type in MARKITDOWN_MIME_TO_EXTENSIONS:
        return MARKITDOWN_MIME_TO_EXTENSIONS[content_type]
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
    md=MarkItDown(enable_plugins=False)
    # The file API supplies a reliable extension and avoids retrying converter
    # implementation errors through a second API with different semantics.
    with tempfile.TemporaryDirectory(prefix="ccsearch-document-") as directory:
        tmp_path=os.path.join(directory, "document" + extension)
        with open(tmp_path, "wb") as tmp:
            tmp.write(content_bytes)
        try:
            result=md.convert(tmp_path)
        except Exception as exc:
            return None, f"Binary document conversion failed: {exc}"
        text=result.text_content
        if not isinstance(text, str) or not text.strip():
            return None, "Binary document conversion returned no extractable content."
        return text.strip(), None

def _convert_binary_response(url, response, fetched_via="direct"):
    """Convert supported binary documents to Markdown, when appropriate."""
    content_type=_normalize_content_type(response)
    extension=_guess_extension(_normalize_final_url(response, url), content_type)
    if not content_type and extension not in MARKITDOWN_EXTENSIONS:
        return None
    if content_type and content_type not in MARKITDOWN_MIME_TO_EXTENSIONS and extension not in MARKITDOWN_EXTENSIONS:
        return None

    content, error=_convert_with_markitdown(response.content, _normalize_final_url(response, url), content_type)
    if error:
        return _build_fetch_result(url, fetched_via, response=response, error=error)
    chunks=_chunk_text_content(content, default_type="markdown")
    return _build_fetch_result(
        url,
        fetched_via,
        response=response,
        title=_resolve_filename(response, url) or _title_from_url(_normalize_final_url(response, url)),
        content=content,
        converted_via="markitdown",
        chunks=chunks,
    )

def _detect_cloudflare(response):
    """Check if an HTTP response is a Cloudflare challenge page.

    Normal Cloudflare-hosted pages load ``/cdn-cgi/challenge-platform`` beacon
    scripts, so that marker alone only counts for error responses or pages
    without meaningful text.
    """
    responseText=response.text
    if '<title>Just a moment...</title>' in responseText:
        return True
    if response.headers.get('cf-mitigated', '').lower() == 'challenge':
        return True
    if "_cf_chl_opt" in responseText:
        return True
    for indicator in CLOUDFLARE_INDICATORS:
        if indicator not in responseText:
            continue
        if indicator != "challenge-platform":
            return True
        status=getattr(response, "status_code", None)
        if isinstance(status, int) and status >= 400:
            return True
        if _visible_text_length(responseText) < 300:
            return True
    return False

def _detect_akamai(response):
    """Recognize observed Akamai interstitials, not ordinary CDN scripts."""
    soup=BeautifulSoup(response.text, "html.parser")
    if soup.select_one('#sec-if-cpt-container') is not None and soup.select_one('.scf-akamai-logo, .scf-akamai-logo-sec-abc') is not None:
        return True
    heading=soup.title or soup.find('h1')
    return bool(heading and heading.get_text(' ', strip=True).lower() == 'access denied'
                and 'errors.edgesuite.net/' in response.text)

def _visible_text_length(html):
    """Approximate visible body text length for challenge heuristics."""
    soup=BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.extract()
    return len((soup.body or soup).get_text(" ", strip=True))

def _simple_fetch(url, maxRetries=2):
    """Fetch a webpage. Uses curl_cffi for TLS impersonation when available, otherwise requests.Session."""
    if type(maxRetries) is not int or maxRetries < 0:
        raise ValueError("max_retries must be a non-negative integer.")
    if HAS_CURL_CFFI:
        for attempt in range(maxRetries+1):
            session=cffi_requests.Session(impersonate="chrome")
            try:
                response=session.get(url, headers=FETCH_HEADERS, timeout=30)
                response.raise_for_status()
                return response
            except FETCH_REQUEST_ERRORS as e:
                status=getattr(getattr(e, 'response', None), 'status_code', None)
                if status and 400<=status<500 and status!=429:
                    raise
                if attempt<maxRetries:
                    time.sleep(2**attempt)
                    continue
                raise
            finally:
                session.close()
    return retry_request('GET', url, maxRetries, headers=FETCH_HEADERS, timeout=(10, 30))

def _flaresolverr_fetch(url, flaresolverrUrl, timeout=60000):
    """Fetch a webpage through FlareSolverr proxy."""
    payload={"cmd": "request.get", "url": url, "maxTimeout": timeout}
    httpTimeout=(10, timeout/1000+10)
    response=requests.post(flaresolverrUrl, json=payload, timeout=httpTimeout)
    response.raise_for_status()
    data=response.json()
    if not isinstance(data, dict):
        raise FlareSolverrError("FlareSolverr error: expected a JSON object")
    if data.get("status")!="ok":
        raise FlareSolverrError(f"FlareSolverr error: {data.get('message', 'Unknown error')}")
    solution=data.get("solution") or {}
    if not isinstance(solution, dict) or "response" not in solution:
        raise FlareSolverrError("FlareSolverr error: response body is missing")
    result=requests.Response()
    result.status_code=int(solution.get("status") or 200)
    result.url=solution.get("url") or url
    headers=solution.get("headers") or {}
    if isinstance(headers, dict):
        result.headers.update(headers)
    elif isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict) and item.get("name"):
                result.headers[str(item["name"])]=str(item.get("value", ""))
    body=solution.get("response") or ""
    result._content=body.encode("utf-8") if isinstance(body, str) else bytes(body)
    result.encoding="utf-8"
    if not result.headers.get("Content-Type"):
        result.headers["Content-Type"]="text/html; charset=utf-8"
    return result

def _coerce_flaresolverr_response(value, url):
    """Accept response objects from production and strings from older callers/tests."""
    if hasattr(value, "status_code") and hasattr(value, "content"):
        return value
    response=requests.Response()
    response.status_code=200
    response.url=url
    response.headers["Content-Type"]="text/html; charset=utf-8"
    response._content=(value or "").encode("utf-8") if isinstance(value, str) else bytes(value or b"")
    response.encoding="utf-8"
    return response

def _http_fetch_error(response):
    """Return a stable error for non-success HTTP responses."""
    status=getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        return f"HTTP {status} returned by {_normalize_final_url(response, '')}"
    return None

def _build_flaresolverr_fetch_result(url, value):
    """Extract a FlareSolverr response while preserving transport metadata."""
    response=_coerce_flaresolverr_response(value, url)
    http_error=_http_fetch_error(response)
    if http_error:
        return _build_fetch_result(url, "flaresolverr", response=response, error=http_error)
    if _detect_cloudflare(response):
        return _build_fetch_result(url, "flaresolverr", response=response,
                                   error="Cloudflare challenge remains after browser rendering.")
    if _detect_akamai(response):
        return _build_fetch_result(url, "flaresolverr", response=response,
                                   error="Akamai challenge or access denial remains after browser rendering.")
    final_url=_normalize_final_url(response, url)
    title, clean_text, chunks=_extract_html_content(response.content, base_url=final_url)
    metadata=_extract_html_metadata(response.content, base_url=final_url)
    empty_error=None
    if not clean_text.strip():
        empty_error="No extractable content returned after browser rendering; use an interactive browser."
    return _build_fetch_result(
        url,
        "flaresolverr",
        response=response,
        title=title,
        content=clean_text,
        error=empty_error,
        metadata=metadata,
        chunks=chunks,
    )

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

# ---------------------------------------------------------------------------
# Fetch chain: site API -> direct -> FlareSolverr -> LLM Context -> Wayback
# ---------------------------------------------------------------------------
FETCH_SERVED_FROM={
    "direct": "direct",
    "flaresolverr": "flaresolverr",
    "fxtwitter": "site-api",
    "discourse": "site-api",
    "reddit": "site-api",
    "v2ex": "site-api",
    "llm-context": "llm-context",
    "archive": "archive",
}
EXTENDED_FETCH_FALLBACKS=("llm-context", "archive")
# Failures that mean "blocked or unreachable", not "the page does not exist".
FALLBACK_ELIGIBLE_STATUSES={"cf_challenge", "akamai_challenge", "transport_error", "empty", "spa_shell", "error"}
FALLBACK_ELIGIBLE_HTTP_STATUSES={401, 403, 408, 425, 429, 451, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527, 530}

class SiteApiError(RuntimeError):
    """A site-specific API was unreachable, blocked, or returned an unexpected shape."""
    def __init__(self, message, status="error", http_status=None):
        super().__init__(message)
        self.status=status
        self.http_status=http_status

def _attempt_entry(method, status, started, **extra):
    entry={"method": method, "status": status, "ms": int(round((time.time() - started) * 1000))}
    for key, value in extra.items():
        if value is not None:
            entry[key]=str(value)[:300] if key == "detail" else value
    return entry

def _result_status(result):
    """Classify a fetch result for the attempts log."""
    error=result.get("error") if isinstance(result, dict) else "no result"
    if not error:
        return "ok"
    status_code=result.get("status_code")
    if isinstance(status_code, int) and status_code >= 400:
        return "http_error"
    lowered=str(error).lower()
    if "akamai" in lowered:
        return "akamai_challenge"
    if "cloudflare" in lowered:
        return "cf_challenge"
    if "spa shell" in lowered:
        return "spa_shell"
    if "no extractable content" in lowered:
        return "empty"
    if "binary document" in lowered or "markitdown" in lowered:
        return "conversion_error"
    return "error"

def _http_status_of(result):
    status_code=result.get("status_code") if isinstance(result, dict) else None
    return status_code if isinstance(status_code, int) and status_code >= 400 else None

def _fetch_settings(config):
    flaresolverrUrl=config.get('Fetch', 'flaresolverr_url', fallback='').strip()
    flaresolverrTimeout=config.getint('Fetch', 'flaresolverr_timeout', fallback=60000)
    flaresolverrMode=config.get('Fetch', 'flaresolverr_mode', fallback='fallback').strip().lower()
    if flaresolverrMode not in {"never", "fallback", "always"}:
        raise ValueError("Fetch.flaresolverr_mode must be never, fallback, or always.")
    if flaresolverrMode == "always" and not flaresolverrUrl:
        raise ValueError("Fetch.flaresolverr_url is required in always mode.")
    if flaresolverrTimeout <= 0:
        raise ValueError("Fetch.flaresolverr_timeout must be positive.")
    return flaresolverrUrl, flaresolverrTimeout, flaresolverrMode

def _extended_fetch_fallbacks(config):
    raw=config.get('Fetch', 'extended_fallbacks', fallback=",".join(EXTENDED_FETCH_FALLBACKS))
    methods=[part.strip().lower() for part in re.split(r"[\s,]+", raw or "") if part.strip()]
    if methods in (["none"], ["never"]):
        return []
    unknown=[method for method in methods if method not in EXTENDED_FETCH_FALLBACKS]
    if unknown:
        raise ValueError(f"Fetch.extended_fallbacks supports only: {', '.join(EXTENDED_FETCH_FALLBACKS)}, or none.")
    return list(dict.fromkeys(methods))

def _flaresolverr_attempt(url, flaresolverrUrl, flaresolverrTimeout, attempts):
    """Render through FlareSolverr and log the attempt. Returns (result, exception)."""
    started=time.time()
    try:
        flare_response=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
        result=_build_flaresolverr_fetch_result(url, flare_response)
    except FETCH_BROWSER_ERRORS as flareErr:
        attempts.append(_attempt_entry("flaresolverr", "error", started, detail=flareErr))
        return None, flareErr
    attempts.append(_attempt_entry("flaresolverr", _result_status(result), started,
                                   http_status=_http_status_of(result), detail=result.get("error")))
    if not result.get("error"):
        sys.stderr.write("[ccsearch] FlareSolverr solved challenge successfully.\n")
    return result, None

def _perform_direct_fetch(url, config, attempts):
    """Direct fetch with Cloudflare/SPA detection and FlareSolverr fallback."""
    flaresolverrUrl, flaresolverrTimeout, flaresolverrMode=_fetch_settings(config)
    maxRetries=config.getint('Brave', 'max_retries', fallback=2)
    useAlways=flaresolverrMode=="always" and flaresolverrUrl
    canFallback=flaresolverrMode=="fallback" and flaresolverrUrl

    # Always mode: skip simple fetch, go directly to FlareSolverr
    if useAlways:
        sys.stderr.write("[ccsearch] Using FlareSolverr (always mode)...\n")
        result, flareErr=_flaresolverr_attempt(url, flaresolverrUrl, flaresolverrTimeout, attempts)
        if result is not None:
            return result
        return _build_fetch_result(url, "flaresolverr", error=f"FlareSolverr failed: {flareErr}")

    # Try simple fetch first
    started=time.time()
    simpleFetchErr=None
    response=None
    try:
        response=_simple_fetch(url, maxRetries)
    except FETCH_REQUEST_ERRORS as e:
        simpleFetchErr=e
        response=getattr(e, "response", None)

    # Simple fetch succeeded — check for Cloudflare challenge
    if response is not None:
        direct_http_error=_http_fetch_error(response)
        direct_status=getattr(response, "status_code", None)
        http_status=direct_status if isinstance(direct_status, int) and direct_status >= 400 else None
        requested_extension=_guess_extension(url)
        contentType=_normalize_content_type(response)
        binary_request=(requested_extension in MARKITDOWN_EXTENSIONS
                        or contentType in MARKITDOWN_MIME_TO_EXTENSIONS)
        cloudflare_status=direct_status in {200, 403, 429, 503}
        cloudflare_blocked=(
            not binary_request
            and cloudflare_status
            and _detect_cloudflare(response)
        )
        if cloudflare_blocked:
            attempts.append(_attempt_entry("direct", "cf_challenge", started, http_status=http_status))
            if not canFallback:
                return _build_fetch_result(url, "direct", response=response, error="Cloudflare challenge detected; browser fallback is not configured or disabled.")
            sys.stderr.write("[ccsearch] Cloudflare detected, falling back to FlareSolverr...\n")
            result, flareErr=_flaresolverr_attempt(url, flaresolverrUrl, flaresolverrTimeout, attempts)
            if result is not None:
                return result
            return _build_fetch_result(url, "direct", response=response, error=f"Cloudflare detected. Direct fetch blocked | FlareSolverr also failed: {flareErr}")
        if direct_http_error:
            attempts.append(_attempt_entry("direct", "http_error", started, http_status=http_status, detail=direct_http_error))
            return _build_fetch_result(url, "direct", response=response, error=direct_http_error)
        if not binary_request and _detect_akamai(response):
            attempts.append(_attempt_entry("direct", "akamai_challenge", started))
            if canFallback:
                rendered, flareErr=_flaresolverr_attempt(url, flaresolverrUrl, flaresolverrTimeout, attempts)
                if rendered is not None:
                    return rendered
                return _build_fetch_result(url, "direct", response=response,
                                           error=f"Akamai challenge detected. Browser failed: {flareErr}")
            return _build_fetch_result(url, "direct", response=response,
                                       error="Akamai challenge detected; browser fallback is not configured or disabled.")
        converted_result=_convert_binary_response(url, response)
        if converted_result:
            attempts.append(_attempt_entry("direct", _result_status(converted_result), started, detail=converted_result.get("error")))
            return converted_result
        title, cleanText, chunks, metadata, looksLikeHtml=_extract_response_content(url, response)
        # Detect JS-heavy SPA shells and auto-fallback to FlareSolverr (HTML only)
        isHtml=looksLikeHtml or contentType is None
        isSpa=False
        spaReason=""
        rendering_error=None
        if isHtml and response.status_code==200:
            isSpa, spaReason=_detect_spa_shell(response.content, len(cleanText))
        if canFallback and isSpa:
            attempts.append(_attempt_entry("direct", "spa_shell", started, detail=spaReason))
            sys.stderr.write(f"[ccsearch] SPA shell detected ({spaReason}), falling back to FlareSolverr...\n")
            rendered, flareErr=_flaresolverr_attempt(url, flaresolverrUrl, flaresolverrTimeout, attempts)
            if rendered is not None:
                rendered_content=rendered.get("content", "")
                if not rendered.get("error") and len(rendered_content)>len(cleanText):
                    sys.stderr.write("[ccsearch] FlareSolverr rendered page successfully.\n")
                    return rendered
                rendering_error=rendered.get("error")
                sys.stderr.write("[ccsearch] FlareSolverr result not better, using direct response.\n")
            else:
                rendering_error=str(flareErr)
                sys.stderr.write(f"[ccsearch] FlareSolverr fallback failed: {flareErr}\n")
        content_error=None
        if isSpa:
            content_error=f"SPA shell detected ({spaReason}); browser rendering did not produce additional extractable content."
            if rendering_error:
                content_error+=f" FlareSolverr failed: {rendering_error}"
        elif not cleanText.strip():
            content_error="No extractable content found in the response."
        if not (canFallback and isSpa):
            status="ok" if not content_error else ("spa_shell" if isSpa else "empty")
            attempts.append(_attempt_entry("direct", status, started, detail=content_error))
        return _build_fetch_result(url, "direct", response=response, title=title, content=cleanText, error=content_error, metadata=metadata, chunks=chunks)

    attempts.append(_attempt_entry("direct", "transport_error", started, detail=simpleFetchErr))
    # Simple fetch failed — try FlareSolverr fallback
    if canFallback:
        sys.stderr.write(f"[ccsearch] Direct fetch failed ({simpleFetchErr}), falling back to FlareSolverr...\n")
        result, flareErr=_flaresolverr_attempt(url, flaresolverrUrl, flaresolverrTimeout, attempts)
        if result is not None:
            return result
        return _build_fetch_result(url, "direct", error=f"Direct fetch failed: {simpleFetchErr} | FlareSolverr also failed: {flareErr}")

    return _build_fetch_result(url, "direct", error=str(simpleFetchErr))

def _extract_response_content(url, response):
    """Extract (title, text, chunks, metadata, looks_like_html) from an HTTP response."""
    contentType=_normalize_content_type(response)
    looksLikeHtml=_is_html_content_type(contentType) or _looks_like_html_payload(response.content)
    final_url=_normalize_final_url(response, url)
    if _is_text_content_type(contentType) and not looksLikeHtml:
        title, cleanText=_decode_text_response(response, final_url)
        return title, cleanText, _chunk_text_content(cleanText), {}, looksLikeHtml
    title, cleanText, chunks=_extract_html_content(response.content, base_url=final_url)
    metadata=_extract_html_metadata(response.content, base_url=final_url)
    return title, cleanText, chunks, metadata, looksLikeHtml

def _extract_fetch_response(url, response, fetched_via):
    """Build a fetch result from an already retrieved response (used by fallbacks)."""
    http_error=_http_fetch_error(response)
    if http_error:
        return _build_fetch_result(url, fetched_via, response=response, error=http_error)
    converted=_convert_binary_response(url, response, fetched_via=fetched_via)
    if converted:
        return converted
    title, cleanText, chunks, metadata, _=_extract_response_content(url, response)
    error=None if cleanText.strip() else "No extractable content found in the response."
    return _build_fetch_result(url, fetched_via, response=response, title=title, content=cleanText,
                               error=error, metadata=metadata, chunks=chunks)

# --- Site APIs --------------------------------------------------------------
_DISCOURSE_KNOWN_HOSTS={
    "linux.do", "meta.discourse.org", "community.openai.com", "forum.cursor.com",
    "discuss.python.org", "discourse.nixos.org", "discuss.huggingface.co", "forum.obsidian.md",
    "community.cloudflare.com", "forums.swift.org", "discourse.julialang.org", "users.rust-lang.org",
    "internals.rust-lang.org", "discuss.pytorch.org", "forum.djangoproject.com",
}
_DISCOURSE_HOST_PREFIXES=("forum.", "forums.", "community.", "discuss.", "discourse.")
_DISCOURSE_TOPIC_RE=re.compile(r"^/t/(?:([^/]+)/)?(\d+)(?:/\d+)?/?$")
_REDDIT_HOSTS={"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "m.reddit.com"}
_REDDIT_COMMENTS_RE=re.compile(r"^(/r/[^/]+/comments/[A-Za-z0-9]+)(?:/[^/]*)?/?")
_V2EX_HOSTS={"v2ex.com", "www.v2ex.com", "global.v2ex.com", "cn.v2ex.com"}
_V2EX_TOPIC_RE=re.compile(r"^/t/(\d+)")

def _match_site_api(url):
    """Return (site, details) when the URL has a structured site API, else None."""
    parsed=urlparse(url)
    host=(parsed.hostname or "").lower()
    twitter=_is_twitter_url(url)
    if twitter:
        return "fxtwitter", {"parsed": twitter}
    if host in _REDDIT_HOSTS:
        matched=_REDDIT_COMMENTS_RE.match(parsed.path or "")
        if matched:
            return "reddit", {"path": matched.group(1)}
        return None
    if host in _V2EX_HOSTS:
        matched=_V2EX_TOPIC_RE.match(parsed.path or "")
        if matched:
            return "v2ex", {"topic_id": matched.group(1)}
        return None
    matched=_DISCOURSE_TOPIC_RE.match(parsed.path or "")
    if matched and (host in _DISCOURSE_KNOWN_HOSTS or host.startswith(_DISCOURSE_HOST_PREFIXES) or matched.group(1)):
        port=f":{parsed.port}" if parsed.port else ""
        return "discourse", {"topic_id": matched.group(2), "origin": f"{parsed.scheme}://{parsed.hostname}{port}"}
    return None

def _json_from_browser_body(body):
    """Parse JSON that a browser rendered inside <pre> or as bare body text."""
    soup=BeautifulSoup(body or "", "html.parser")
    pre=soup.find("pre")
    text=(pre.get_text() if pre else (soup.body or soup).get_text()).strip()
    return json.loads(text)

def _fetch_site_json(url, config):
    """GET a JSON API, rendering through FlareSolverr when Cloudflare blocks it."""
    response=None
    try:
        response=_simple_fetch(url, 1)
    except FETCH_REQUEST_ERRORS as exc:
        response=getattr(exc, "response", None)
        if response is None:
            raise SiteApiError(f"Site API request failed: {exc}", status="transport_error") from exc
    status=getattr(response, "status_code", None)
    blocked=_detect_cloudflare(response) if status in {200, 403, 429, 503} else False
    if not blocked and isinstance(status, int) and status < 400:
        try:
            return response.json()
        except ValueError as exc:
            raise SiteApiError(f"Site API returned invalid JSON: {exc}") from exc
    if blocked or status in {403, 429, 503}:
        flaresolverrUrl, flaresolverrTimeout, flaresolverrMode=_fetch_settings(config)
        if flaresolverrUrl and flaresolverrMode != "never":
            flare=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
            rendered_status=getattr(flare, "status_code", 200)
            if _detect_cloudflare(flare) or (isinstance(rendered_status, int) and rendered_status >= 400):
                raise SiteApiError("Site API remains blocked after browser rendering.",
                                   status="cf_challenge", http_status=rendered_status if rendered_status >= 400 else None)
            try:
                return _json_from_browser_body(flare.text)
            except ValueError as exc:
                raise SiteApiError(f"Browser-rendered site API response is not JSON: {exc}") from exc
        raise SiteApiError("Site API blocked by Cloudflare." if blocked else f"Site API returned HTTP {status}.",
                           status="cf_challenge" if blocked else "http_error", http_status=status if isinstance(status, int) and status >= 400 else None)
    raise SiteApiError(f"Site API returned HTTP {status}.", status="http_error", http_status=status)

def _html_fragment_to_text(fragment, base_url=None):
    """Convert trusted-structure HTML fragments (forum posts) into block text."""
    root=BeautifulSoup(f"<div>{fragment or ''}</div>", "html.parser")
    for tag in root(["script", "style", "noscript", "svg"]):
        tag.extract()
    blocks=_extract_content_blocks(root, base_url=base_url)
    if blocks:
        return "\n".join(block["text"] for block in blocks)
    return _normalize_block_text(root.get_text("\n"))

def _site_api_result(url, fetched_via, title, content, replies, reply_count, forum, published_at=None, author=None, final_url=None):
    metadata={"published_at": published_at, "author": author}
    result=_build_fetch_result(url, fetched_via, title=title, content=content, metadata=metadata,
                               chunks=_chunk_text_content(content) if content else None,
                               error=None if (content or replies) else "Site API returned no post content.")
    result["status_code"]=200
    result["content_type"]="application/json"
    if final_url:
        result["final_url"]=final_url
    result["replies"]=replies
    result["reply_count"]=reply_count
    result["returned_reply_count"]=len(replies)
    result["forum"]=forum
    return result

def _discourse_replies_from_posts(posts, base_url, max_replies):
    replies=[]
    for post in posts:
        if not isinstance(post, dict) or post.get("post_number") == 1:
            continue
        reply={
            "author": post.get("username") or post.get("name"),
            "created_at": post.get("created_at"),
            "post_number": post.get("post_number"),
            "content": _html_fragment_to_text(post.get("cooked"), base_url=base_url),
        }
        if post.get("reply_to_post_number"):
            reply["reply_to"]=post["reply_to_post_number"]
        replies.append(reply)
        if len(replies) >= max_replies:
            break
    return replies

def _parse_discourse_raw(text):
    """Parse Discourse /raw/{id} output into post dictionaries."""
    posts=[]
    for block in re.split(r"\n-{10,}\n", text or ""):
        block=block.strip()
        if not block:
            continue
        header, _, body=block.partition("\n")
        matched=re.match(r"^(.*?)\s*\|\s*(.*?)\s*\|\s*#(\d+)\s*$", header.strip())
        if not matched:
            continue
        posts.append({"username": matched.group(1), "created_at": matched.group(2),
                      "post_number": int(matched.group(3)), "raw": body.strip()})
    return posts

def _fetch_discourse(url, config, max_replies, topic_id, origin):
    base_url=f"{origin}/t/{topic_id}"
    try:
        data=_fetch_site_json(f"{origin}/t/{topic_id}.json", config)
    except SiteApiError as json_error:
        if json_error.http_status in {404, 410}:
            raise
        # The plain-text export is lighter and sometimes less aggressively filtered.
        try:
            raw_response=_simple_fetch(f"{origin}/raw/{topic_id}", 1)
        except FETCH_REQUEST_ERRORS:
            raise json_error
        if _detect_cloudflare(raw_response):
            raise json_error
        posts=_parse_discourse_raw(raw_response.text)
        if not posts:
            raise json_error
        first=posts[0]
        replies=[{"author": post["username"], "created_at": post["created_at"], "post_number": post["post_number"], "content": post["raw"]}
                 for post in posts[1:max_replies + 1]]
        return _site_api_result(url, "discourse", _title_from_url(url), first["raw"], replies, len(posts) - 1,
                                {"platform": "discourse", "topic_id": int(topic_id), "source": "raw"},
                                published_at=first["created_at"], author=first["username"], final_url=f"{origin}/raw/{topic_id}")
    if not isinstance(data, dict) or not isinstance(data.get("post_stream"), dict):
        raise SiteApiError("Response is not a Discourse topic.")
    stream=data["post_stream"]
    posts=[post for post in stream.get("posts") or [] if isinstance(post, dict)]
    loaded={post.get("id") for post in posts}
    missing=[post_id for post_id in (stream.get("stream") or [])[:max_replies + 1] if post_id not in loaded]
    for offset in range(0, len(missing), 20):
        batch=missing[offset:offset + 20]
        query=urlencode([("post_ids[]", post_id) for post_id in batch])
        try:
            extra=_fetch_site_json(f"{origin}/t/{topic_id}/posts.json?{query}", config)
        except (SiteApiError,) + FETCH_BROWSER_ERRORS:
            break  # Keep the posts already loaded rather than failing the topic.
        extra_posts=(extra.get("post_stream") or {}).get("posts") if isinstance(extra, dict) else None
        posts.extend(post for post in extra_posts or [] if isinstance(post, dict))
    posts.sort(key=lambda post: post.get("post_number") or 0)
    first=next((post for post in posts if post.get("post_number") == 1), None)
    content=_html_fragment_to_text(first.get("cooked"), base_url=base_url) if first else ""
    replies=_discourse_replies_from_posts(posts, base_url, max_replies)
    slug=data.get("slug") or "topic"
    return _site_api_result(
        url, "discourse", data.get("title") or data.get("fancy_title") or _title_from_url(url), content, replies,
        max(0, int(data.get("posts_count") or len(posts)) - 1),
        {"platform": "discourse", "topic_id": int(topic_id)},
        published_at=data.get("created_at") or (first or {}).get("created_at"),
        author=(first or {}).get("username"), final_url=f"{origin}/t/{slug}/{topic_id}",
    )

def _flatten_reddit_comments(children, max_replies, depth=0, output=None):
    output=[] if output is None else output
    for child in children or []:
        if len(output) >= max_replies:
            break
        if not isinstance(child, dict) or child.get("kind") != "t1":
            continue
        data=child.get("data") or {}
        body=(data.get("body") or "").strip()
        if body and body not in {"[deleted]", "[removed]"}:
            output.append({
                "author": data.get("author"),
                "created_at": _iso_from_epoch(data["created_utc"]) if isinstance(data.get("created_utc"), (int, float)) else None,
                "content": body,
                "score": data.get("score"),
                "depth": depth,
            })
        nested=data.get("replies")
        if isinstance(nested, dict):
            _flatten_reddit_comments(((nested.get("data") or {}).get("children")), max_replies, depth + 1, output)
    return output

def _fetch_reddit(url, config, max_replies, path):
    last_error=None
    for host in ("www.reddit.com", "old.reddit.com"):
        api_url=f"https://{host}{path}.json?limit={max_replies}&raw_json=1"
        try:
            data=_fetch_site_json(api_url, config)
            break
        except SiteApiError as exc:
            last_error=exc
    else:
        raise last_error
    if not isinstance(data, list) or len(data) < 2:
        raise SiteApiError("Response is not a Reddit comment thread.")
    post_children=((data[0] or {}).get("data") or {}).get("children") or []
    if not post_children:
        raise SiteApiError("Reddit thread has no post.")
    post=post_children[0].get("data") or {}
    content_parts=[part for part in (post.get("selftext"), None if post.get("is_self") else post.get("url")) if part]
    replies=_flatten_reddit_comments(((data[1] or {}).get("data") or {}).get("children"), max_replies)
    created=post.get("created_utc")
    return _site_api_result(
        url, "reddit", post.get("title") or _title_from_url(url), "\n\n".join(content_parts).strip(), replies,
        post.get("num_comments") if isinstance(post.get("num_comments"), int) else len(replies),
        {"platform": "reddit", "subreddit": post.get("subreddit"), "post_id": post.get("id"), "score": post.get("score")},
        published_at=_iso_from_epoch(created) if isinstance(created, (int, float)) else None,
        author=post.get("author"), final_url=f"https://www.reddit.com{path}",
    )

def _fetch_v2ex(url, config, max_replies, topic_id):
    topics=_fetch_site_json(f"https://www.v2ex.com/api/topics/show.json?id={topic_id}", config)
    topic=topics[0] if isinstance(topics, list) and topics and isinstance(topics[0], dict) else None
    if topic is None:
        raise SiteApiError("V2EX topic not found.", status="http_error", http_status=404)
    replies=[]
    replies_error=None
    try:
        raw_replies=_fetch_site_json(f"https://www.v2ex.com/api/replies/show.json?topic_id={topic_id}", config)
    except SiteApiError as exc:
        raw_replies=[]
        replies_error=str(exc)
    for reply in raw_replies if isinstance(raw_replies, list) else []:
        if not isinstance(reply, dict):
            continue
        created=reply.get("created")
        replies.append({
            "author": (reply.get("member") or {}).get("username"),
            "created_at": _iso_from_epoch(created) if isinstance(created, (int, float)) else None,
            "content": (reply.get("content") or "").strip() or _html_fragment_to_text(reply.get("content_rendered")),
        })
        if len(replies) >= max_replies:
            break
    created=topic.get("created")
    content=(topic.get("content") or "").strip() or _html_fragment_to_text(topic.get("content_rendered"))
    forum={"platform": "v2ex", "topic_id": int(topic_id), "node": (topic.get("node") or {}).get("title")}
    if replies_error:
        forum["replies_error"]=replies_error
    return _site_api_result(
        url, "v2ex", topic.get("title") or _title_from_url(url), content, replies,
        topic.get("replies") if isinstance(topic.get("replies"), int) else len(replies), forum,
        published_at=_iso_from_epoch(created) if isinstance(created, (int, float)) else None,
        author=(topic.get("member") or {}).get("username"), final_url=f"https://www.v2ex.com/t/{topic_id}",
    )

def _run_site_api(site, details, url, config, max_replies):
    if site == "fxtwitter":
        result=_fetch_twitter(url, details["parsed"])
        if result is None:
            raise SiteApiError("fxtwitter API returned no content.")
        return result
    if site == "discourse":
        return _fetch_discourse(url, config, max_replies, details["topic_id"], details["origin"])
    if site == "reddit":
        return _fetch_reddit(url, config, max_replies, details["path"])
    if site == "v2ex":
        return _fetch_v2ex(url, config, max_replies, details["topic_id"])
    raise SiteApiError(f"Unsupported site API: {site}")

# --- Extended fallbacks -----------------------------------------------------
_GENERIC_PATH_SEGMENTS={"t", "topic", "topics", "index", "blog", "blogs", "docs", "doc", "post", "posts", "p",
                        "article", "articles", "news", "en", "en-us", "www", "html", "amp"}
_GENERIC_TITLES={"just a moment...", "no title", "attention required! | cloudflare", "access denied", "403 forbidden"}

def _url_match_key(url):
    parsed=urlparse(normalize_fetch_cache_url(url or ""))
    return (_normalize_hostname(parsed.hostname), parsed.port,
            (parsed.path or "/").rstrip("/") or "/", parsed.params, parsed.query)

def _slug_terms(path):
    terms=[]
    for segment in (path or "").split("/"):
        segment=re.sub(r"\.(?:html?|php|aspx?)$", "", segment.lower())
        if not segment or segment.isdigit() or segment in _GENERIC_PATH_SEGMENTS:
            continue
        terms.append(re.sub(r"[-_+]+", " ", segment))
    return " ".join(terms[-2:]).strip()

def _llm_context_fetch_fallback(url, config, title_hint=None):
    """Return LLM Context passages for exactly this URL. Returns (result, status, detail)."""
    api_key, _=_select_brave_api_key()
    if not api_key:
        return None, "unavailable", "No Brave Search key is configured."
    parsed=urlparse(url)
    host=_normalize_hostname(parsed.hostname)
    terms=title_hint if title_hint and title_hint.strip().lower() not in _GENERIC_TITLES else _slug_terms(parsed.path)
    query=f"site:{host} {terms}".strip() if terms and not parsed.query else url
    data=perform_llm_context_search(query, api_key, config)
    target=_url_match_key(url)
    for item in data.get("results", []):
        if _url_match_key(item.get("url")) != target:
            continue
        snippets=[snippet for snippet in item.get("snippets") or [] if snippet]
        content="\n\n".join(snippets) or item.get("snippet") or ""
        if not content:
            continue
        result=_build_fetch_result(url, "llm-context", title=item.get("title"), content=content,
                                   metadata={"published_at": item.get("published_at")},
                                   chunks=_chunk_text_content(content, default_type="excerpt"))
        result["final_url"]=item.get("url")
        # Search-engine excerpts, not the complete page.
        result["content_scope"]="excerpts"
        return result, "ok", None
    return None, "no_match", f"LLM Context returned no passages for this URL (query: {query})."

def _wayback_timestamp_iso(timestamp):
    try:
        return datetime.strptime(str(timestamp)[:14], "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None

def _archive_fetch_fallback(url, config):
    """Serve the newest Wayback Machine snapshot. Returns (result, status, detail)."""
    availability=requests.get("https://archive.org/wayback/available", params={"url": url}, timeout=(10, 20))
    availability.raise_for_status()
    snapshot=((availability.json() or {}).get("archived_snapshots") or {}).get("closest") or {}
    timestamp=snapshot.get("timestamp")
    if not snapshot.get("available") or str(snapshot.get("status")) != "200" or not timestamp:
        return None, "no_snapshot", "The Wayback Machine has no successful snapshot for this URL."
    # id_ returns the archived bytes without the Wayback toolbar.
    raw_url=f"https://web.archive.org/web/{timestamp}id_/{url}"
    try:
        response=_simple_fetch(raw_url, 1)
    except FETCH_REQUEST_ERRORS as exc:
        response=getattr(exc, "response", None)
        if response is None:
            raise
    if _detect_cloudflare(response):
        return None, "cf_challenge", "The archived snapshot is itself a challenge page."
    result=_extract_fetch_response(url, response, "archive")
    if result.get("error"):
        return None, _result_status(result), result["error"]
    result["final_url"]=f"https://web.archive.org/web/{timestamp}/{url}"
    result["snapshot_date"]=_wayback_timestamp_iso(timestamp)
    return result, "ok", None

def _should_try_extended_fallbacks(attempts):
    """Fall back only when the page was blocked or unreachable, never for 404s."""
    for attempt in attempts:
        if attempt.get("http_status") in {404, 410}:
            return False
    for attempt in attempts:
        if attempt.get("method") == "site-api":
            continue
        if attempt.get("status") in FALLBACK_ELIGIBLE_STATUSES:
            return True
        if attempt.get("http_status") in FALLBACK_ELIGIBLE_HTTP_STATUSES:
            return True
    return False

def _finalize_fetch_result(result, attempts):
    """Attach the attempt log, provenance, and normalized dates to a fetch result."""
    result=dict(result)
    result["attempts"]=list(attempts)
    result["ok"]=not result.get("error")
    result["served_from"]=FETCH_SERVED_FROM.get(result.get("fetched_via")) if result["ok"] else None
    result.setdefault("fetched_at", _utc_now_iso())
    raw_published=result.get("published_at")
    if raw_published:
        result["published_at"]=normalize_published_at(raw_published)
    modified=result.pop("modified_at", None)
    result["content_date"]=(
        result.get("published_at")
        or normalize_published_at(modified)
        or normalize_published_at(result.get("last_modified"))
    )
    return result

def perform_fetch(url, config, max_replies=DEFAULT_MAX_REPLIES):
    """Fetch through a site API (when available), then direct fetch and FlareSolverr.

    Every result carries ``attempts``, ``served_from``, ``ok``, ``fetched_at``,
    and ``content_date``. Extended fallbacks live in ``fetch_with_fallbacks``.
    """
    attempts=[]
    _fetch_settings(config)
    site=_match_site_api(url)
    if site:
        name, details=site
        started=time.time()
        try:
            result=_run_site_api(name, details, url, config, max_replies)
            status=_result_status(result)
            attempts.append(_attempt_entry("site-api", status, started, site=name, detail=result.get("error")))
            if status == "ok":
                return _finalize_fetch_result(result, attempts)
        except SiteApiError as exc:
            attempts.append(_attempt_entry("site-api", exc.status, started, site=name, http_status=exc.http_status, detail=exc))
        except FETCH_BROWSER_ERRORS as exc:
            attempts.append(_attempt_entry("site-api", "transport_error", started, site=name, detail=exc))
        if name == "fxtwitter":
            sys.stderr.write("[ccsearch] fxtwitter API failed, falling back to normal fetch...\n")
    result=_perform_direct_fetch(url, config, attempts)
    return _finalize_fetch_result(result, attempts)

def fetch_with_fallbacks(url, config, max_replies=DEFAULT_MAX_REPLIES):
    """Run the full fetch chain, adding LLM Context and Wayback fallbacks when blocked."""
    result=perform_fetch(url, config, max_replies=max_replies)
    if result.get("ok"):
        return result
    fallbacks=_extended_fetch_fallbacks(config)
    attempts=list(result.get("attempts") or [])
    if not fallbacks or not _should_try_extended_fallbacks(attempts):
        return result
    title_hint=result.get("title") if isinstance(result.get("title"), str) else None
    for method in fallbacks:
        started=time.time()
        try:
            if method == "llm-context":
                candidate, status, detail=_llm_context_fetch_fallback(url, config, title_hint=title_hint)
            else:
                candidate, status, detail=_archive_fetch_fallback(url, config)
        except FETCH_BROWSER_ERRORS + (RuntimeError,) as exc:
            candidate, status, detail=None, "error", str(exc)
        attempts.append(_attempt_entry(method, status, started, detail=detail))
        if candidate is not None and not candidate.get("error"):
            sys.stderr.write(f"[ccsearch] Served {url} from {method} fallback.\n")
            return _finalize_fetch_result(candidate, attempts)
    failed=dict(result)
    failed["attempts"]=attempts
    return failed

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
        "brave": ["BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"],
        "perplexity": ["OPENROUTER_API_KEY"],
        "perplexity-verify": ["OPENROUTER_API_KEY"],
        "both": ["BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY", "OPENROUTER_API_KEY"],
        "llm-context": ["BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"],
        "fetch": [],
    }
    return requirements.get(engine, [])

def _normalize_secret_token(raw):
    """Strip wrapping quotes and whitespace from a secret token."""
    token=str(raw or "").strip()
    token=token.strip("\"'")
    token=token.strip("\u201c\u201d\u2018\u2019")
    return token.strip()

def _split_api_key_blob(raw):
    """Split one environment value into ordered unique API keys."""
    token=_normalize_secret_token(raw)
    if not token:
        return []
    keys=[]
    seen=set()
    for part in re.split(r"[\s,;]+", token):
        key=_normalize_secret_token(part)
        if not key or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return keys

def _add_brave_api_keys(keys, seen, blob, env_name, source):
    """Append newly discovered Brave keys while remembering the first source."""
    for key in _split_api_key_blob(blob):
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
        if source[0] is None:
            source[0]=env_name

def _list_brave_api_keys():
    """Return configured Brave keys in round-robin order plus the primary source name."""
    keys=[]
    seen=set()
    source=[None]

    _add_brave_api_keys(keys, seen, os.environ.get("BRAVE_SEARCH_API_KEY", ""), "BRAVE_SEARCH_API_KEY", source)

    numbered=[]
    for name, value in os.environ.items():
        match=_BRAVE_NUMBERED_KEY_RE.fullmatch(name)
        if match:
            numbered.append((int(match.group(1)), name, value))
    for _, name, value in sorted(numbered, key=lambda item: item[0]):
        _add_brave_api_keys(keys, seen, value, name, source)

    _add_brave_api_keys(keys, seen, os.environ.get("BRAVE_SEARCH_API_KEYS", ""), "BRAVE_SEARCH_API_KEYS", source)

    if not keys:
        _add_brave_api_keys(keys, seen, os.environ.get("BRAVE_API_KEY", ""), "BRAVE_API_KEY", source)

    return keys, source[0]

def _resolve_brave_api_key():
    """Return the first configured Brave key and its environment variable name."""
    keys, source=_list_brave_api_keys()
    if not keys:
        return None, None
    return keys[0], source

def _select_brave_api_key():
    """Return the next Brave key using a cross-process round-robin counter."""
    keys, source=_list_brave_api_keys()
    if not keys:
        return None, None
    if len(keys) == 1:
        return keys[0], source
    return keys[_next_brave_key_index(len(keys))], source

def _engine_configured_via(engine):
    """Return the active environment variable(s) satisfying an engine."""
    if engine == "fetch":
        return "built-in"
    if engine in ("brave", "llm-context", "both"):
        _, brave_env_name=_resolve_brave_api_key()
        if not brave_env_name:
            return None
        if engine == "both":
            return f"{brave_env_name} + OPENROUTER_API_KEY" if os.environ.get("OPENROUTER_API_KEY") else None
        return brave_env_name
    required=_engine_required_env_vars(engine)
    for name in required:
        if os.environ.get(name):
            return name
    return None

def _is_engine_configured(engine):
    """Return whether the engine is runnable with the current environment."""
    return _engine_configured_via(engine) is not None

_openrouter_quota_cache={"at": 0.0, "value": None}
_openrouter_quota_lock=threading.Lock()

def _openrouter_quota(timeout=5):
    """Return OpenRouter key usage/limits (cached for 60 seconds), without secrets."""
    api_key=_normalize_secret_token(os.environ.get("OPENROUTER_API_KEY", ""))
    if not api_key:
        return {"configured": False}
    with _openrouter_quota_lock:
        if _openrouter_quota_cache["value"] is not None and time.time() - _openrouter_quota_cache["at"] < 60:
            return _openrouter_quota_cache["value"]
    try:
        response=requests.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        response.raise_for_status()
        data=(response.json() or {}).get("data") or {}
        value={
            "configured": True,
            "limit": data.get("limit"),
            "usage": data.get("usage"),
            "limit_remaining": data.get("limit_remaining"),
            "is_free_tier": data.get("is_free_tier"),
            "rate_limit": data.get("rate_limit"),
            "observed_at": _utc_now_iso(),
        }
    except (requests.exceptions.RequestException, ValueError, AttributeError) as exc:
        value={"configured": True, "error": f"Quota lookup failed: {type(exc).__name__}"}
    with _openrouter_quota_lock:
        _openrouter_quota_cache.update({"at": time.time(), "value": value})
    return value

def _brave_quota_report():
    """Report the last observed rate-limit windows for each configured Brave key."""
    observed=_load_brave_quota()
    keys, _=_list_brave_api_keys()
    report=[]
    for position, key in enumerate(keys, 1):
        fingerprint=_brave_key_fingerprint(key)
        entry=observed.get(fingerprint) or {}
        report.append({
            "key": position,
            "fingerprint": fingerprint,
            "windows": entry.get("windows"),
            "observed_at": entry.get("observed_at"),
        })
    return {
        "source": "X-RateLimit headers from the most recent Brave response per key",
        "keys": report,
    }

def get_diagnostics(config=None, include_engines=True, include_quota=False):
    """Return runtime diagnostics without exposing secret values.

    ``include_quota`` adds Brave rate-limit windows (from recorded response
    headers) and a live OpenRouter key-usage lookup.
    """
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
        "cache": {
            "default_ttl_minutes": DEFAULT_CACHE_TTL_MINUTES,
            "max_read_age_days": CACHE_MAX_READ_AGE_SECONDS // 86400,
            "delete_age_days": CACHE_DELETE_AGE_SECONDS // 86400,
        },
        "brave_rate_limit": {
            "configured_rps": _brave_requests_per_second(fetch_config),
            "subscription_cap_rps": BRAVE_SEARCH_MAX_RPS,
            "shared_across_local_processes": fcntl is not None,
            "key_count": 0,
            "round_robin": False,
            "combined_cap_rps": 0,
        },
        "dependencies": dependencies,
        "environment": {
            "BRAVE_API_KEY": bool(_normalize_secret_token(os.environ.get("BRAVE_API_KEY", ""))),
            "BRAVE_SEARCH_API_KEY": False,
            "OPENROUTER_API_KEY": bool(_normalize_secret_token(os.environ.get("OPENROUTER_API_KEY", ""))),
            "CCSEARCH_API_KEY": bool(_normalize_secret_token(os.environ.get("CCSEARCH_API_KEY", ""))),
        },
        "fetch": {
            "flaresolverr_configured": bool(fetch_config.get("Fetch", "flaresolverr_url", fallback="").strip()),
            "flaresolverr_mode": fetch_config.get("Fetch", "flaresolverr_mode", fallback="fallback").strip().lower(),
        },
        "batch": {
            "max_workers": fetch_config.getint("Batch", "max_workers", fallback=4),
        },
    }
    brave_keys, brave_source=_list_brave_api_keys()
    per_key_rps=_brave_requests_per_second(fetch_config)
    diagnostics["brave_rate_limit"]["key_count"]=len(brave_keys)
    diagnostics["brave_rate_limit"]["round_robin"]=len(brave_keys) > 1
    diagnostics["brave_rate_limit"]["combined_cap_rps"]=per_key_rps * len(brave_keys)
    diagnostics["brave_rate_limit"]["source"]=brave_source
    diagnostics["environment"]["BRAVE_SEARCH_API_KEY"]=bool(brave_keys) and brave_source != "BRAVE_API_KEY"
    diagnostics["fetch"]["extended_fallbacks"]=_extended_fetch_fallbacks(fetch_config)
    if include_quota:
        diagnostics["quota"]={
            "brave": _brave_quota_report(),
            "openrouter": _openrouter_quota(),
        }
    if include_engines:
        diagnostics["engines"] = list_engines()
    return diagnostics

def validate_query(query, engine):
    """Validate the query shape for a given engine. Returns an error message or None."""
    if not isinstance(query, str) or not query.strip():
        return "'query' is required"
    if engine == "fetch":
        try:
            parsed = urlparse(query)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and not any(char.isspace() or ord(char) < 32 for char in query)
                     and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid:
            return "For fetch engine, query must be a valid HTTP or HTTPS URL."
    if engine == "perplexity-verify":
        try:
            normalize_claims(query)
        except ValueError as exc:
            return str(exc)
    return None

SEMANTIC_CACHE_ENGINES={"brave", "perplexity", "both", "llm-context"}
# Engines each option applies to. Batch defaults only fill applicable options;
# explicit per-request values are validated strictly.
OPTION_ENGINES={
    "offset": {"brave", "both"},
    "result_limit": RESULT_LIMIT_ENGINES,
    "include_hosts": HOST_FILTER_ENGINES,
    "exclude_hosts": HOST_FILTER_ENGINES,
    "semantic_cache": SEMANTIC_CACHE_ENGINES | {"fetch"},
    "semantic_threshold": SEMANTIC_CACHE_ENGINES | {"fetch"},
    "freshness": SEARCH_OPTION_ENGINES,
    "country": SEARCH_OPTION_ENGINES,
    "search_lang": SEARCH_OPTION_ENGINES,
    "snippet_limit": SNIPPET_LIMIT_ENGINES,
    "flaresolverr": {"fetch"},
    "format": {"fetch"},
    "focus": {"fetch"},
    "focus_k": {"fetch"},
    "max_chars": {"fetch"},
    "max_replies": {"fetch"},
}
EXECUTION_OPTION_DEFAULTS={
    "offset": None,
    "cache": False,
    "cache_ttl": DEFAULT_CACHE_TTL_MINUTES,
    "max_cache_age": None,
    "semantic_cache": False,
    "semantic_threshold": 0.9,
    "flaresolverr": False,
    "include_hosts": None,
    "exclude_hosts": None,
    "result_limit": None,
    "freshness": None,
    "country": None,
    "search_lang": None,
    "snippet_limit": None,
    "format": "text",
    "verbose": False,
    "focus": None,
    "focus_k": DEFAULT_FOCUS_K,
    "max_chars": None,
    "max_replies": DEFAULT_MAX_REPLIES,
}
EXECUTION_OPTION_NAMES=tuple(EXECUTION_OPTION_DEFAULTS)

def _option_is_set(name, value):
    return value not in (None, False, "", [], ()) and value != EXECUTION_OPTION_DEFAULTS.get(name)

def validate_execution_options(engine, offset=None, cache_ttl=DEFAULT_CACHE_TTL_MINUTES, semantic_threshold=0.9, flaresolverr=False, include_hosts=None, exclude_hosts=None, result_limit=None, cache=False, semantic_cache=False, max_cache_age=None, freshness=None, country=None, search_lang=None, snippet_limit=None, format="text", verbose=False, focus=None, focus_k=DEFAULT_FOCUS_K, max_chars=None, max_replies=DEFAULT_MAX_REPLIES):
    """Validate shared execution options. Returns an error message or None."""
    for name, value in (("cache", cache), ("semantic_cache", semantic_cache), ("flaresolverr", flaresolverr), ("verbose", verbose)):
        if type(value) is not bool:
            return f"'{name}' must be a boolean."
    for name, value in (("offset", offset), ("result_limit", result_limit), ("cache_ttl", cache_ttl),
                        ("max_cache_age", max_cache_age), ("snippet_limit", snippet_limit), ("focus_k", focus_k),
                        ("max_chars", max_chars), ("max_replies", max_replies)):
        required=name in {"cache_ttl", "focus_k", "max_replies"}
        if (value is not None or required) and type(value) is not int:
            return f"'{name}' must be an integer."
    for name, value in (("freshness", freshness), ("country", country), ("search_lang", search_lang), ("focus", focus)):
        if value is not None and not isinstance(value, str):
            return f"'{name}' must be a string."
    if type(semantic_threshold) not in (int, float) or not 0.0 <= semantic_threshold <= 1.0:
        return "'semantic_threshold' must be a finite number between 0.0 and 1.0."
    if offset is not None and engine not in {"brave", "both"}:
        return "The 'offset' option is only supported for brave and both engines."
    if offset is not None and offset < 0:
        return "'offset' must be greater than or equal to 0."
    if flaresolverr and engine != "fetch":
        return "The 'flaresolverr' option is only supported for the fetch engine."
    if cache_ttl <= 0:
        return "'cache_ttl' must be greater than 0."
    if cache_ttl > DEFAULT_CACHE_TTL_MINUTES:
        return f"'cache_ttl' cannot exceed {DEFAULT_CACHE_TTL_MINUTES} minutes (90 days)."
    if max_cache_age is not None and not 0 < max_cache_age <= DEFAULT_CACHE_TTL_MINUTES:
        return f"'max_cache_age' must be between 1 and {DEFAULT_CACHE_TTL_MINUTES} minutes."
    if result_limit is not None:
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
    if any(value for value in (freshness, country, search_lang)) and engine not in SEARCH_OPTION_ENGINES:
        return "The 'freshness', 'country', and 'search_lang' options are only supported for brave, both, and llm-context engines."
    if freshness and freshness not in FRESHNESS_WINDOWS_DAYS and not _FRESHNESS_RANGE_RE.match(freshness):
        return "'freshness' must be pd, pw, pm, py, or a YYYY-MM-DDtoYYYY-MM-DD range."
    if country and not _COUNTRY_RE.match(country):
        return "'country' must be a two-letter country code (for example US, TW, JP) or ALL."
    if search_lang and not _SEARCH_LANG_RE.match(search_lang):
        return "'search_lang' must be a language code such as en, ja, or zh-hant."
    if snippet_limit is not None:
        if engine not in SNIPPET_LIMIT_ENGINES:
            return "The 'snippet_limit' option is only supported for the llm-context engine."
        if snippet_limit < 1:
            return "'snippet_limit' must be greater than or equal to 1."
    fetch_only=(("format", format != "text"), ("focus", focus is not None), ("max_chars", max_chars is not None),
                ("focus_k", focus_k != DEFAULT_FOCUS_K), ("max_replies", max_replies != DEFAULT_MAX_REPLIES))
    for name, is_set in fetch_only:
        if is_set and engine != "fetch":
            return f"The '{name}' option is only supported for the fetch engine."
    if format not in FETCH_FORMATS:
        return "'format' must be text or chunks."
    if focus is not None and (not focus.strip() or len(focus) > 500):
        return "'focus' must be a non-empty string of at most 500 characters."
    if not 1 <= focus_k <= 50:
        return "'focus_k' must be between 1 and 50."
    if max_chars is not None and max_chars < 1:
        return "'max_chars' must be greater than or equal to 1."
    if not 0 <= max_replies <= 200:
        return "'max_replies' must be between 0 and 200."
    return None

def _resolve_batch_max_workers(config, max_workers=None):
    """Resolve and validate effective batch concurrency."""
    if max_workers is None:
        max_workers = config.getint("Batch", "max_workers", fallback=4)
    if type(max_workers) is not int or max_workers <= 0:
        raise ValueError("'max_workers' must be a positive integer.")
    return max_workers

def _batch_request_fingerprint(query, engine, options):
    """Build a stable fingerprint for duplicate suppression within a batch."""
    normalized={}
    for name in EXECUTION_OPTION_NAMES:
        value=options.get(name, EXECUTION_OPTION_DEFAULTS[name])
        if name in {"include_hosts", "exclude_hosts"}:
            value=_normalize_host_filters(value)
        normalized[name]=value
    return (engine, normalize_cache_query(query, engine), json.dumps(normalized, sort_keys=True, default=str))

BATCH_OPS=("search", "fetch")

def _resolve_batch_target(entry, defaults):
    """Apply the batch dispatch rules. Returns (engine, query, error)."""
    op=entry.get("op")
    has_url=entry.get("url") is not None
    has_query=entry.get("query") is not None
    has_claims=entry.get("claims") is not None
    entry_engine=entry.get("engine")
    if entry_engine is not None:
        if not isinstance(entry_engine, str):
            return None, None, "'engine' must be a string."
        entry_engine=entry_engine.strip().lower()
    default_engine=str(defaults.get("engine") or "brave").strip().lower()
    if op is not None:
        if not isinstance(op, str) or op.strip().lower() not in BATCH_OPS:
            return None, None, "'op' must be search or fetch."
        op=op.strip().lower()
    elif has_url and (has_query or has_claims):
        return None, None, "Provide either 'url' (fetch) or 'query' (search), not both; set 'op' to choose."
    elif has_url:
        op="fetch"
    elif has_query or has_claims:
        # Legacy form: an explicit (or default) fetch engine with a URL in 'query'.
        op="fetch" if (entry_engine or default_engine) == "fetch" and not has_claims else "search"
    else:
        return None, None, "Each batch request needs 'url' (fetch) or 'query' (search)."

    if op == "fetch":
        if entry_engine not in (None, "fetch"):
            return None, None, f"op 'fetch' cannot use engine '{entry_engine}'."
        target=entry.get("url") if has_url else entry.get("query")
        return "fetch", target.strip() if isinstance(target, str) else target, None

    engine=entry_engine or (default_engine if default_engine != "fetch" else "brave")
    if engine == "fetch":
        return None, None, "op 'search' cannot use the fetch engine; send 'url' instead."
    if engine == "perplexity-verify" and has_claims:
        try:
            return engine, "\n".join(normalize_claims(entry.get("claims"))), None
        except ValueError as exc:
            return None, None, str(exc)
    if not has_query:
        return None, None, "Search requests need 'query'."
    query=entry.get("query")
    return engine, query.strip() if isinstance(query, str) else query, None

def _dedupe_batch_result_items(results):
    """Replace repeated search result URLs with references to their first occurrence."""
    first_seen={}
    replaced=0
    for item in results:
        if not isinstance(item, dict) or item.get("error") or item.get("_batch_deduped"):
            continue
        engine=item.get("engine")
        for field in ("results", "brave_results"):
            entries=item.get(field)
            if not isinstance(entries, list) or engine not in {"brave", "both", "llm-context"}:
                continue
            family="llm-context" if engine == "llm-context" else "brave"
            updated=[]
            changed=False
            for entry in entries:
                url=entry.get("url") if isinstance(entry, dict) and "ref" not in entry else None
                key=(family, _normalize_result_url(url)) if url else None
                if key and key in first_seen and first_seen[key] != item.get("index"):
                    updated.append({"ref": url, "see_index": first_seen[key], "rank": entry.get("rank")})
                    replaced+=1
                    changed=True
                    continue
                if key:
                    first_seen.setdefault(key, item.get("index"))
                updated.append(entry)
            if changed:
                item[field]=updated
    return replaced

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
    if not isinstance(defaults, dict):
        raise ValueError("'defaults' must be an object when provided.")
    return requests_payload, defaults

def execute_batch(requests_payload, config, defaults=None, max_workers=None, dedupe_results=True):
    """Execute a batch of heterogeneous requests while isolating per-item failures.

    Dispatch: ``url`` -> fetch, ``query`` -> search engine (``engine``, default
    brave), explicit ``op`` wins. Batch-level defaults fill only the options that
    apply to each request's engine, and the default engine applies to searches only.
    """
    if not isinstance(requests_payload, list) or not requests_payload:
        raise ValueError("'requests' must be a non-empty list.")
    defaults={} if defaults is None else defaults
    if not isinstance(defaults, dict):
        raise ValueError("'defaults' must be an object when provided.")
    if type(dedupe_results) is not bool:
        raise ValueError("'dedupe_results' must be a boolean.")
    resolved_max_workers = min(len(requests_payload), _resolve_batch_max_workers(config, max_workers=max_workers))

    def build_entry(idx, entry):
        if not isinstance(entry, dict):
            return None, {"index": idx, "error": "Each batch request must be an object."}

        engine, query, dispatch_error=_resolve_batch_target(entry, defaults)
        if dispatch_error:
            return None, {"index": idx, "engine": engine or entry.get("engine"), "query": entry.get("query", entry.get("url")), "error": dispatch_error}
        if engine not in VALID_ENGINES:
            return None, {"index": idx, "engine": engine, "query": query, "error": f"Unsupported engine: {engine}"}

        options={}
        for name in EXECUTION_OPTION_NAMES:
            if name in entry:
                options[name]=entry[name]
            elif name in defaults and (name not in OPTION_ENGINES or engine in OPTION_ENGINES[name]):
                if defaults[name] is not None:
                    options[name]=defaults[name]
        request_data={"index": idx, "engine": engine, "query": query, "options": options}
        validation_error=validate_query(query, engine)
        if validation_error:
            return None, {"index": idx, "engine": engine, "query": query, "error": validation_error}
        option_error=validate_execution_options(engine, **options)
        if option_error:
            return None, {"index": idx, "engine": engine, "query": query, "error": option_error}
        request_data["fingerprint"] = _batch_request_fingerprint(query, engine, options)
        return request_data, None

    def run_request(request_data):
        started = time.time()
        try:
            result=execute_query(request_data["query"], request_data["engine"], config, **request_data["options"])
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
    deduped_request_count=sum(1 for item in results if isinstance(item, dict) and item.get("_batch_deduped"))
    deduped_result_count=_dedupe_batch_result_items(results) if dedupe_results else 0
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
        "deduped_count": deduped_request_count + deduped_result_count,
        "deduped_request_count": deduped_request_count,
        "deduped_result_count": deduped_result_count,
        "engine_counts": engine_counts,
    }

# ---------------------------------------------------------------------------
# Response shaping (applied after cache lookup so cached payloads stay complete)
# ---------------------------------------------------------------------------
FETCH_VERBOSE_FIELDS=(
    "content_sha256", "outbound_links", "outbound_link_count", "internal_outbound_link_count",
    "external_outbound_link_count", "outbound_hosts", "content_word_count", "content_length",
    "etag", "last_modified", "filename", "hostname", "fetched_via",
)
CHUNK_VERBOSE_FIELDS=(
    "text_sha256", "chunk_id", "char_start", "char_end", "relative_position", "section_path",
    "section_path_text", "section_depth", "char_count", "word_count", "links", "link_count",
    "internal_link_count", "external_link_count", "list_item_count", "code_line_count",
    "table_row_count", "table_column_count",
)
_FOCUS_STOPWORDS={
    "a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "is", "are", "was", "were", "be", "with",
    "by", "at", "from", "how", "what", "which", "does", "do", "it", "its", "this", "that", "as", "about",
}
_FOCUS_WORD_RE=re.compile(r"[a-z0-9$€£¥%]+(?:[.,][0-9]+)*")
_FOCUS_CJK_RE=re.compile(r"[぀-ヿ㐀-鿿가-힯]+")

def _focus_tokens(text):
    """Tokenize for lexical relevance: words plus CJK character bigrams."""
    lowered=(text or "").lower()
    tokens=[]
    for word in _FOCUS_WORD_RE.findall(lowered):
        if word in _FOCUS_STOPWORDS:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word=word[:-1]
        tokens.append(word)
    for run in _FOCUS_CJK_RE.findall(text or ""):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[idx:idx + 2] for idx in range(len(run) - 1))
    return tokens

def _focus_passages(chunks, content):
    """Build scoring passages: non-heading chunks, long ones split by sentence."""
    passages=[]
    source=chunks or _chunk_text_content(content or "")
    for chunk in source:
        text=(chunk.get("text") or "").strip()
        if not text or chunk.get("type") == "heading":
            continue
        section=chunk.get("section_title")
        if chunk.get("type") == "list":
            # Score list items individually; takeaway lists pack unrelated facts together.
            passages.extend({"text": item.strip(), "section": section} for item in text.splitlines() if item.strip())
            continue
        if chunk.get("type") == "table":
            rows=[row for row in text.splitlines() if row.strip()]
            # Keep the header row(s) with each data row so values stay interpretable.
            if len(rows) > 2 and set(rows[1].replace("|", "").strip()) <= {"-", " "}:
                header=rows[:2]
            else:
                header=rows[:1] if len(rows) > 2 else []
            for row in rows[len(header):]:
                passages.append({"text": "\n".join(header + [row]), "section": section})
            continue
        if len(text) <= 1200:
            passages.append({"text": text, "section": section})
            continue
        window=""
        for sentence in re.split(r"(?<=[.!?。！？])\s+", text):
            if window and len(window) + len(sentence) > 700:
                passages.append({"text": window.strip(), "section": section})
                window=""
            window+=sentence + " "
        if window.strip():
            passages.append({"text": window.strip(), "section": section})
    for order, passage in enumerate(passages):
        passage["order"]=order
    return passages

def _rank_focus_passages(passages, focus):
    """Score passages against the focus string with BM25 plus phrase bonuses."""
    query_tokens=list(dict.fromkeys(_focus_tokens(focus)))
    if not query_tokens or not passages:
        return []
    tokenized=[_focus_tokens(passage["text"]) for passage in passages]
    average_length=sum(len(tokens) for tokens in tokenized) / len(tokenized) or 1.0
    document_frequency={token: sum(1 for tokens in tokenized if token in tokens) for token in query_tokens}
    total=len(passages)
    focus_lower=re.sub(r"\s+", " ", focus.strip().lower())
    query_pairs=list(zip(query_tokens, query_tokens[1:]))
    scored=[]
    for passage, tokens in zip(passages, tokenized):
        if not tokens:
            continue
        counts={}
        for token in tokens:
            counts[token]=counts.get(token, 0) + 1
        score=0.0
        for token in query_tokens:
            frequency=counts.get(token, 0)
            if not frequency:
                continue
            idf=math.log(1 + (total - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            score+=idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * len(tokens) / average_length))
        if score <= 0:
            continue
        adjacent=set(zip(tokens, tokens[1:]))
        score+=sum(0.75 for pair in query_pairs if pair in adjacent)
        if focus_lower and focus_lower in passage["text"].lower():
            score+=2.0
        section_tokens=set(_focus_tokens(passage.get("section") or ""))
        score+=0.3 * sum(1 for token in query_tokens if token in section_tokens)
        scored.append((score, passage))
    scored.sort(key=lambda item: (-item[0], item[1]["order"]))
    return scored

def _truncate_text(text, max_chars):
    """Cut text to max_chars, preferring a paragraph or sentence boundary."""
    if len(text) <= max_chars:
        return text
    cut=text[:max_chars]
    floor=int(max_chars * 0.8)
    for marker in ("\n\n", "\n", ". ", "。", "! ", "? "):
        position=cut.rfind(marker)
        if position >= floor:
            return cut[:position + len(marker)].rstrip()
    return cut.rstrip()

def _compact_chunk(chunk, verbose):
    if verbose:
        return dict(chunk)
    return {key: value for key, value in chunk.items() if key not in CHUNK_VERBOSE_FIELDS}

def shape_fetch_result(result, format="text", verbose=False, focus=None, focus_k=DEFAULT_FOCUS_K, max_chars=None):
    """Apply injection scrubbing, focus, truncation, and output format to a fetch result."""
    if not isinstance(result, dict):
        return result
    shaped=dict(result)
    findings=[]
    for field in ("title",):
        if isinstance(shaped.get(field), str):
            cleaned, found=scrub_injection(shaped[field], field=field)
            if found:
                shaped[field]=cleaned or ""
                findings.extend(found)
    content=shaped.get("content")
    chunks=[dict(chunk) for chunk in shaped.get("chunks") or [] if isinstance(chunk, dict)]
    chunk_findings=[]
    if isinstance(content, str):
        cleaned, found=scrub_injection(content, field="content")
        if found:
            content=cleaned or ""
            findings.extend(found)
    kept_chunks=[]
    for chunk in chunks:
        cleaned, found=scrub_injection(chunk.get("text"), field=f"chunks[{chunk.get('index')}]")
        if found:
            chunk_findings.extend(found)
            if not cleaned:
                continue
            chunk["text"]=cleaned
        kept_chunks.append(chunk)
    chunks=kept_chunks
    if isinstance(shaped.get("replies"), list):
        replies=[]
        for idx, reply in enumerate(shaped["replies"]):
            reply=dict(reply) if isinstance(reply, dict) else reply
            if isinstance(reply, dict) and isinstance(reply.get("content"), str):
                cleaned, found=scrub_injection(reply["content"], field=f"replies[{idx}].content")
                if found:
                    findings.extend(found)
                    reply["content"]=cleaned or ""
            replies.append(reply)
        shaped["replies"]=replies

    if focus and isinstance(content, str) and not shaped.get("error"):
        passages=_focus_passages(chunks, content)
        ranked=_rank_focus_passages(passages, focus)
        selected=sorted((passage for _, passage in ranked[:focus_k]), key=lambda passage: passage["order"])
        lines=[]
        previous_section=None
        for passage in selected:
            section=passage.get("section")
            if section and section != previous_section:
                lines.append(f"## {section}")
            previous_section=section
            lines.append(passage["text"])
        shaped["focus"]={
            "query": focus,
            "k": focus_k,
            "matched_passages": len(selected),
            "total_passages": len(passages),
            "full_content_chars": len(content),
        }
        content="\n\n".join(lines)
        chunks=[
            {"index": idx, "type": "excerpt", "text": passage["text"], "section_title": passage.get("section")}
            for idx, passage in enumerate(selected, 1)
        ]

    total_chars=len(content) if isinstance(content, str) else None
    truncated=False
    if max_chars is not None and isinstance(content, str) and len(content) > max_chars:
        content=_truncate_text(content, max_chars)
        truncated=True
    if max_chars is not None and format == "chunks" and chunks:
        budget=max_chars
        limited=[]
        for chunk in chunks:
            text=chunk.get("text") or ""
            if len(text) > budget:
                if budget > 0 and not limited:
                    limited.append(dict(chunk, text=_truncate_text(text, budget)))
                truncated=True
                break
            limited.append(chunk)
            budget-=len(text) + 1
        chunks=limited
    if truncated:
        shaped["truncated"]=True
        shaped["total_chars"]=total_chars

    if format == "chunks":
        shaped.pop("content", None)
        shaped["chunks"]=[_compact_chunk(chunk, verbose) for chunk in chunks]
        shaped["chunk_count"]=len(shaped["chunks"])
        findings.extend(chunk_findings)
    else:
        if isinstance(content, str):
            shaped["content"]=content
            shaped["content_word_count"]=len(re.findall(r"\S+", content))
        shaped.pop("chunks", None)
        shaped.pop("chunk_count", None)
    if not verbose:
        for field in FETCH_VERBOSE_FIELDS:
            shaped.pop(field, None)
        if shaped.get("final_url") == shaped.get("url"):
            shaped.pop("final_url", None)
        if shaped.get("canonical_url") in {shaped.get("url"), shaped.get("final_url")}:
            shaped.pop("canonical_url", None)
    if findings:
        shaped["injection_suspected"]=findings
    return shaped

def shape_search_result(result, engine, verbose=False, freshness=None, snippet_limit=None):
    """Scrub injected text, normalize dates, apply freshness and snippet limits."""
    if not isinstance(result, dict):
        return result
    shaped=dict(result)
    fields=[field for field in ("results", "brave_results") if isinstance(shaped.get(field), list)]
    earliest, latest=_freshness_bounds(freshness)
    for field in fields:
        items=[]
        removed_by_date=0
        removed_snippets=0
        for item in shaped[field]:
            if not isinstance(item, dict):
                items.append(item)
                continue
            item=dict(item)
            if isinstance(item.get("snippets"), list):
                item["snippets"]=list(item["snippets"])
            if "published_at" not in item:
                item["published_at"]=normalize_published_at(item.get("age"))
            _scrub_result_item(item)
            published=item.get("published_at")
            if published and (earliest or latest):
                published_date=datetime.strptime(published, "%Y-%m-%d").date()
                if (earliest and published_date < earliest) or (latest and published_date > latest):
                    removed_by_date+=1
                    continue
            if engine == "llm-context":
                limit=snippet_limit or DEFAULT_SNIPPET_LIMIT
                if len(item.get("snippets") or []) > limit:
                    removed_snippets+=len(item["snippets"]) - limit
                    item["snippets"]=item["snippets"][:limit]
                if not verbose:
                    item.pop("age", None)
            items.append(item)
        if removed_by_date or len(items) != len(shaped[field]):
            items=_annotate_rank(items)
            if field == "results":
                shaped["result_count"]=len(items)
                shaped["result_hosts"]=_collect_hostnames(items)
                shaped["result_host_count"]=len(shaped["result_hosts"])
            else:
                shaped["brave_result_count"]=len(items)
                shaped["brave_result_hosts"]=_collect_hostnames(items)
                shaped["brave_result_host_count"]=len(shaped["brave_result_hosts"])
        shaped[field]=items
        if removed_by_date:
            shaped["freshness_filtering"]={
                "freshness": freshness,
                "earliest": earliest.isoformat() if earliest else None,
                "latest": latest.isoformat() if latest else None,
                "removed_results": removed_by_date,
            }
        if removed_snippets:
            shaped["snippet_limiting"]={"limit": snippet_limit or DEFAULT_SNIPPET_LIMIT, "removed_snippets": removed_snippets}
    return shaped

def _exact_cache_lookup(query, engine, offset, cache_ttl, use_semantic, variant=None):
    """Attempt exact cache lookup and semantic-index backfill when needed."""
    result = read_from_cache(query, engine, offset, cache_ttl, variant=variant)
    if result and engine == "fetch":
        if result.get("fetched_via") == "llm-context" and _url_match_key(result.get("final_url")) != _url_match_key(query):
            return None
        if (result.get("title") or "").strip().lower() == "access denied" and "errors.edgesuite.net/" in (result.get("content") or ""):
            return None
    if result:
        result["_from_cache"] = True
        try:
            result["_cache_mtime"] = os.path.getmtime(_cache_file_path(query, engine, offset, variant))
        except OSError:
            pass
        if use_semantic and engine != "fetch":
            backfill_semantic_index(query, engine, offset, variant=variant)
    return result

def _semantic_cache_lookup(query, engine, offset, cache_ttl, semantic_threshold, variant=None):
    """Attempt semantic cache lookup for non-fetch engines."""
    if engine == "fetch":
        return None
    result, sim = read_from_semantic_cache(query, engine, offset, cache_ttl, semantic_threshold, variant=variant)
    if result:
        result["_from_cache"] = True
        result["_semantic_similarity"] = sim
    return result

def execute_engine(query, engine, config, offset=None, flaresolverr=False, search_options=None, max_replies=DEFAULT_MAX_REPLIES):
    """Run a single engine without any cache handling."""
    if engine not in VALID_ENGINES:
        raise ValueError(f"Unsupported engine: {engine}")

    error = validate_query(query, engine)
    if error:
        raise ValueError(error)

    if engine == "brave":
        api_key, _ = _select_brave_api_key()
        if not api_key:
            raise RuntimeError("BRAVE_SEARCH_API_KEY (or BRAVE_API_KEY fallback) environment variable not found.")
        return perform_brave_search(query, api_key, config, offset=offset, search_options=search_options)

    if engine in ("perplexity", "perplexity-verify"):
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY environment variable not found.")
        if engine == "perplexity-verify":
            return perform_perplexity_verify(query, api_key, config)
        return perform_perplexity_search(query, api_key, config)

    if engine == "both":
        brave_api_key, _ = _select_brave_api_key()
        perplexity_api_key = os.environ.get("OPENROUTER_API_KEY")
        if not brave_api_key or not perplexity_api_key:
            raise RuntimeError("A Brave key (BRAVE_SEARCH_API_KEY preferred, BRAVE_API_KEY fallback) and OPENROUTER_API_KEY are required for 'both' engine.")
        return perform_both_search(query, brave_api_key, perplexity_api_key, config, offset=offset, search_options=search_options)

    if engine == "llm-context":
        api_key, _ = _select_brave_api_key()
        if not api_key:
            raise RuntimeError("BRAVE_SEARCH_API_KEY (or BRAVE_API_KEY fallback) environment variable not found.")
        return perform_llm_context_search(query, api_key, config, search_options=search_options)

    fetch_config = config
    if flaresolverr:
        fetch_config = configparser.ConfigParser()
        fetch_config.read_dict({section: dict(config[section]) for section in config.sections()})
        fetch_config.set('Fetch', 'flaresolverr_mode', 'always')
    return fetch_with_fallbacks(query, fetch_config, max_replies=max_replies)

def execute_query(query, engine, config, offset=None, cache=False, cache_ttl=DEFAULT_CACHE_TTL_MINUTES, semantic_cache=False, semantic_threshold=0.9, flaresolverr=False, include_hosts=None, exclude_hosts=None, result_limit=None, max_cache_age=None, freshness=None, country=None, search_lang=None, snippet_limit=None, format="text", verbose=False, focus=None, focus_k=DEFAULT_FOCUS_K, max_chars=None, max_replies=DEFAULT_MAX_REPLIES):
    """Run a query through cache + engine execution and return a structured result."""
    started=time.time()
    if engine not in VALID_ENGINES:
        raise ValueError(f"Unsupported engine: {engine}")
    query_error = validate_query(query, engine)
    if query_error:
        raise ValueError(query_error)
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
        cache=cache,
        semantic_cache=semantic_cache,
        max_cache_age=max_cache_age,
        freshness=freshness,
        country=country,
        search_lang=search_lang,
        snippet_limit=snippet_limit,
        format=format,
        verbose=verbose,
        focus=focus,
        focus_k=focus_k,
        max_chars=max_chars,
        max_replies=max_replies,
    )
    if option_error:
        raise ValueError(option_error)
    if engine == "perplexity-verify":
        query="\n".join(normalize_claims(query))
    prune_cache()
    normalized_result_limit=None if result_limit is None else int(result_limit)
    if normalized_result_limit is None and engine in DEFAULT_RESULT_LIMITS:
        normalized_result_limit=DEFAULT_RESULT_LIMITS[engine]
    effective_ttl=min(cache_ttl, max_cache_age) if max_cache_age else cache_ttl

    search_options={}
    if engine in SEARCH_OPTION_ENGINES:
        search_options={
            key: value for key, value in (
                ("freshness", freshness.lower() if freshness else None),
                ("country", country.upper() if country else None),
                ("search_lang", search_lang.lower() if search_lang else None),
            ) if value
        }
    variant=dict(search_options)
    if engine == "fetch" and max_replies != DEFAULT_MAX_REPLIES:
        variant["max_replies"]=max_replies

    use_cache = cache or semantic_cache
    use_semantic = semantic_cache and engine in SEMANTIC_CACHE_ENGINES
    cache_status="disabled"

    result = None
    if use_cache:
        result = _exact_cache_lookup(query, engine, offset, effective_ttl, use_semantic, variant=variant)
        if result:
            cache_status="exact"
    if not result and use_semantic:
        result = _semantic_cache_lookup(query, engine, offset, effective_ttl, semantic_threshold, variant=variant)
        if result:
            cache_status="semantic"
    if not result:
        result = execute_engine(query, engine, config, offset=offset, flaresolverr=flaresolverr,
                                search_options=search_options, max_replies=max_replies)
        if use_cache:
            cache_status="miss"
        if use_cache and is_cacheable_result(result):
            cache_key = get_cache_key(query, engine, offset, variant)
            write_to_cache(query, engine, offset, result, variant=variant)
            if use_semantic:
                update_semantic_index(query, engine, offset, cache_key, variant=variant)
    if isinstance(result, dict):
        result=dict(result)
        cache_mtime=result.pop("_cache_mtime", None)
        result=_apply_host_filters(result, engine, include_hosts=normalized_include, exclude_hosts=normalized_exclude)
        if engine in {"brave", "both", "llm-context"}:
            result=shape_search_result(result, engine, verbose=verbose, freshness=search_options.get("freshness"), snippet_limit=snippet_limit)
        result=_apply_result_limit(result, engine, result_limit=normalized_result_limit)
        if engine == "llm-context":
            result.pop("sources", None)
            result.pop("source_count", None)
        if engine == "fetch":
            result=shape_fetch_result(result, format=format, verbose=verbose, focus=focus, focus_k=focus_k, max_chars=max_chars)
        if engine == "perplexity-verify" and not verbose:
            result.pop("citations", None)
        result["cache_status"]=cache_status
        result["cached_at"]=_iso_from_epoch(cache_mtime) if cache_status in {"exact", "semantic"} and cache_mtime else None
        result["duration_ms"]=round((time.time() - started) * 1000, 2)
    return result

def main():
    parser = argparse.ArgumentParser(description="Search, context retrieval, and URL fetch utility for humans and LLMs.")
    parser.add_argument("query", nargs="?", help="The search query, keyword, or URL (for fetch engine)")
    parser.add_argument("-e", "--engine", choices=list(VALID_ENGINES), required=False, help="Engine to use (brave, perplexity, both, fetch, llm-context, or perplexity-verify)")
    parser.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini"), help="Path to config INI file")
    parser.add_argument("--batch-file", help="Path to a JSON/JSONL batch file containing multiple requests")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format: json or text")
    parser.add_argument("--offset", type=int, default=None, help="Pagination offset (for brave and both engines)")
    parser.add_argument("--limit", type=int, default=None, help="Limit returned results for brave, both, and llm-context (default: 8)")
    parser.add_argument("--freshness", default=None, help="Brave freshness for brave/both/llm-context: pd, pw, pm, py, or YYYY-MM-DDtoYYYY-MM-DD")
    parser.add_argument("--country", default=None, help="Brave country code for brave/both/llm-context (for example US, TW, JP)")
    parser.add_argument("--search-lang", default=None, help="Brave search language for brave/both/llm-context (for example en, ja, zh-hant)")
    parser.add_argument("--snippet-limit", type=int, default=None, help=f"Maximum snippets per llm-context result (default: {DEFAULT_SNIPPET_LIMIT})")
    parser.add_argument("--fetch-format", choices=list(FETCH_FORMATS), default="text", help="Fetch body format: text returns content only, chunks returns chunks only (default: text)")
    parser.add_argument("--focus", default=None, help="Fetch only the passages most relevant to this topic")
    parser.add_argument("--focus-k", type=int, default=DEFAULT_FOCUS_K, help=f"Number of passages returned by --focus (default: {DEFAULT_FOCUS_K})")
    parser.add_argument("--max-chars", type=int, default=None, help="Truncate fetched content to this many characters")
    parser.add_argument("--max-replies", type=int, default=DEFAULT_MAX_REPLIES, help=f"Maximum forum replies for Discourse/Reddit/V2EX URLs (default: {DEFAULT_MAX_REPLIES})")
    parser.add_argument("--verbose", action="store_true", help="Include hashes, offsets, section paths, outbound links, and raw ages")
    parser.add_argument("--max-cache-age", type=int, default=None, help="Ignore cache entries older than this many minutes")
    parser.add_argument("--claim", action="append", default=[], help="Claim for perplexity-verify (repeatable; alternatively pass newline-separated claims as the query)")
    parser.add_argument("--cache", action="store_true", help="Enable results caching")
    parser.add_argument("--cache-ttl", type=int, default=DEFAULT_CACHE_TTL_MINUTES, help=f"Cache Time-To-Live in minutes (default/max: {DEFAULT_CACHE_TTL_MINUTES}, 90 days)")
    parser.add_argument("--semantic-cache", action="store_true", help="Enable semantic similarity cache via fastembed (implies --cache)")
    parser.add_argument("--semantic-threshold", type=float, default=0.9, help="Cosine similarity threshold for semantic cache (default: 0.9)")
    parser.add_argument("--flaresolverr", action="store_true", help="Force FlareSolverr mode for fetch engine (overrides config flaresolverr_mode to 'always')")
    parser.add_argument("--list-engines", action="store_true", help="List available engines and their configured status")
    parser.add_argument("--doctor", action="store_true", help="Show runtime diagnostics and dependency status")
    parser.add_argument("--prune-cache", action="store_true", help="Delete cache entries that have reached day 91")
    parser.add_argument("--batch-workers", type=int, default=None, help="Maximum concurrent workers for --batch-file execution")
    parser.add_argument("--include-host", action="append", default=[], help="Restrict brave/both/llm-context results to these hostnames (repeatable or comma-separated)")
    parser.add_argument("--exclude-host", action="append", default=[], help="Exclude brave/both/llm-context results from these hostnames (repeatable or comma-separated)")

    args = parser.parse_args()
    config = load_config(args.config)

    try:
        if args.prune_cache:
            payload=prune_cache(force=True)
            if args.format == "json":
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(
                    f"Cache cleanup: {payload['deleted_files']} file(s) deleted, "
                    f"{payload['pruned_index_entries']} semantic index entry/entries pruned, "
                    f"{payload['errors']} error(s)."
                )
            return

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
            payload=get_diagnostics(config, include_quota=True)
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
                print(f"- extended_fallbacks: {', '.join(payload['fetch']['extended_fallbacks']) or 'none'}")
                print("\nBatch:")
                print(f"- max_workers: {payload['batch']['max_workers']}")
                print(f"- cache_dir: {payload['cache_dir']}")
                quota=payload.get("quota") or {}
                print("\nQuota:")
                for key in (quota.get("brave") or {}).get("keys", []):
                    windows=key.get("windows") or []
                    summary=", ".join(
                        f"{window.get('remaining')}/{window.get('limit')} per {window.get('window_seconds')}s"
                        + (" (unlimited)" if window.get("unlimited") else "")
                        for window in windows
                    ) or "not observed yet"
                    print(f"- brave key {key['key']} ({key['fingerprint']}): {summary}")
                openrouter=quota.get("openrouter") or {}
                if openrouter.get("configured"):
                    print(f"- openrouter: usage={openrouter.get('usage')} limit={openrouter.get('limit')} remaining={openrouter.get('limit_remaining')}" if not openrouter.get("error") else f"- openrouter: {openrouter['error']}")
                else:
                    print("- openrouter: not configured")
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
                "max_cache_age": args.max_cache_age,
                "freshness": args.freshness,
                "country": args.country,
                "search_lang": args.search_lang,
                "snippet_limit": args.snippet_limit,
                "format": args.fetch_format,
                "verbose": args.verbose,
                "focus": args.focus,
                "focus_k": args.focus_k,
                "max_chars": args.max_chars,
                "max_replies": args.max_replies,
            }
            merged_defaults = dict(file_defaults)
            cli_default_values = {
                "engine": "brave",
                "cache": False,
                "cache_ttl": DEFAULT_CACHE_TTL_MINUTES,
                "semantic_cache": False,
                "semantic_threshold": 0.9,
                "offset": None,
                "result_limit": None,
                "flaresolverr": False,
                "include_hosts": [],
                "exclude_hosts": [],
                "max_cache_age": None,
                "freshness": None,
                "country": None,
                "search_lang": None,
                "snippet_limit": None,
                "format": "text",
                "verbose": False,
                "focus": None,
                "focus_k": DEFAULT_FOCUS_K,
                "max_chars": None,
                "max_replies": DEFAULT_MAX_REPLIES,
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
                    print(f"Deduplicated requests: {batch_result.get('deduped_request_count', batch_result['deduped_count'])}")
                    if batch_result.get("deduped_result_count"):
                        print(f"Repeated result URLs replaced by references: {batch_result['deduped_result_count']}")
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
                        if item.get("served_from"):
                            print(f"Served from: {item['served_from']}")
                        if item.get("final_url"):
                            print(f"Final URL: {item['final_url']}")
                        snippet=(item.get("content") or "").splitlines()
                        if snippet:
                            print(snippet[0])
                    elif item.get("engine") == "perplexity":
                        print((item.get("answer") or "").splitlines()[0] if item.get("answer") else "")
                    elif item.get("engine") == "both":
                        print((item.get("perplexity_answer") or "").splitlines()[0] if item.get("perplexity_answer") else "")
                    elif item.get("engine") == "perplexity-verify":
                        for verdict in item.get("results") or []:
                            print(f"[{verdict.get('verdict')}] {verdict.get('claim')}")
                    else:
                        first=(item.get("results") or item.get("brave_results") or [{}])[0]
                        if first.get("title"):
                            print(first["title"])
                        elif first.get("ref"):
                            print(f"{first['ref']} (see request {first.get('see_index')})")
                    print()
            if batch_result.get("has_errors") or any(item.get("has_partial_failure") for item in batch_result["results"]):
                sys.exit(1)
            return

        if args.claim:
            if args.engine not in (None, "perplexity-verify"):
                parser.error("--claim is only supported with -e perplexity-verify")
            args.engine="perplexity-verify"
            args.query="\n".join(args.claim + ([args.query] if args.query else []))
        if not args.query:
            parser.error("the following arguments are required: query")
        if not args.engine:
            parser.error("the following arguments are required: -e/--engine")

        extra_options={
            name: value for name, value in (
                ("max_cache_age", args.max_cache_age),
                ("freshness", args.freshness),
                ("country", args.country),
                ("search_lang", args.search_lang),
                ("snippet_limit", args.snippet_limit),
                ("format", args.fetch_format),
                ("verbose", args.verbose),
                ("focus", args.focus),
                ("focus_k", args.focus_k),
                ("max_chars", args.max_chars),
                ("max_replies", args.max_replies),
            ) if value != EXECUTION_OPTION_DEFAULTS[name]
        }
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
            **extra_options,
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
                    published = f" ({res['published_at']})" if res.get("published_at") else ""
                    print(f"{res.get('rank', '?')}. {res['title']}{hostname}{published}\n   URL: {res['url']}\n   {res.get('description') or ''}\n")
            elif args.engine == "perplexity-verify":
                print(f"Claim verification ({result.get('model', 'unknown')}):\n")
                for idx, verdict in enumerate(result.get("results") or [], 1):
                    print(f"{idx}. [{verdict.get('verdict')}] {verdict.get('claim')}")
                    if verdict.get("note"):
                        print(f"   {verdict['note']}")
                    for source in verdict.get("sources") or []:
                        print(f"   - {source}")
                    print()
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
                print(f"Results: {result.get('result_count', len(result['results']))}\n")
                for res in result["results"]:
                    hostname = f" [{res['hostname']}]" if res.get("hostname") else ""
                    print(f"{res.get('rank', '?')}. {res['title']}{hostname}")
                    print(f"   URL: {res['url']}")
                    if res.get("published_at"):
                        print(f"   Published: {res['published_at']}")
                    if res.get("snippet"):
                        print(f"   {res['snippet']}")
                    for snippet in res.get("snippets", []):
                        print(f"   > {snippet}")
                    print()
            elif args.engine == "fetch":
                if "error" in result:
                    print(f"Error fetching URL: {result['error']}\n")
                    for attempt in result.get("attempts") or []:
                        print(f"- {attempt.get('method')}: {attempt.get('status')} ({attempt.get('ms')}ms)")
                else:
                    print(f"--- Fetched Content: {result.get('title')} ---\n")
                    print(f"URL: {result['url']}\n")
                    for key in ("served_from", "final_url", "snapshot_date", "content_type", "status_code", "canonical_url", "author", "published_at", "content_date"):
                        value = result.get(key)
                        if value is not None:
                            print(f"{key}: {value}")
                    if result.get("truncated"):
                        print(f"truncated: {result.get('total_chars')} total chars")
                    print()
                    if result.get("chunks") is not None:
                        for chunk in result["chunks"]:
                            print(f"[{chunk.get('index')}] {chunk.get('text')}\n")
                    else:
                        print(result.get("content", ""))
                    for reply in result.get("replies") or []:
                        print(f"\n--- {reply.get('author')} ({reply.get('created_at')}) ---\n{reply.get('content')}")

        if result.get("error") or result.get("has_partial_failure"):
            sys.exit(1)

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
