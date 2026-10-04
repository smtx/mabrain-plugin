#!/usr/bin/env python3
"""mabrain-crawl: bring web pages and files into a MaBrain brain from your own machine.

Ships with the MaBrain plugin (Python 3.10+, standard library only).
The page content never goes through a model: the script downloads the literal HTML and
uploads it, and the server converts it to text without an LLM.

    mabrain-crawl.py preview --brain BRAIN --url URL [--prefix /guides/] [--max-pages 50]
    mabrain-crawl.py preview --brain BRAIN --url URL --urls-file pages.txt
    mabrain-crawl.py upload  --run RUN_ID
    mabrain-crawl.py status  --run RUN_ID
    mabrain-crawl.py login                      # once per machine: sign in in the browser
    mabrain-crawl.py upload-file PATH --brain BRAIN [--source-url URL] [--title T]
    mabrain-crawl.py key create --name my-agent [--role read] [--env-file .env] [--brain BRAIN]

``preview`` finds the pages (sitemap.xml, or links from the start page under the prefix),
downloads them into ``.mabrain/crawl/<run>/`` and writes the manifest: what would be
uploaded, what is unchanged since the last run for this brain, and the size to estimate
the credit. Nothing is uploaded until ``upload`` is run on that same run, and ``upload``
sends exactly the files the preview downloaded. ``upload`` can be interrupted and run
again: it continues where it stopped and never submits a page whose job may still run.

Uploads go to POST {api}/v1/brains/{brain}/sources with the session from ``login`` (kept in
~/.mabrain/credentials.json, mode 0600, refreshed on its own), or with MABRAIN_API_KEY when set.
``key create`` writes a key for an app straight into its env file: it is never printed.

Exit codes: 0 done; 1 usage; 2 some pages failed; 3 stopped (credit exhausted, or a job
still running: run ``upload`` again later).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

VERSION = "0.2.1"
USER_AGENT = f"mabrain-crawl/{VERSION}"
HTTP_TIMEOUT_S = 60
MAX_PAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PAGES = 50
JOB_CAP_S = 1800  # a job still running after this is never resubmitted
JOB_POLL_S = 5.0
BUSY_WAIT_CAP_S = 600
MAX_BUSY_IN_A_ROW = 30  # then stop and let the next upload continue
# The preview's cost estimate uses the server's prices (GET /v1/pricing, public); these defaults only
# apply when the server cannot be reached, and the preview then says so. The server has the final word.
TOKENS_PER_CHAR = 1.35
TOKENS_PER_DOCUMENT = 9_500
USD_PER_MILLION = 1.0


def server_pricing(api: str) -> dict:
    """MaBrain's current rate, or the defaults (``source: "default"``) when it cannot be read."""
    try:
        req = urllib.request.Request(api.rstrip("/") + "/v1/pricing", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 -- https API from --api
            body = json.loads(resp.read())
        return {"usd_per_million": float(body["usd_per_million"]), "tokens_per_char": float(body["tokens_per_char"]),
                "tokens_per_document": float(body.get("tokens_per_document", 0)),  # a server before 0.3.1 had no fixed part
                "source": "server"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"usd_per_million": USD_PER_MILLION, "tokens_per_char": TOKENS_PER_CHAR, "tokens_per_document": TOKENS_PER_DOCUMENT,
                "source": "default"}
DEFAULT_API = "https://api.mabra.in"

EXIT_OK, EXIT_USAGE, EXIT_FAILED, EXIT_STOPPED = 0, 1, 2, 3

# Entry states. A page is "completed" only when its extraction job completed.
PENDING, UPLOADING, UPLOADED, COMPLETED, UNCHANGED, FAILED, OPERATOR, SKIPPED = (
    "pending", "uploading", "uploaded", "completed", "unchanged", "failed", "operator", "skipped",
)
OPEN_STATES = (PENDING, UPLOADING, UPLOADED)


class CrawlError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class Stop(CrawlError):
    """Stop the run cleanly; the manifest keeps what is left for the next ``upload``."""


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Fetching pages


class OffsiteRedirect(Exception):
    pass


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, url: str) -> None:
        self.origin = _origin(url)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        # Same scheme, host and port; the only change allowed is an upgrade to https on the same host.
        scheme, host, port = _origin(newurl)
        upgrade = scheme == "https" and port == 443 and host == self.origin[1]
        if (scheme, host, port) != self.origin and not upgrade:
            raise OffsiteRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class Page:
    status: int
    final_url: str
    content_type: str
    body: bytes


def fetch(url: str) -> Page:
    opener = urllib.request.build_opener(_SameHostRedirect(url))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5"})
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
            body = resp.read(MAX_PAGE_BYTES + 1)
            return Page(resp.status, resp.geturl(), resp.headers.get("Content-Type", ""), body)
    except urllib.error.HTTPError as e:
        return Page(e.code, url, e.headers.get("Content-Type", "") if e.headers else "", b"")


# ---------------------------------------------------------------------------
# Discovery


def _origin(url: str) -> tuple[str, str, int]:
    """(scheme, host, port): pages of a site share all three, so a sitemap or a link cannot
    make this machine request another service on the same host (another port)."""
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    return parts.scheme, (parts.hostname or ""), port or (443 if parts.scheme == "https" else 80)


