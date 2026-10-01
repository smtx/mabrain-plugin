#!/usr/bin/env python3
"""mabrain-crawl: bring web pages into a MaBrain brain from your own machine.

Installed once as ``~/.mabrain/mabrain-crawl.py`` (Python 3.10+, standard library only).
The page content never goes through a model: the script downloads the literal HTML and
uploads it, and the server converts it to text without an LLM.

    mabrain-crawl.py preview --brain BRAIN --url URL [--prefix /guides/] [--max-pages 50]
    mabrain-crawl.py preview --brain BRAIN --url URL --urls-file pages.txt
    mabrain-crawl.py upload  --run RUN_ID
    mabrain-crawl.py status  --run RUN_ID

``preview`` finds the pages (sitemap.xml, or links from the start page under the prefix),
downloads them into ``.mabrain/crawl/<run>/`` and writes the manifest: what would be
uploaded, what is unchanged since the last run for this brain, and the size to estimate
the credit. Nothing is uploaded until ``upload`` is run on that same run, and ``upload``
sends exactly the files the preview downloaded. ``upload`` can be interrupted and run
again: it continues where it stopped and never submits a page whose job may still run.

Uploads go to POST {api}/v1/brains/{brain}/sources with the ingest key in MABRAIN_API_KEY.

Exit codes: 0 done; 1 usage; 2 some pages failed; 3 stopped (credit exhausted, or a job
still running: run ``upload`` again later).
"""

from __future__ import annotations

import argparse
import hashlib
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

VERSION = "0.1.0"
USER_AGENT = f"mabrain-crawl/{VERSION}"
HTTP_TIMEOUT_S = 60
MAX_PAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PAGES = 50
JOB_CAP_S = 1800  # a job still running after this is never resubmitted
JOB_POLL_S = 5.0
BUSY_WAIT_CAP_S = 600
MAX_BUSY_IN_A_ROW = 30  # then stop and let the next upload continue
CHARS_PER_TOKEN = 4  # rough, only for the preview's estimate
DEFAULT_API = "https://api.mabra.in"

EXIT_OK, EXIT_USAGE, EXIT_FAILED, EXIT_STOPPED = 0, 1, 2, 3

# Entry states. A page is "completed" only when its extraction job completed.
PENDING, UPLOADING, UPLOADED, COMPLETED, UNCHANGED, FAILED, OPERATOR = (
    "pending", "uploading", "uploaded", "completed", "unchanged", "failed", "operator",
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


def discover(start_url: str, prefix: str | None, max_pages: int) -> tuple[list[str], str]:
    """Pages to bring in, in a stable order, and how they were found ("sitemap" or "links")."""
    parts = urllib.parse.urlsplit(start_url)
    site = _origin(start_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    prefix = prefix if prefix is not None else (parts.path or "/")
    found: list[str] = []
    method = "sitemap"
    for loc in sitemap_urls(origin, accept=lambda u: _same_site(_normalize(u), site, prefix), limit=max_pages):
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


class Http:
    def __init__(self, base: str, headers: dict[str, str]) -> None:
        if not (base.startswith("https://") or re.match(r"http://(127\.0\.0\.1|localhost)(:\d+)?$", base.rstrip("/"))):
            raise CrawlError(EXIT_USAGE, "the API must be https:// (or a localhost http://)")
        self.base = base.rstrip("/")
        self.headers = {"User-Agent": USER_AGENT, **headers}
        self.opener = urllib.request.build_opener(_NoRedirect)

    def call(self, method: str, path: str, *, json_body=None, multipart=None, params=None, headers=None):  # noqa: ANN001
        headers = {**self.headers, **(headers or {})}
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


class MaBrainTarget:
    """POST /v1/brains/{brain}/sources. The server decides a 409: ``duplicate`` (this brain
    already has the facts) or ``duplicate_other_brain`` (the operator re-extracts)."""

    def __init__(self, target: dict) -> None:
        key = os.environ.get("MABRAIN_API_KEY", "")
        if not key:
            raise CrawlError(EXIT_USAGE, "MABRAIN_API_KEY is not set in this shell")
        self.brain = target["brain"]
        self.http = Http(target["endpoint"], {"Authorization": f"Bearer {key}"})

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
    else:
        urls, method = discover(url, args.prefix, args.max_pages)
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
        if page.status != 200:
            entry.update(state=FAILED, reason=f"HTTP {page.status}")
        elif "html" not in page.content_type.lower():
            entry.update(state=FAILED, reason=f"not HTML ({page.content_type or 'no Content-Type'})")
        elif not page.body.strip():
            entry.update(state=FAILED, reason="empty page")
        elif len(page.body) > MAX_PAGE_BYTES:
            entry.update(state=FAILED, reason="page larger than 10 MB")
        else:
            text = visible_text(page.body)
            name = slug_for(u)
            (rdir / "pages" / name).write_bytes(page.body)
            entry.update(file=f"pages/{name}", bytes=len(page.body), chars=len(text), title=page_title(page.body),
                         html_sha256=sha256(page.body), text_sha256=sha256(text), final_url=page.final_url)
            before = history.get(u)
            if before and before.get("text_sha256") == entry["text_sha256"] and before.get("state") in (COMPLETED, UNCHANGED):
                entry.update(state=UNCHANGED, reason="same visible text as the last upload", document_id=before.get("document_id", ""))
            if before:
                entry["last_state"] = before.get("state", "")
                entry["document_id"] = entry["document_id"] or before.get("document_id", "")
        entries.append(entry)

    manifest = {
        "version": VERSION, "run_id": run_id, "created_at": now(), "start_url": url,
        "prefix": args.prefix if args.prefix is not None else (urllib.parse.urlsplit(url).path or "/"),
        "discovered_by": method, "target": target, "approved_at": "", "entries": entries,
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
        "estimate": {"visible_chars": chars, "approx_tokens": chars // CHARS_PER_TOKEN},
    }


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
            _record(path, manifest, entry, state=COMPLETED, reason="")
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
        say(f"{entry['state']:9} {entry['url']}")
    return summary(manifest, rdir)


def cmd_status(args: argparse.Namespace) -> dict:
    path, manifest = load_run(args.run)
    return summary(manifest, path.parent)


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
    p.add_argument("--api", default=os.environ.get("MABRAIN_API_URL", DEFAULT_API))
    u = sub.add_parser("upload", help="upload the pages of an approved preview (also resumes)")
    u.add_argument("--run", required=True)
    u.add_argument("--job-cap-s", default=JOB_CAP_S)
    s = sub.add_parser("status", help="summary of a run")
    s.add_argument("--run", required=True)
    args = ap.parse_args(argv)
    try:
        result = {"preview": cmd_preview, "upload": cmd_upload, "status": cmd_status}[args.cmd](args)
    except CrawlError as e:
        say(f"error: {e}")
        print(json.dumps({"error": str(e), "exit": e.code}))
        return e.code
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.cmd == "upload" and result["failed"]:
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
