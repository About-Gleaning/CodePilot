from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from lxml import html as lxml_html

from codepilot.tools.base import BaseTool, ToolExecutionContext, ToolSpec
from codepilot.tools.file_tool_common import FileToolError, build_tool_failure, build_tool_success, load_tool_description


MAX_DOWNLOAD_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 50_000
MAX_REDIRECTS = 5
USER_AGENT = "CodePilotWebFetch/0.1"


@dataclass(frozen=True, slots=True)
class FetchedPage:
    final_url: str
    html: str


class WebFetchTool(BaseTool):
    def __init__(self, timeout_seconds: int) -> None:
        self.spec = ToolSpec(
            name="webfetch",
            description=load_tool_description("webfetch"),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要获取核心正文内容的 http 或 https URL。"},
                },
                "required": ["url"],
            },
            can_parallel=True,
            requires_approval=False,
            timeout_seconds=timeout_seconds,
            side_effect="read_only",
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> dict[str, Any]:
        try:
            url = str(args.get("url", "")).strip()
            await self._validate_url(url)
            fetched = await self._fetch_html(url)
            markdown = await asyncio.to_thread(self._extract_markdown, fetched.html, fetched.final_url)
            if not markdown:
                raise FileToolError("页面没有可抽取的核心正文内容。", error_type="WebFetchContentEmpty")

            truncated = len(markdown) > MAX_OUTPUT_CHARS
            output = markdown[:MAX_OUTPUT_CHARS]
            return build_tool_success(
                self.spec.name,
                url=url,
                final_url=fetched.final_url,
                output=output,
                content_length=len(markdown),
                truncated=truncated,
            )
        except Exception as exc:  # noqa: BLE001
            return build_tool_failure(self.spec.name, exc)

    async def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise FileToolError("url 只支持 http 或 https。", error_type="WebFetchUrlSchemeUnsupported")
        if not parsed.hostname:
            raise FileToolError("url 缺少有效 host。", error_type="WebFetchUrlInvalid")
        if parsed.username or parsed.password:
            raise FileToolError("url 不允许包含用户名或密码。", error_type="WebFetchUrlCredentialForbidden")

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = await asyncio.to_thread(self._resolve_host_addresses, parsed.hostname, port)
        if not addresses:
            raise FileToolError("url host 无法解析。", error_type="WebFetchHostUnresolved")
        for address in addresses:
            ip = ipaddress.ip_address(address)
            # 仅允许公网地址，避免 SSRF 访问本机、内网、保留地址或链路本地地址。
            if not ip.is_global:
                raise FileToolError("url 指向非公网地址，已拒绝访问。", error_type="WebFetchHostForbidden")

    def _resolve_host_addresses(self, hostname: str, port: int) -> set[str]:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        return {str(item[4][0]) for item in results}

    async def _fetch_html(self, url: str) -> FetchedPage:
        current_url = url
        timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            for _ in range(MAX_REDIRECTS + 1):
                response = await client.get(current_url, follow_redirects=False)
                if response.is_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise FileToolError("页面重定向缺少 Location。", error_type="WebFetchRedirectInvalid")
                    current_url = urljoin(current_url, location)
                    await self._validate_url(current_url)
                    continue

                if response.status_code < 200 or response.status_code >= 300:
                    raise FileToolError(f"页面请求失败，HTTP 状态码：{response.status_code}", error_type="WebFetchHttpError")
                content = response.content
                if len(content) > MAX_DOWNLOAD_BYTES:
                    raise FileToolError("页面内容超过下载大小限制。", error_type="WebFetchContentTooLarge")
                return FetchedPage(final_url=str(response.url), html=response.text)

        raise FileToolError("页面重定向次数过多。", error_type="WebFetchRedirectTooMany")

    def _extract_markdown(self, html: str, url: str) -> str:
        cleaned_html = self._remove_boilerplate_tags(html)
        extracted = trafilatura.extract(
            cleaned_html,
            url=url,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            deduplicate=True,
        )
        return (extracted or "").strip()

    def _remove_boilerplate_tags(self, html: str) -> str:
        try:
            document = lxml_html.fromstring(html)
        except Exception:  # noqa: BLE001
            return html

        # 先移除语义明确的样板区域，再交给 trafilatura 做正文密度抽取。
        for element in document.xpath("//nav|//footer|//aside|//script|//style|//noscript"):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
        return lxml_html.tostring(document, encoding="unicode")