def _same_site(url: str, origin: tuple[str, str, int], prefix: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and _origin(url) == origin and parts.path.startswith(prefix)


def _normalize(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


MAX_SITEMAPS = 50  # sitemap files fetched per preview, whatever the index says


def sitemap_urls(origin: str, *, accept=lambda url: True, limit: int | None = None, depth: int = 2) -> list[str]:  # noqa: ANN001
    """The accepted <loc> entries of origin/sitemap.xml, following a sitemap index down to
    ``depth``. Child sitemaps must be on the same scheme, host and port as ``origin`` (an index
    must not make this machine request another host), and the walk stops at ``limit`` pages
    or MAX_SITEMAPS files."""
    home = urllib.parse.urlsplit(origin)
    seen: set[str] = set()
    out: list[str] = []

    def full() -> bool:
        return (limit is not None and len(out) >= limit) or len(seen) >= MAX_SITEMAPS

    def walk(url: str, level: int) -> None:
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme, parts.netloc) != (home.scheme, home.netloc):
            return
        if url in seen or level > depth or full():
            return
        seen.add(url)
        try:
            page = fetch(url)
        except (urllib.error.URLError, TimeoutError, OffsiteRedirect, ConnectionError):
            return
        if page.status != 200 or not page.body:
            return
        try:
            root = ET.fromstring(page.body)
        except ET.ParseError:
            return
        tag = root.tag.rsplit("}", 1)[-1]
        locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
        if tag == "sitemapindex":
            for loc in locs:
                if full():
                    return
                walk(loc, level + 1)
        else:
            for loc in locs:
                if limit is not None and len(out) >= limit:
                    return
                if accept(loc) and loc not in out:
                    out.append(loc)

    walk(origin.rstrip("/") + "/sitemap.xml", 0)
    return out


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


# Legal and account pages: never knowledge worth paying for. Skipped when a site is discovered
# (--include-legal keeps them); a page listed by hand in --urls-file is always kept.
_LEGAL_SEGMENTS = {
    # English
    "privacy", "privacy-policy", "privacy-notice", "privacy-statement", "terms", "tos", "terms-of-service",
    "terms-of-use", "terms-and-conditions", "conditions", "legal", "legal-notice", "imprint", "impressum",
    "cookies", "cookie-policy", "cookie-notice", "gdpr", "disclaimer", "accessibility", "accessibility-statement",
    # Spanish and Catalan
    "privacidad", "politica-de-privacidad", "politica-privacidad", "politica-de-cookies", "aviso-legal",
    "terminos", "terminos-y-condiciones", "condiciones", "condiciones-de-uso", "rgpd", "accesibilidad",
    "privacitat", "politica-de-privacitat", "avis-legal", "termes-i-condicions", "condicions",
    # Accounts and shopping
    "login", "signin", "sign-in", "logout", "signup", "sign-up", "register", "account", "my-account",
    "cart", "checkout", "unsubscribe", "password-reset",
}


def is_legal(url: str) -> bool:
    """A page whose path has a legal or account segment (``/privacy``, ``/es/aviso-legal``,
    ``/legal/notice``), never a word inside a longer one (``/cookie-recipes``)."""
    for segment in urllib.parse.urlsplit(url).path.lower().split("/"):
        if segment.rsplit(".", 1)[0] in _LEGAL_SEGMENTS:
            return True
    return False


def discover(start_url: str, prefix: str | None, max_pages: int, skip=lambda url: False) -> tuple[list[str], str]:  # noqa: ANN001
    """Pages to bring in, in a stable order, and how they were found ("sitemap" or "links").
    Pages for which ``skip`` is true are left out and do not count towards ``max_pages``."""
    parts = urllib.parse.urlsplit(start_url)
    site = _origin(start_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    prefix = prefix if prefix is not None else (parts.path or "/")
    found: list[str] = []
    method = "sitemap"
    for loc in sitemap_urls(origin, accept=lambda u: _same_site(_normalize(u), site, prefix) and not skip(_normalize(u)), limit=max_pages):
        url = _normalize(loc)
        if url not in found:
            found.append(url)
    if not found:
        method = "links"
        queue, seen = [_normalize(start_url)], set()
        while queue and len(found) < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if not skip(url):  # a skipped page is still read for its links, never uploaded
                found.append(url)
            try:
                page = fetch(url)
            except (urllib.error.URLError, TimeoutError, OffsiteRedirect, ConnectionError):
                continue
            if page.status != 200 or "html" not in page.content_type:
                continue
            links = _Links()
            links.feed(page.body.decode("utf-8", errors="replace"))
            for href in links.hrefs:
                nxt = _normalize(urllib.parse.urljoin(url, href))
                if _same_site(nxt, site, prefix) and nxt not in seen and nxt not in queue:
                    queue.append(nxt)
    return found[:max_pages], method


def robots_for(start_url: str) -> urllib.robotparser.RobotFileParser | None:
    parts = urllib.parse.urlsplit(start_url)
    rp = urllib.robotparser.RobotFileParser()
    try:
        page = fetch(f"{parts.scheme}://{parts.netloc}/robots.txt")
    except (urllib.error.URLError, TimeoutError, OffsiteRedirect, ConnectionError):
        return None
    if page.status != 200:
        return None
    rp.parse(page.body.decode("utf-8", errors="replace").splitlines())
    return rp


# ---------------------------------------------------------------------------
# Visible text: what decides "unchanged"


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):  # noqa: ANN001
        if not self.depth:
            self.parts.append(data)


class _Title(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = self.in_h1 = False
        self.title = ""
        self.h1 = ""

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        if tag == "title":
            self.in_title = True
        elif tag == "h1" and not self.h1:
            self.in_h1 = True

    def handle_endtag(self, tag):  # noqa: ANN001
        if tag == "title":
            self.in_title = False
        elif tag == "h1":
            self.in_h1 = False

    def handle_data(self, data):  # noqa: ANN001
        if self.in_title:
            self.title += data
        if self.in_h1:
            self.h1 += data


def page_title(html: bytes) -> str:
    """The page's first <h1>, or its <title>: what a receipt shows as the source."""
    parser = _Title()
    parser.feed(html.decode("utf-8", errors="replace"))
    return " ".join((parser.h1 or parser.title).split())[:200]


def visible_text(html: bytes) -> str:
    """The page's visible text with whitespace collapsed. The server deduplicates by the bytes of
    the file, so a nonce or a build id in the HTML would re-extract (and bill) a page whose
    text did not change; the script compares this instead."""
    parser = _Text()
    parser.feed(html.decode("utf-8", errors="replace"))
    return " ".join(" ".join(parser.parts).split())


# ---------------------------------------------------------------------------
# Main content: what is worth paying for. Menus, headers, footers, sidebars and forms repeat on
# every page of a site; extracting them again for each page spends credit on the same few facts.


# Never content, wherever they appear.
_ALWAYS_DROP = {"head", "script", "style", "noscript", "template", "svg", "iframe", "nav", "aside", "form",
                "button", "select", "dialog", "canvas", "object"}
# The site's chrome when outside the content; an article's own header (with its title) stays.
_CHROME = {"header", "footer"}
_DROP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog", "menu", "menubar"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_KEEP_ATTRS = {"href", "alt", "title", "lang", "colspan", "rowspan"}
MIN_CONTENT_CHARS = 200  # less than this left after cleaning: the cleaner guessed wrong, keep more


class _Clean(HTMLParser):
    def __init__(self, only_main: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.only_main = only_main
        self.stack: list[tuple[str, bool, bool, bool]] = []  # tag, dropped here, main here, emitted
        self.drop = self.main = 0
        self.out: list[str] = []
        self.kept_h1 = False

    def _emitting(self) -> bool:
        return not self.drop and (self.main > 0 or not self.only_main)

    def _start(self, tag: str, attrs: dict) -> str:
        kept = "".join(f' {k}="{html.escape(v or "", quote=True)}"' for k, v in attrs.items() if k in _KEEP_ATTRS)
        return f"<{tag}{kept}>"

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        a = dict(attrs)
        if tag in _VOID:
            if self._emitting() and tag in ("br", "hr", "img"):
                self.out.append(self._start(tag, a))
            return
        in_content = any(t in ("main", "article") or m for t, _, m, _ in self.stack)
        dropped = tag in _ALWAYS_DROP or a.get("role") in _DROP_ROLES or (tag in _CHROME and not in_content)
        main_here = tag == "main" or a.get("role") == "main"
        self.drop += dropped
        self.main += main_here
        emitted = self._emitting() and tag not in ("html", "body")  # the output has its own wrapper
        if emitted:
            self.out.append(self._start(tag, a))
            self.kept_h1 = self.kept_h1 or tag == "h1"
        self.stack.append((tag, dropped, main_here, emitted))

    def handle_endtag(self, tag):  # noqa: ANN001
        if not any(t == tag for t, _, _, _ in self.stack):
            return  # a stray end tag
        while self.stack:
            t, dropped, main_here, emitted = self.stack.pop()
            if emitted:
                self.out.append(f"</{t}>")
            self.drop -= dropped
            self.main -= main_here
            if t == tag:
                break

    def handle_data(self, data):  # noqa: ANN001
        if self._emitting():
            self.out.append(html.escape(data, quote=False))


def main_content(body: bytes) -> bytes:
    """The page reduced to its content, as HTML: the <main> element when there is one, otherwise
    the page without its navigation, header, footer, sidebars and forms. Falls back to keeping more
    when too little text would be left, so a page is never emptied by a wrong guess."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body  # another encoding: sent as it is rather than re-encoded wrongly
    has_main = re.search(r"<main[\s>]|role\s*=\s*[\"']?main\b", text, re.I) is not None
    title = html.escape(page_title(body), quote=False)
    for only_main in ((True, False) if has_main else (False,)):
        parser = _Clean(only_main)
        parser.feed(text)
        parser.close()
        kept = "".join(parser.out)
        # The page's title is often in a header outside <main>: keep it as the content's heading.
        heading = f"<h1>{title}</h1>" if title and not parser.kept_h1 else ""
        out = f"<!doctype html><html><head><title>{title}</title></head><body>{heading}{kept}</body></html>".encode()
        if len(visible_text(out)) >= MIN_CONTENT_CHARS:
            return out
    return body


def sha256(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def slug_for(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.strip("/") or "index"
    base = re.sub(r"[^a-zA-Z0-9]+", "-", path).strip("-")[:80] or "page"
    return f"{base}-{sha256(url)[:8]}.html"


# ---------------------------------------------------------------------------
# Manifest and per-brain state (atomic writes)


def home() -> Path:
    return Path(os.environ.get("MABRAIN_HOME") or Path.cwd() / ".mabrain")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def target_key(target: dict) -> str:
    """Runs and history are bound to where they upload: the same site in testing and in
    production must not share states."""
    ident = "|".join(str(target.get(k, "")) for k in ("kind", "endpoint", "workspace", "brain"))
    return sha256(ident)[:16]


def state_path(target: dict) -> Path:
    return home() / "state" / f"{target_key(target)}.json"


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}", run_id):
        raise CrawlError(EXIT_USAGE, f"not a run id: {run_id!r}")
    return home() / "crawl" / run_id


def load_run(run_id: str) -> tuple[Path, dict]:
    path = run_dir(run_id) / "manifest.json"
    if not path.is_file():
        raise CrawlError(EXIT_USAGE, f"no run {run_id} under {home()}")
    return path, read_json(path)


# ---------------------------------------------------------------------------
# Upload targets


@dataclass
class Submitted:
    kind: str  # "job" | "unchanged" | "operator" | "busy" | "stop" | "error"
    job_id: str = ""
    document_id: str = ""
    retry_after: float = 0.0
    reason: str = ""


@dataclass
class JobResult:
    kind: str  # "completed" | "running" | "failed" | "retry"
    reason: str = ""
    job_id: str = ""


def _multipart(fields: dict[str, str], file: tuple[str, str, bytes]) -> tuple[bytes, str]:
    boundary = "mabrain" + secrets.token_hex(12)
    out: list[bytes] = []
    for name, value in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    filename, ctype, data = file
    out.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n"
    )
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def check_api(base: str) -> str:
    """Credentials and tokens only travel over https (or to this machine)."""
    if not (base.startswith("https://") or re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?$", base.rstrip("/"))):
        raise CrawlError(EXIT_USAGE, "the API must be https:// (or a localhost http://)")
    return base.rstrip("/")


class Http:
    def __init__(self, base: str, headers: dict[str, str], auth=None) -> None:  # noqa: ANN001
        self.base = check_api(base)
        self.headers = {"User-Agent": USER_AGENT, **headers}
        self.auth = auth  # called before each request: a session token may have been refreshed
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(self, method: str, path: str, *, json_body=None, multipart=None, params=None, headers=None):  # noqa: ANN001
        headers = {**self.headers, **(headers or {})}
        if self.auth is not None:
            headers["Authorization"] = f"Bearer {self.auth()}"
        body = None
        if params:
            path += "?" + urllib.parse.urlencode(params)
        if json_body is not None:
            body, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif multipart is not None:
            body, headers["Content-Type"] = _multipart(*multipart)
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=HTTP_TIMEOUT_S) as resp:
                status, raw, rh = resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            status, raw, rh = e.code, e.read(), e.headers
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as e:
            # The request may or may not have arrived: the manifest keeps the page as
            # "uploading", and the next upload submits it again (the server deduplicates).
            raise Stop(EXIT_STOPPED, f"{method} {self.base}{path.split('?')[0]}: {getattr(e, 'reason', e) or type(e).__name__}; run upload again") from e
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {"raw": text[:300]}
        return status, data, rh


def _retry_after(headers) -> float:  # noqa: ANN001
    try:
        return float(headers.get("Retry-After", "")) if headers else 30.0
    except ValueError:
        return 30.0


def _error(data: dict) -> tuple[str, str]:
    """(code, hint) from {"error": {"code", "hint"}}, or an older {"detail": {...}} envelope."""
    if not isinstance(data, dict):
        return "", ""
    for key in ("error", "detail"):
        inner = data.get(key)
        if isinstance(inner, dict):
            return str(inner.get("code") or ""), str(inner.get("hint") or inner.get("message") or "")
    return str(data.get("code") or ""), str(data.get("hint") or "")


def _code(data: dict) -> str:
    return _error(data)[0]


# ---------------------------------------------------------------------------
# Signing in (T26): the crawler is its own OAuth client of MaBrain, so the person never copies a key.
# ``login`` opens the browser (PKCE, loopback redirect, RFC 8252); the session lives in
# ``~/.mabrain/credentials.json`` (0600) and is refreshed under a file lock, so two runs never
# reuse one refresh token (the server revokes the whole sign-in when that happens; eng review A3).
# MABRAIN_API_KEY, when set, still wins: agents and CI keep using keys.

LOGIN_SCOPES = "brain:read brain:ingest"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
LOGIN_WAIT_S = 300
REFRESH_MARGIN_S = 60


def config_dir() -> Path:
    return Path(os.environ.get("MABRAIN_CONFIG_DIR") or Path.home() / ".mabrain")


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def _save_credentials(data: dict) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _form_post(url: str, fields: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310 -- the --api https URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def _store_tokens(api: str, client_id: str, tok: dict) -> dict:
    data = {"api": api, "client_id": client_id, "access_token": tok["access_token"],
            "refresh_token": tok.get("refresh_token", ""), "expires_at": time.time() + float(tok.get("expires_in", 3600))}
    _save_credentials(data)
    return data


def cmd_login(args: argparse.Namespace) -> dict:
    import base64
    import threading
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    api = check_api(args.api)
    got: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: ANN002
            pass

        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in q
            self.wfile.write(("<!doctype html><meta charset=utf-8><title>MaBrain</title><body style='font-family:system-ui'>"
                              + ("<h1>Listo</h1><p>Ya puedes volver a la terminal.</p>" if ok else
                                 "<h1>No se pudo iniciar sesión</h1><p>Vuelve a la terminal.</p>")).encode())

    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    status, client = _form_json(f"{api}/register", {
        "client_name": "MaBrain crawler", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none", "scope": LOGIN_SCOPES})
    if status not in (200, 201) or "client_id" not in client:
        raise CrawlError(EXIT_USAGE, f"MaBrain did not register the crawler (HTTP {status}): {_error(client)[1]}")
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    url = f"{api}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect, "scope": LOGIN_SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": api})
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    say(f"Opening your browser to sign in to MaBrain. If it does not open, visit:\n{url}")
    if not args.no_browser:
        webbrowser.open(url)
    thread.join(LOGIN_WAIT_S)
    server.server_close()
    if got.get("state") != state or "code" not in got:
        raise CrawlError(EXIT_USAGE, "sign-in not completed: " + (got.get("error_description") or got.get("error") or "no answer in 5 minutes"))
    status, tok = _form_post(f"{api}/token", {"grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
                                              "client_id": client["client_id"], "code_verifier": verifier, "resource": api})
    if status != 200 or "access_token" not in tok:
        raise CrawlError(EXIT_USAGE, f"MaBrain did not issue a session (HTTP {status}): {tok.get('error_description', '')}")
    _store_tokens(api, client["client_id"], tok)
    return {"signed_in": True, "api": api, "credentials": str(credentials_path())}


def _form_json(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise CrawlError(EXIT_USAGE, f"cannot reach {url}: {getattr(e, 'reason', e)}") from e


def bearer(api: str) -> str:
    """What to send as ``Authorization: Bearer``: MABRAIN_API_KEY if set, else the signed-in
    session, refreshed when it is about to expire (one process at a time, A3)."""
    import fcntl

    key = os.environ.get("MABRAIN_API_KEY", "")
    if key:
        return key
    path = credentials_path()
    if not path.is_file():
        raise CrawlError(EXIT_USAGE, "not signed in: run `mabrain-crawl.py login` (or set MABRAIN_API_KEY)")
    creds = read_json(path)
    if creds.get("api", "").rstrip("/") != api.rstrip("/"):
        raise CrawlError(EXIT_USAGE, f"signed in to {creds.get('api')}, not {api}: run login again")
    if creds["expires_at"] - REFRESH_MARGIN_S > time.time():
        return creds["access_token"]
    lock = path.with_name("credentials.lock")
    with open(lock, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            creds = read_json(path)  # another run may have refreshed while we waited
            if creds["expires_at"] - REFRESH_MARGIN_S > time.time():
                return creds["access_token"]
            try:
                status, tok = _form_post(f"{check_api(api)}/token", {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
                                                                     "client_id": creds["client_id"], "resource": api.rstrip("/")})
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, ValueError):
                status, tok = 0, {}
            if status != 200 or "access_token" not in tok:
                # The server may have rotated the token and the answer got lost: reusing the old one
                # would make it revoke the whole sign-in. Forget it and sign in again (fail closed).
                path.unlink(missing_ok=True)
                raise CrawlError(EXIT_USAGE, "your MaBrain sign-in could not be renewed: run `mabrain-crawl.py login` again")
            return _store_tokens(creds["api"], creds["client_id"], tok)["access_token"]
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def cmd_logout(args: argparse.Namespace) -> dict:
    path = credentials_path()
    existed = path.is_file()
    path.unlink(missing_ok=True)
    return {"signed_out": existed}


class MaBrainTarget:
    """POST /v1/brains/{brain}/sources. The server decides a 409: ``duplicate`` (this brain
    already has the facts) or ``duplicate_other_brain`` (the operator re-extracts)."""

    def __init__(self, target: dict) -> None:
        bearer(target["endpoint"])  # fail early, before any upload, when there is no key nor session
        self.brain = target["brain"]
        endpoint = target["endpoint"]
        self.http = Http(endpoint, {}, auth=lambda: bearer(endpoint))

    def submit(self, entry: dict, data: bytes) -> Submitted:
        # The entry's Idempotency-Key is saved in the manifest before the first attempt, so a
        # retry after a lost answer gets the stored result instead of a second upload.
        status, body, headers = self.http.call(
            "POST", f"/v1/brains/{urllib.parse.quote(self.brain)}/sources",
            multipart=({"source_url": entry["url"], "title": entry.get("title", "")}, (Path(entry["file"]).name, "text/html", data)),
            headers={"Idempotency-Key": entry["idempotency_key"]} if entry.get("idempotency_key") else None,
        )
        code, hint = _error(body)
        if status in (201, 202):
            return Submitted("job", job_id=str(body.get("job_id", "")), document_id=str(body.get("document_id", "")))
        if status == 409:
            if code == "idempotency_in_progress":
                return Submitted("busy", retry_after=_retry_after(headers), reason=code)
            if code == "idempotency_outcome_unknown":
                return Submitted("operator", reason=f"{code}: an earlier attempt may have started this page's extraction; "
                                                    "ask the brain about it before uploading it again")
            if code == "duplicate_other_brain":
                return Submitted("operator", reason=code)
            if code in ("", "duplicate"):
                return Submitted("unchanged", document_id=str(body.get("document_id", "")))
            return Submitted("error", reason=f"409 {code} {hint}".strip())
        if status == 429:
            if code == "credit_exhausted":
                return Submitted("stop", reason=f"credit_exhausted: {hint}".strip())
            return Submitted("busy", retry_after=_retry_after(headers), reason=code or "busy")
        return Submitted("error", reason=f"{status} {code} {hint}".strip())

    def job(self, entry: dict) -> JobResult:
        status, body, _ = self.http.call("GET", f"/v1/brains/{urllib.parse.quote(self.brain)}/jobs/{urllib.parse.quote(entry['job_id'])}")
        if status != 200:
            return JobResult("running" if status >= 500 else "failed", reason=f"GET job -> {status}")
        state = body.get("state") or body.get("status")
        if state == "completed":
            return JobResult("completed")
        if state in ("partial", "failed"):
            return JobResult("failed", reason=f"{state}: {body.get('detail') or body.get('hint') or ''}".strip())
        return JobResult("running")


def make_target(target: dict):  # noqa: ANN201
    if target["kind"] == "mabrain":
        return MaBrainTarget(target)
    raise CrawlError(EXIT_USAGE, f"unknown target {target['kind']} (made by an older version of this script)")


# ---------------------------------------------------------------------------
# Commands


def cmd_preview(args: argparse.Namespace) -> dict:
    url = args.url
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise CrawlError(EXIT_USAGE, "the URL must start with http:// or https://")
    target = {"kind": "mabrain", "endpoint": args.api.rstrip("/"), "brain": args.brain}

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    rdir = run_dir(run_id)
    (rdir / "pages").mkdir(parents=True)
    history = read_json(state_path(target)) if state_path(target).is_file() else {}
    robots = robots_for(url)
    if args.urls_file:
        site = _origin(url)
        listed = [_normalize(line.strip()) for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
        offsite = [u for u in listed if _origin(u) != site]
        if offsite:
            raise CrawlError(EXIT_USAGE, f"--urls-file has pages on another site than --url: {offsite[:3]}")
        urls, method = list(dict.fromkeys(listed))[: args.max_pages], "list"
        skipped_legal = []
    else:
        skipped_legal: list[str] = []

        def skip(u: str) -> bool:
            if args.include_legal or not is_legal(u):
                return False
            if u not in skipped_legal:
                skipped_legal.append(u)
            return True

        urls, method = discover(url, args.prefix, args.max_pages, skip=skip)
    say(f"found {len(urls)} page(s) by {method}")

    entries = []
    for u in urls:
        entry = {"url": u, "state": PENDING, "reason": "", "file": "", "bytes": 0, "chars": 0,
                 "html_sha256": "", "text_sha256": "", "document_id": "", "job_id": "", "retries": 0, "updated_at": now()}
        if robots is not None and not robots.can_fetch(USER_AGENT, u):
            entry.update(state=FAILED, reason="disallowed by robots.txt")
            entries.append(entry)
            continue
        try:
            page = fetch(u)
        except OffsiteRedirect as e:
            entry.update(state=FAILED, reason=f"redirects to another site: {e}")
            entries.append(entry)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            entry.update(state=FAILED, reason=f"download failed: {getattr(e, 'reason', e)}")
            entries.append(entry)
            continue
        if not args.urls_file and not args.include_legal and is_legal(page.final_url):
            skipped_legal.append(u)  # a normal-looking address that redirects to a legal page
            continue
        if page.status != 200:
            entry.update(state=FAILED, reason=f"HTTP {page.status}")
        elif "html" not in page.content_type.lower():
            entry.update(state=FAILED, reason=f"not HTML ({page.content_type or 'no Content-Type'})")
        elif not page.body.strip():
            entry.update(state=FAILED, reason="empty page")
        elif len(page.body) > MAX_PAGE_BYTES:
            entry.update(state=FAILED, reason="page larger than 10 MB")
        else:
            body = page.body if args.full_page else main_content(page.body)
            text = visible_text(body)
            name = slug_for(u)
            (rdir / "pages" / name).write_bytes(body)
            entry.update(file=f"pages/{name}", bytes=len(body), chars=len(text), title=page_title(page.body),
                         html_sha256=sha256(body), text_sha256=sha256(text), final_url=page.final_url)
            before = history.get(u)
            # Uploads made before the content was cleaned remembered the whole page's text.
            same = before and before.get("text_sha256") in (entry["text_sha256"], sha256(visible_text(page.body)))
            if same and before.get("state") in (COMPLETED, UNCHANGED):
                entry.update(state=UNCHANGED, reason="same visible text as the last upload", document_id=before.get("document_id", ""))
            if before:
                entry["last_state"] = before.get("state", "")
                entry["document_id"] = entry["document_id"] or before.get("document_id", "")
        entries.append(entry)

    manifest = {
        "version": VERSION, "run_id": run_id, "created_at": now(), "start_url": url,
        "prefix": args.prefix if args.prefix is not None else (urllib.parse.urlsplit(url).path or "/"),
        "discovered_by": method, "target": target, "approved_at": "", "entries": entries,
        "skipped": [{"url": u, "reason": "legal or account page (use --include-legal to keep it)"} for u in skipped_legal],
        "pricing": server_pricing(target["endpoint"]),
    }
    write_json(rdir / "manifest.json", manifest)
    return summary(manifest, rdir)


def summary(manifest: dict, rdir: Path) -> dict:
    entries = manifest["entries"]
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["state"]] = counts.get(e["state"], 0) + 1
    to_upload = [e for e in entries if e["state"] in OPEN_STATES]
    chars = sum(e["chars"] for e in to_upload)
    return {
        "run_id": manifest["run_id"], "manifest": str(rdir / "manifest.json"), "target": manifest["target"],
        "counts": counts, "to_upload": [e["url"] for e in to_upload],
        "unchanged": [e["url"] for e in entries if e["state"] == UNCHANGED],
        "failed": [{"url": e["url"], "reason": e["reason"]} for e in entries if e["state"] in (FAILED, OPERATOR)],
        "skipped": manifest.get("skipped", []),
        "estimate": _estimate(chars, manifest["target"].get("endpoint", ""), documents=len(to_upload)),
        "progress": progress(manifest),
    }


def progress(manifest: dict) -> dict:
    """How far an upload is: pages done of the approved ones, what they cost, and how long the rest
    should take at the pace so far (one page at a time: each waits for its extraction)."""
    # The total is fixed when the upload is approved: a page that ends failed or unchanged is done,
    # not removed from the count.
    done = [e for e in manifest["entries"] if e["state"] == COMPLETED]
    left = [e for e in manifest["entries"] if e["state"] in OPEN_STATES]
    total = manifest.get("upload_total") or len(left) + len(done)
    price = manifest.get("pricing") or {"usd_per_million": USD_PER_MILLION, "tokens_per_char": TOKENS_PER_CHAR}
    # A manifest saved by crawler 0.2.0 has no fixed part: keep its own rate, never mix in the new one.
    tokens = (sum(e["chars"] for e in done) * price["tokens_per_char"]
              + len(done) * price.get("tokens_per_document", 0))
    out = {"done": total - len(left), "total": total, "left": len(left),
           "approx_spent_usd": round(tokens * price["usd_per_million"] / 1_000_000, 2), "eta_minutes": None}
    # The pace of this sitting only: an upload resumed next month must not count the month between.
    started = manifest.get("upload_started_at", "")
    times = sorted(e["completed_at"] for e in done if started and e.get("completed_at", "") >= started)
    if times and left:
        elapsed = (_parse(times[-1]) - _parse(started)).total_seconds()
        out["eta_minutes"] = max(1, round(elapsed / len(times) * len(left) / 60))
    return out


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _estimate(chars: int, api: str, documents: int = 0) -> dict:
    """Each page is one document: a per-character part plus a fixed part per document."""
    price = server_pricing(api) if api else {"usd_per_million": USD_PER_MILLION, "tokens_per_char": TOKENS_PER_CHAR,
                                             "tokens_per_document": TOKENS_PER_DOCUMENT, "source": "default"}
    tokens = int(chars * price["tokens_per_char"] + documents * price.get("tokens_per_document", 0))
    return {"visible_chars": chars, "approx_tokens": tokens,
            "approx_cost_usd": round(tokens * price["usd_per_million"] / 1_000_000, 2), "rate_source": price["source"]}


def _record(path: Path, manifest: dict, entry: dict, **changes) -> None:
    entry.update(changes, updated_at=now())
    write_json(path, manifest)


def _remember(target: dict, entry: dict) -> None:
    sp = state_path(target)
    history = read_json(sp) if sp.is_file() else {}
    history[entry["url"]] = {"text_sha256": entry["text_sha256"], "document_id": entry["document_id"], "state": entry["state"], "at": now()}
    write_json(sp, history)


def _wait(target_impl, path: Path, manifest: dict, entry: dict, cap_s: float) -> str:  # noqa: ANN001
    deadline = time.monotonic() + cap_s
    while True:
        res = target_impl.job(entry)
        if res.kind == "completed":
            _record(path, manifest, entry, state=COMPLETED, reason="", completed_at=now())
            _remember(manifest["target"], entry)
            return COMPLETED
        if res.kind == "failed":
            _record(path, manifest, entry, state=FAILED, reason=res.reason)
            return FAILED
        if res.kind == "retry":
            _record(path, manifest, entry, state=PENDING, reason=res.reason, job_id="", idempotency_key="", retries=entry.get("retries", 0) + 1)
            return PENDING
        if time.monotonic() > deadline:
            raise Stop(EXIT_STOPPED, f"job {entry['job_id']} for {entry['url']} is still running: not resubmitted; run upload again later")
        time.sleep(JOB_POLL_S)


def cmd_upload(args: argparse.Namespace) -> dict:
    path, manifest = load_run(args.run)
    rdir = path.parent
    target_impl = make_target(manifest["target"])
    if not manifest.get("approved_at"):
        manifest["approved_at"] = now()
        manifest["upload_total"] = sum(e["state"] in OPEN_STATES for e in manifest["entries"])
    manifest["upload_started_at"] = now()
    write_json(path, manifest)
    job_cap = float(args.job_cap_s)
    for entry in manifest["entries"]:
        busy_in_a_row = 0
        while entry["state"] in OPEN_STATES:
            if entry["state"] in (UPLOADED, UPLOADING) and entry.get("job_id"):
                _wait(target_impl, path, manifest, entry, job_cap)
                continue
            # PENDING, or UPLOADING whose response was lost: submitting again reuses the entry's
            # Idempotency-Key, so the server replays the first answer instead of uploading twice.
            data = (rdir / entry["file"]).read_bytes()
            _record(path, manifest, entry, state=UPLOADING,
                    idempotency_key=entry.get("idempotency_key") or f"crawl-{manifest['run_id']}-{secrets.token_hex(12)}")
            sub = target_impl.submit(entry, data)
            if sub.kind == "job":
                _record(path, manifest, entry, state=UPLOADED, job_id=sub.job_id, document_id=sub.document_id or entry.get("document_id", ""))
            elif sub.kind == "unchanged":
                _record(path, manifest, entry, state=UNCHANGED, reason="already in this brain", document_id=sub.document_id or entry.get("document_id", ""))
                _remember(manifest["target"], entry)
            elif sub.kind == "operator":
                _record(path, manifest, entry, state=OPERATOR, reason=sub.reason)
            elif sub.kind == "busy":
                busy_in_a_row += 1
                if busy_in_a_row > MAX_BUSY_IN_A_ROW:
                    _record(path, manifest, entry, state=PENDING, reason=f"still busy ({sub.reason})")
                    raise Stop(EXIT_STOPPED, "the brain stayed busy with another job; run upload again later")
                wait = min(max(sub.retry_after, 0.0), BUSY_WAIT_CAP_S)
                _record(path, manifest, entry, state=PENDING, reason=f"busy ({sub.reason}); waiting {int(wait)} s")
                say(f"busy: waiting {int(wait)} s")
                time.sleep(wait)
            elif sub.kind == "stop":
                _record(path, manifest, entry, state=PENDING, reason=sub.reason)
                raise Stop(EXIT_STOPPED, sub.reason)
            else:
                _record(path, manifest, entry, state=FAILED, reason=sub.reason)
        p = progress(manifest)
        eta = f", about {p['eta_minutes']} min left" if p["eta_minutes"] else ""
        say(f"[{p['done']}/{p['total']}] {entry['state']:9} {entry['url']} (~{p['approx_spent_usd']:.2f} $ so far{eta})")
    return summary(manifest, rdir)


def cmd_status(args: argparse.Namespace) -> dict:
    path, manifest = load_run(args.run)
    return summary(manifest, path.parent)


def cmd_upload_file(args: argparse.Namespace) -> dict:
    """One local text file (Markdown, text or HTML; up to 50 MB) into a brain, with the signed-in session.
    A PDF or Word file is never sent: MaBrain takes text only, so the AI running this reads the file
    itself and sends its text as a document dossier (MCP ``dossier_start`` with ``document``)."""
    path = Path(args.path).expanduser()
    if not path.is_file():
        raise CrawlError(EXIT_USAGE, f"{path}: no such file")
    if path.suffix.lower() in (".pdf", ".docx", ".doc", ".odt", ".rtf", ".pptx", ".xlsx"):
        raise CrawlError(EXIT_USAGE, f"{path.name}: MaBrain takes text only. Read the file yourself and send its text as a "
                                     "document dossier (dossier_start with `document`), one section per chapter.")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise CrawlError(EXIT_USAGE, f"{path} is larger than 50 MB; split it or upload a smaller export")
    with open(path, "rb") as fh:
        data = fh.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise CrawlError(EXIT_USAGE, f"{path} is larger than 50 MB; split it or upload a smaller export")
    api = args.api.rstrip("/")
    http_ = Http(api, {}, auth=lambda: bearer(api))
    ctype = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".html": "text/html",
             ".htm": "text/html"}.get(path.suffix.lower(), "text/plain")
    fields = {k: v for k, v in (("source_url", args.source_url), ("title", args.title)) if v}
    status, body, _ = http_.call("POST", f"/v1/brains/{urllib.parse.quote(args.brain)}/sources", multipart=(fields, (path.name, ctype, data)),
                                 headers={"Idempotency-Key": f"file-{sha256(data)[:32]}-{args.brain}"})
    code, hint = _error(body)
    if status not in (201, 202):
        raise CrawlError(EXIT_FAILED, f"{status} {code}: {hint}".strip())
    return body


def git_guard(env_file: Path) -> str:
    """Whether a secret may be written to ``env_file`` without ending up in a commit: asks Git for
    the effective state (tracked, ignored), and refuses when it cannot tell (fail closed)."""
    import shutil
    import subprocess

    folder = env_file.parent.resolve()
    in_repo = any((d / ".git").exists() for d in (folder, *folder.parents))
    if not in_repo:
        return "not_a_git_repository"
    if shutil.which("git") is None:
        raise CrawlError(EXIT_USAGE, f"{env_file} is in a Git repository and git is not available to check it is ignored")

    def git(*a: str) -> int:
        return subprocess.run(["git", "-C", str(folder), *a], capture_output=True).returncode

    if git("ls-files", "--error-unmatch", "--", env_file.name) == 0:
        raise CrawlError(EXIT_USAGE, f"{env_file} is tracked by Git: a key written there would be committed. "
                                     "Untrack it (git rm --cached) and ignore it, or use another --env-file.")
    rc = git("check-ignore", "-q", "--", env_file.name)
    if rc == 1:
        raise CrawlError(EXIT_USAGE, f"{env_file} is not ignored by Git: add it to .gitignore first.")
    if rc != 0:
        raise CrawlError(EXIT_USAGE, f"could not check with Git that {env_file} is ignored")
    return "ignored"


def cmd_key_create(args: argparse.Namespace) -> dict:
    """A key for the person's own app, written straight into its env file: it never reaches the
    screen nor the conversation. Only a signed-in person can create keys (eng review A4/O2).

    The operation key is saved before the request and reused on a retry, so a lost answer never
    leaves a second live key behind: a replayed answer has no key (it is shown once), so that
    credential is revoked and a new one created."""
    api = check_api(args.api)
    env_file = Path(args.env_file).expanduser()
    git_state = git_guard(env_file)  # before anything exists on the server
    http_ = Http(api, {}, auth=lambda: bearer(api))
    pending = config_dir() / "pending-keys" / f"{sha256(f'{api}|{env_file.resolve()}|{args.name}')[:24]}.json"
    pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl

    # One process at a time per (API, env file, name), from choosing the operation key to writing the
    # env file: two concurrent runs can never end with two live keys.
    with open(pending.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            issued = pending.with_suffix(".issued.json")  # the live key this command made for this app
            current = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
            has_key = any(ln.startswith(f"{args.var}=") for ln in current)
            if not pending.is_file():
                if issued.is_file():
                    # A live key exists for this app, whatever the env file says now (it may have been
                    # deleted or edited): never make a second one silently.
                    if not args.replace:
                        raise CrawlError(EXIT_USAGE, f"a key for {args.name!r} already exists "
                                                     f"({read_json(issued)['credential_id']}); pass --replace to revoke it and "
                                                     "write a new one")
                    old = read_json(issued)["credential_id"]
                    status, rbody, _ = http_.call("DELETE", f"/v1/keys/{urllib.parse.quote(old)}")
                    if status not in (200, 404):
                        code, _hint = _error(rbody)
                        raise CrawlError(EXIT_FAILED, f"could not revoke the previous key ({status} {code}); nothing was changed")
                    issued.unlink()
                elif has_key:
                    # A key this command did not make: it cannot be revoked from here (fail closed).
                    raise CrawlError(EXIT_USAGE, f"{env_file} already has {args.var} from elsewhere; remove that line (and revoke "
                                                 "the key if it is a MaBrain key) before creating a new one")
            op = read_json(pending)["op"] if pending.is_file() else f"key-{secrets.token_hex(12)}"
            write_json(pending, {"op": op, "name": args.name, "env_file": str(env_file)})

            def create(op_key: str) -> dict:
                # --brain: a key that opens that brain only (also one shared with you, on its owner's credit)
                status, body, _ = http_.call("POST", "/v1/keys", json_body={"role": args.role, "name": args.name,
                                                                          **({"brain": args.brain} if args.brain else {})},
                                             headers={"Idempotency-Key": op_key})
                if status != 201:
                    code, hint = _error(body)
                    raise CrawlError(EXIT_FAILED, f"{status} {code}: {hint}".strip())
                return body

            body = create(op)
            if not body.get("key"):  # replay of an earlier attempt whose answer was lost: that key is unknown
                status, rbody, _ = http_.call("DELETE", f"/v1/keys/{urllib.parse.quote(body['credential_id'])}")
                if status not in (200, 404):  # 404: already inactive
                    code, hint = _error(rbody)
                    raise CrawlError(EXIT_FAILED, f"could not revoke the key whose answer was lost ({status} {code}); "
                                                  "run the same command again")
                op = f"key-{secrets.token_hex(12)}"
                write_json(pending, {"op": op, "name": args.name, "env_file": str(env_file)})
                body = create(op)
            lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.is_file() else []
            lines = [ln for ln in lines if not ln.startswith(f"{args.var}=")] + [f"{args.var}={body['key']}"]
            if args.brain:
                lines = [ln for ln in lines if not ln.startswith("MABRAIN_BRAIN=")] + [f"MABRAIN_BRAIN={args.brain}"]
            tmp = env_file.with_name(f".{env_file.name}.{secrets.token_hex(4)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, env_file)
            write_json(issued, {"credential_id": body["credential_id"], "env_file": str(env_file), "var": args.var})
            pending.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return {"credential_id": body["credential_id"], "role": body["role"], "name": body["name"], "written_to": str(env_file),
            "variable": args.var, "key_prefix": body["key"][:8] + "…", "git": git_state}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mabrain-crawl", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview", help="find and download the pages; upload nothing")
    p.add_argument("--url", required=True)
    p.add_argument("--brain", required=True, help="the brain's slug")
    p.add_argument("--prefix", default=None, help="only pages whose path starts with this (default: the URL's path)")
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    p.add_argument("--urls-file", default=None, help="exact pages to bring, one URL per line (same site as --url); skips discovery")
    p.add_argument("--include-legal", action="store_true", help="also bring privacy, terms, cookie and account pages")
    p.add_argument("--full-page", action="store_true", help="upload whole pages, with menus, headers and footers")
    p.add_argument("--api", default=os.environ.get("MABRAIN_API_URL", DEFAULT_API))
    u = sub.add_parser("upload", help="upload the pages of an approved preview (also resumes)")
    u.add_argument("--run", required=True)
    u.add_argument("--job-cap-s", default=JOB_CAP_S)
    s = sub.add_parser("status", help="summary of a run")
    s.add_argument("--run", required=True)
    api_default = os.environ.get("MABRAIN_API_URL", DEFAULT_API)
    lg = sub.add_parser("login", help="sign in to MaBrain in the browser (once per machine)")
    lg.add_argument("--api", default=api_default)
    lg.add_argument("--no-browser", action="store_true", help="only print the URL to open")
    sub.add_parser("logout", help="forget this machine's MaBrain session")
    f = sub.add_parser("upload-file", help="upload one local file to a brain")
    f.add_argument("path"); f.add_argument("--brain", required=True); f.add_argument("--api", default=api_default)
    f.add_argument("--source-url", default=""); f.add_argument("--title", default="")
    k = sub.add_parser("key", help="keys for your own apps")
    ksub = k.add_subparsers(dest="key_cmd", required=True)
    kc = ksub.add_parser("create", help="create a key for an app and write it to its env file (never printed)")
    kc.add_argument("--name", required=True, help="the app or agent the key is for")
    kc.add_argument("--role", choices=["read", "ingest"], default="read")
    kc.add_argument("--env-file", default=".env"); kc.add_argument("--var", default="MABRAIN_API_KEY")
    kc.add_argument("--brain", default=None, help="also write MABRAIN_BRAIN=<slug>")
    kc.add_argument("--replace", action="store_true", help="revoke the key this command wrote there before and write a new one")
    kc.add_argument("--api", default=api_default)
    args = ap.parse_args(argv)
    if args.cmd == "key":
        args.cmd = f"key-{args.key_cmd}"
    try:
        result = {"preview": cmd_preview, "upload": cmd_upload, "status": cmd_status, "login": cmd_login, "logout": cmd_logout,
                  "upload-file": cmd_upload_file, "key-create": cmd_key_create}[args.cmd](args)
    except CrawlError as e:
        say(f"error: {e}")
        print(json.dumps({"error": str(e), "exit": e.code}))
        return e.code
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.cmd == "upload" and result.get("failed"):
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
