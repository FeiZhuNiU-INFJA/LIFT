"""GA firecrawl plugin — 通过 monkey-patch GenericAgentHandler 暴露
``firecrawl_search`` / ``firecrawl_scrape`` 两个工具，供 LLM tool calling 使用。

LIFT 评测容器内没有浏览器，GA 自带 ``web_scan`` / ``web_execute_js`` 不可用；本插件
通过 Firecrawl Cloud SaaS（``api.firecrawl.dev/v1/*``）补齐「真上网」能力。

工具实现遵循 GA 既有约定：
  * generator 函数：先 ``yield`` 一行进度日志，再 ``return StepOutcome(...)``；
  * 失败返回 ``{'status':'error','msg':...}`` 而非抛异常，与 ``ga.py`` 中
    ``file_read`` / ``file_patch`` 等工具保持一致；
  * ``next_prompt`` 用 ``self._get_anchor_prompt(skip=args.get('_index', 0) > 0)``
    与 ``do_code_run`` / ``do_web_execute_js`` 一致。

API key 来源优先级：``mykey.firecrawl_config['apikey']`` → ``$FIRECRAWL_API_KEY``。
两者皆空时插件仍能 import，调用工具时返回 error，由 LLM 决定 fallback 策略。

注册由 ``plugins/hooks.py:discover_and_load`` 完成（``agentmain.py`` 启动时调用）。
插件 import 时直接 ``GenericAgentHandler.do_firecrawl_xxx = ...``，无需注册 hook。
"""
import os

import requests

from agent_loop import StepOutcome
from ga import GenericAgentHandler

try:
    from mykey import firecrawl_config as _CFG
except Exception:
    _CFG = {}

_API_KEY = (_CFG.get("apikey") if isinstance(_CFG, dict) else "") or os.environ.get(
    "FIRECRAWL_API_KEY", ""
)
_API_BASE = (
    (_CFG.get("apibase") if isinstance(_CFG, dict) else None)
    or "https://api.firecrawl.dev/v1"
).rstrip("/")
_TIMEOUT = 60.0


def _request(path: str, payload: dict) -> dict:
    if not _API_KEY:
        return {
            "status": "error",
            "msg": "FIRECRAWL_API_KEY 未配置；无法联网。请告知用户「无法联网获取」。",
        }
    try:
        r = requests.post(
            f"{_API_BASE}/{path}",
            headers={
                "Authorization": f"Bearer {_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        return {"status": "error", "msg": f"network error: {e}"}
    if r.status_code != 200:
        # 截断 body 避免污染上下文；保留状态码方便 LLM 判断
        return {
            "status": "error",
            "msg": f"HTTP {r.status_code}: {r.text[:300]}",
        }
    try:
        return r.json()
    except ValueError:
        return {"status": "error", "msg": f"non-JSON response: {r.text[:300]}"}


def do_firecrawl_search(self, args, response):
    """Firecrawl 联网搜索；返回搜索结果 list（含标题/URL/摘要）。"""
    query = (args.get("query") or "").strip()
    if not query:
        return StepOutcome(
            "[Error] firecrawl_search 缺 query 参数",
            next_prompt="\n",
        )
    try:
        limit = int(args.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    lang = args.get("lang", "zh") or "zh"
    yield f"[Action] firecrawl search: {query[:80]}\n"
    payload: dict = {"query": query, "limit": limit, "lang": lang}
    result = _request("search", payload)
    next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
    return StepOutcome(result, next_prompt=next_prompt)


def do_firecrawl_scrape(self, args, response):
    """Firecrawl 单页抓取；以 markdown 返回正文（默认 onlyMainContent）。"""
    url = (args.get("url") or "").strip()
    if not url:
        return StepOutcome(
            "[Error] firecrawl_scrape 缺 url 参数",
            next_prompt="\n",
        )
    payload = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": bool(args.get("only_main_content", True)),
    }
    yield f"[Action] firecrawl scrape: {url[:120]}\n"
    result = _request("scrape", payload)
    if isinstance(result, dict) and result.get("success") and isinstance(
        result.get("data"), dict
    ):
        md = result["data"].get("markdown") or ""
        # 单页 markdown 可能上 100K，截断到 ~10K 防止打爆 LLM 上下文。
        # 与 ``ga.py:file_read`` 中 ``L_MAX = ... 10000`` 上限相近。
        try:
            tool_num = int(args.get("_tool_num", 1)) or 1
        except (TypeError, ValueError):
            tool_num = 1
        maxlen = max(2000, 10000 // tool_num)
        if len(md) > maxlen:
            result["data"]["markdown"] = (
                md[:maxlen] + f"\n\n[TRUNCATED at {maxlen} chars]"
            )
    next_prompt = self._get_anchor_prompt(skip=args.get("_index", 0) > 0)
    return StepOutcome(result, next_prompt=next_prompt)


GenericAgentHandler.do_firecrawl_search = do_firecrawl_search
GenericAgentHandler.do_firecrawl_scrape = do_firecrawl_scrape
