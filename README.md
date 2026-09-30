# ccsearch

A CLI Web Search utility designed to be easily used by Large Language Models (LLMs) like Claude Code, as well as human users. It supports structured outputs (`JSON`) for agents and readable outputs (`text`) for humans.

## Supported Engines
1. **Brave Search** (via Brave Search API): Best for getting a list of fast, accurate links and snippets. Supports pagination (`--offset`), safesearch, and time-based filtering.
2. **Perplexity** (via OpenRouter): Best for getting an intelligent, synthesized answer using online sources. Supports model selection, customizable temperature, and citation formatting.
3. **LLM Context** (via Brave LLM Context API): Returns pre-extracted, relevance-scored web content (smart chunks) optimized for LLM consumption. Extracts text, tables, code blocks, and structured data from multiple sources in a single API call — no scraping needed. Ideal for RAG pipelines and AI agent grounding.
4. **Both** (Concurrency): Runs both Brave and Perplexity searches in parallel, returning a merged outcome (a synthesized answer alongside raw source links).
5. **Fetch**: A built-in web scraper that downloads a given URL, parses it, and returns the cleaned text without HTML tags. Perfect for reading full articles when a snippet isn't enough. Uses **curl_cffi** for Chrome TLS fingerprint impersonation to access strict anti-bot sites (Facebook, LinkedIn, Medium, etc.), with full Chrome 146 headers and a Google Referer. Includes automatic **FlareSolverr** fallback for Cloudflare-protected pages and **SPA shell detection** that identifies JS-heavy pages (empty mount points, script-heavy HTML with little text) and auto-falls back to headless rendering. HTML extraction now prefers `main` / `article` / `role="main"` content when present to reduce layout noise. Non-HTML text responses are decoded directly, and supported binary documents (`PDF`, `DOCX`, `PPTX`, `XLSX`, etc.) can be converted to Markdown via optional **MarkItDown** integration. **Twitter/X URLs** are automatically intercepted and routed through the [fxtwitter API](https://github.com/FixTweet/FixTweet) to retrieve tweet content, author info, and engagement metrics without login. Forum threads on Discourse (for example `linux.do`), Reddit, and V2EX are read through their JSON APIs with a structured `replies` list. Pages that stay blocked fall back to Brave LLM Context passages for the same URL and then to the newest Wayback Machine snapshot (see [Fetch fallback chain](#fetch-fallback-chain)).
6. **Perplexity Verify** (`perplexity-verify`): Checks up to 10 claims one by one and returns `supported`, `contradicted`, or `not_found` for each, with sources mapped onto the real citation URLs Perplexity returned so they can be re-opened with `fetch`.

### Which engine to use

| Engine | Use it for |
|--------|------------|
| `brave` | Finding links and short summaries. Start research here. |
| `llm-context` | Reading long documents as pre-extracted passages, or getting content when the original site blocks `fetch`. |
| `fetch` | Reading the original page. |
| `perplexity` | Final cross-checking only; do not use it as the primary source. |
| `perplexity-verify` | Checking a short list of conclusions claim by claim. |

