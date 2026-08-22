---
name: firecrawl
description: Fetch live web content and run web searches via Firecrawl's official hosted MCP server (scrape a URL to markdown, search the web, crawl/map a site). Use whenever a task needs up-to-date information from the internet — the LIFT eval container has no browser and no other network-search tool. Tools are auto-discovered from the server at runtime.
---

# Firecrawl (web scraping & search)

Reach the live internet through Firecrawl's official hosted MCP server, straight
from the IPython kernel. This is the only real "go online" capability in the
LIFT eval container (there is no browser and no bundled `websearch`), so prefer
it over guessing when a task depends on current external facts.

## Setup

This integration uses a **static bearer token**, not OAuth. It is connected
whenever the `FIRECRAWL_API_KEY` environment variable is set (the LIFT image
bakes it from the build-time secret). If a call raises `NotEnabled`, the key is
missing — tell the user to set `FIRECRAWL_API_KEY` in the environment and rebuild
the image. **Do not** point them at `/mcp login`; that flow has no provider for a
bearer-only server and reports "Unknown MCP integration".

## Usage

The tool set is defined by the server, not by this skill, so **discover before
you call** — don't assume tool names or argument names:

```python
import firecrawl

# 1. Discover available tools
for tool in await firecrawl.list_tools():
    print(tool["name"], "-", tool["description"])

# 2. Inspect a specific tool's arguments (rendered from its JSON Schema)
help(firecrawl.firecrawl_scrape)

# 3. Call it; keyword args must match the tool's input schema
page = await firecrawl.firecrawl_scrape(url="https://example.com")
print(page)

hits = await firecrawl.firecrawl_search(query="prime intellect prime-agent release")
print(hits)
```

Notes:
- Every tool is an `async` method — always `await`.
- Results are already-parsed Python (a `dict` for structured output, otherwise a
  string). No need to `json.loads` them.
- For tools whose names aren't valid Python identifiers, use the escape hatch:
  `await firecrawl.call_tool("tool-name", {"arg": "value"})`.
- Run `list_tools()` before relying on `help()` or assuming a tool exists — it
  populates the schemas `help()` shows, and the server is the source of truth for
  tool names and arguments.
- Scrapes can be large; when you only need the main text, pass
  `onlyMainContent=True` (check the tool schema) and summarize rather than
  dumping the whole page back into context.
