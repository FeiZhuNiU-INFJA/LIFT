"""Firecrawl integration: tools auto-discovered from Firecrawl's official hosted
MCP server.

Usage in the kernel:

    import firecrawl
    page = await firecrawl.firecrawl_scrape(url="https://example.com")
    hits = await firecrawl.firecrawl_search(query="prime-agent release")

Auth is a static bearer token: the ``bearer_token_env`` value below matches the
``bearerTokenEnvVar`` declared for the ``firecrawl`` server in settings.json, so
``McpIntegration`` sends ``Authorization: Bearer $FIRECRAWL_API_KEY`` to the
endpoint. With no key set, calls raise ``NotEnabled`` (see SKILL.md).
"""

from __future__ import annotations

from rlm import McpIntegration

__all__ = ["Firecrawl", "firecrawl"]


class Firecrawl(McpIntegration):
    server = "firecrawl"  # matches the mcpServers key / auth.json `mcp:firecrawl`
    url = "https://mcp.firecrawl.dev/v2/mcp"
    bearer_token_env = "FIRECRAWL_API_KEY"


firecrawl = Firecrawl()


# Names the kernel bootstrap probes to decide if a module is a callable skill.
# Don't forward them, or `getattr(module, "run")` returns an MCP tool stub and the
# module gets wrapped as callable, breaking `await firecrawl.<tool>()` dispatch.
_RESERVED = {"run", "__wrapped__", "__call__"}


def __getattr__(name: str):
    # Forward bare module-level access (e.g. firecrawl.firecrawl_scrape) to the
    # instance, so `import firecrawl; await firecrawl.firecrawl_scrape(...)` works
    # without `.firecrawl`.
    if name.startswith("_") or name in _RESERVED:
        raise AttributeError(name)
    return getattr(firecrawl, name)
