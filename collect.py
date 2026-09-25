#!/usr/bin/env python3
"""Collect the current content and images of one-page WordPress/Elementor sites.

For every domain in sites.txt this saves the raw HTML, the Elementor CSS files,
every image it can find, and a manifest.json describing the page structure, then
writes report.csv summarising the whole run. See README.md for setup and usage.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import mimetypes
import random
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parent

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
# Deliberately no image/webp or image/avif: some optimisation plugins would
# otherwise hand us a converted copy instead of the original upload.
IMAGE_ACCEPT = "image/jpeg,image/png,image/gif,image/svg+xml,image/*;q=0.8,*/*;q=0.5"

TIMEOUT = 20
RETRIES = 2
MAX_IMAGE_BYTES = 50 * 1024 * 1024
LOW_TEXT_THRESHOLD = 300

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".gif", ".webp", ".svg", ".avif",
    ".bmp", ".ico", ".tif", ".tiff", ".heic",
}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".ogv"}
TRACKING_HOSTS = (
    "facebook.com", "facebook.net", "google-analytics.com", "googletagmanager.com",
    "doubleclick.net", "googleadservices.com", "googlesyndication.com",
    "analytics.twitter.com", "t.co", "ads.linkedin.com", "px.ads.linkedin.com",
    "bat.bing.com", "ct.pinterest.com", "pixel.wp.com", "stats.wp.com",
    "quantserve.com", "scorecardresearch.com", "hotjar.com", "clarity.ms",
    "s.w.org", "analytics.tiktok.com", "snap.licdn.com",
)
PLACEHOLDER_RE = re.compile(r"(lazy[_-]?placeholder|1x1[^/]*|blank|spacer|transparent|pixel)\.(gif|png)$", re.I)
RESIZED_RE = re.compile(r"^(?P<stem>.+)-(?P<w>\d{1,5})x(?P<h>\d{1,5})(?P<ext>(?:\.[A-Za-z0-9]{2,5}){1,2})$")
CSS_URL_RE = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))\s*\)""", re.I)
CSS_ELEMENT_RE = re.compile(r"elementor-element-([0-9a-z]+)")
DNS_MARKERS = (
    "NameResolutionError", "Name or service not known", "nodename nor servname",
    "Temporary failure in name resolution", "getaddrinfo failed",
    "No address associated with hostname", "Failed to resolve",
)
LOGO_CLASSES = {
    "logo", "custom-logo", "custom-logo-link", "site-logo", "navbar-brand",
    "elementor-widget-theme-site-logo", "elementor-widget-site-logo",
}
# Background-type sources, used to split section images into images/background_images.
BACKGROUND_SOURCES = {"style", "css", "css-inline", "data-settings", "computed-style"}
# Page states, most severe first. Fetch failures (no HTML at all) come before these.
STATE_PRIORITY = ["blocked", "password_protected", "maintenance", "suspended", "http_error", "redirect_offsite"]
MAINTENANCE_MARKERS = (
    "elementor-maintenance-mode", "cmp-coming-soon", "wp-maintenance-mode",
    "under-construction-page", "id=\"mtnc", "class=\"mtnc", "csmm-", "wpmm-",
    "seedprod-coming-soon", "coming-soon-page",
)
MAINTENANCE_TITLE_RE = re.compile(
    r"coming soon|under construction|launching soon|maintenance mode|under maintenance|down for maintenance"
    r"|site is down|be right back",
    re.I,
)
BLOCK_MARKERS = (
    "cf-browser-verification", "challenge-platform", "cf_chl_", "sucuri website firewall",
    "your access to this site has been limited",
)
REPORT_FIELDS = [
    "domain", "status", "final_url", "sections", "images_found",
    "images_downloaded", "images_failed", "forms", "warnings",
]

log = logging.getLogger("collect")


# --------------------------------------------------------------------------- helpers


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_domain(line: str) -> str:
    """'https://Example.com/path' -> 'example.com'."""
    value = line.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/", 1)[0].rstrip(".")


def dir_name(domain: str) -> str:
    return domain.replace(":", "_")


