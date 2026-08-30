import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit


_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
_CHARSET_PATTERN = re.compile(r"charset\s*=\s*[\"']?([^\s;\"']+)", re.IGNORECASE)
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_USER_AGENT = "WhatsAppSearchLinkMetadata/1.0"


class LinkEnrichmentError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LinkMetadata:
    final_url: str
    title: str | None
    description: str | None


class LinkEnricher(Protocol):
    def fetch(self, url: str) -> LinkMetadata: ...


@dataclass(frozen=True, slots=True)
class _FetchedResponse:
    status_code: int
    location: str | None
    content_type: str | None
    body: bytes


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, hostname: str, port: int, connect_ip: str, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout)
        self._connect_ip = connect_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, connect_ip: str, timeout: float) -> None:
        super().__init__(
            hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_ip = connect_ip

    def connect(self) -> None:
        socket_connection = socket.create_connection(
            (self._connect_ip, self.port),
            self.timeout,
        )
        self.sock = self._context.wrap_socket(
            socket_connection,
            server_hostname=self.host,
        )


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._title_parts: list[str] = []
        self.description: str | None = None

    @property
    def title(self) -> str | None:
        return _normalize_metadata(" ".join(self._title_parts))

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "title":
            self._in_title = True
            return
        if normalized_tag != "meta" or self.description is not None:
            return
        values = {
            name.casefold(): value
            for name, value in attrs
            if value is not None
        }
        if values.get("name", "").casefold() == "description":
            self.description = _normalize_metadata(values.get("content", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


def _normalize_metadata(value: str) -> str | None:
    normalized = " ".join(value.split())[:1000]
    return normalized or None


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        if url and url not in urls:
            urls.append(url)
    return urls


class SecureLinkEnricher:
    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        max_redirects: int = 3,
        max_response_bytes: int = 262_144,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes

    def fetch(self, url: str) -> LinkMetadata:
        current_url = url
        for redirect_count in range(self._max_redirects + 1):
            normalized_url, connect_ip = self._validate_destination(current_url)
            response = self._request_once(normalized_url, connect_ip)
            if response.status_code in _REDIRECT_STATUSES:
                if redirect_count == self._max_redirects:
                    raise LinkEnrichmentError("redirect limit exceeded")
                if response.location is None:
                    raise LinkEnrichmentError("redirect has no destination")
                current_url = urljoin(normalized_url, response.location)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise LinkEnrichmentError("link returned a non-success status")
            if response.content_type is None:
                raise LinkEnrichmentError("link has no content type")
            media_type = response.content_type.split(";", 1)[0].strip().casefold()
            if media_type not in _HTML_CONTENT_TYPES:
                raise LinkEnrichmentError("link is not HTML")
            parser = _MetadataParser()
            parser.feed(self._decode_html(response.body, response.content_type))
            return LinkMetadata(
                final_url=normalized_url,
                title=parser.title,
                description=parser.description,
            )
        raise LinkEnrichmentError("redirect limit exceeded")

    def _validate_destination(self, url: str) -> tuple[str, str]:
        if any(ord(character) <= 32 for character in url):
            raise LinkEnrichmentError("link contains invalid characters")
        if len(url) > 2048:
            raise LinkEnrichmentError("link is too long")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as error:
            raise LinkEnrichmentError("link is invalid") from error
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            raise LinkEnrichmentError("link scheme is not allowed")
        if parsed.username is not None or parsed.password is not None:
            raise LinkEnrichmentError("link credentials are not allowed")
        hostname = parsed.hostname
        if hostname is None:
            raise LinkEnrichmentError("link has no host")
        resolved_port = port or (443 if scheme == "https" else 80)
        try:
            address_info = socket.getaddrinfo(
                hostname,
                resolved_port,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise LinkEnrichmentError("link host resolution failed") from error
        addresses = list(dict.fromkeys(item[4][0] for item in address_info))
        if not addresses:
            raise LinkEnrichmentError("link host resolution returned no address")
        try:
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except ValueError as error:
            raise LinkEnrichmentError("link host resolution returned an invalid address") from error
        if any(not address.is_global for address in parsed_addresses):
            raise LinkEnrichmentError("link destination is not public")
        normalized_url = urlunsplit(
            (scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
        )
        return normalized_url, addresses[0]

    def _request_once(self, url: str, connect_ip: str) -> _FetchedResponse:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if hostname is None:
            raise LinkEnrichmentError("link has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection_type = (
            _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
        )
        connection = connection_type(
            hostname,
            port,
            connect_ip,
            self._timeout_seconds,
        )
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        try:
            connection.request(
                "GET",
                target,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "identity",
                    "User-Agent": _USER_AGENT,
                },
            )
            response = connection.getresponse()
            if response.status in _REDIRECT_STATUSES:
                return _FetchedResponse(
                    status_code=response.status,
                    location=response.getheader("Location"),
                    content_type=None,
                    body=b"",
                )
            content_type = response.getheader("Content-Type")
            body = b""
            if 200 <= response.status < 300 and content_type is not None:
                media_type = content_type.split(";", 1)[0].strip().casefold()
                if media_type in _HTML_CONTENT_TYPES:
                    content_encoding = response.getheader("Content-Encoding")
                    if content_encoding not in {None, "", "identity"}:
                        raise LinkEnrichmentError("link uses unsupported content encoding")
                    body = self._read_response_body(response)
            return _FetchedResponse(
                status_code=response.status,
                location=None,
                content_type=content_type,
                body=body,
            )
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            raise LinkEnrichmentError("link request failed") from error
        finally:
            connection.close()

    def _read_response_body(self, response: http.client.HTTPResponse) -> bytes:
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as error:
                raise LinkEnrichmentError("link has an invalid content length") from error
            if declared_length < 0 or declared_length > self._max_response_bytes:
                raise LinkEnrichmentError("link response is too large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(65_536, self._max_response_bytes - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > self._max_response_bytes:
                raise LinkEnrichmentError("link response is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_html(body: bytes, content_type: str) -> str:
        charset_match = _CHARSET_PATTERN.search(content_type)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            return body.decode(charset, errors="replace")
        except LookupError as error:
            raise LinkEnrichmentError("link uses an unknown character encoding") from error
