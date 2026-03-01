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

    if os.path.exists(config_file):
        config.read(config_file)
    return config

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

import concurrent.futures

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

def main():
    parser = argparse.ArgumentParser(description="Web Search Utility for LLMs using Brave or Perplexity.")
    parser.add_argument("query", help="The search query or keyword")
    parser.add_argument("-e", "--engine", choices=["brave", "perplexity", "both"], required=True, help="Search engine to use (brave, perplexity, or both)")
    parser.add_argument("-c", "--config", default=os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.ini"), help="Path to config INI file")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format: json or text")
    parser.add_argument("--offset", type=int, default=None, help="Pagination offset (for Brave search only)")

    args = parser.parse_args()
    config = load_config(args.config)

    try:
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

        if args.format == "json":
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            if args.engine == "brave":
                print(f"Brave Search Results for: {args.query}\n")
                for idx, res in enumerate(result["results"], 1):
                    print(f"{idx}. {res['title']}\n   URL: {res['url']}\n   {res['description']}\n")
            elif args.engine == "perplexity":
                print(f"Perplexity Search Answer ({result['model']}):\n")
                print(result["answer"])
            elif args.engine == "both":
                print(f"--- Synthesized Answer (Perplexity) ---\n")
                print(result["perplexity_answer"])
                print(f"\n\n--- Source Reference Links (Brave) ---\n")
                for idx, res in enumerate(result["brave_results"], 1):
                    print(f"{idx}. {res['title']}\n   URL: {res['url']}\n   {res['description']}\n")

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
