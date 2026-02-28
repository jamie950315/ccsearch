# ccsearch

A CLI Web Search utility designed to be easily used by Large Language Models (LLMs) like Claude Code, as well as human users. It supports structured outputs (`JSON`) for agents and readable outputs (`text`) for humans.

## Supported Engines
1. **Brave Search** (via Brave Data for Search API): Best for getting a list of fast, accurate links and snippets. Supports pagination (`--offset`), safsearch, and time-based filtering.
2. **Perplexity** (via OpenRouter): Best for getting an intelligent, synthesized answer using online sources. Supports model selection, customizable temperature, and citation formatting.

## Requirements & Setup

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy the example configuration:
   ```bash
   cp config.ini.example config.ini
   ```
   *Modify `config.ini` to adjust rate limits, models, filtering, or retry logic.*
3. Set your Environment Variables:
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
```

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

**Perplexity Example:**
```bash
python ccsearch.py "summary of the latest AI news this week" -e perplexity --format json
```
*Use this when you want a synthesized summary or direct answer instead of raw links.*

### Error Handling
- If the command returns an error about missing `BRAVE_API_KEY` or `OPENROUTER_API_KEY`, immediately inform the user that they need to set the environment variable and provide them the exact `export` command they need to run in their terminal.
- Don't try to guess URLs; use this tool instead!