def bare_host(host: str | None) -> str:
    host = (host or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def read_sites(path: Path) -> list[str]:
    domains: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        domain = normalize_domain(line)
        if domain and domain not in domains:
            domains.append(domain)
    return domains


def short_error(exc: BaseException) -> str:
    text = re.sub(r" at 0x[0-9a-f]+", "", str(exc))
    return f"{type(exc).__name__}: {text}"[:300]


def url_ext(url: str) -> str:
    return Path(unquote(urlsplit(url).path)).suffix.lower()


def is_video_url(url: str) -> bool:
    host = bare_host(urlsplit(url).hostname)
    return url_ext(url) in VIDEO_EXTS or host.endswith(("youtube.com", "youtu.be", "vimeo.com", "youtube-nocookie.com"))


def looks_like_image_url(value: str) -> bool:
    value = value.strip()
    if not value or value.startswith("data:") or " " in value or len(value) > 2000:
        return False
    if not value.startswith(("http://", "https://", "//", "/", "./", "../")) and "/" not in value:
        return False
    return url_ext(value) in IMAGE_EXTS


def css_urls(text: str):
    """Yield (url, selector) for every url(...) in a chunk of CSS."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    for m in CSS_URL_RE.finditer(text):
        url = next((g for g in m.groups() if g is not None), "").strip()
        brace = text.rfind("{", 0, m.start())
        selector = ""
        if brace != -1:
            start = max(text.rfind("}", 0, brace), text.rfind("{", 0, brace), text.rfind(";", 0, brace))
            selector = text[start + 1:brace]
        yield url, selector


def parse_srcset(value: str) -> list[tuple[str, str]]:
    """Parse a srcset into (url, descriptor) pairs; URLs may contain commas."""
    out, i, n = [], 0, len(value)
    while i < n:
        while i < n and (value[i].isspace() or value[i] == ","):
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not value[j].isspace():
            j += 1
        url, i, desc = value[i:j], j, ""
        if url.endswith(","):
            url = url.rstrip(",")
        else:
            k = value.find(",", i)
            k = n if k == -1 else k
            desc, i = value[i:k].strip(), k + 1
        if url:
            out.append((url, desc))
    return out


def largest_srcset(value: str) -> str | None:
    best, best_score = None, -1.0
    for url, desc in parse_srcset(value):
        score = 1.0
        m = re.match(r"^([\d.]+)([wxh])$", desc)
        if m:
            score = float(m.group(1)) * (1 if m.group(2) in "wh" else 1000)
        if score > best_score:
            best, best_score = url, score
    return best


def walk_json(obj, fn) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            walk_json(value, fn)
    elif isinstance(obj, list):
        for value in obj:
            walk_json(value, fn)
    elif isinstance(obj, str):
        fn(obj)


def classes(el: Tag) -> list[str]:
    value = el.get("class") or []
    return value.split() if isinstance(value, str) else list(value)


def nearest_data_id(el) -> str | None:
    node = el
    while isinstance(node, Tag):
        if node.get("data-id"):
            return node["data-id"]
        node = node.parent
    return None


SKIP_TEXT_TAGS = {"script", "style", "noscript", "template", "svg", "head", "title", "iframe", "select", "option"}


def visible_text(el: Tag) -> str:
    chunks = []
    for s in el.find_all(string=True):
        if type(s) is not NavigableString:  # comments, CDATA, script/style strings
            continue
        node, hidden = s.parent, False
        while node is not None:
            if node.name in SKIP_TEXT_TAGS:
                hidden = True
                break
            if node is el:
                break
            node = node.parent
        if not hidden:
            chunks.append(s)
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def unique(items):
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def safe_filename(name: str) -> str:
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", name).strip().lstrip(".")
    if len(name) > 150:
        stem, ext = Path(name).stem, Path(name).suffix
        name = stem[: 150 - len(ext)] + ext
    return name


def claim_name(name: str, used: set[str]) -> str:
    """Return name, or name-2, name-3... so it is unique (case-insensitively, for macOS)."""
    stem, ext = Path(name).stem, "".join(Path(name).suffixes[-1:])
    candidate, n = name, 2
    while candidate.lower() in used:
        candidate = f"{stem}-{n}{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def sniff_image(head: bytes, content_type: str) -> bool:
    ctype = content_type.split(";")[0].strip().lower()
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM", b"\x00\x00\x01\x00", b"II*\x00", b"MM\x00*")):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    if head[4:8] == b"ftyp":  # avif / heic
        return True
    lowered = head.lower()
    if b"<html" in lowered or b"<!doctype html" in lowered:
        return False  # an HTML page (often a soft 404), even if it contains inline SVG
    if b"<svg" in lowered:
        return True
    return ctype.startswith("image/")


# --------------------------------------------------------------------------- HTTP


class FetchError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def classify_exception(exc: BaseException) -> str:
    text = repr(exc)
    if isinstance(exc, requests.exceptions.SSLError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return "ssl_error"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "redirect_loop"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "dns_error" if any(m in text for m in DNS_MARKERS) else "connection_error"
    return "error"


def make_session(pool: int) -> requests.Session:
    session = requests.Session()
    session.headers.update(PAGE_HEADERS)
    adapter = requests.adapters.HTTPAdapter(pool_connections=pool * 2, pool_maxsize=pool * 2)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def http_get(session: requests.Session, url: str, *, stream: bool = False, headers=None) -> requests.Response:
    """GET with a 20s timeout and 2 retries on connection problems and 5xx responses."""
    for attempt in range(RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=stream, headers=headers)
        except requests.RequestException as exc:
            kind = classify_exception(exc)
            if kind in ("dns_error", "ssl_error", "redirect_loop") or attempt == RETRIES:
                message = f"DNS lookup failed for {urlsplit(url).hostname}" if kind == "dns_error" else short_error(exc)
                raise FetchError(kind, message) from exc
        else:
            if resp.status_code < 500 or attempt == RETRIES:
                return resp
            resp.close()
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- page analysis


def detect_page_state(domain: str, resp: requests.Response, soup: BeautifulSoup, final_url: str):
    """Return (list of states, list of warnings) for maintenance/password/redirect etc."""
    states, warnings = [], []
    code = resp.status_code
    html_lower = resp.content[:500_000].decode("utf-8", "replace").lower()
    title = (soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    final = urlsplit(final_url)

    if bare_host(final.hostname) != bare_host(urlsplit(f"//{domain}").hostname):
        states.append("redirect_offsite")
        warnings.append(f"redirect_offsite: {domain} redirects to {final.hostname}")
    if "suspendedpage" in final_url.lower() or "account suspended" in title:
        states.append("suspended")
        warnings.append("suspended: hosting account suspended page")
    if (
        code in (403, 429)
        or any(m in html_lower for m in BLOCK_MARKERS)
        or title in ("just a moment...", "access denied")
        or "attention required! | cloudflare" in title
    ):
        states.append("blocked")
        warnings.append(f"blocked: HTTP {code} / bot protection page — try --render")
    if (
        code == 401
        or soup.select_one("form.post-password-form, form[action*='action=postpass'], #password_protected_pass")
        or "wp-login.php" in final.path
        or "password-protected=login" in final.query
    ):
        states.append("password_protected")
        warnings.append("password_protected: page asks for a password or login")
    maint_reasons = []
    if code == 503:
        maint_reasons.append("HTTP 503")
    maint_reasons += [f"markup '{m}'" for m in MAINTENANCE_MARKERS if m in html_lower]
    if MAINTENANCE_TITLE_RE.search(title):
        maint_reasons.append(f"title '{title[:60]}'")
    if maint_reasons:
        states.append("maintenance")
        warnings.append("maintenance: " + ", ".join(maint_reasons[:3]))
    if code >= 400 and code not in (401, 403, 429, 503):
        states.append("http_error")
        warnings.append(f"http_error: HTTP {code}")
    if final.scheme == "http":
        warnings.append("insecure: final URL is plain http")
    if "wp-content" not in html_lower and "wp-includes" not in html_lower:
        warnings.append("not_wordpress: no wp-content/wp-includes references found")
    return states, warnings


def section_role(el: Tag) -> tuple[str, str | None, str | None]:
    """Return (role, template_type, template_id) for a top-level section."""
    template_type = template_id = None
    for parent in el.parents:
        if not isinstance(parent, Tag):
            continue
        etype = parent.get("data-elementor-type")
        if etype and template_type is None:
            template_type, template_id = etype, parent.get("data-elementor-id")
        if etype in ("header", "footer", "popup"):
            return etype, template_type, template_id
        if parent.name in ("header", "footer"):
            return parent.name, template_type, template_id
    return "content", template_type, template_id


def is_container(el) -> bool:
    return isinstance(el, Tag) and el.get("data-element_type") in ("section", "container")


def find_sections(soup: BeautifulSoup, warnings: list[str]) -> list[dict]:
    """Top-level Elementor sections/containers, plus non-Elementor header/footer."""
    found = []  # (tag, role, template_type, template_id)
    for el in soup.find_all(attrs={"data-element_type": ["section", "container"]}):
        if not any(is_container(p) for p in el.parents):
            found.append((el, *section_role(el)))

    if not found:
        fallback = [s for s in soup.find_all("section") if not s.find_parent("section")]
        if fallback:
            warnings.append("no_elementor_sections: used plain <section> elements instead")
            found = [(s, *section_role(s)) for s in fallback]
        else:
            warnings.append("no_sections: no Elementor sections/containers or <section> elements found")

    # Theme header/footer that aren't built from Elementor sections.
    top_ids = {id(t) for t, *_ in found}
    for tag_name in ("header", "footer"):
        for el in soup.find_all(tag_name):
            if el.find_parent(["header", "footer", "main", "article"]) or any(id(p) in top_ids for p in el.parents):
                continue
            if any(id(d) in top_ids for d in el.find_all(True)):
                continue  # already represented by the Elementor sections inside it
            found.append((el, tag_name, None, None))
            break

    position = {id(t): i for i, t in enumerate(soup.find_all(True))}
    found.sort(key=lambda item: position.get(id(item[0]), 0))
    return [
        {"_el": el, "role": role, "template_type": ttype, "template_id": tid}
        for el, role, ttype, tid in found
    ]


def extract_nav(soup: BeautifulSoup, base_url: str, sections: list[dict]):
    """Return (nav items, set of <a> elements used) for the top-of-page navigation."""
    regions = [s["_el"] for s in sections if s["role"] == "header"]
    regions += [h for h in soup.find_all("header") if not h.find_parent(["main", "article", "header"])]
    if not regions:
        content = [s["_el"] for s in sections if s["role"] == "content"]
        regions = content[:1]

    menu_selector = (
        "nav a[href], .elementor-nav-menu a[href], ul.menu a[href], "
        "[data-widget_type^='nav-menu'] a[href], [data-widget_type^='mega-menu'] a[href]"
    )
    links = [a for r in regions for a in r.select(menu_selector)]
    if not links:
        links = [a for r in regions for a in r.find_all("a", href=True) if "#" in a["href"]]
    if not links:
        menu = soup.find("nav") or soup.select_one(".elementor-nav-menu")
        links = menu.find_all("a", href=True) if menu else []

    page = urlsplit(base_url)
    items, seen, used = [], set(), set()
    for a in links:
        cls = set(classes(a))
        href = (a.get("href") or "").strip()
        if not href or href.lower().startswith("javascript:") or cls & {"elementor-menu-toggle", "skip-link", "screen-reader-text"}:
            continue
        label = a.get_text(" ", strip=True) or a.get("aria-label") or a.get("title") or ""
        if not label and a.find("img"):
            label = a.find("img").get("alt", "")
        target = None
        if href.startswith("#"):
            target = href[1:] or None
        else:
            absolute = urlsplit(urljoin(base_url, href))
            if bare_host(absolute.hostname) == bare_host(page.hostname) and absolute.path.rstrip("/") == page.path.rstrip("/"):
                target = absolute.fragment or None
        key = (label, href)
        used.add(id(a))
        if key in seen:
            continue
        seen.add(key)
        items.append({"label": label, "href": href, "target": target})
    return items, used


def build_section(el: Tag, order: int, meta: dict, nav_targets: set[str], nav_link_ids: set[int]) -> dict:
    own_id = el.get("id")
    menu_anchors = [a["id"] for a in el.select(".elementor-menu-anchor[id]")]
    targets_inside = [d["id"] for d in el.find_all(id=True) if d["id"] in nav_targets]
    anchor_ids = unique([i for i in [own_id, *menu_anchors, *targets_inside] if i])
    widget_types = unique(
        w["data-widget_type"].split(".")[0]
        for w in ([el] + el.find_all(attrs={"data-widget_type": True}))
        if w.get("data-widget_type")
    )
    headings = unique(
        h.get_text(" ", strip=True)
        for h in el.select("h1, h2, h3, h4, h5, h6, .elementor-heading-title")
        if h.get_text(strip=True)
    )
    text = visible_text(el)
    hidden_on = unique(m.group(1) for c in classes(el) if (m := re.match(r"elementor-hidden-(\w+)$", c)))
    return {
        "order": order,
        "role": meta["role"],
        "tag": el.name,
        "element_type": el.get("data-element_type"),
        "element_id": el.get("data-id"),
        "anchor": own_id or (menu_anchors[0] if menu_anchors else None) or (targets_inside[0] if targets_inside else None),
        "anchor_ids": anchor_ids,
        "anchor_only": widget_types == ["menu-anchor"],
        "template_type": meta["template_type"],
        "template_id": meta["template_id"],
        "hidden_on": hidden_on,
        "has_nav": any(id(a) in nav_link_ids for a in el.find_all("a")),
        "widget_types": widget_types,
        "headings": headings,
        "text_preview": text[:200],
        "text_length": len(text),
        "images": [],
        "background_images": [],
    }


def form_type(form: Tag) -> str | None:
    cls = set(classes(form))
    fid = form.get("id") or ""
    if "elementor-form" in cls:
        return "elementor-form"
    if "wpcf7-form" in cls or form.find_parent(class_="wpcf7"):
        return "contact-form-7"
    if "wpforms-form" in cls or form.find_parent(class_="wpforms-container"):
        return "wpforms"
    if fid.startswith("gform_") or form.find_parent(class_="gform_wrapper"):
        return "gravity-forms"
    if "frm-fluent-form" in cls or fid.startswith("fluentform") or form.find_parent(class_="fluentform"):
        return "fluent-forms"
    if form.get("role") == "search" or "search-form" in cls or form.find("input", attrs={"name": "s"}):
        return None  # WordPress search box, not a content form
    if "post-password-form" in cls or "action=postpass" in (form.get("action") or ""):
        return None  # password prompt; reported via status instead
    if "mc4wp-form" in cls:
        return "mailchimp-for-wp"
    return "other"


def field_label(form: Tag, field_el: Tag) -> str:
    fid = field_el.get("id")
    if fid:
        label = form.find("label", attrs={"for": fid})
        if label and label.get_text(strip=True):
            return label.get_text(" ", strip=True)
    group = field_el.find_parent(class_=re.compile(r"(elementor-field-group|wpforms-field|gfield|ff-el-group|wpcf7-form-control-wrap)"))
    if group:
        label = group.find(["label", "legend"])
        if label and label.get_text(strip=True):
            return label.get_text(" ", strip=True)
    return field_el.get("placeholder") or field_el.get("aria-label") or ""


def extract_forms(soup: BeautifulSoup, section_of) -> list[dict]:
    forms = []
    for form in soup.find_all("form"):
        ftype = form_type(form)
        if ftype is None:
            continue
        details, seen = [], set()
        for fld in form.find_all(["input", "select", "textarea"]):
            kind = (fld.get("type") or fld.name).lower() if fld.name == "input" else fld.name
            name = fld.get("name") or ""
            if kind in ("hidden", "submit", "button", "image", "reset") or not name:
                continue
            wrapper_classes = " ".join(classes(fld) + [c for p in list(fld.parents)[:3] if isinstance(p, Tag) for c in classes(p)])
            if "honeypot" in wrapper_classes or "gform_validation_container" in wrapper_classes or name.startswith("_wpcf7") or "[hp]" in name:
                continue
            m = re.match(r"^form_fields\[(.+?)\]", name)
            clean = m.group(1) if m else re.sub(r"\[\]$", "", name)
            if clean in seen:
                continue
            seen.add(clean)
            details.append({
                "name": clean,
                "label": field_label(form, fld),
                "type": kind,
                "required": fld.has_attr("required") or fld.get("aria-required") == "true",
            })
        use_labels = ftype in ("wpforms", "gravity-forms")
        forms.append({
            "type": ftype,
            "name": form.get("name") or form.get("id") or form.get("data-form_id"),
            "section": section_of(form),
            "fields": [(d["label"] or d["name"]) if use_labels else d["name"] for d in details],
            "field_details": details,
        })
    return forms


# --------------------------------------------------------------------------- image discovery


@dataclass
class Found:
    url: str
    sources: list[str] = field(default_factory=list)
    element_ids: list[str] = field(default_factory=list)
    sections: list[int] = field(default_factory=list)
    css_files: list[str] = field(default_factory=list)


class ImageFinder:
    """Collects image URLs (and embeds/videos) from HTML, CSS and rendered pages."""

    def __init__(self, section_of, id_to_section: dict[str, int]):
        self.found: dict[str, Found] = {}
        self.embeds: list[dict] = []
        self.section_of = section_of
        self.id_to_section = id_to_section

    def add(self, raw: str | None, base: str, source: str, el=None, element_ids=None, css_file=None) -> None:
        if not raw:
            return
        raw = raw.strip().strip("'\"")
        if not raw or raw.startswith(("data:", "blob:", "about:", "javascript:", "#")):
            return
        url = urljoin(base, raw)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return
        url = urlunsplit(parts._replace(fragment=""))
        host = (parts.hostname or "").lower()
        if any(host == t or host.endswith("." + t) for t in TRACKING_HOSTS):
            return
        if "/wp-includes/" in parts.path or PLACEHOLDER_RE.search(parts.path):
            return
        if el is not None and el.get("width") in ("0", "1") and el.get("height") in ("0", "1"):
            return  # 1x1 tracking pixel
        if url_ext(url) not in IMAGE_EXTS and is_video_url(url):
            self.add_embed("video", url, el)
            return
        entry = self.found.setdefault(url, Found(url))
        if source not in entry.sources:
            entry.sources.append(source)
        ids = list(element_ids or [])
        section = None
        if el is not None:
            data_id = nearest_data_id(el)
            if data_id:
                ids.append(data_id)
            section = self.section_of(el)
        for i in ids:
            if i not in entry.element_ids:
                entry.element_ids.append(i)
            if section is None and i in self.id_to_section:
                section = self.id_to_section[i]
        if section is not None and section not in entry.sections:
            entry.sections.append(section)
        if css_file and css_file not in entry.css_files:
            entry.css_files.append(css_file)

    def add_embed(self, kind: str, src: str, el=None) -> None:
        host = bare_host(urlsplit(src).hostname)
        if kind == "iframe":
            if "youtube" in host or host == "youtu.be":
                kind = "youtube"
            elif "vimeo" in host:
                kind = "vimeo"
            elif "google." in host and "/maps" in src:
                kind = "google-maps"
        elif kind == "video" and ("youtube" in host or host == "youtu.be"):
            kind = "youtube"
        elif kind == "video" and "vimeo" in host:
            kind = "vimeo"
        section = self.section_of(el) if el is not None else None
        if not any(e["src"] == src for e in self.embeds):
            self.embeds.append({"type": kind, "src": src, "section": section})

    def add_css(self, text: str, base: str, source: str, css_file: str | None = None) -> int:
        count = 0
        for url, selector in css_urls(text):
            if url_ext(url) not in IMAGE_EXTS:
                continue  # fonts etc.
            self.add(url, base, source, element_ids=CSS_ELEMENT_RE.findall(selector), css_file=css_file)
            count += 1
        return count

    def _json_attr(self, value: str, base: str, source: str, el: Tag) -> bool:
        try:
            data = json.loads(value)
        except ValueError:
            return False

        def visit(s: str) -> None:
            if s.startswith(("http", "//", "/")) and is_video_url(s):
                self.add_embed("video", urljoin(base, s), el)
            elif looks_like_image_url(s):
                self.add(s, base, source, el)

        walk_json(data, visit)
        return True

    def scan_html(self, soup: BeautifulSoup, page_url: str) -> None:
        base_tag = soup.find("base", href=True)
        base = urljoin(page_url, base_tag["href"]) if base_tag else page_url

        for meta in soup.find_all("meta"):
            prop = (meta.get("property") or meta.get("name") or "").lower()
            if prop in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"):
                self.add(meta.get("content"), base, "og")
            elif prop == "msapplication-tileimage":
                self.add(meta.get("content"), base, "favicon")

        for link in soup.find_all("link", href=True):
            rel = " ".join(link.get("rel") or []).lower()
            if "icon" in rel:
                self.add(link["href"], base, "favicon")
            elif "image_src" in rel:
                self.add(link["href"], base, "og")
            elif "preload" in rel and link.get("as") == "image":
                self.add(link["href"], base, "img")
                if link.get("imagesrcset"):
                    self.add(largest_srcset(link["imagesrcset"]), base, "srcset")

        for el in soup.find_all(True):
            if el.name in ("script", "style", "link", "meta", "base"):
                continue
            logo = el.name == "img" and self._is_logo(el)
            for attr, value in el.attrs.items():
                if isinstance(value, list):
                    value = " ".join(value)
                if not value:
                    continue
                attr = attr.lower()
                if attr == "style":
                    for url, _ in css_urls(value):
                        self.add(url, base, "style", el)
                elif "srcset" in attr:
                    self.add(largest_srcset(value), base, "logo" if logo else "srcset", el)
                elif attr == "src" and el.name in ("img", "input"):
                    self.add(value, base, "logo" if logo else "img", el)
                elif attr == "src" and el.name in ("source", "video"):
                    if url_ext(value) in IMAGE_EXTS:
                        self.add(value, base, "img", el)
                    else:
                        self.add_embed("video", urljoin(base, value), el)
                elif attr == "src" and el.name == "iframe":
                    self.add_embed("iframe", urljoin(base, value), el)
                elif attr == "poster":
                    self.add(value, base, "img", el)
                elif attr in ("href", "xlink:href") and el.name in ("a", "image"):
                    if looks_like_image_url(value):
                        self.add(value, base, "img" if el.name == "image" else "link", el)
                elif attr.startswith("data-"):
                    stripped = value.lstrip()
                    if stripped[:1] in ("{", "[") and self._json_attr(
                        value, base, "data-settings" if attr == "data-settings" else "data-attr", el
                    ):
                        continue
                    if "url(" in value:
                        for url, _ in css_urls(value):
                            self.add(url, base, "data-src", el)
                    elif looks_like_image_url(value):
                        self.add(value, base, "logo" if logo else "data-src", el)
                    elif el.name == "iframe" and attr in ("data-src", "data-lazy-src") and value.startswith(("http", "//")):
                        self.add_embed("iframe", urljoin(base, value), el)

        for style in soup.find_all("style"):
            self.add_css(style.get_text(), base, "css-inline")

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.get_text())
            except ValueError:
                continue
            walk_json(data, lambda s: self.add(s, base, "json-ld") if looks_like_image_url(s) else None)

        # Some parsers leave <noscript> content as text; re-parse it so lazy-load fallbacks count.
        for ns in soup.find_all("noscript"):
            if ns.find(True) is None and "<" in ns.get_text():
                inner = BeautifulSoup(ns.get_text(), "lxml")
                for img in inner.find_all("img"):
                    self.add(img.get("src"), base, "img", ns)
                    if img.get("srcset"):
                        self.add(largest_srcset(img["srcset"]), base, "srcset", ns)

    @staticmethod
    def _is_logo(el: Tag) -> bool:
        node = el
        for _ in range(5):
            if not isinstance(node, Tag):
                break
            for c in classes(node):
                if c in LOGO_CLASSES or c.endswith("site-logo"):
                    return True
            node = node.parent
        return False


# --------------------------------------------------------------------------- downloads


@dataclass
class ImageGroup:
    """One distinct image: the original plus every resized variant found on the page."""
    key: str
    originals: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    download_url: str | None = None
    content_type: str = ""
    local_path: str | None = None
    error: str | None = None

    def candidates(self) -> list[str]:
        def area(url: str) -> int:
            m = RESIZED_RE.match(Path(unquote(urlsplit(url).path)).name)
            return int(m["w"]) * int(m["h"]) if m else 10**12

        return unique(self.originals + sorted(self.variants, key=area, reverse=True))


def original_candidates(url: str) -> list[str]:
    """Likely full-size originals for a WordPress image URL (may be empty)."""
    parts = urlsplit(url)
    out = []
    if re.fullmatch(r"i[0-3]\.wp\.com", parts.hostname or ""):  # Jetpack Photon CDN
        direct = "https://" + parts.path.lstrip("/")
        out.append(direct)
        parts = urlsplit(direct)
    path = parts.path
    if "/uploads/" in path:
        directory, _, name = path.rpartition("/")
        m = RESIZED_RE.match(unquote(name))
        if m:
            original = urlunsplit((parts.scheme, parts.netloc, f"{directory}/{m['stem']}{m['ext']}", "", ""))
            out.insert(0, original)
    return out


def group_key(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(("", bare_host(parts.hostname) + (f":{parts.port}" if parts.port else ""), parts.path, "", ""))


def group_images(urls: list[str]) -> tuple[list[ImageGroup], dict[str, ImageGroup]]:
    groups: dict[str, ImageGroup] = {}
    by_url: dict[str, ImageGroup] = {}
    for url in urls:
        originals = original_candidates(url)
        key = group_key(originals[0] if originals else url)
        group = groups.setdefault(key, ImageGroup(key))
        group.originals = unique(group.originals + originals)
        group.variants = unique(group.variants + [url])
        by_url[url] = group
    return list(groups.values()), by_url


def download_to(session, url: str, dest: Path, referer: str, max_bytes: int, check_image: bool):
    """Download url to dest. Returns content-type, raises FetchError on failure."""
    headers = {"Referer": referer, "Accept": IMAGE_ACCEPT if check_image else "text/css,*/*;q=0.1"}
    resp = http_get(session, url, stream=True, headers=headers)
    with resp:
        if resp.status_code != 200:
            raise FetchError("http_error", f"HTTP {resp.status_code}")
        ctype = resp.headers.get("Content-Type", "")
        if int(resp.headers.get("Content-Length") or 0) > max_bytes:
            raise FetchError("too_large", f"larger than {max_bytes // 1_048_576} MB")
        size, head = 0, b""
        try:
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(65536):
                    if len(head) < 4096:
                        head += chunk[: 4096 - len(head)]
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError("too_large", f"larger than {max_bytes // 1_048_576} MB")
                    fh.write(chunk)
        except requests.RequestException as exc:
            dest.unlink(missing_ok=True)
            raise FetchError(classify_exception(exc), short_error(exc)) from exc
        except FetchError:
            dest.unlink(missing_ok=True)
            raise
    if check_image and not sniff_image(head, ctype):
        dest.unlink(missing_ok=True)
        raise FetchError("not_image", f"not an image (Content-Type {ctype or 'missing'})")
    if not check_image and b"<html" in head.lower():
        dest.unlink(missing_ok=True)
        raise FetchError("not_css", "got an HTML page instead of CSS")
    return ctype


def download_group(session, group: ImageGroup, tmp: Path, referer: str) -> None:
    errors = []
    for candidate in group.candidates():
        try:
            group.content_type = download_to(session, candidate, tmp, referer, MAX_IMAGE_BYTES, True)
            group.download_url = candidate
            return
        except Exception as exc:  # noqa: BLE001 - keep trying the other candidates
            errors.append(f"{candidate} -> {exc}")
    group.error = " | ".join(errors)[:1000]


def local_name_for(url: str, content_type: str, fallback: str) -> str:
    name = safe_filename(unquote(urlsplit(url).path.rsplit("/", 1)[-1])) or fallback
    if not Path(name).suffix:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        name += {".jpe": ".jpg"}.get(ext, ext)
    return name


def should_fetch_css(url: str) -> bool:
    path = urlsplit(url).path.lower()
    if "/elementor/css/" in path:
        return True
    # Generated/combined CSS (uploads, cache plugins) — not plugin or theme assets.
    return "/wp-content/" in path and "/wp-content/plugins/" not in path and "/wp-content/themes/" not in path


def stylesheet_urls(soup: BeautifulSoup, base: str) -> list[str]:
    urls = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        if "stylesheet" in rel or ("preload" in rel and link.get("as") == "style"):
            urls.append(urljoin(base, link["href"].strip()))
    return unique(u for u in urls if should_fetch_css(u))


# --------------------------------------------------------------------------- rendering


RENDER_BG_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('*')) {
    for (const pseudo of [null, '::before', '::after']) {
      const bg = getComputedStyle(el, pseudo).backgroundImage;
      if (!bg || bg === 'none') continue;
      const re = /url\\(["']?(.*?)["']?\\)/g;
      let m;
      while ((m = re.exec(bg))) {
        const holder = el.closest('[data-id]');
        out.push([m[1], holder ? holder.getAttribute('data-id') : null]);
      }
    }
  }
  return out;
}
"""


def launch_browser(p):
    """Playwright's own Chromium if installed, otherwise the user's Google Chrome or Edge."""
    errors = []
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            return p.chromium.launch(headless=True, **kwargs)
        except Exception as exc:  # noqa: BLE001 - try the next browser
            errors.append(str(exc).splitlines()[0][:120])
    raise RuntimeError(
        "no browser available for rendering — install Google Chrome "
        "(or run: python -m playwright install chromium). Details: " + " / ".join(errors)
    )


def render_page(url: str) -> dict:
    """Load url in headless Chromium, scroll to the bottom, return DOM + observed images."""
    from playwright.sync_api import sync_playwright  # optional dependency

    network: list[str] = []
    with sync_playwright() as p:
        browser = launch_browser(p)
        try:
            context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on("response", lambda r: network.append(r.url) if r.request.resource_type == "image" and r.ok else None)
            page.goto(url, wait_until="load", timeout=45_000)
            stable = 0
            for _ in range(80):  # scroll in steps so lazy-loaders fire
                page.evaluate("window.scrollBy(0, Math.round(window.innerHeight * 0.8))")
                page.wait_for_timeout(350)
                at_bottom = page.evaluate("window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2")
                stable = stable + 1 if at_bottom else 0
                if stable >= 3:
                    break
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:  # noqa: BLE001 - some sites never go idle
                pass
            computed = page.evaluate(RENDER_BG_JS)
            html = page.content()
            final_url = page.url
        finally:
            browser.close()
    return {"html": html, "final_url": final_url, "computed": computed, "network": network}


# --------------------------------------------------------------------------- per-site collection


def site_metadata(soup: BeautifulSoup) -> dict:
    def meta(**attrs):
        tag = soup.find("meta", attrs=attrs)
        return tag.get("content", "").strip() if tag else None

    themes = unique(
        m.group(1)
        for tag in soup.find_all(["link", "script"])
        for m in [re.search(r"/wp-content/themes/([^/]+)/", tag.get("href") or tag.get("src") or "")]
        if m
    )
    body_classes = classes(soup.body) if soup.body else []
    page_id = next((c.rsplit("-", 1)[1] for c in body_classes if re.fullmatch(r"elementor-page-\d+", c)), None)
    templates = unique(
        (t.get("data-elementor-type"), t.get("data-elementor-id"))
        for t in soup.find_all(attrs={"data-elementor-type": True})
    )
    return {
        "title": soup.title.get_text(" ", strip=True) if soup.title else None,
        "meta_description": meta(name="description"),
        "og": {
            "title": meta(property="og:title"),
            "description": meta(property="og:description"),
            "image": meta(property="og:image"),
        },
        "generator": [m.get("content") for m in soup.find_all("meta", attrs={"name": "generator"}) if m.get("content")],
        "theme": themes,
        "elementor": {
            "page_id": page_id,
            "templates": [{"type": t, "id": i} for t, i in templates],
        },
    }


def base_manifest(domain: str) -> dict:
    return {
        "domain": domain,
        "final_url": None,
        "fetched_at": utc_now(),
        "status": None,
        "http_status": None,
        "redirects": [],
        "parsed_from": None,
        "title": None,
        "meta_description": None,
        "og": {"title": None, "description": None, "image": None},
        "nav": [],
        "sections": [],
        "forms": [],
        "embeds": [],
        "stylesheets": [],
        "images": [],
        "warnings": [],
        "stats": {"sections": 0, "images_found": 0, "images_downloaded": 0, "images_failed": 0, "forms": 0},
    }


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def collect_site(domain: str, work: Path, session: requests.Session, args) -> tuple[dict, bool]:
    """Collect one site into the (empty) folder `work`. Returns (manifest, got_html)."""
    manifest = base_manifest(domain)
    warnings = manifest["warnings"]
    url = f"{args.scheme}://{domain}/"

    try:
        resp = http_get(session, url)
    except FetchError as exc:
        if exc.kind != "ssl_error" or args.scheme != "https":
            manifest["status"] = exc.kind
            warnings.append(f"{exc.kind}: {exc}")
            return manifest, False
        warnings.append(f"ssl_error: {exc}")
        try:
            resp = http_get(session, f"http://{domain}/")
            warnings.append("ssl_error: HTTPS failed, content collected over plain http")
        except FetchError as exc2:
            manifest["status"] = "ssl_error"
            warnings.append(f"http fallback also failed: {exc2}")
            return manifest, False

    final_url = resp.url
    manifest["final_url"] = final_url
    manifest["http_status"] = resp.status_code
    manifest["redirects"] = [r.url for r in resp.history]
    (work / "index.html").write_bytes(resp.content)
    raw_soup = BeautifulSoup(resp.content, "lxml")

    states, state_warnings = detect_page_state(domain, resp, raw_soup, final_url)
    warnings.extend(state_warnings)
    if any(w.startswith("ssl_error") for w in warnings):
        states.insert(0, "ssl_error")

    rendered = None
    if args.render:
        try:
            log.info("  rendering in a headless browser...")
            rendered = render_page(final_url)
            (work / "rendered.html").write_text(rendered["html"], encoding="utf-8")
        except ImportError:
            warnings.append("render_failed: playwright is not installed (pip install -r requirements-render.txt)")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"render_failed: {short_error(exc)}")
    parse_soup = BeautifulSoup(rendered["html"], "lxml") if rendered else raw_soup
    manifest["parsed_from"] = "rendered.html" if rendered else "index.html"

    meta = site_metadata(parse_soup)
    manifest.update({k: meta[k] for k in ("title", "meta_description", "og")})
    manifest["generator"], manifest["theme"], manifest["elementor"] = meta["generator"], meta["theme"], meta["elementor"]

    # Sections, nav, forms
    section_meta = find_sections(parse_soup, warnings)
    nav, nav_link_ids = extract_nav(parse_soup, final_url, section_meta)
    manifest["nav"] = nav
    nav_targets = {n["target"] for n in nav if n["target"]}
    sections = [
        build_section(m["_el"], i, m, nav_targets, nav_link_ids) for i, m in enumerate(section_meta, 1)
    ]
    manifest["sections"] = sections

    owner: dict[int, int] = {}
    id_to_section: dict[str, int] = {}
    for m, sec in zip(section_meta, sections):
        el = m["_el"]
        for node in [el, *el.find_all(True)]:
            owner[id(node)] = sec["order"]
            if node.get("data-id"):
                id_to_section[node["data-id"]] = sec["order"]

    def section_of(el) -> int | None:
        node = el
        while isinstance(node, Tag):
            if id(node) in owner:
                return owner[id(node)]
            node = node.parent
        data_id = nearest_data_id(el)
        return id_to_section.get(data_id) if data_id else None

    for target in sorted(nav_targets):
        if not parse_soup.find(id=target) and not parse_soup.find("a", attrs={"name": target}):
            warnings.append(f"nav_target_missing: menu link #{target} has no matching section on the page")

    manifest["forms"] = extract_forms(parse_soup, section_of)

    # Images from HTML (raw and rendered), CSS files, and the live browser.
    finder = ImageFinder(section_of, id_to_section)
    finder.scan_html(raw_soup, final_url)
    if rendered:
        finder.scan_html(parse_soup, rendered["final_url"])
        for img_url, data_id in rendered["computed"]:
            finder.add(img_url, rendered["final_url"], "computed-style", element_ids=[data_id] if data_id else None)
        for img_url in rendered["network"]:
            if url_ext(img_url) in IMAGE_EXTS or "/wp-content/" in img_url:
                finder.add(img_url, rendered["final_url"], "rendered-network")

    css_dir = work / "css"
    used_css: set[str] = set()
    css_list = stylesheet_urls(raw_soup, final_url)
    if rendered:
        css_list = unique(css_list + stylesheet_urls(parse_soup, rendered["final_url"]))
    for css_url in css_list:
        entry = {"source_url": css_url, "local_path": None, "status": "failed", "images": 0, "error": None}
        css_dir.mkdir(exist_ok=True)
        name = claim_name(local_name_for(css_url, "text/css", "style.css"), used_css)
        try:
            download_to(session, css_url, css_dir / name, final_url, 20 * 1024 * 1024, False)
            entry.update(local_path=f"css/{name}", status="downloaded")
            text = (css_dir / name).read_text(encoding="utf-8", errors="replace")
            entry["images"] = finder.add_css(text, css_url, "css", css_file=f"css/{name}")
        except FetchError as exc:
            used_css.discard(name.lower())
            entry["error"] = str(exc)
            warnings.append(f"css_failed: {css_url} ({exc})")
        manifest["stylesheets"].append(entry)

    manifest["embeds"] = finder.embeds

    # Download: one request stream per distinct image, original size first.
    groups, by_url = group_images(list(finder.found))
    images_dir = work / "images"
    images_dir.mkdir(exist_ok=True)
    tmp_paths = {id(g): images_dir / f".download-{i}" for i, g in enumerate(groups)}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(lambda g: download_group(session, g, tmp_paths[id(g)], final_url), groups))
    used_names: set[str] = set()
    for i, group in enumerate(groups, 1):
        if group.download_url:
            name = claim_name(local_name_for(group.download_url, group.content_type, f"image-{i}"), used_names)
            tmp_paths[id(group)].rename(images_dir / name)
            group.local_path = f"images/{name}"

    by_order = {s["order"]: s for s in sections}
    for url, found in finder.found.items():
        group = by_url[url]
        manifest["images"].append({
            "source_url": url,
            "local_path": group.local_path,
            "found_in": found.sources[0],
            "sources": found.sources,
            "download_url": group.download_url,
            "status": "downloaded" if group.local_path else "failed",
            "error": group.error if not group.local_path else None,
            "sections": sorted(found.sections),
            "element_ids": found.element_ids,
            "css_files": found.css_files,
        })
        ref = group.local_path or url
        for order in found.sections:
            sec = by_order[order]
            bucket = "background_images" if set(found.sources) & BACKGROUND_SOURCES else "images"
            if ref not in sec[bucket]:
                sec[bucket].append(ref)

    ok = sum(1 for g in groups if g.local_path)
    failed = len(groups) - ok
    if failed:
        warnings.append(f"images_failed: {failed} of {len(groups)} images could not be downloaded")

    body_text = len(visible_text(parse_soup.body)) if parse_soup.body else 0
    if body_text < LOW_TEXT_THRESHOLD and not states and not rendered:
        warnings.append(f"low_content: only {body_text} characters of visible text — try --render")

    manifest["status"] = next((s for s in ["ssl_error", *STATE_PRIORITY] if s in states), "ok")
    manifest["stats"] = {
        "sections": len(sections),
        "images_found": len(groups),
        "images_downloaded": ok,
        "images_failed": failed,
        "forms": len(manifest["forms"]),
    }
    return manifest, True


