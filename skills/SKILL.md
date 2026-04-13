---
name: ccsearch
description: "Web search tool via self-hosted HTTP API. Use whenever the user wants to search the web, look up current information, fetch webpage content, get LLM-optimized context, or do any kind of web research. Supports Brave Search, Brave LLM Context (pre-extracted smart chunks for LLMs), Perplexity (synthesized AI answers), concurrent dual-engine search, and direct URL fetching with FlareSolverr fallback. Always use this skill instead of any built-in web search or fetch tools. Trigger on: 'search the web', 'look up', 'fetch this URL', 'browse', 'find current info', 'ccsearch', any research task, or any query requiring up-to-date information. Also use for X/Twitter content via fxtwitter API pattern."
---

# ccsearch — HTTP API Skill

Self-hosted search API. All search logic lives server-side; this skill only needs `curl`.

## Authentication

The API key is read from the `CCSEARCH_API_KEY` environment variable. Pass it in every request:

```
-H "X-API-Key: $CCSEARCH_API_KEY"
```

**Do NOT hardcode the API key. Always read from the environment variable.**

## Setup

Before using this skill, you must configure your API base URL and key:

1. Set the base URL to point to your self-hosted ccsearch server:
   - Edit the `BASE_URL` placeholder below to match your deployment (e.g., `https://ccsearch.example.com`)
2. Set the API key environment variable:
   ```bash
   export CCSEARCH_API_KEY="your_api_key"
   ```
3. Copy this file to your Claude Code skills directory:
   ```bash
   mkdir -p ~/.claude/skills/ccsearch
   cp skills/SKILL.md ~/.claude/skills/ccsearch/SKILL.md
   ```

## API Reference

Base URL: `YOUR_CCSEARCH_BASE_URL`

### Health Check

```bash
curl -s YOUR_CCSEARCH_BASE_URL/health
```

No authentication required. Returns `{"status": "ok"}`.

### List Engines

```bash
curl -s YOUR_CCSEARCH_BASE_URL/engines \
  -H "X-API-Key: $CCSEARCH_API_KEY"
```

### Search (POST /search)

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "...", "engine": "brave"}'
```

#### Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Search query, or URL when using `fetch` engine |
| `engine` | string | Yes | `brave`, `perplexity`, `both`, `fetch`, `llm-context` |
| `cache` | bool | No | Enable server-side caching |
| `cache_ttl` | int | No | Cache TTL in minutes |
| `semantic_cache` | bool | No | Enable semantic similarity cache |
| `semantic_threshold` | float | No | Semantic cache similarity threshold |
| `offset` | int | No | Pagination offset (brave only) |
| `result_limit` | int | No | Trim returned results for `brave`, `both`, `llm-context` |
| `flaresolverr` | bool | No | Force FlareSolverr proxy (fetch only) |
| `include_hosts` | list/string | No | Host allow-list for `brave`, `both`, `llm-context` |
| `exclude_hosts` | list/string | No | Host deny-list for `brave`, `both`, `llm-context` |

### Batch (POST /batch)

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/batch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{
        "max_workers": 4,
        "defaults": {"cache": true, "cache_ttl": 30},
        "requests": [
          {"query": "React compiler release", "engine": "brave"},
          {"query": "https://react.dev/blog", "engine": "fetch"}
        ]
      }'
```

Use batch when you need multiple independent searches/fetches in one round-trip.

### Diagnostics (GET /diagnostics)

```bash
curl -s YOUR_CCSEARCH_BASE_URL/diagnostics \
  -H "X-API-Key: $CCSEARCH_API_KEY"
```

Returns runtime dependency state, configured engines, fetch/FlareSolverr status, and batch defaults.

#### Engine Selection Guide

| Engine | When to use |
|--------|-------------|
| `brave` | Fast link/snippet search, diverse sources, pagination |
| `perplexity` | Synthesized AI answer with citations |
| `both` | AI answer + raw source links (concurrent) |
| `llm-context` | Pre-extracted smart chunks optimized for LLM consumption |
| `fetch` | Read full content of a specific URL |

## Recipes