Search-style engines also normalize their output for downstream agents:
- Brave results include `hostname`, strip inline HTML tags, decode HTML entities, and deduplicate repeated URLs. They return the top **8** results by default (`result_limit` overrides this).
- Every `brave`, `both`, and `llm-context` result has a single `published_at` date (`YYYY-MM-DD`, or `null` when unknown). With `freshness`, results dated before the window are removed and reported in `freshness_filtering`.
- LLM Context results include `hostname`, `published_at`, cleaned snippet chunks, and any unique short `snippet` from Brave. By default they return at most 8 results with at most 5 snippets each (`result_limit` / `snippet_limit` override this). The raw, mixed-format `age` list is returned only with `verbose`. The duplicate top-level `sources` catalog is omitted.
- Text addressed to AI readers (for example `[CRITICAL INSTRUCTIONS FOR ALL AI ASSISTANTS ...]` blocks, "ignore previous instructions", and linux.do's fixed anti-AI notice) is removed from titles, descriptions, snippets, fetched content, and forum replies. Each removal is reported in the item's `injection_suspected` list with the original `text`, its `field`, character `start`, and the matching `rule`.
- Perplexity responses preserve normalized `citations` when the upstream model returns them.
- `both` preserves partial-failure visibility through `brave_error` or `perplexity_error` fields when one backend fails, and forwards `perplexity_citations` when available.
- Search results also carry stable positional metadata such as `rank`, `result_count`, and `brave_result_count` where relevant.

## Requirements & Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/jamie950315/ccsearch.git
   cd ccsearch
   ```
2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy the example configuration:
   ```bash
   cp config.ini.example config.ini
   ```
   *Modify `config.ini` to adjust rate limits, models, filtering, or retry logic.*
4. Add it to your CLI `$PATH` for global use:
   ```bash
   mkdir -p ~/.local/bin
   ln -sf $(pwd)/ccsearch.py ~/.local/bin/ccsearch
   ```
   *(Ensure `~/.local/bin` is in your environment's PATH so you can just run `ccsearch` from anywhere)*
5. Set your Environment Variables:
   - For all Brave-backed engines: `export BRAVE_SEARCH_API_KEY="your_brave_search_plan_key"`
   - Extra Brave Search keys rotate automatically for any count, not just three. Keep the original key first, then add `BRAVE_SEARCH_API_KEY_2`, `BRAVE_SEARCH_API_KEY_3`, `BRAVE_SEARCH_API_KEY_4`, and so on. You can also put several keys in one comma-separated `BRAVE_SEARCH_API_KEY` or in `BRAVE_SEARCH_API_KEYS`.
   - `brave`, the Brave side of `both`, and `llm-context` all prefer `BRAVE_SEARCH_API_KEY`. The legacy `BRAVE_API_KEY` remains a compatibility fallback only when no Search key is set. Each live Brave request uses the next key; cache hits do not rotate, and retries of the same request keep the same key.
   - For Perplexity: `export OPENROUTER_API_KEY="your_openrouter_api_key"`

### Fetch Document Support

- `requirements.txt` includes MarkItDown's PDF dependencies, so PDF-to-Markdown conversion works in the standard install.
- For additional Office formats, install the extra formats you need:
  ```bash
  pip install 'markitdown[docx,pptx,xlsx]'
  ```
- If a requested converter is unavailable or fails, `fetch` returns a structured error payload instead of low-quality extracted text.

## Usage for Humans

```bash
# Brave Search (Text Output)
ccsearch "latest React documentation" -e brave --format text

# Brave Search (2nd page of results using offset)
ccsearch "latest React documentation" -e brave --format text --offset 1

# Perplexity Synthesis (Text Output)
ccsearch "What is the difference between Vue 3 and React 18?" -e perplexity --format text

# LLM Context (Pre-extracted smart chunks for grounding)
ccsearch "React hooks best practices" -e llm-context --format text

# Both Engines Concurrently (Merged Text Output)
ccsearch "What is the new React compiler?" -e both --format text

# Fetch a webpage's clean text
ccsearch "https://react.dev/blog/2025/10/07/react-compiler-1" -e fetch --format text

# Fetch a PDF (requires optional MarkItDown install for Markdown conversion)
ccsearch "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf" -e fetch --format json

# Fetch a tweet (auto-routed via fxtwitter API)
ccsearch "https://x.com/jack/status/20" -e fetch --format text

# Fetch a Twitter/X user profile
ccsearch "https://x.com/NASA" -e fetch --format text

# Only results from the past month, from Taiwan, in Traditional Chinese
ccsearch "Cursor pricing" -e brave --freshness pm --country TW --search-lang zh-hant --format json

# Read only the passages about one topic, capped at 4,000 characters
ccsearch "https://flexprice.io/blog/cursor-pricing-guide" -e fetch --focus "Pro Plus included usage" --max-chars 4000 --format json

# Read a forum thread (Discourse, Reddit, V2EX) with up to 10 replies
ccsearch "https://linux.do/t/topic/2911949" -e fetch --max-replies 10 --format json

# Verify conclusions claim by claim
ccsearch -e perplexity-verify --claim "Cursor Pro Plus includes \$70 of third-party usage" --claim "Cursor Pro costs \$20 per month" --format json

# Run a mixed batch from JSON/JSONL with bounded concurrency
ccsearch --batch-file requests.json --batch-workers 4 --format json

# Keep only the top 3 results after host filtering/post-processing
ccsearch "OpenAI Responses API" -e brave --include-host developers.openai.com --limit 3 --format json

# Force FlareSolverr for a Cloudflare-protected page
ccsearch "https://some-cloudflare-site.com" -e fetch --format text --flaresolverr

# Inspect engine availability and current setup
ccsearch --list-engines --format json
ccsearch --doctor --format text
```

## Advanced Usage

### Caching Results

#### Exact Cache (`--cache`)
Caches results by an exact hash of the query string. Subsequent identical queries return instantly without hitting the API.
```bash
# Cache the result for the default 90 days
ccsearch "React 19 release date" -e perplexity --cache

# Cache the result for a custom duration (e.g., 60 minutes)
ccsearch "React 19 release date" -e perplexity --cache --cache-ttl 60
```
*Cache files are stored in `~/.cache/ccsearch/` as JSON files keyed by MD5 hash of `(query, engine, offset)` plus any request options that change upstream results (`freshness`, `country`, `search_lang`, and a non-default `max_replies`).* Cache hits return `cached_at` (UTC ISO 8601) with `cache_status`; `--max-cache-age` (`max_cache_age` in the API/MCP, in minutes) ignores entries older than that age and fetches fresh data. The default and maximum readable age is 90 days (`129600` minutes). A smaller `--cache-ttl` shortens the freshness window. Beginning on day 91, result files are deleted by the hourly maintenance timer or the next cache cleanup pass. Run `ccsearch --prune-cache --format json` to enforce retention immediately.

Automatic cleanup scans run at most once per hour across CLI, API, and MCP processes sharing the same cache directory on systems with file locking. Explicit `--prune-cache` always runs a scan.

For the `fetch` engine, URLs are normalized before hashing so cache hits survive:

- tracking parameters such as `utm_*`, `fbclid`, `gclid`, etc.
- query parameter name reordering (the order of repeated values is preserved)
- fragment-only differences
- host casing and default port differences

For search-style engines, exact cache keys also normalize repeated whitespace so `React   hooks` and `React hooks` reuse the same cache entry.

#### Semantic Cache (`--semantic-cache`)
Extends exact caching with **embedding-based similarity matching**. If a semantically equivalent query was previously cached, the result is returned without a new API call — even if the wording differs.

Requires `fastembed` (`pip install fastembed`). Uses the `BAAI/bge-small-en-v1.5` model (384-dim, ~40MB, runs entirely locally via ONNX).

```bash
# First search — result is cached and embedding is stored
ccsearch "Python asyncio event loop tutorial" -e brave --semantic-cache --cache-ttl 60

# Semantically similar query — returns the cached result (no API call)
ccsearch "Python asyncio event loop guide" -e brave --semantic-cache --cache-ttl 60
# Output includes: "_from_cache": true, "_semantic_similarity": 0.9434
```

**Adjusting the similarity threshold** (default `0.9`, range `0.0`–`1.0`):
```bash
# Stricter: only very close paraphrases hit the cache
ccsearch "Python asyncio tutorial" -e brave --semantic-cache --semantic-threshold 0.95

# Looser: broader topic matching (useful for exploratory queries)
ccsearch "Python asyncio tutorial" -e brave --semantic-cache --semantic-threshold 0.85
```

**How it works:**
1. On a **cache miss**, the query is embedded and stored alongside the result in `~/.cache/ccsearch/semantic_index.json`
2. On a subsequent query, the new embedding is compared against all stored embeddings using cosine similarity
3. If the best match exceeds the threshold, the cached result is returned with `_semantic_similarity` set
4. Falls back to exact-match cache first (faster), then semantic search, then live API call
5. `--semantic-cache` implies `--cache` — no need to pass both flags

**Notes:**
- Semantic cache is **off by default** and should stay off for time-sensitive research. Candidates must contain exactly the same numbers as the new query, so "Opus 5.5 pricing" never reuses "Opus 5 pricing", and they must share the same `freshness`/`country`/`search_lang` options.
- Applies to `brave`, `perplexity`, `both`, and `llm-context` engines. The `fetch` and `perplexity-verify` engines always use exact matching.
- If `fastembed` is not installed, a warning is printed and the tool continues without semantic matching.
- The same `--cache-ttl` applies to both caches. It cannot exceed 90 days, and semantic-index entries are removed when their result files are deleted.

**Benchmark results** (Brave engine, 6 query pairs):

| Condition | Avg. latency |
|-----------|-------------|
| Cold API call | ~1,350ms |
| Semantic cache hit | ~360ms |
| Exact cache hit | ~95ms |

Semantic cache delivers ~**73% faster** responses vs. cold API calls for similar queries.

## HTTP API Server

ccsearch can also be accessed remotely via the built-in HTTP API server (`api_server.py`), allowing other LLMs and services to use ccsearch over the network.

### Quick Start

```bash
# Start the server (default port 8888)
python3 api_server.py

# Or via systemd (current server deployment)
sudo systemctl start ccsearch-api
```

### Authentication

All endpoints except `/health` require an `X-API-Key` header. The API key is resolved in this order:
1. `CCSEARCH_API_KEY` environment variable
2. `.api_key` file in the project directory (auto-generated on first run with `0600` permissions)

### Endpoints

#### `GET /health`
Health check (no auth required).
```bash
curl https://ccsearch.example.com/health
# {"status": "ok", "service": "ccsearch-api"}
```

#### `POST /search`
Main search endpoint. Accepts a JSON body with the following fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Search query, URL (for fetch engine), or newline-separated claims (for `perplexity-verify`) |
| `engine` | string | Yes | `brave`, `perplexity`, `both`, `fetch`, `llm-context`, or `perplexity-verify` |
| `claims` | list | No | Claims for `perplexity-verify` (up to 10), instead of `query` |
| `cache` | bool | No | Enable result caching (default: `false`) |
| `cache_ttl` | int | No | Cache freshness in minutes (default/max: `129600`, or 90 days) |
| `max_cache_age` | int | No | Ignore cache entries older than this many minutes |
| `semantic_cache` | bool | No | Enable semantic similarity cache (default: `false`) |
| `semantic_threshold` | float | No | Cosine similarity threshold (default: `0.9`) |
| `offset` | int | No | Pagination offset (`brave` and `both` only) |
| `result_limit` | int | No | Results returned for `brave`, `both`, and `llm-context` (default: `8`) |
| `freshness` | string | No | `pd`, `pw`, `pm`, `py`, or `YYYY-MM-DDtoYYYY-MM-DD` for `brave`, `both`, `llm-context`; dates must exist and the start must not follow the end |
| `country` | string | No | Two-letter country code (for example `US`, `TW`) or `ALL` |
| `search_lang` | string | No | Search language code (for example `en`, `ja`, `zh-hant`) |
| `snippet_limit` | int | No | Snippets per `llm-context` result (default: `5`) |
| `flaresolverr` | bool | No | Force FlareSolverr for fetch engine (default: `false`) |
| `format` | string | No | Fetch body: `text` (default, `content` only) or `chunks` (`chunks` only) |
| `focus` | string | No | Fetch only the passages most relevant to this topic |
| `focus_k` | int | No | Number of focus passages (default: `5`) |
| `max_chars` | int | No | Truncate fetched content; adds `truncated: true` and `total_chars` |
| `max_replies` | int | No | Forum replies for Discourse/Reddit/V2EX threads (default: `30`); `0` returns no replies and skips optional reply requests |
| `verbose` | bool | No | Include hashes, offsets, section paths, outbound links, transport headers, and raw ages |
| `include_hosts` | list/string | No | Host allow-list for `brave`, `both`, and `llm-context` |
| `exclude_hosts` | list/string | No | Host deny-list for `brave`, `both`, and `llm-context` |

All single-query responses now include:
- `cache_status`: one of `disabled`, `exact`, `semantic`, or `miss`
- `cached_at`: when the served cache entry was written (UTC), or `null` for live results
- `duration_ms`: end-to-end execution time for the request

Search-style engines also expose lightweight source-host summaries:
- Brave / LLM Context: `result_hosts`, `result_host_count`
- Perplexity: `citation_hosts`, `citation_host_count` when citations are available
- Both: `brave_result_hosts`, `brave_result_host_count`, `perplexity_citation_hosts`, `perplexity_citation_host_count`

For `brave`, `both`, and `llm-context`, you can also apply host filters at request time:
- `include_hosts`: only keep results from these hosts
- `exclude_hosts`: drop results from these hosts
- `host_filtering`: response metadata showing the normalized filters that were applied and how many results were removed
- `result_limit`: trim the remaining result list to a stable top-N after filtering, with `result_limiting` metadata describing the applied limit and removed count

For `fetch` responses, the default JSON payload is compact:
- `content`: the extracted main text. Chunks are **not** returned by default, so the body is not duplicated. With `format: "chunks"`, only `chunks` is returned (no `content`).
- `ok`: `true` when content was extracted; `false` together with `error` otherwise. Callers must treat any `error` as a failed fetch even when transport metadata is present.
- `served_from`: `direct`, `flaresolverr`, `site-api`, `llm-context`, or `archive` (`null` on failure)
- `attempts`: every step tried, in order, as `{"method", "status", "ms"}` plus `http_status`, `site`, or `detail` when relevant. Statuses include `ok`, `cf_challenge`, `http_error`, `transport_error`, `empty`, `spa_shell`, `no_match`, `no_snapshot`, `unavailable`, and `error`.
- `fetched_at`: when the page was retrieved (UTC); `content_date`: the page's own date (`published_at`, else modified time or `Last-Modified`) as `YYYY-MM-DD`
- `snapshot_date`: Wayback snapshot time when `served_from` is `archive`; `content_scope: "excerpts"` when `served_from` is `llm-context`
- `title`, `status_code`, `content_type`, `final_url` (only when it differs from `url`), `converted_via` for converted documents
- Forum threads add `replies` (`author`, `created_at`, `content`, and `post_number`/`score`/`depth` where the site provides them), `reply_count`, `returned_reply_count`, and `forum` (`platform`, topic id)
- `focus` metadata and a focused `content` when `focus` is set; `truncated` and `total_chars` when `max_chars` cut the content
- `injection_suspected` when AI-directed text was removed

With `verbose: true`, fetch results also include `content_sha256`, `content_word_count`, `content_length`, `etag`, `last_modified`, `filename`, `hostname`, `fetched_via`, `outbound_links` with their counts and hosts, and full chunk metadata (`chunk_id`, `char_start`, `char_end`, `relative_position`, `section_path`, `text_sha256`, link counts, and list/table/code counts). Compact chunks keep `index`, `type`, `text`, `section_title`, and small structural hints such as `heading_level`, `code_language`, `list_ordered`, and `table_headers`.

Content hashes and chunk offsets describe the returned text after scrubbing, focus, and truncation. For chunk output, `content_sha256` hashes chunk texts joined with a single newline. Transport fields such as `content_length` and `etag` describe the original response.

HTML extraction removes documentation chrome before choosing the main text: a single `<main>`/`role="main"` or `<article>` landmark wins over longer navigation sidebars, and side navigation, tables of contents, breadcrumbs, pagers, author boxes, and call-to-action blocks marked by whole class/id tokens are pruned. Repeated responsive-layout headings are merged and "Previous/Next post" links are dropped.

For HTML pages, `fetch` also extracts page metadata when available:
- `lang`: page language from the root HTML tag
- `description`: page summary from standard or Open Graph meta tags
- `author`: author metadata from common article meta tags
- `published_at`: publish date from common article meta tags, normalized to `YYYY-MM-DD`
- `canonical_url`: canonical URL from `<link rel="canonical">` when it differs from the requested URL

When those HTML meta tags are missing, ccsearch also falls back to JSON-LD article schemas and prunes common non-content UI blocks such as cookie banners and newsletter popups before extracting the main text.
It also sniffs mislabeled HTML payloads (for example, pages served as `application/octet-stream`) so SPA fallback and metadata extraction still work on poorly configured sites.
For HTML pages, lists and tables are preserved in a Markdown-like form inside both `content` and `chunks`.
Code examples are preserved as fenced Markdown code blocks, and code chunks expose `code_language` when the page declares a recognizable language class such as `language-python`.

```bash
# Brave search
curl -X POST https://ccsearch.example.com/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "React 19 new features", "engine": "brave"}'

# Perplexity synthesized answer
curl -X POST https://ccsearch.example.com/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "What is the difference between Vue 3 and React 18?", "engine": "perplexity"}'

# Fetch a URL
curl -X POST https://ccsearch.example.com/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "https://react.dev/blog", "engine": "fetch"}'

# With caching
curl -X POST https://ccsearch.example.com/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "Python asyncio tutorial", "engine": "brave", "cache": true, "cache_ttl": 60}'
```

#### `POST /batch`
Execute multiple search and fetch requests in a single round-trip.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `requests` | array | Yes | List of request objects (see the dispatch rules below), plus any per-request options |
| `defaults` | object | No | Default options applied to each entry where they apply (for example `engine`, `cache`, `result_limit`, `freshness`, `focus`, `max_chars`) |
| `max_workers` | int | No | Maximum concurrent worker threads (defaults to `[Batch].max_workers`) |
| `dedupe_results` | bool | No | Replace repeated search-result URLs with references (default: `true`) |

Dispatch rules for each request:
- An entry with `url` is fetched (engine `fetch`).
- An entry with `query` is searched with its own `engine`, else `defaults.engine`, else `brave`.
- `op: "search"` or `op: "fetch"` overrides the inference.
- An entry with both `url` and `query` and no `op`, or with neither, fails on its own; the other entries still run.
- `defaults.engine` applies to search entries only; it never changes a `url` entry. (For compatibility, a `query` entry whose engine resolves to `fetch` is still fetched.)
- `{"engine": "perplexity-verify", "claims": [...]}` verifies claims.
- Defaults fill only the options that apply to each entry's engine (search options for searches, `format`/`focus`/`max_chars`/`max_replies`/`flaresolverr` for fetches). Options set explicitly on an entry are validated strictly.
- Every result reports the `engine` it actually used.

The response includes:
- `results`: per-request results in original order
- `count`, `success_count`, `error_count`, `has_errors`
- `duration_ms`: total batch runtime
- `max_workers`: effective concurrency used
- `deduped_count`: `deduped_request_count` + `deduped_result_count`
- `deduped_request_count`: identical requests reused instead of executed again (marked with `_batch_deduped` and `_batch_deduped_from`)
- `deduped_result_count`: search results whose URL already appeared in an earlier entry; they are replaced by `{"ref": "<url>", "see_index": n, "rank": r}` pointing at the first entry that returned it. Brave-style results (`brave`, `both`) and `llm-context` results are deduplicated separately because their payloads differ.
- `engine_counts`: request count by engine

```bash
curl -X POST https://ccsearch.example.com/batch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{
        "defaults": {"freshness": "pm", "max_chars": 4000},
        "requests": [
          {"query": "Cursor pricing 2026"},
          {"query": "Cursor Pro Plus usage", "engine": "llm-context"},
          {"url": "https://cursor.com/docs/models-and-pricing", "focus": "Pro Plus included usage"},
          {"url": "https://linux.do/t/topic/2911949", "max_replies": 10}
        ]
      }'