def run_site(domain: str, out_root: Path, session, args) -> dict:
    """Collect into a temp folder, then swap it into place (never loses a good copy)."""
    site_dir = out_root / dir_name(domain)
    work = out_root / f".{dir_name(domain)}.tmp"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        manifest, got_html = collect_site(domain, work, session, args)
    except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
        log.error("  unexpected error: %s", short_error(exc))
        log.debug(traceback.format_exc())
        manifest, got_html = base_manifest(domain), False
        manifest["status"] = "error"
        manifest["warnings"].append(f"error: {short_error(exc)}")
        for leftover in work.iterdir():
            shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()

    if not got_html and (site_dir / "index.html").exists():
        log.warning("  %s now (%s); keeping the previous collection", manifest["status"], manifest["warnings"][-1])
        shutil.rmtree(work, ignore_errors=True)
        manifest["kept_previous"] = True
        return manifest

    write_json(work / "manifest.json", manifest)
    shutil.rmtree(site_dir, ignore_errors=True)
    work.rename(site_dir)
    return manifest


# --------------------------------------------------------------------------- report & CLI


def load_manifest(out_root: Path, domain: str) -> dict | None:
    path = out_root / dir_name(domain) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def build_report_rows(out_root: Path, domains: list[str], include_extras: bool = True) -> list[dict]:
    """One row per domain (plus any other collected sites found in out_root)."""
    names = list(domains)
    if include_extras and out_root.exists():
        for mpath in sorted(out_root.glob("*/manifest.json")):
            try:
                extra = json.loads(mpath.read_text(encoding="utf-8")).get("domain")
            except ValueError:
                continue
            if extra and extra not in names:
                names.append(extra)
    rows = []
    for domain in names:
        m = load_manifest(out_root, domain)
        if m is None:
            rows.append({**{k: "" for k in REPORT_FIELDS}, "domain": domain, "status": "not_collected"})
            continue
        stats = m.get("stats", {})
        rows.append({
            "domain": domain,
            "status": m.get("status"),
            "final_url": m.get("final_url") or "",
            "sections": stats.get("sections", 0),
            "images_found": stats.get("images_found", 0),
            "images_downloaded": stats.get("images_downloaded", 0),
            "images_failed": stats.get("images_failed", 0),
            "forms": stats.get("forms", 0),
            "warnings": " | ".join(m.get("warnings", [])),
        })
    return rows


