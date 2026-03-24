# ccsearch

A CLI Web Search utility designed to be easily used by Large Language Models (LLMs) like Claude Code, as well as human users. It supports structured outputs (`JSON`) for agents and readable outputs (`text`) for humans.

## Supported Engines
1. **Brave Search** (via Brave Search API): Best for getting a list of fast, accurate links and snippets. Supports pagination (`--offset`), safesearch, and time-based filtering.
2. **Perplexity** (via OpenRouter): Best for getting an intelligent, synthesized answer using online sources. Supports model selection, customizable temperature, and citation formatting.
3. **LLM Context** (via Brave LLM Context API): Returns pre-extracted, relevance-scored web content (smart chunks) optimized for LLM consumption. Extracts text, tables, code blocks, and structured data from multiple sources in a single API call — no scraping needed. Ideal for RAG pipelines and AI agent grounding.
4. **Both** (Concurrency): Runs both Brave and Perplexity searches in parallel, returning a merged outcome (a synthesized answer alongside raw source links).
5. **Fetch**: A built-in web scraper that downloads a given URL, parses it, and returns the cleaned text without HTML tags. Perfect for reading full articles when a snippet isn't enough. Uses **curl_cffi** for Chrome TLS fingerprint impersonation to access strict anti-bot sites (Facebook, LinkedIn, Medium, etc.), with full Chrome 146 headers and a Google Referer. Includes automatic **FlareSolverr** fallback for Cloudflare-protected and JS-rendered pages.

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
   - For Brave Web Search: `export BRAVE_API_KEY="your_brave_api_key"`
   - For LLM Context: `export BRAVE_SEARCH_API_KEY="your_brave_search_plan_key"` *(falls back to `BRAVE_API_KEY` if not set; note that the LLM Context API requires a key from Brave's Search plan, which is separate from the Pro plan)*
   - For Perplexity: `export OPENROUTER_API_KEY="your_openrouter_api_key"`

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

# Force FlareSolverr for a Cloudflare-protected page
ccsearch "https://some-cloudflare-site.com" -e fetch --format text --flaresolverr
```

## Advanced Usage

### Caching Results

#### Exact Cache (`--cache`)
Caches results by an exact hash of the query string. Subsequent identical queries return instantly without hitting the API.
```bash
# Cache the result for the default 10 minutes
ccsearch "React 19 release date" -e perplexity --cache

# Cache the result for a custom duration (e.g., 60 minutes)
ccsearch "React 19 release date" -e perplexity --cache --cache-ttl 60
```
*Cache files are stored in `~/.cache/ccsearch/` as JSON files keyed by MD5 hash of `(query, engine, offset)`.*

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
- Applies to `brave`, `perplexity`, `both`, and `llm-context` engines. The `fetch` engine always uses exact URL matching.
- If `fastembed` is not installed, a warning is printed and the tool continues without semantic matching.
- The same `--cache-ttl` applies to both caches.

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

# Or via systemd (production)
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
curl https://ccsearch.0ruka.dev/health
# {"status": "ok", "service": "ccsearch-api"}
```

#### `POST /search`
Main search endpoint. Accepts a JSON body with the following fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Search query or URL (for fetch engine) |
| `engine` | string | Yes | `brave`, `perplexity`, `both`, `fetch`, or `llm-context` |
| `cache` | bool | No | Enable result caching (default: `false`) |
| `cache_ttl` | int | No | Cache TTL in minutes (default: `10`) |
| `semantic_cache` | bool | No | Enable semantic similarity cache (default: `false`) |
| `semantic_threshold` | float | No | Cosine similarity threshold (default: `0.9`) |
| `offset` | int | No | Pagination offset (Brave only) |
| `flaresolverr` | bool | No | Force FlareSolverr for fetch engine (default: `false`) |

```bash
# Brave search
curl -X POST https://ccsearch.0ruka.dev/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "React 19 new features", "engine": "brave"}'

# Perplexity synthesized answer
curl -X POST https://ccsearch.0ruka.dev/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "What is the difference between Vue 3 and React 18?", "engine": "perplexity"}'

# Fetch a URL
curl -X POST https://ccsearch.0ruka.dev/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "https://react.dev/blog", "engine": "fetch"}'

# With caching
curl -X POST https://ccsearch.0ruka.dev/search \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_API_KEY" \
  -d '{"query": "Python asyncio tutorial", "engine": "brave", "cache": true, "cache_ttl": 60}'
```

#### `GET /engines`
List available search engines and their server-side requirements.
```bash
curl https://ccsearch.0ruka.dev/engines \
  -H "X-API-Key: YOUR_API_KEY"
```

### Deployment

The API server runs as a systemd service (`ccsearch-api.service`) with automatic restart on failure. Environment variables (API keys, port) are loaded from `.env`.

```bash
sudo systemctl enable ccsearch-api   # Enable on boot
sudo systemctl start ccsearch-api    # Start
sudo systemctl status ccsearch-api   # Check status
journalctl -u ccsearch-api -f        # View logs
```

The service is exposed publicly via Cloudflare Tunnel at `ccsearch.0ruka.dev`.

---

## FlareSolverr Integration (Optional)

The `fetch` engine uses a multi-layered approach to access protected websites:

1. **curl_cffi** (recommended): Impersonates Chrome's TLS fingerprint (JA3/JA4), which bypasses most anti-bot detection (Facebook, LinkedIn, Medium, Instagram, etc.). Install with `pip install curl_cffi`. Falls back to `requests` if not installed.
2. **FlareSolverr**: For Cloudflare challenge pages and JS-rendered SPAs that require a real browser. [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) is a self-hosted proxy that uses a real Chromium browser to solve browser challenges.

### Setup
1. Run FlareSolverr via Docker:
   ```bash
   docker run -d --name flaresolverr -p 8191:8191 ghcr.io/flaresolverr/flaresolverr:latest
   ```
2. Add the URL to your `config.ini`:
   ```ini
   [Fetch]
   flaresolverr_url = http://localhost:8191/v1
   flaresolverr_mode = fallback
   ```

### Modes
- **`fallback`** (default): Tries a normal HTTP request first. If it fails or detects a Cloudflare challenge, automatically retries through FlareSolverr.
- **`always`**: Skips the normal request and always uses FlareSolverr. Useful for sites that are known to be protected.
- **`never`**: Never uses FlareSolverr, even if configured.

You can also force FlareSolverr for a single invocation with the `--flaresolverr` CLI flag:
```bash
ccsearch "https://cloudflare-site.com" -e fetch --format json --flaresolverr
```

### Detection
The tool automatically detects Cloudflare challenges by checking for:
- `"Just a moment..."` in the page title
- `"Checking your browser"`, `"cf-browser-verification"`, or `"challenge-platform"` in the response body
- Suspiciously short responses (< 1KB) with a `cf-ray` header

## Advanced Configuration (`config.ini`)

You can deeply customize tool behavior by adjusting `config.ini`:

### `[Brave]`
- **`requests_per_second`**: Rate limiting to prevent ban (Default: `1`).
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

---

## 🤖 Guide for LLMs & AI Agents

If you are an AI assistant (like Claude, Cursor, or ChatGPT) reading this, you can use this tool to browse the internet to answer user queries!

### Features Built for Agents
- **JSON First**: Use `--format json` to get machine-readable structures.
- **Resilience**: The script has built-in timeouts and exponential backoff retry logic. If the network hiccups, `ccsearch` handles it safely, avoiding hangs.
- **Semantic Cache**: Use `--semantic-cache` to skip redundant API calls when you're researching the same topic across multiple queries with slightly different wording. The `_from_cache` and `_semantic_similarity` fields in the JSON response tell you when a cached result was returned and how similar it was.

### How to use `ccsearch`
When the user asks you a question that requires up-to-date knowledge, run the python script directly using your bash/terminal tool.

**Brave Search Example:**
```bash
ccsearch "anthropic claude 3.5 sonnet release date" -e brave --format json
```
*Use this when you need to research specific websites, gather URLs, or need diverse sources.*

*(Agent Tip: If you didn't find what you need in the first 10 results, you can fetch the next page by adding `--offset 1`)*

**LLM Context Example:**
```bash
ccsearch "React hooks best practices" -e llm-context --format json
```
*Use this when you need pre-extracted web content optimized for LLM grounding. Returns smart chunks (text, tables, code blocks, structured data) from multiple sources in a single call — far more token-efficient than fetching pages individually. Requires `BRAVE_SEARCH_API_KEY` (or falls back to `BRAVE_API_KEY`).*

**Both Engines Example:**
```bash
ccsearch "what are the architectural differences between Next.js app router and pages router" -e both --format json
```
*Use this when you need a deeply synthesized answer but ALSO need immediate access to primary source URLs to read further context in the same query.*

**Fetch Webpage Example:**
```bash
ccsearch "https://eslint.org/docs/latest/rules/no-unused-vars" -e fetch --format json
```
*Use this when a prior search returned a promising URL, but the snippet wasn't detailed enough and you need to read the full page content.*

**Fetch with FlareSolverr (Cloudflare bypass):**
```bash
ccsearch "https://cloudflare-protected-site.com" -e fetch --format json --flaresolverr
```
*Use this when a normal fetch fails due to Cloudflare protection. Requires FlareSolverr configured in `config.ini`. The JSON output includes a `"fetched_via"` field (`"direct"` or `"flaresolverr"`) so you know which method was used. In `fallback` mode (default), Cloudflare is auto-detected and FlareSolverr is used automatically — no flag needed.*

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

## Instructing Your AI to Use `ccsearch` (CLAUDE.md)
To ensure your AI agents (like Claude Code) automatically use this tool instead of hallucinating or using default, less-capable search tools, we highly recommend adding the following guidelines to your project's or home directory's `CLAUDE.md` (or equivalent system prompt instructions file).

Copy and paste the snippet below into your `CLAUDE.md`:

```markdown
### AI Assistant Guidelines & Tools

**Web Search Instructions:**
- **DO NOT** use any default `Web Search Tool` provided by your environment.
- **INSTEAD**, you MUST use the custom `ccsearch` utility CLI tool located at `~/ccsearch/ccsearch.py` (which is also globally accessible as `ccsearch`).
- **PROACTIVE SEARCHING**: You must proactively search the web using `ccsearch` whenever the user asks about something you are unsure of, or whenever the topic involves relatively new technologies, news, or frameworks (released within the last year). Do not guess or hallucinate!
- **ITERATIVE RESEARCH**: You are encouraged to perform multiple rounds of searches with different keywords or different engines (Brave vs Perplexity) to compile the most accurate and reliable answer.
- **Why?** It utilizes Brave Search API (Web Search + LLM Context endpoints) and OpenRouter Perplexity, providing faster, more robust results with automatic error-handling and retries.
- **How to Use Examples (always use `--format json` for agents):**
  1. For finding specific links, documentation, or diverse web sources:
     `ccsearch "Next.js 14 hydration docs" -e brave --format json`
  2. For broad questions requiring a synthesized answer from the web (Use `--cache` to save time on repeated inquiries):
     `ccsearch "What are the latest breaking changes in React 19?" -e perplexity --format json --cache`
  3. For pre-extracted web content optimized for LLM grounding (smart chunks with structured data, code blocks, tables — no scraping needed):
     `ccsearch "React hooks best practices" -e llm-context --format json --cache`
     *(Preferred for RAG/grounding — returns query-relevant content from multiple sources in a single call, far more token-efficient than fetch.)*
  4. For complex research requiring BOTH an intelligent summary and raw URLs to read further:
     `ccsearch "Next.js app router architecture" -e both --format json --cache`
  5. **Use `--semantic-cache` when researching a topic across multiple related queries** to avoid redundant API calls — semantically similar queries reuse cached results:
     `ccsearch "React Server Components explained" -e perplexity --format json --semantic-cache --cache-ttl 60`
     *(Requires `pip install fastembed`. Check `_from_cache` and `_semantic_similarity` in the JSON output to know if a cached result was returned.)*
  6. If you didn't find what you need via Brave, you can fetch the next page of results:
     `ccsearch "Next.js 14 hydration docs" -e brave --format json --offset 1`
  7. **To read the FULL text of a specific URL (like a documentation page or article) when the search snippet isn't enough:**
     `ccsearch "https://react.dev/reference/react" -e fetch --format json`
  8. **If a fetch fails due to Cloudflare protection or JS-rendered content**, force FlareSolverr:
     `ccsearch "https://cloudflare-protected-site.com" -e fetch --format json --flaresolverr`
     *(Requires `flaresolverr_url` in `config.ini`. In `fallback` mode, Cloudflare is auto-detected — no flag needed. Check the `"fetched_via"` field in the JSON output to see which method was used.)*
- For the full tutorial and advanced parameters (like how to configure limits or handle missing APIs), please read the README located at `~/ccsearch/README.md` FIRST before making assumptions.
```