```

#### `GET /engines`
List available engines and their server-side capabilities.
```bash
curl https://ccsearch.example.com/engines \
  -H "X-API-Key: YOUR_API_KEY"
```

Each engine entry includes:
- `name`, `description`, `use_for`, `requires`
- `category` (`search`, `answer`, `context`, `hybrid`, `fetch`, or `verify`)
- defaults where relevant: `default_result_limit`, `default_snippet_limit`, `default_format`, `default_focus_k`, `default_max_replies`, `max_claims`
- `supports_search_options` (`freshness`, `country`, `search_lang`)
- `supports_offset`
- `supports_semantic_cache`
- `supports_flaresolverr`
- `supports_host_filter`
- `supports_result_limit`
- `required_env_vars`
- `configured`
- `configured_via`

Invalid option combinations are rejected consistently across CLI, HTTP API, and MCP.
Examples: `offset` is only valid for `brave` / `both`, and `flaresolverr` is only valid for `fetch`.

#### `GET /diagnostics`
Return runtime diagnostics without exposing secret values.
```bash
curl https://ccsearch.example.com/diagnostics \
  -H "X-API-Key: YOUR_API_KEY"
```

The response includes:
- dependency availability (`curl_cffi`, `fastembed`, `markitdown`, `mcp`)
- environment-key presence as booleans
- Brave key rotation state such as `key_count`, `round_robin`, per-key RPS, and `combined_cap_rps` (no secret values)
- fetch runtime state such as `flaresolverr_configured`, `flaresolverr_mode`, and `extended_fallbacks`
- batch runtime defaults such as `max_workers`
- `quota.brave.keys[]`: for each configured key (by position and non-secret fingerprint), the rate-limit windows from the most recent Brave response (`window_seconds`, `limit`, `remaining`, `reset_seconds`, `unlimited`) and when they were observed. The limiter cannot see traffic from other hosts, so these numbers come from Brave's own headers.
- `quota.openrouter`: live OpenRouter key usage (`usage`, `limit`, `limit_remaining`, `rate_limit`), cached for 60 seconds
- the current engine list

### Deployment

A typical deployment runs the API server as a systemd service (for example `ccsearch-api.service` with `Restart=always`) whose unit loads the project's `.env` through systemd's `EnvironmentFile=` setting. The Python program does not load `.env` itself, so manual runs must export the variables first. Standby hosts can keep an up-to-date checkout with their services disabled until an explicit failover.

`ccsearch-cache-prune.timer` runs the checked-in `systemd/ccsearch-cache-prune.service` hourly so files that have reached day 91 are removed even when they are never requested again.

This service currently starts Flask's built-in server directly. It is suitable for a personal deployment but is not a production WSGI/ASGI setup; replacing it remains tracked in `TODO.md`.

```bash
sudo systemctl enable ccsearch-api   # Enable on boot
sudo systemctl start ccsearch-api    # Start
sudo systemctl status ccsearch-api   # Check status
journalctl -u ccsearch-api -f        # View logs
```

Expose the service publicly through a reverse proxy or tunnel (for example Cloudflare Tunnel) rather than opening port 8888 directly; keep the API and MCP ports firewalled from the Internet. FlareSolverr is bound to `127.0.0.1:8191` by the checked-in compose file and is not publicly reachable.

---

## MCP Server

`mcp_server.py` exposes ccsearch as an [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server over both SSE and Streamable HTTP transport. It runs as an independent process alongside the Flask HTTP API, sharing the same `ccsearch.py` core and `config.ini`. In a systemd deployment, both units load the same `.env`; manual runs must export those variables themselves.

### Architecture

```
ccsearch.py (core search logic, shared)
    ├── api_server.py   (Flask HTTP API, port 8888)
    └── mcp_server.py   (MCP server, port 8890, SSE + Streamable HTTP)