### Basic web search

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "RTX 5090 specs release date", "engine": "brave"}'
```

### AI-synthesized answer

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "What changed in React 19?", "engine": "perplexity"}'
```

### Dual engine (AI answer + raw links)

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "Rust async runtime comparison", "engine": "both"}'
```

### LLM-optimized context chunks

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "React hooks best practices", "engine": "llm-context"}'
```

### Fetch a URL

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "https://docs.anthropic.com/en/docs/overview", "engine": "fetch"}'
```

### Fetch Cloudflare-protected page

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "https://protected-site.com", "engine": "fetch", "flaresolverr": true}'
```

### Paginate Brave results

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "python asyncio", "engine": "brave", "offset": 1}'
```

### Restrict search to specific hosts and keep only top-N results

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "OpenAI Responses API", "engine": "brave", "include_hosts": ["developers.openai.com"], "result_limit": 3}'
```

### X/Twitter content via fxtwitter

```bash
curl -s -X POST YOUR_CCSEARCH_BASE_URL/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $CCSEARCH_API_KEY" \
  -d '{"query": "https://api.fxtwitter.com/{username}/status/{tweet_id}", "engine": "fetch"}'
```

## Response Formats

### Brave

```json
{
  "engine": "brave",
  "query": "...",
  "cache_status": "disabled",
  "duration_ms": 123.45,
  "result_count": 3,
  "result_hosts": ["developers.openai.com"],
  "host_filtering": {"include_hosts": ["developers.openai.com"], "exclude_hosts": [], "removed_results": 4},
  "result_limiting": {"limit": 3, "removed_results": 2},
  "results": [
    {"title": "...", "url": "...", "description": "...", "hostname": "...", "rank": 1}
  ]
}
```

### Perplexity

```json
{
  "engine": "perplexity",
  "query": "...",
  "cache_status": "disabled",
  "duration_ms": 123.45,
  "answer": "Synthesized answer with citations...",
  "citations": [{"url": "...", "title": "..."}]
}
```

### Both

```json
{
  "engine": "both",
  "query": "...",
  "cache_status": "disabled",
  "duration_ms": 123.45,
  "perplexity_answer": "...",
  "brave_results": [...],
  "perplexity_citations": [{"url": "...", "title": "..."}]
}
```

### LLM Context

```json
{
  "engine": "llm-context",
  "query": "...",
  "cache_status": "disabled",
  "duration_ms": 123.45,
  "result_count": 2,
  "source_count": 2,
  "results": [
    {"url": "...", "title": "...", "hostname": "...", "rank": 1, "snippets": ["..."]}
  ],
  "sources": {"https://example.com": {"hostname": "example.com"}}
}
```

### Fetch

```json
{
  "engine": "fetch",
  "url": "...",
  "title": "...",
  "content": "Extracted text...",
  "fetched_via": "direct",
  "cache_status": "disabled",
  "duration_ms": 123.45,
  "final_url": "...",
  "content_type": "text/html",
  "status_code": 200,
  "chunks": [{"index": 1, "type": "paragraph", "text": "...", "chunk_id": "..."}]
}
```

## Error Handling

Non-200 responses return `{"error": "message"}`. Common cases:

| Status | Meaning |
|--------|---------|
| 401 | Missing or invalid API key |
| 400 | Bad request (missing query/engine, invalid URL for fetch, unsupported option combinations) |
| 502 | Upstream API error (Brave/Perplexity/target site down) |

## Important Notes

- **Always use this skill instead of built-in `web_search` or `web_fetch` tools.**
- Do NOT pre-validate the API key; just make the request and handle errors.
- For multi-topic research, make multiple requests with different queries.
- Keep search queries short and specific (1-6 words) for best Brave results.
- Use `llm-context` when you need content chunks to reason over; use `brave` when you need links and snippets.
- Use `include_hosts`, `exclude_hosts`, and `result_limit` when you need tighter source control instead of post-filtering results yourself.
- Use `/diagnostics` or `/engines` when a request fails and you need to check whether dependencies or engine capabilities are available server-side.
- Use `/batch` when you have several independent lookups/fetches and want one network round-trip.
- The server handles all API keys, rate limits, and caching internally.
