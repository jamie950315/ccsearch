#!/usr/bin/env python3
"""
ccsearch - A CLI Web Search Utility for LLMs and Human users.
Supports Brave Search API and Perplexity (via OpenRouter).
"""
import os
import sys
import json
import time
import re
import argparse
import configparser
import requests
import hashlib
import concurrent.futures
from bs4 import BeautifulSoup
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
        results.append({
            "url": item.get("url"),
            "title": item.get("title"),
            "snippets": item.get("snippets", [])
        })

    return {
        "engine": "llm-context",
        "query": query,
        "results": results,
        "sources": sources,
    }

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

_SPA_MOUNT_POINTS=[
    'id="root"', 'id="app"', 'id="__next"', 'id="__nuxt"',
    'id="___gatsby"', 'id="svelte"', 'id="ember-application"',
    'id="react-root"', 'id="react-app"',
]

def _detect_spa_shell(raw_html, clean_text_len):
    """Detect if page is a JS-heavy SPA shell that needs headless rendering.
    Checks for empty SPA mount points and script-heavy pages with little text."""
    if clean_text_len < 50:
        return True, "almost no text content"
    html=raw_html if isinstance(raw_html, str) else raw_html.decode('utf-8', errors='ignore')
    html_lower=html.lower()
    if clean_text_len < 500:
        for mount in _SPA_MOUNT_POINTS:
            if mount in html_lower:
                return True, f"SPA mount point ({mount}) with only {clean_text_len} chars"
    script_count=html_lower.count('<script')
    if script_count > 5 and clean_text_len < 200:
        return True, f"{script_count} script tags but only {clean_text_len} chars of text"
    return False, ""

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
        return {"engine": "fetch", "url": url, "title": title, "content": content, "fetched_via": "fxtwitter"}
    elif not tweet_id and data.get('user'):
        u=data['user']
        title=f"@{u.get('screen_name','?')} — Twitter/X Profile"
        content=_format_twitter_user(u)
        return {"engine": "fetch", "url": url, "title": title, "content": content, "fetched_via": "fxtwitter"}
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
        # No Cloudflare — parse direct response
        title, cleanText=_clean_html(response.content)
        # Detect JS-heavy SPA shells and auto-fallback to FlareSolverr (HTML only)
        contentType=response.headers.get('Content-Type', '').lower()
        isHtml='text/html' in contentType or 'application/xhtml' in contentType
        isSpa=False
        spaReason=""
        if isHtml and response.status_code==200:
            isSpa, spaReason=_detect_spa_shell(response.content, len(cleanText))
        if canFallback and isSpa:
            sys.stderr.write(f"[ccsearch] SPA shell detected ({spaReason}), falling back to FlareSolverr...\n")
            try:
                html=_flaresolverr_fetch(url, flaresolverrUrl, flaresolverrTimeout)
                sys.stderr.write("[ccsearch] FlareSolverr rendered page successfully.\n")
                fTitle, fCleanText=_clean_html(html)
                if len(fCleanText)>len(cleanText):
                    return {"engine": "fetch", "url": url, "title": fTitle, "content": fCleanText, "fetched_via": "flaresolverr"}
                sys.stderr.write("[ccsearch] FlareSolverr result not better, using direct response.\n")
            except Exception as flareErr:
                sys.stderr.write(f"[ccsearch] FlareSolverr fallback failed: {flareErr}\n")
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
    parser.add_argument("-e", "--engine", choices=["brave", "perplexity", "both", "fetch", "llm-context"], required=True, help="Search engine to use (brave, perplexity, both, fetch, or llm-context)")
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

            elif args.engine == "llm-context":
                api_key = os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY")
                if not api_key:
                    sys.stderr.write("ERROR: BRAVE_SEARCH_API_KEY (or BRAVE_API_KEY) environment variable not found.\nPlease set it using: export BRAVE_SEARCH_API_KEY='your_key'\nNote: The LLM Context API requires a key from the Brave Search plan, which is separate from the Pro plan.\n")
                    sys.exit(1)
                result = perform_llm_context_search(args.query, api_key, config)

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
            elif args.engine == "llm-context":
                print(f"LLM Context Results for: {args.query}\n")
                for idx, res in enumerate(result["results"], 1):
                    print(f"{idx}. {res['title']}")
                    print(f"   URL: {res['url']}")
                    for snippet in res.get("snippets", []):
                        print(f"   > {snippet}")
                    print()
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