```

### Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `search` | Web search via brave/perplexity/both/llm-context engines | `query`, `engine`, `offset`, `result_limit`, `freshness`, `country`, `search_lang`, `snippet_limit`, `include_hosts`, `exclude_hosts`, `verbose`, `cache`, `cache_ttl`, `max_cache_age`, `semantic_cache`, `semantic_threshold` |
| `fetch` | Fetch a URL through the fallback chain | `url`, `format`, `focus`, `focus_k`, `max_chars`, `max_replies`, `verbose`, `flaresolverr`, `cache`, `cache_ttl`, `max_cache_age` |
| `verify` | Check claims one by one with Perplexity | `claims`, `verbose`, `cache`, `cache_ttl`, `max_cache_age` |
| `batch` | Execute multiple search/fetch requests in one call | `requests`, shared defaults for every option above, `max_workers`, `dedupe_results` |
| `engines` | List available engines, what each is for, and their defaults | none |
| `diagnostics` | Return dependency, runtime, and quota diagnostics | none |

The MCP server instructions and every tool description state which engine to use for what, the batch dispatch rules with an example, and the defaults (`result_limit` 8, `snippet_limit` 5, `format` text, `focus_k` 5, `max_replies` 30, and when FlareSolverr is used). `fetch` returns the same fields as the HTTP API.

### Authentication

Path-based authentication — the API key is embedded in the URL path:

```
SSE:             https://ccsearch-mcp.example.com/<CCSEARCH_API_KEY>/sse
Streamable HTTP: https://ccsearch-mcp.example.com/<CCSEARCH_API_KEY>/mcp
```

Requests to any other path (missing or incorrect key) receive a `401 Unauthorized` response.

Uvicorn access logging is disabled, but the key-bearing path can still appear in proxy and client logs. Treat complete MCP URLs as secrets and redact the first path segment before sharing logs. Only rotate a key after explicit operator approval.

### Client Configuration

**Claude Desktop (`claude_desktop_config.json`)**:
```json
{
  "mcpServers": {
    "ccsearch": {
      "url": "https://ccsearch-mcp.example.com/<CCSEARCH_API_KEY>/sse"
    }
  }
}
```

**Python MCP SDK (SSE)**:
```python
from mcp import ClientSession
from mcp.client.sse import sse_client

