"""Web access for Jarvis, using nothing but the standard library.

Search goes through DuckDuckGo's no-JavaScript HTML endpoint, which needs no
API key. Page reading strips markup down to plain text so the model gets prose
instead of a wall of div soup.
"""

import gzip
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from . import config

SEARCH_URL = "https://html.duckduckgo.com/html/"


def _get(url: str, data: bytes | None = None) -> str:
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "User-Agent": config.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps outbound links in a redirect; pull the real URL out."""
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        query = urllib.parse.urlparse(href).query
        target = urllib.parse.parse_qs(query).get("uddg")
        if target:
            return target[0]
    if href.startswith("//"):
        return "https:" + href
    return href


class _ResultParser(HTMLParser):
    """Pulls (title, url, snippet) triples out of the DDG HTML result list."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._mode: str | None = None
        self._buf: list[str] = []
        self._url = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._mode = "title"
            self._buf = []
            self._url = _unwrap(attrs.get("href", ""))
        elif "result__snippet" in classes:
            self._mode = "snippet"
            self._buf = []

    def handle_endtag(self, tag):
        if self._mode == "title" and tag == "a":
            self.results.append(
                {"title": "".join(self._buf).strip(), "url": self._url, "snippet": ""}
            )
            self._mode = None
        elif self._mode == "snippet" and tag in ("a", "div", "td"):
            if self.results:
                self.results[-1]["snippet"] = " ".join("".join(self._buf).split())
            self._mode = None

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)


def search(query: str, max_results: int = 5) -> str:
    """Search the web. Returns a compact numbered digest for the model.

    Search engines drop connections on bursts of queries, which surfaced as a
    flat "no network?" even with the network fine. Two shapes of request and a
    short pause between attempts cover the common transient cases; a genuine
    outage still fails, just accurately.
    """
    encoded = urllib.parse.urlencode({"q": query, "kl": "wt-wt"})
    attempts = (
        (SEARCH_URL, encoded.encode()),                 # POST
        (f"{SEARCH_URL}?{encoded}", None),              # GET
    )

    last = ""
    for index, (url, body) in enumerate(attempts):
        try:
            html = _get(url, data=body)
            break
        except Exception as exc:  # noqa: BLE001 - retry, then report
            last = f"{type(exc).__name__}: {exc}"
            if index + 1 < len(attempts):
                time.sleep(1.5)
    else:
        return (
            f"Search failed after {len(attempts)} attempts ({last}). "
            "The connection may be down, or the search engine may be refusing "
            "requests for the moment. Answer from what you already know, and "
            "say that you could not check."
        )

    parser = _ResultParser()
    parser.feed(html)
    results = [r for r in parser.results if r["url"].startswith("http")][:max_results]

    if not results:
        # A refused request comes back as a short page with no result markup at
        # all — not an error. Reporting that as "no results" is worse than
        # useless: it tells the model that nothing on the subject exists, and it
        # will confidently pass that on. Say what actually happened instead.
        if "result__a" not in html and len(html) < 20000:
            return (
                "The search engine refused this request, most likely rate "
                "limiting after several searches in quick succession. This is "
                "NOT evidence that nothing exists on the subject. Tell the user "
                "you could not check just now and answer from your own "
                "knowledge if you can, or suggest trying again shortly."
            )
        return f"No results for {query!r}."

    lines = [f"Search results for {query!r}:"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[{i}] {r['title']}\n    {r['url']}")
        if r["snippet"]:
            lines.append(f"    {r['snippet']}")
    lines.append(
        "\n(Call fetch_page on a URL above if the snippets are not enough.)"
    )
    return "\n".join(lines)


class _TextParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BREAK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BREAK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.chunks.append(data)


def fetch_page(url: str, max_chars: int | None = None) -> str:
    """Fetch a URL and return its readable text, truncated to a sane length."""
    max_chars = max_chars or config.MAX_PAGE_CHARS
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        html = _get(url)
    except urllib.error.HTTPError as exc:
        return f"Could not fetch {url}: HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"Could not fetch {url}: {exc}"

    parser = _TextParser()
    parser.feed(html)
    text = "".join(parser.chunks)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[... truncated ...]"
    return f"Contents of {url}:\n\n{text}" if text else f"{url} returned no readable text."
