# Reddit Post: Introducing ccsearch

---

**Title:** I built a CLI web search tool for Claude Code (and any other LLM/agent) that uses non-Anthropic models — give your AI real-time internet access in minutes

---

**Body:**

Hey everyone,

If you use **Claude Code** (or any other CLI-based AI coding agent) you've probably run into the frustrating situation where the model is confidently wrong about something because it's a few months out of date. I built a small open-source tool to fix that: **ccsearch**.

**GitHub:** https://github.com/jamie950315/ccsearch

---

### What it does

`ccsearch` is a lightweight Python CLI that gives your AI agent (or you, as a human) real-time web search in one command. It supports four modes:

| Engine | Best for |
|---|---|
| **Brave** | Fast, diverse links & snippets via the Brave Data for Search API |
| **Perplexity** (via OpenRouter) | AI-synthesized answers with citations |
| **Both** | Runs Brave + Perplexity *concurrently* and merges the results |
| **Fetch** | Scrapes and returns clean text from any URL you already have |

The key design goal: **machine-friendly JSON output** so LLMs can parse it, alongside **human-readable text output** for when you want to use it yourself.

---

### Why I built it specifically for Claude Code with non-Anthropic models

Claude Code's built-in web search only works with Anthropic's own hosted models. If you're routing through OpenRouter to use models like DeepSeek, Qwen, Mistral, Llama, or any other non-Anthropic provider, you simply don't get web search at all. `ccsearch` fills that gap — it's a standalone CLI that any model can call via a bash/terminal tool, regardless of the provider.

It also works great with the standard Claude (Sonnet/Haiku) if you just want more control over *how* web search works.

---

### Quick setup (takes ~2 minutes)

```bash
git clone https://github.com/jamie950315/ccsearch.git
cd ccsearch
pip install -r requirements.txt
cp config.ini.example config.ini

# Add to PATH so Claude Code can call it from anywhere
mkdir -p ~/.local/bin
ln -sf $(pwd)/ccsearch.py ~/.local/bin/ccsearch

# Set your API keys (only need the ones for engines you use)
export BRAVE_API_KEY="your_brave_api_key"
export OPENROUTER_API_KEY="your_openrouter_api_key"
```

API keys needed:
- **Brave Search API** — has a free tier (2,000 queries/month)
- **OpenRouter** — pay-as-you-go; Perplexity Sonar is very cheap

---

### Tell Claude Code to use it

Drop this into your project's `CLAUDE.md` (or home directory `~/.claude/CLAUDE.md`):

```markdown
**Web Search Instructions:**
- DO NOT use any default Web Search Tool provided by your environment.
- INSTEAD, use the `ccsearch` CLI for all web searches.
- Use `--format json` for machine-readable output.

Examples:
  ccsearch "Next.js 15 breaking changes" -e brave --format json
  ccsearch "What changed in Python 3.13?" -e perplexity --format json --cache
  ccsearch "https://docs.example.com/api" -e fetch --format json
```

Once that's in place, Claude will automatically reach for `ccsearch` every time it needs current information — no hallucinations about library versions, no confidently wrong answers.

---

### A few features I'm particularly happy with

- **Concurrent mode** (`-e both`): fires off Brave and Perplexity at the same time and gives you a synthesized answer *and* a list of raw URLs to dig deeper into — in a single command.
- **Filesystem cache** (`--cache --cache-ttl 60`): avoids burning API credits on repeated identical queries during an AI session.
- **Exponential backoff retry logic**: handles flaky networks and 429 rate-limit responses automatically so the AI doesn't crash mid-task.
- **Pagination** (`--offset`): if the first 10 Brave results weren't enough, the agent can call again with `--offset 1` to get the next page.
- **Fetch engine**: grabs the full text of any URL, strips all HTML/JS/CSS noise, and returns clean readable content — perfect for when a search snippet isn't detailed enough.

---

### Works with any agent, not just Claude Code

Because it's just a CLI that prints JSON to stdout, it works with anything that can run shell commands:

- **Cursor** — add it to your rules/context
- **Aider**
- **ChatGPT desktop app** (with shell access)
- Any custom agent built with LangChain, CrewAI, AutoGen, etc.
- Just plain humans in a terminal

---

Would love feedback, bug reports, or feature requests! Happy to answer questions in the comments.

*P.S. — If you're already using Claude Code with OpenRouter/custom providers and have figured out your own search solution, I'd be curious what you're using!*