async with sse_client("https://ccsearch-mcp.example.com/<KEY>/sse") as (r, w):
    async with ClientSession(r, w) as session:
        await session.initialize()
        await session.call_tool("search", {"query": "hello", "engine": "brave"})
```

**Python MCP SDK (Streamable HTTP)**:
```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("https://ccsearch-mcp.example.com/<KEY>/mcp") as (r, w, _):
    async with ClientSession(r, w) as session:
        await session.initialize()
        await session.call_tool("search", {"query": "hello", "engine": "brave"})
```

### Deployment

- **Runtime**: Python 3 with `mcp>=1.26.0,<2` (FastMCP imports used by this project are not compatible with MCP 2.x)
- **Known warning**: MCP 1.26 emits an upstream Pydantic startup warning; both transports work despite it.
- **Port**: 8890 (configurable via `CCSEARCH_MCP_PORT` env var)
- **Systemd service**: `ccsearch-mcp.service`
- **Public route**: expose it through a reverse proxy or tunnel (for example `ccsearch-mcp.example.com → localhost:8890`), not by opening the port directly

```bash
sudo systemctl enable --now ccsearch-mcp.service
sudo systemctl status ccsearch-mcp
```

## FlareSolverr Integration (Optional)

The `fetch` engine uses a multi-layered approach to access protected websites:

1. **curl_cffi** (recommended): Impersonates Chrome's TLS fingerprint (JA3/JA4), which bypasses most anti-bot detection (Facebook, LinkedIn, Medium, Instagram, etc.). Install with `pip install curl_cffi`. Falls back to `requests` if not installed.
2. **FlareSolverr**: For Cloudflare challenge pages and JS-rendered SPAs that require a real browser. [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) is a self-hosted proxy that uses a real Chromium browser to solve browser challenges.

### Setup
1. Start the checked-in Docker Compose service:
   ```bash
   docker compose up -d flaresolverr
   ```
   The included compose file publishes port `8191` on `127.0.0.1` only. FlareSolverr has no authentication; preserve this loopback binding on Internet-facing hosts.
2. Add the URL to your `config.ini`:
   ```ini
   [Fetch]
   flaresolverr_url = http://localhost:8191/v1
   flaresolverr_mode = fallback
   ```

### Modes
- **`fallback`** (default): Tries a normal HTTP request first. Eligible Cloudflare challenge responses and network failures may retry through FlareSolverr. Ordinary HTTP errors such as 404 and known binary-document URLs are preserved instead of being replaced by rendered HTML.
- **`always`**: Skips the normal request and always uses FlareSolverr. Useful for sites that are known to be protected.
- **`never`**: Never uses FlareSolverr, even if configured.

You can also force FlareSolverr for a single invocation with the `--flaresolverr` CLI flag:
```bash
ccsearch "https://cloudflare-site.com" -e fetch --format json --flaresolverr
```

### Detection
The tool automatically detects Cloudflare challenges by checking for:
- `"Just a moment..."` in the page title, a `cf-mitigated: challenge` header, or the `_cf_chl_opt` challenge script
- `"Checking your browser"` or `"cf-browser-verification"` in the response body
- `"challenge-platform"` only on error responses or pages with almost no visible text. Normal Cloudflare-hosted pages (for example `openai.com` and `linux.do`) load `/cdn-cgi/challenge-platform` beacon scripts and are no longer mistaken for challenges.

### Fetch fallback chain

Akamai interstitials trigger the configured browser fallback. Unresolved Akamai
challenges and denial pages fail even if the browser proxy reports HTTP 200;
rendering does not guarantee access. LLM Context URL matching preserves topic
IDs and page numbers, ignoring only known tracking parameters. Legacy cached
wrong-page excerpts and Akamai denial pages are bypassed.

Each step runs only when the previous one did not produce content:

1. **Site API** — only for recognized URLs: X/Twitter (fxtwitter), Discourse topics (`/t/{id}.json`, then `/raw/{id}`), Reddit threads (`.json` on `www.reddit.com`, then `old.reddit.com`), and V2EX topics (`/api/topics/show.json` and `/api/replies/show.json`). Blocked JSON APIs are retried through FlareSolverr when it is configured.
2. **Direct** fetch with curl_cffi.
3. **FlareSolverr** for Cloudflare challenges, SPA shells, and network failures (`flaresolverr_mode`).
4. **LLM Context** — a Brave LLM Context query restricted to the page's host (`site:host` plus the page title or URL slug). Only passages whose URL matches the requested page are used; results are marked `content_scope: "excerpts"`.
5. **Archive** — the newest successful Wayback Machine snapshot, with `snapshot_date`.

Steps 4 and 5 run only when the page was blocked or unreachable (challenge, 401/403/429/451/5xx, network failure, empty or SPA-shell content). A 404 or 410 is reported as-is, never replaced with another page. `[Fetch] extended_fallbacks` controls steps 4 and 5. Every response lists all steps in `attempts`.

## Advanced Configuration (`config.ini`)

You can deeply customize tool behavior by adjusting `config.ini`:

### `[Brave]`
- **`requests_per_second`**: Per-key local limit for Brave Web Search and LLM Context (Default: `1`, hard-capped at the Search plan's `50` RPS). CLI, HTTP API, MCP, batch workers, and retries coordinate through one cross-process limiter on this host, with a separate 1-second window per Brave key. Other devices using the same Brave subscription are outside this limiter.
- **`count`**: Number of results to fetch per request (Default: `10`).
- **`safesearch`**: Content filtering level: `off`, `moderate`, or `strict`.
- **`freshness`**: Filter by time: `pd` (Past 24h), `pw` (Past week), `pm` (Past month), `py` (Past year). Leave blank for no limit.
- **`max_retries`**: Auto-retry count for network timeouts or 429 Too Many Requests.

### `[Perplexity]`
- **`model`**: OpenRouter model string (e.g., `perplexity/sonar`, `perplexity/sonar-pro`).
- **`citations`**: Set to `true` to require markdown citations `[1]` in the synthesized output.
- **`temperature`**: Creativity control (`0.0` - `1.0`). Keep low (e.g., `0.1`) for factual answering.
- **`max_tokens`**: Hard limit on generation length to save costs.
- **`max_retries`**: Auto-retry count for network anomalies.

### `[LLMContext]`
- **`count`**: Number of search results to consider for context extraction, 1-50 (Default: `20`).
- **`maximum_number_of_tokens`**: Approximate max tokens in the context response, 1024-32768 (Default: `8192`). Lower for simple factual queries (~2048), higher for deep research (~16384).
- **`maximum_number_of_urls`**: Maximum URLs in the response, 1-50 (Default: `20`).
- **`context_threshold_mode`**: Relevance filtering: `strict` (fewer, more precise), `balanced` (default), `lenient` (more results), or `disabled` (no filtering).
- **`freshness`**: Same time-based filtering as Brave (`pd`, `pw`, `pm`, `py`).
- **`max_retries`**: Auto-retry count for network anomalies.

### `[Fetch]`
- **`flaresolverr_url`**: FlareSolverr endpoint URL (e.g., `http://localhost:8191/v1`). Leave blank to disable.
- **`flaresolverr_timeout`**: Max time in milliseconds for FlareSolverr to solve a challenge (Default: `60000`).
- **`flaresolverr_mode`**: Strategy: `fallback`, `always`, or `never` (Default: `fallback`).
- **`extended_fallbacks`**: Comma-separated fallbacks after direct/FlareSolverr fail on a blocked page: `llm-context`, `archive`, or `none` (Default: `llm-context, archive`). The LLM Context step uses a Brave key and counts against its rate limit.

