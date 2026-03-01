# ccsearch

A CLI Web Search utility designed to be easily used by Large Language Models (LLMs) like Claude Code, as well as human users. It supports structured outputs (`JSON`) for agents and readable outputs (`text`) for humans.

## Supported Engines
1. **Brave Search** (via Brave Data for Search API): Best for getting a list of fast, accurate links and snippets. Supports pagination (`--offset`), safsearch, and time-based filtering.
2. **Perplexity** (via OpenRouter): Best for getting an intelligent, synthesized answer using online sources. Supports model selection, customizable temperature, and citation formatting.
3. **Both** (Concurrency): Runs both Brave and Perplexity searches in parallel, returning a merged outcome (a synthesized answer alongside raw source links).

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
4. Set your Environment Variables:
   - For Brave: `export BRAVE_API_KEY="your_brave_api_key"`
   - For Perplexity: `export OPENROUTER_API_KEY="your_openrouter_api_key"`

## Usage for Humans

```bash
# Brave Search (Text Output)
python ccsearch.py "latest React documentation" -e brave --format text

# Brave Search (2nd page of results using offset)
python ccsearch.py "latest React documentation" -e brave --format text --offset 1

# Perplexity Synthesis (Text Output)
python ccsearch.py "What is the difference between Vue 3 and React 18?" -e perplexity --format text

# Both Engines Concurrently (Merged Text Output)
python ccsearch.py "What is the new React compiler?" -e both --format text
```

## Advanced Usage

### Caching Results
To save API credits and retrieve results instantly for repeated queries, use the built-in filesystem cache:
```bash
# Cache the result for the default 10 minutes
python ccsearch.py "React 19 release date" -e perplexity --cache

# Cache the result for a custom duration (e.g., 60 minutes)
python ccsearch.py "React 19 release date" -e perplexity --cache --cache-ttl 60
```
*Note: The cache uses a hashed key based on the query, engine, and offset. Cache files are stored in `~/.cache/ccsearch/`.*

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

---

## 🤖 Guide for LLMs & AI Agents

If you are an AI assistant (like Claude, Cursor, or ChatGPT) reading this, you can use this tool to browse the internet to answer user queries!

### Features Built for Agents
- **JSON First**: Use `--format json` to get machine-readable structures.
- **Resilience**: The script has built-in timeouts and exponential backoff retry logic. If the network hiccups, `ccsearch` handles it safely, avoiding hangs.

### How to use `ccsearch`
When the user asks you a question that requires up-to-date knowledge, run the python script directly using your bash/terminal tool.

**Brave Search Example:**
```bash
python ccsearch.py "anthropic claude 3.5 sonnet release date" -e brave --format json
```
*Use this when you need to research specific websites, gather URLs, or need diverse sources.*

*(Agent Tip: If you didn't find what you need in the first 10 results, you can fetch the next page by adding `--offset 1`)*

**Both Engines Example:**
```bash
python ccsearch.py "what are the architectural differences between Next.js app router and pages router" -e both --format json
```
*Use this when you need a deeply synthesized answer but ALSO need immediate access to primary source URLs to read further context in the same query.*

### Error Handling
- If the command returns an error about missing `BRAVE_API_KEY` or `OPENROUTER_API_KEY`, immediately inform the user that they need to set the environment variable and provide them the exact `export` command they need to run in their terminal.
- Don't try to guess URLs; use this tool instead!

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
- **Why?** It utilizes Brave Data for Search API and OpenRouter Perplexity, providing faster, more robust results with automatic error-handling and retries.
- **How to Use Examples (always use `--format json` for agents):**
  1. For finding specific links, documentation, or diverse web sources:
     `ccsearch "Next.js 14 hydration docs" -e brave --format json`
  2. For broad questions requiring a synthesized answer from the web (Use `--cache` to save time on repeated inquiries):
     `ccsearch "What are the latest breaking changes in React 19?" -e perplexity --format json --cache`
  3. For complex research requiring BOTH an intelligent summary and raw URLs to read further:
     `ccsearch "Next.js app router architecture" -e both --format json --cache`
  4. If you didn't find what you need via Brave, you can fetch the next page of results:
     `ccsearch "Next.js 14 hydration docs" -e brave --format json --offset 1`
- For the full tutorial and advanced parameters (like how to configure limits or handle missing APIs), please read the README located at `~/ccsearch/README.md` FIRST before making assumptions.
```
