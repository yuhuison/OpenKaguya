"""
Web 搜索工具 — 支持 Exa / Tavily 双后端。

工具名对 AI 统一为 web_search / web_read，
后端由配置的 API Key 自动切换。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from loguru import logger

from kaguya.tools.registry import Tool


# ===================== 后端抽象 =====================

class SearchBackend(ABC):
    """搜索后端接口"""

    @abstractmethod
    def search(self, query: str, num_results: int = 5, **kwargs) -> list[dict]:
        """
        搜索，返回 [{"title": ..., "url": ..., "content": ...}, ...]
        """
        ...

    @abstractmethod
    def read_url(self, url: str, max_characters: int = 5000) -> dict | None:
        """
        读取网页内容，返回 {"title": ..., "url": ..., "text": ...} 或 None
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...


class ExaBackend(SearchBackend):
    """Exa AI 搜索后端"""

    def __init__(self, api_key: str):
        from exa_py import Exa
        self._exa = Exa(api_key=api_key)

    @property
    def provider_name(self) -> str:
        return "Exa"

    def search(self, query: str, num_results: int = 5, **kwargs) -> list[dict]:
        response = self._exa.search(
            query,
            num_results=num_results,
            type="auto",
            contents={"text": {"max_characters": 1500}},
        )
        return [
            {
                "title": r.title or "(无标题)",
                "url": r.url or "",
                "content": (r.text or "")[:800].strip(),
            }
            for r in response.results
        ]

    def read_url(self, url: str, max_characters: int = 5000) -> dict | None:
        response = self._exa.get_contents(
            [url],
            text={"max_characters": max_characters},
        )
        if not response.results:
            return None
        r = response.results[0]
        return {
            "title": r.title or "(无标题)",
            "url": url,
            "text": (r.text or "").strip(),
        }


class TavilyBackend(SearchBackend):
    """Tavily 搜索后端"""

    def __init__(self, api_key: str):
        from tavily import TavilyClient
        self._client = TavilyClient(api_key=api_key)

    @property
    def provider_name(self) -> str:
        return "Tavily"

    def search(self, query: str, num_results: int = 5, **kwargs) -> list[dict]:
        response = self._client.search(
            query=query,
            max_results=num_results,
            include_answer=True,
            search_depth="basic",
        )
        results = []

        # Tavily 可能返回一个简洁回答
        answer = response.get("answer")
        if answer:
            results.append({
                "title": "📝 AI 摘要",
                "url": "",
                "content": answer,
            })

        for r in response.get("results", []):
            results.append({
                "title": r.get("title", "(无标题)"),
                "url": r.get("url", ""),
                "content": (r.get("content") or "")[:800].strip(),
            })
        return results

    def read_url(self, url: str, max_characters: int = 5000) -> dict | None:
        response = self._client.extract(urls=[url])
        results = response.get("results", [])
        if not results:
            return None
        r = results[0]
        return {
            "title": "(网页内容)",
            "url": r.get("url", url),
            "text": (r.get("raw_content") or "")[:max_characters].strip(),
        }


# ===================== 工具实现 =====================

class WebSearchTool(Tool):
    """使用搜索引擎搜索互联网，返回结构化的搜索结果"""

    def __init__(self, backend: SearchBackend):
        self._backend = backend

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "在互联网上搜索信息，返回搜索结果（标题、链接、内容摘要）。"
            "适合查资料、看新闻、了解新事物。速度很快，优先使用这个而不是浏览器。"
            "示例：web_search(query=\"今天微博热搜\", num_results=5)"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或自然语言问题",
                },
                "num_results": {
                    "type": "integer",
                    "description": "返回结果数量（默认 5，最大 10）",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, num_results: int = 5, **_) -> str:
        num_results = min(max(num_results, 1), 10)
        try:
            results = await asyncio.to_thread(
                self._backend.search, query, num_results
            )
            if not results:
                return f"没有找到关于「{query}」的搜索结果。"

            lines = [f"搜索「{query}」的结果（{self._backend.provider_name}，共 {len(results)} 条）：\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"[{i}] {r['title']}")
                if r["url"]:
                    lines.append(f"    链接: {r['url']}")
                if r["content"]:
                    lines.append(f"    摘要: {r['content']}")
                lines.append("")
            return "\n".join(lines)

        except Exception as e:
            logger.error(f"网络搜索失败 ({self._backend.provider_name}): {e}")
            return f"搜索失败: {e}"


class WebReadTool(Tool):
    """读取指定网页的文本内容"""

    def __init__(self, backend: SearchBackend):
        self._backend = backend

    @property
    def name(self) -> str:
        return "web_read"

    @property
    def description(self) -> str:
        return (
            "读取指定网页的文本内容（自动去除广告和杂乱元素，提取干净正文）。"
            "适合阅读搜索结果中的某个链接、查看文章详情。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要读取的网页 URL",
                },
                "max_characters": {
                    "type": "integer",
                    "description": "最大返回字符数（默认 5000）",
                },
            },
            "required": ["url"],
        }

    async def execute(self, url: str, max_characters: int = 5000, **_) -> str:
        max_characters = min(max(max_characters, 500), 10000)
        try:
            result = await asyncio.to_thread(
                self._backend.read_url, url, max_characters
            )
            if not result:
                return f"无法读取 {url} 的内容。"
            return f"📄 {result['title']}\n链接: {result['url']}\n\n{result['text']}"

        except Exception as e:
            logger.error(f"网页读取失败 ({self._backend.provider_name}): {e}")
            return f"读取网页失败: {e}"


# ===================== 工厂函数 =====================

def create_web_search_tools(
    exa_api_key: str = "",
    tavily_api_key: str = "",
) -> list[Tool]:
    """
    根据提供的 API Key 创建搜索工具。
    优先使用 Exa（更快、结果更结构化），如果没有则用 Tavily。
    """
    backend: SearchBackend | None = None

    if exa_api_key:
        backend = ExaBackend(exa_api_key)
    elif tavily_api_key:
        backend = TavilyBackend(tavily_api_key)

    if backend is None:
        return []

    logger.info(f"网络搜索后端: {backend.provider_name}")
    return [
        WebSearchTool(backend),
        WebReadTool(backend),
    ]