### `[Batch]`
- **`max_workers`**: Maximum worker threads for batch execution (Default: `4`).
- Batch results preserve input order even when executed concurrently.
- Use a modest value if you are close to Brave or OpenRouter rate limits.

---

## 🤖 Guide for LLMs & AI Agents

If you are an AI assistant (like Claude, Cursor, or ChatGPT) reading this, you can use this tool to browse the internet to answer user queries!

### Recommended research flow
1. **Search**: batch 2–3 differently worded `brave` queries (`result_limit` 8 is the default); add `freshness: "pm"` for time-sensitive topics.
2. **Read**: batch-fetch 3–5 pages with `focus` and `max_chars`. Blocked sites go through the fallback chain automatically; check `served_from`.
3. **Long documents**: use `llm-context` when you need the whole document's relevant passages.
4. **Verify**: run `perplexity-verify` on 3–5 key conclusions. For any `contradicted` claim, fetch its sources to confirm.

### Features Built for Agents
- **JSON First**: Use `--format json` to get machine-readable structures.
- **Resilience**: The script has built-in timeouts and exponential backoff retry logic. If the network hiccups, `ccsearch` handles it safely, avoiding hangs.
- **Semantic Cache**: Use `--semantic-cache` to skip redundant API calls when you're researching the same topic across multiple queries with slightly different wording. The `_from_cache` and `_semantic_similarity` fields in the JSON response tell you when a cached result was returned and how similar it was.
- **Result Shaping**: Use `--include-host`, `--exclude-host`, and `--limit` on `brave`, `both`, and `llm-context` to keep only the sources and top-N items you actually want.

