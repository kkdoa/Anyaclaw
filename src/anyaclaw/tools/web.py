"""Web search (DuckDuckGo HTML) and page fetching.

No search API key is required. Searches are POSTed to the no-JavaScript
DuckDuckGo endpoint and parsed with regexes; fetching converts HTML to text with
:mod:`html.parser`, caps the payload and refuses to touch private/loopback hosts
so a prompt-injected link cannot turn the agent into an SSRF probe.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from anyaclaw.core.errors import ToolError
from anyaclaw.core.types import ToolResult
from anyaclaw.tools.base import Tool

_SEARCH_URL = "https://html.duckduckgo.com/html/"
_RESULT_BLOCK = re.compile(
    r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*>(?P<title>.*?)</a>(?P<rest>.*?)(?=<a[^>]*class="[^"]*result__a|$)',
    re.S,
)
_HREF = re.compile(r'href="([^"]+)"')
_SNIPPET = re.compile(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|span)>', re.S)
_TAG = re.compile(r"<[^>]+>")


class WebSearchTool(Tool):
    name = "web_search"
    readonly = True
    description = (
        "Search the web (DuckDuckGo) and return titles, URLs and snippets. "
        "Use web_fetch on a result URL to read the full page."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Default 5, max 10"},
        },
        "required": ["query"],
    }

    def __init__(self, *, user_agent: str, timeout: float = 20.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            raise ToolError("query must not be empty")
        limit = max(1, min(int(kwargs.get("max_results") or 5), 10))

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, headers=headers
            ) as client:
                response = await client.post(_SEARCH_URL, data={"q": query, "kl": "wt-wt"})
                response.raise_for_status()
                body = response.text
        except httpx.HTTPStatusError as exc:
            raise ToolError(f"search failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"search failed: {exc}") from exc

        results = _parse_results(body, limit)
        if not results:
            return ToolResult.ok(
                f"no results parsed for {query!r} (the search endpoint may be rate limiting)"
            )
        lines = [f"Results for {query!r}:"]
        for index, (title, url, snippet) in enumerate(results, 1):
            lines.append(f"{index}. {title}\n   {url}\n   {snippet}")
        return ToolResult.ok("\n".join(lines), meta={"count": len(results)})


class WebFetchTool(Tool):
    name = "web_fetch"
    readonly = True
    description = (
        "Fetch an http(s) URL and return readable text (HTML is converted). "
        "Private/loopback addresses are refused."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL"},
            "max_chars": {"type": "integer", "description": "Default from config"},
        },
        "required": ["url"],
    }

    def __init__(self, *, user_agent: str, max_chars: int = 40000, timeout: float = 30.0) -> None:
        self.user_agent = user_agent
        self.max_chars = max_chars
        self.timeout = timeout

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = str(kwargs.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ToolError("only http(s) URLs are supported")
        if not parsed.hostname:
            raise ToolError("URL has no host")
        await _ensure_public_host(parsed.hostname)

        limit = max(1000, min(int(kwargs.get("max_chars") or self.max_chars), 200000))
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,text/*;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                max_redirects=5,
                headers=headers,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                raw = response.content[: 2_000_000]
        except httpx.HTTPStatusError as exc:
            raise ToolError(f"fetch failed: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"fetch failed: {exc}") from exc

        text, title = _render_content(raw, content_type)
        if not text.strip():
            raise ToolError(f"no readable text at {url} ({content_type or 'unknown type'})")

        if len(text) > limit:
            text = text[:limit] + f"\n…[truncated {len(text) - limit} chars]"

        header = f"# {title or parsed.hostname}\nURL: {url}"
        return ToolResult.ok(f"{header}\n\n{text}", meta={"chars": len(text), "title": title})


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _parse_results(body: str, limit: int) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    for match in _RESULT_BLOCK.finditer(body):
        title = _clean(match.group("title"))
        href_match = _HREF.search(body[match.start() : match.end()])
        if not title or not href_match:
            continue
        url = _clean_url(href_match.group(1))
        snippet_match = _SNIPPET.search(match.group("rest"))
        snippet = _clean(snippet_match.group(1)) if snippet_match else ""
        results.append((title, url, snippet))
        if len(results) >= limit:
            break
    return results


def _clean(fragment: str) -> str:
    text = _TAG.sub(" ", fragment)
    text = html.unescape(text)
    return " ".join(text.split())


def _clean_url(raw: str) -> str:
    url = html.unescape(raw)
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return url


class _TextExtractor(HTMLParser):
    _BLOCK = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "pre", "blockquote", "table",
    }
    _SKIP = {"script", "style", "noscript", "svg", "iframe", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        compact: list[str] = []
        for line in lines:
            if line:
                compact.append(line)
            elif compact and compact[-1] != "":
                compact.append("")
        return "\n".join(compact).strip()


def _render_content(raw: bytes, content_type: str) -> tuple[str, str]:
    if "json" in content_type:
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
            return json.dumps(parsed, ensure_ascii=False, indent=2), ""
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace"), ""
    if "html" in content_type or content_type.startswith("text/") or not content_type:
        charset = _charset(content_type)
        try:
            decoded = raw.decode(charset, errors="replace")
        except LookupError:
            decoded = raw.decode("utf-8", errors="replace")
        if "html" in content_type or decoded.lstrip().startswith("<"):
            extractor = _TextExtractor()
            extractor.feed(decoded)
            return extractor.text(), " ".join(extractor.title.split())
        return decoded, ""
    return raw.decode("utf-8", errors="replace"), ""


def _charset(content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type)
    return match.group(1) if match else "utf-8"


async def _ensure_public_host(hostname: str) -> None:
    lowered = hostname.lower()
    if lowered in {"localhost", "localhost.localdomain"} or lowered.endswith(".local"):
        raise ToolError("refusing to fetch a local host")

    def _resolve() -> list[str]:
        infos = socket.getaddrinfo(hostname, None)
        return [info[4][0] for info in infos]

    try:
        addresses = await asyncio.to_thread(_resolve)
    except OSError:
        return  # Let httpx produce the DNS error with a clearer message.

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ToolError(f"refusing to fetch a private address: {hostname} -> {address}")
