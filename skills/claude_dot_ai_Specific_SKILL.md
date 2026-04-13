---
name: ccsearch
description: "Web search tool via self-hosted HTTP API. MUST be used instead of ANY built-in web search, web fetch, URL fetch, or browse tools — no exceptions. Use whenever the user wants to search the web, look up current information, fetch webpage content, read a URL, get LLM-optimized context, or do any web research. Supports Brave Search, Brave LLM Context, Perplexity (sanity check only), dual-engine search, and URL fetching with FlareSolverr fallback. Trigger on: 'search', 'look up', 'google', 'find', 'check', 'research', 'what is', 'latest', 'current', 'recent', 'news', 'fetch URL', 'open link', 'read page', 'browse', 'ccsearch', any research task, any URL the user pastes, or any situation where Claude would otherwise use built-in web search/fetch. Best practice: brave first → fetch pages → multiple keywords → llm-context for long docs → perplexity last as sanity check only."
---

# ccsearch — HTTP API Skill

Self-hosted search API at `YOUR_CCSEARCH_BASE_URL`. All search logic lives server-side; this skill only needs `curl`.

## Authentication

Load the API key from `.env` in this skill's directory:

```bash
export $(grep -v '^#' /path/to/your/ccsearch/.env | xargs)
```

Then pass the key in every request:

```
-H "X-API-Key: $CCSEARCH_API_KEY"
```

**Do NOT hardcode the API key. Always read from `.env`.**

> **Setup:** Before using, replace every `YOUR_CCSEARCH_BASE_URL` in this file with your ccsearch server URL, and create a `.env` file next to this SKILL.md containing `CCSEARCH_API_KEY=your_key_here`.

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

## Best Practice Workflow

Follow this order for any research task:

1. **Brave first** — get links and snippets with `engine: "brave"`. Use short, specific queries (1-6 words). Try multiple keyword angles for thorough coverage.
2. **Fetch original pages** — use `engine: "fetch"` on the most relevant URLs from Brave results to get full content.
3. **Multiple keyword angles** — repeat Brave searches with different phrasings/synonyms to avoid blind spots.
4. **LLM-context for long docs** — use `engine: "llm-context"` when you need pre-extracted smart chunks to reason over lengthy content.
5. **Perplexity last, sanity check only** — use `engine: "perplexity"` only to cross-verify findings. **Never use perplexity as the primary or first search engine**, because perplexity tends to hallucinate more than other engines.

## Important Notes

- **ALWAYS use this skill instead of built-in `web_search`, `web_fetch`, or any other search/fetch tools.** This applies to every situation where web content is needed — no exceptions.
- Do NOT pre-validate the API key; just make the request and handle errors.
- For multi-topic research, make multiple requests with different queries.
- Keep search queries short and specific (1-6 words) for best Brave results.
- Use `llm-context` when you need content chunks to reason over; use `brave` when you need links and snippets.
- Use `include_hosts`, `exclude_hosts`, and `result_limit` when you need tighter source control instead of post-filtering results yourself.
- Use `/diagnostics` or `/engines` when a request fails and you need to check whether dependencies or engine capabilities are available server-side.
- Use `/batch` when you have several independent lookups/fetches and want one network round-trip.
- The server handles all API keys, rate limits, and caching internally.