### How to use `ccsearch`
When the user asks you a question that requires up-to-date knowledge, run the python script directly using your bash/terminal tool.

**Brave Search Example:**
```bash
ccsearch "anthropic claude 3.5 sonnet release date" -e brave --format json
```
*Use this when you need to research specific websites, gather URLs, or need diverse sources.*

*(Agent Tip: results default to the top 8; use `--limit 20` for more, `--offset 1` for the next page, and `--freshness pm` to drop results older than a month.)*

**LLM Context Example:**
```bash
ccsearch "React hooks best practices" -e llm-context --format json
```
*Use this when you need pre-extracted web content optimized for LLM grounding. Returns smart chunks (text, tables, code blocks, structured data) from multiple sources in a single call — far more token-efficient than fetching pages individually. Like every Brave-backed engine, it prefers `BRAVE_SEARCH_API_KEY`, can round-robin extra Search keys, and falls back to `BRAVE_API_KEY` only when no Search key is set.*

**Both Engines Example:**
```bash
ccsearch "what are the architectural differences between Next.js app router and pages router" -e both --format json
```
*Use this when you need a deeply synthesized answer but ALSO need immediate access to primary source URLs to read further context in the same query.*

**Fetch Webpage Example:**
```bash
ccsearch "https://eslint.org/docs/latest/rules/no-unused-vars" -e fetch --format json
```
*Use this when a prior search returned a promising URL, but the snippet wasn't detailed enough and you need to read the full page content. The JSON response includes transport metadata such as `final_url`, `status_code`, `content_type`, and `content_length`, plus HTML metadata like `canonical_url`, `description`, `author`, and `published_at` when the page exposes them.*