def write_report(out_root: Path, domains: list[str], report_path: Path) -> list[dict]:
    rows = build_report_rows(out_root, domains)
    with open(report_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


@dataclass
class Options:
    """Run settings shared by the command line and the desktop app."""
    force: bool = False
    retry_failed: bool = False
    render: bool = False
    workers: int = 4
    min_delay: float = 1.0
    max_delay: float = 2.0
    scheme: str = "https"


@dataclass
class RunSummary:
    collected: int = 0
    skipped: int = 0
    stopped: bool = False
    rows: list[dict] = field(default_factory=list)


def run_collection(selected: list[str], all_domains: list[str], out_root: Path, report_path: Path,
                   opts: Options, *, on_start=None, on_done=None, stop=None) -> RunSummary:
    """Collect the selected domains, then rewrite the report for all_domains.

    on_start(index, total, domain) is called before a site is fetched, and
    on_done(index, total, domain, manifest, skipped) after it. Setting the
    threading.Event `stop` ends the run after the current site.
    """
    out_root.mkdir(parents=True, exist_ok=True)
    summary = RunSummary()
    session = make_session(opts.workers)
    total = len(selected)
    try:
        for index, domain in enumerate(selected, 1):
            if stop is not None and stop.is_set():
                summary.stopped = True
                break
            existing = load_manifest(out_root, domain)
            redo = opts.force or opts.render or (opts.retry_failed and existing and existing.get("status") != "ok")
            if existing and not redo:
                summary.skipped += 1
                log.info("[%d/%d] %s — already collected (%s), skipping", index, total, domain, existing.get("status"))
                if on_done:
                    on_done(index, total, domain, existing, True)
                continue
            if summary.collected:
                delay = random.uniform(opts.min_delay, opts.max_delay)
                if stop is not None:
                    if stop.wait(delay):
                        summary.stopped = True
                        break
                else:
                    time.sleep(delay)
            if on_start:
                on_start(index, total, domain)
            log.info("[%d/%d] %s", index, total, domain)
            m = run_site(domain, out_root, session, opts)
            s = m["stats"]
            log.info("  -> %s | %d sections | images %d/%d downloaded | %d forms%s",
                     m["status"], s["sections"], s["images_downloaded"], s["images_found"], s["forms"],
                     f" | {len(m['warnings'])} warnings" if m["warnings"] else "")
            summary.collected += 1
            if on_done:
                on_done(index, total, domain, m, False)
    except KeyboardInterrupt:
        summary.stopped = True
    finally:
        session.close()
    if summary.stopped:
        log.warning("Stopped — the report covers what has been collected so far.")
    log.info("\nCollected %d site(s), skipped %d already-collected site(s).", summary.collected, summary.skipped)
    summary.rows = write_report(out_root, all_domains, report_path)
    return summary


def log_report_summary(rows: list[dict], render_hint: str) -> None:
    problems = [r for r in rows if r["status"] != "ok"]
    log.info("Report: %d site(s), %d ok, %d need attention.", len(rows), len(rows) - len(problems), len(problems))
    if problems:
        log.info("\nSites that are not 'ok':")
        for r in problems:
            first = r["warnings"].split(" | ")[0] if r["warnings"] else ""
            log.info("  %-35s %-20s %s", r["domain"], r["status"], first[:100])
    low = [r["domain"] for r in rows if "low_content" in (r["warnings"] or "")]
    if low:
        log.info("\nPossibly missing content — %s", render_hint.format(domains=" ".join(low), only=" ".join(f"--only {d}" for d in low)))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Collect one-page WordPress/Elementor sites into sites/<domain>/.")
    p.add_argument("--only", action="append", metavar="DOMAIN", help="collect just this domain (repeatable)")
    p.add_argument("--limit", type=int, metavar="N", help="only process the first N domains in sites.txt")
    p.add_argument("--force", action="store_true", help="re-collect sites that were already collected")
    p.add_argument("--retry-failed", action="store_true", help="re-collect sites whose last status was not ok")
    p.add_argument("--render", action="store_true",
                   help="also load the page in headless Chromium and save rendered.html (implies re-collecting)")
    p.add_argument("--report-only", action="store_true", help="just rebuild report.csv from existing manifests")
    p.add_argument("--sites", type=Path, default=ROOT / "sites.txt", help="input list (default: sites.txt)")
    p.add_argument("--out", type=Path, default=ROOT / "sites", help="output folder (default: sites/)")
    p.add_argument("--report", type=Path, default=ROOT / "report.csv", help="report path (default: report.csv)")
    p.add_argument("--workers", type=int, default=4, help="parallel image downloads per site (default: 4)")
    p.add_argument("--min-delay", type=float, default=1.0, help="minimum pause between sites, seconds")
    p.add_argument("--max-delay", type=float, default=2.0, help="maximum pause between sites, seconds")
    p.add_argument("--scheme", default="https", choices=["https", "http"], help=argparse.SUPPRESS)
    p.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    return p.parse_args(argv)


def setup_logging(log_path: Path | None, verbose: bool = False, console: bool = True) -> None:
    log.setLevel(logging.DEBUG)
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(logging.DEBUG if verbose else logging.INFO)
        stream.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(stream)
    if log_path is not None:
        logfile = logging.FileHandler(log_path, encoding="utf-8")
        logfile.setLevel(logging.DEBUG)
        logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(logfile)


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.report.resolve().parent / "collect.log", args.verbose)
    if not args.sites.exists():
        log.error("Input file %s not found", args.sites)
        return 2
    domains = read_sites(args.sites)

    if args.only:
        selected = unique(normalize_domain(d) for d in args.only)
        for d in selected:
            if d not in domains:
                log.warning("note: %s is not in %s", d, args.sites.name)
    else:
        selected = domains[: args.limit] if args.limit else domains

    if args.report_only:
        args.out.mkdir(parents=True, exist_ok=True)
        rows = write_report(args.out, domains, args.report)
    else:
        opts = Options(force=args.force, retry_failed=args.retry_failed, render=args.render, workers=args.workers,
                       min_delay=args.min_delay, max_delay=args.max_delay, scheme=args.scheme)
        rows = run_collection(selected, domains, args.out, args.report, opts).rows
    log_report_summary(rows, "try: python collect.py --render {only}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