**Fetch Binary Document Example:**
```bash
ccsearch "https://example.com/report.pdf" -e fetch --format json
```
*Use this for PDFs or Office files. PDF conversion is included in the standard requirements; supported documents are converted into Markdown and the JSON response includes `"converted_via": "markitdown"`. Conversion failures return a structured `error`.*

**Fetch with FlareSolverr (Cloudflare bypass):**
```bash
ccsearch "https://cloudflare-protected-site.com" -e fetch --format json --flaresolverr
```
*Use this when a normal fetch fails due to Cloudflare protection. Requires FlareSolverr configured in `config.ini`. You rarely need it: the default fallback chain already renders challenge pages and then tries LLM Context and the Wayback Machine. The JSON output includes `served_from` and `attempts`, and preserves the rendered response's final URL, status, and content type. Empty rendered pages return an `error` directing callers to an interactive browser.*

**Semantic Cache Example:**
```bash
ccsearch "Python asyncio event loop tutorial" -e brave --format json --semantic-cache --cache-ttl 60
```
*Use `--semantic-cache` when researching a topic across multiple queries with slightly different wording. Semantically similar queries return the cached result instantly without a new API call. Check `_from_cache` and `_semantic_similarity` in the JSON output to know when a cache hit occurred. Requires `pip install fastembed`.*

### Error Handling
- If the command returns an error about missing `BRAVE_API_KEY`, `BRAVE_SEARCH_API_KEY`, or `OPENROUTER_API_KEY`, immediately inform the user that they need to set the environment variable and provide them the exact `export` command they need to run in their terminal.
- Don't try to guess URLs; use this tool instead!

## Claude Code Skill (HTTP API Mode)

If you deploy ccsearch as a self-hosted HTTP server, you can install it as a **Claude Code skill** so that Claude automatically uses your server for all web searches — no CLI needed on the client machine.

### Setup

1. Copy the skill file into your Claude Code skills directory:
   ```bash
   mkdir -p ~/.claude/skills/ccsearch
   cp skills/SKILL.md ~/.claude/skills/ccsearch/SKILL.md
   ```
2. Edit `~/.claude/skills/ccsearch/SKILL.md` and replace all `YOUR_CCSEARCH_BASE_URL` with your actual server URL (e.g., `https://ccsearch.example.com`).
3. Set the API key:
   ```bash
   export CCSEARCH_API_KEY="your_api_key"
   ```

Once installed, Claude Code will automatically invoke `/ccsearch` whenever it needs to search the web, fetch URLs, or get LLM-optimized context — routing all requests through your server via `curl`.

The skill template is located at [`skills/SKILL.md`](skills/SKILL.md).

## Failure visibility and verification

Inputs use their documented JSON types: booleans must be `true`/`false`, integer options must be integers, and thresholds must be finite numbers in [0, 1]. Fetch URLs require HTTP(S), a hostname, and a valid port. Invalid batch items produce per-item errors without aborting valid requests. CLI commands exit nonzero for failed or partially failed results, including batches; JSON output remains available for inspection.

Failed or partially failed responses are never reused or stored in either cache. Fetch cache normalization preserves meaningful path separators, path parameters, IPv6 hosts, and repeated query-value order. Semantic index updates are process-safe; unavailable candidates do not trigger embedding work. Broken embedding runtimes raise their original errors rather than silently becoming cache misses.

Brave/OpenRouter error payloads and missing answer content are failures, not empty successful answers. `both` retains a successful side and reports the failed side; when both sides fail, the result also includes `error`. Invalid configured modes fail explicitly. FlareSolverr retries expected transport failures only, does not hide programming errors, and rejects unresolved challenges. Explicit `always` mode requires a configured URL. Binary conversion uses one MarkItDown path and rejects empty output.

MCP runs blocking tools in a bounded worker pool, so a slow search does not freeze all client connections. Individual tool failures use MCP error signaling. Batch calls retain their per-item errors and summary. Uvicorn access logging is disabled to avoid recording key-bearing URLs; proxies and clients can still log them. Empty authentication files prevent startup, and concurrent first starts safely share one newly generated key.

Run all unit, regression, and local transport tests with:

```bash
python3 -m py_compile ccsearch.py api_server.py mcp_server.py
python3 -m unittest discover -v
python3 ccsearch.py --doctor --format json
python3 ccsearch.py --list-engines --format json
```

The suite does not clear inherited provider credentials globally. Individual tests use explicit fixtures for key selection and missing-key scenarios; the loopback MCP test server uses a disposable authentication key. Live upstream, public-route, and deployment checks are separate from this suite.
