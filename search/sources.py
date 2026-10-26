"""Official ATS job boards: Greenhouse, Lever, Ashby, Comeet, Workday. Public, free, no API key.

Only these hosts are ever contacted. A failed request (403, timeout, changed format) is recorded as a
failed check of that source; it never means that a vacancy closed.
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

USER_AGENT = "CareerConsole/0.4 (personal job search; a few requests per day)"
TIMEOUT_S = 20
MAX_BYTES = 15 * 1024 * 1024
ALLOWED_HOSTS = {"boards-api.greenhouse.io", "api.lever.co", "api.eu.lever.co", "api.ashbyhq.com", "www.comeet.com"}
WORKDAY_HOST = re.compile(r"[a-z0-9-]+\.wd\d{1,2}\.myworkdayjobs\.com")   # <company>.wd5.myworkdayjobs.com: its own careers site
BOARD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")
COMEET_BOARD = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]{0,80})/([0-9A-F]{2}\.[0-9A-F]{3})", re.I)


class SourceError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Posting:
    provider: str
    provider_job_id: str
    title: str
    url: str
    location: str = ""
    countries: list[str] = field(default_factory=list)
    remote: bool | None = None
    workplace: str | None = None
    apply_url: str | None = None
    requisition_id: str | None = None
    posted_at: str | None = None        # when the source says it was published
    source_updated_at: str | None = None
    description: str = ""


def http_get(url: str, payload: dict | None = None) -> bytes:
    """GET an official board; with a payload, POST it as JSON (Workday's job list is a search request)."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not (parts.hostname in ALLOWED_HOSTS or WORKDAY_HOST.fullmatch(parts.hostname or "")):
        raise SourceError(f"Refusing to fetch {parts.hostname}: not an allowed official ATS host")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}
    data = None
    if payload is not None:
        data, headers["Content-Type"] = json.dumps(payload).encode(), "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        hint = " (access blocked — not proof that anything closed)" if e.code in (401, 403, 429) else ""
        raise SourceError(f"HTTP {e.code}{hint}", e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceError(f"Network error: {getattr(e, 'reason', e)}") from None
    if len(body) > MAX_BYTES:
        raise SourceError("Response too large")
    return body


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")


def html_to_text(markup: str | None) -> str:
    if not markup:
        return ""
    parser = _Text()
    parser.feed(html.unescape(markup) if "&lt;" in markup else markup)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", "".join(parser.parts))).strip()[:20000]


def _json(body: bytes):
    try:
        return json.loads(body)
    except ValueError:
        raise SourceError("Response was not the expected JSON (format may have changed)") from None


def fetch_greenhouse(board: str, get=http_get) -> list[Posting]:
    data = _json(get(f"https://boards-api.greenhouse.io/v1/boards/{quote(board)}/jobs?content=true"))
    out = []
    for j in data.get("jobs", []):
        offices = [o.get("name", "") for o in j.get("offices") or [] if o.get("name")]
        location = (j.get("location") or {}).get("name") or ""
        if offices and not location:
            location = " / ".join(offices)
        out.append(Posting("greenhouse", str(j["id"]), (j.get("title") or "").strip(), j.get("absolute_url") or "",
                           location=location, requisition_id=j.get("requisition_id"),
                           posted_at=j.get("first_published"), source_updated_at=j.get("updated_at"),
                           apply_url=j.get("absolute_url"), description=html_to_text(j.get("content"))))
    return out


def fetch_lever(board: str, get=http_get) -> list[Posting]:
    data = _json(get(f"https://api.lever.co/v0/postings/{quote(board)}?mode=json"))
    if not isinstance(data, list):
        raise SourceError("Unexpected Lever response")
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        created = j.get("createdAt")
        posted = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created / 1000)) if isinstance(created, (int, float))
                  else None)
        out.append(Posting("lever", str(j["id"]), (j.get("text") or "").strip(), j.get("hostedUrl") or "",
                           location=" / ".join(locations), countries=[j["country"]] if j.get("country") else [],
                           remote=(j.get("workplaceType") == "remote") or None, workplace=j.get("workplaceType"),
                           apply_url=j.get("applyUrl"), posted_at=posted, description=_lever_text(j)[:20000]))
    return out


def _lever_text(j: dict) -> str:
    """The whole job, as on its page: Lever keeps the opening, the lists (responsibilities, requirements) and the
    closing text in separate fields."""
    parts = [j.get("descriptionPlain") or ""]
    for section in j.get("lists") or []:
        parts.append(f"{section.get('text') or ''}\n{html_to_text(section.get('content'))}".strip())
    parts.append(j.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def fetch_ashby(board: str, get=http_get) -> list[Posting]:
    data = _json(get(f"https://api.ashbyhq.com/posting-api/job-board/{quote(board)}?includeCompensation=false"))
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        out.append(Posting("ashby", str(j["id"]), (j.get("title") or "").strip(), j.get("jobUrl") or "",
                           location=" / ".join(l for l in locations if l),
                           countries=["IL"] if (country or "").casefold() == "israel" else ([country] if country else []),
                           remote=bool(j.get("isRemote")) or None, workplace=j.get("workplaceType"),
                           apply_url=j.get("applyUrl"), posted_at=j.get("publishedAt"),
                           description=(j.get("descriptionPlain") or "")[:20000]))
    return out


def fetch_comeet(board: str, get=http_get) -> list[Posting]:
    m = COMEET_BOARD.fullmatch(board)
    if not m:
        raise SourceError("Comeet board must look like company-slug/UID, e.g. acme/AB.123")
    page = get(f"https://www.comeet.com/jobs/{quote(m.group(1))}/{m.group(2)}").decode("utf-8", "replace")
    found = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);\s*\n", page, re.S)
    if not found:
        raise SourceError("Comeet page no longer contains the positions list (format may have changed)")
    try:
        positions = json.loads(found.group(1))
    except ValueError:
        raise SourceError("Comeet positions list could not be parsed") from None
    out = []
    for p in positions:
        loc = p.get("location") or {}
        hosted = p.get("url_comeet_hosted_page") or ""
        active = p.get("url_active_page") or ""
        # The employer's page is often a generic careers list; use it only when it points at this position.
        apply_url = active if str(p["uid"]).casefold() in active.casefold() else (hosted or active)
        # The description is in the same list, as named sections (Description, Requirements, …)
        details = ((p.get("custom_fields") or {}).get("details") or []) if isinstance(p.get("custom_fields"), dict) else []
        description = "\n".join(f"{d.get('name') or ''}\n{html_to_text(d.get('value'))}".strip()
                                for d in details if isinstance(d, dict) and d.get("value"))
        out.append(Posting("comeet", str(p["uid"]), (p.get("name") or "").strip(), hosted or active,
                           location=loc.get("name") or "", countries=[loc["country"]] if loc.get("country") else [],
                           remote=(str(p.get("workplace_type") or "").casefold() == "remote") or None,
                           workplace=p.get("workplace_type"), apply_url=apply_url, description=description[:20000],
                           posted_at=None, source_updated_at=p.get("time_updated")))  # Comeet publishes no posting date
    return out


_ROLE_HEADING = re.compile(r"^\s*(about (the|this) (role|position|job)|the role|the position|role overview|job description|"
                           r"what you(['’]ll| will) do|responsibilities|your role|what we(['’]re| are) looking for|"
                           r"requirements|תיאור התפקיד|תיאור משרה|דרישות)\b", re.I)


# Workday: <tenant>.wd<N>.myworkdayjobs.com/<site>. The list is a search (POST, 20 jobs a page, newest first),
# filtered to Israel with the site's own location filter. Pages stop at jobs older than a month, and at most
# WORKDAY_MAX_PAGES are read, with a pause between them: a large employer lists hundreds of jobs in Israel.
WORKDAY_BOARD = re.compile(r"([a-z0-9-]+)\.(wd\d{1,2})/([A-Za-z0-9_-]+)")
WORKDAY_MAX_PAGES = 15
WORKDAY_PAGE_DELAY_S = 0.5
_WD_POSTED = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def _workday_israel(facets) -> tuple[str, str] | None:
    """The site's filter value for Israel (the facets are nested, and their IDs differ from site to site)."""
    for f in facets or []:
        for v in f.get("values") or []:
            if "values" in v:
                if found := _workday_israel([v]):
                    return found
            elif (v.get("descriptor") or "").strip().casefold() == "israel" and f.get("facetParameter"):
                return f["facetParameter"], v["id"]
    return None


def _workday_posted(text: str | None, now: float) -> str | None:
    m = _WD_POSTED.search(text or "")
    if not m:
        return None
    days = 0 if m.group(1).casefold() == "today" else 1 if m.group(1).casefold() == "yesterday" else int(m.group(2))
    return time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(now - days * 86400))


def fetch_workday(board: str, get=http_get) -> list[Posting]:
    m = WORKDAY_BOARD.fullmatch(board)
    if not m:
        raise SourceError(f"Not a Workday board: {board}")
    tenant, wd, site = m.groups()
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    page = lambda offset, facets: _json(get(api, {"appliedFacets": facets, "limit": 20, "offset": offset, "searchText": ""}))
    first = page(0, {})
    israel = _workday_israel(first.get("facets"))
    if israel is None:
        return []   # no jobs in Israel listed on this site now
    facets, now, out = {israel[0]: [israel[1]]}, time.time(), []
    for n in range(WORKDAY_MAX_PAGES):
        if get is http_get:
            time.sleep(WORKDAY_PAGE_DELAY_S)
        data = page(n * 20, facets)
        jobs = data.get("jobPostings") or []
        for j in jobs:
            path, bullets = j.get("externalPath") or "", j.get("bulletFields") or []
            req = next((b for b in bullets if re.fullmatch(r"[A-Z]{0,3}R?\d{4,}", b or "")), None)
            job_id = req or path.rsplit("_", 1)[-1] or path
            where = j.get("locationsText") or ""
            out.append(Posting("workday", f"{tenant}:{job_id}", (j.get("title") or "").strip(),
                               f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
                               location=where if not re.fullmatch(r"\d+ Locations?", where) else f"Israel ({where.lower()})",
                               countries=["IL"], requisition_id=req,
                               posted_at=_workday_posted(j.get("postedOn"), now)))
        if len(jobs) < 20 or n * 20 + len(jobs) >= (data.get("total") or first.get("total") or 0) \
                or any("30+" in (j.get("postedOn") or "") for j in jobs):
            break   # the last page, or jobs older than a month from here on
    return out


_WD_JOB_URL = re.compile(r"https://(([a-z0-9-]+)\.wd\d{1,2}\.myworkdayjobs\.com)/([A-Za-z0-9_-]+)(/job/\S+)")


def workday_details(url: str, get=http_get) -> tuple[str, str | None]:
    """One Workday job's description and exact posting date (the list has neither): (description, posted_at)."""
    m = _WD_JOB_URL.fullmatch(url or "")
    if not m:
        raise SourceError(f"Not a Workday job link: {url}")
    host, tenant, site, path = m.groups()
    info = _json(get(f"https://{host}/wday/cxs/{tenant}/{site}{path}")).get("jobPostingInfo") or {}
    start = info.get("startDate")
    return html_to_text(info.get("jobDescription"))[:20000], (f"{start}T00:00:00Z" if start else None)


def role_summary(description: str | None, limit: int = 480) -> str:
    """The part of a job description about the role, skipping the employer's general 'about us' opening."""
    lines = [l.strip() for l in (description or "").splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if _ROLE_HEADING.match(l) and len(l) < 60), None)
    text = " ".join(lines[start:] if start is not None else lines)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def apply_destination(url: str | None, company: str, provider: str) -> str:
    """Where the apply button goes, in words: the employer's site, or the employer's own form on its ATS."""
    host = (urlsplit(url or "").hostname or "").removeprefix("www.")
    if any(host.endswith(ats) for ats in ("greenhouse.io", "lever.co", "ashbyhq.com", "comeet.com", "comeet.co",
                                          "myworkdayjobs.com")):
        return f"{company}'s own form ({PROVIDER_NAMES.get(provider, provider)})"
    return host or "employer site"


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby, "comeet": fetch_comeet,
            "workday": fetch_workday}
PROVIDER_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "comeet": "Comeet", "workday": "Workday"}

_DETECT = [
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("comeet", re.compile(r"comeet\.com/jobs/([A-Za-z0-9_.-]+/[0-9A-F]{2}\.[0-9A-F]{3})", re.I)),
    ("workday", re.compile(r"([a-z0-9-]+\.wd\d{1,2})\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
]


def _board(provider: str, m: re.Match) -> str | None:
    if provider == "workday":   # tenant.wdN/site
        return f"{m.group(1).casefold()}/{m.group(2)}" if m.group(2).casefold() not in ("wday", "job", "details") else None
    return m.group(1) if provider == "comeet" or BOARD_PATTERN.fullmatch(m.group(1)) else None


def detect_board(url: str) -> tuple[str, str] | None:
    """A job or board URL -> (provider, board). None for anything we cannot fetch officially."""
    for provider, pattern in _DETECT:
        m = pattern.search(url or "")
        if m and (board := _board(provider, m)):
            return provider, board
    return None


def find_boards(text: str) -> list[tuple[str, str]]:
    found = []
    for provider, pattern in _DETECT:
        for m in pattern.finditer(text or ""):
            item = (provider, _board(provider, m))
            if item[1] and item not in found:
                found.append(item)
    return found


# Provider-level job identities, used to recognise postings that are already in the tracker.
_JOB_IDS = [
    ("greenhouse", re.compile(r"greenhouse\.io/[A-Za-z0-9_-]+/jobs/(\d{5,})", re.I)),
    ("greenhouse", re.compile(r"[?&](?:gh_jid|token)=(\d{5,})", re.I)),
    ("lever", re.compile(r"lever\.co/[A-Za-z0-9_.-]+/([0-9a-f]{8}-[0-9a-f-]{27,})", re.I)),
    ("ashby", re.compile(r"ashbyhq\.com/[A-Za-z0-9_.%-]+/([0-9a-f]{8}-[0-9a-f-]{27,})", re.I)),
    # LinkedIn job IDs: a job from a LinkedIn alert e-mail is known when your tracker names its link or number
    ("linkedin", re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[a-z0-9%-]*-)?(\d{8,12})", re.I)),
    ("linkedin", re.compile(r"\bLinkedIn\s*(?:job\s*)?#\s*(\d{9,11})\b", re.I)),
    # Drushim job numbers: a job from a Drushim alert is known when your tracker names its link or number
    ("drushim", re.compile(r"drushim\.co\.il/job/(\d{6,10})\b", re.I)),
    ("drushim", re.compile(r"\b(?:Drushim|דרושים)\s*(?:IL\s*)?(?:job\s*)?#\s*(\d{6,10})\b", re.I)),
]
# Workday job IDs (JR1234567, R12345) are unique within a company: the identity includes the company.
_WORKDAY_JOB = re.compile(r"([a-z0-9-]+)\.wd\d{1,2}\.myworkdayjobs\.com/\S*?_([A-Z]{0,3}R?\d{4,})\b", re.I)
# Comeet position IDs are short and only unique within a company, so the identity includes the company UID.
_COMEET_JOB = re.compile(r"comeet\.com/jobs/[A-Za-z0-9_.-]+/([0-9A-F]{2}\.[0-9A-F]{3})/[^/\s]*/([0-9A-F]{2}\.[0-9A-F]{3})", re.I)


def job_identities(text: str) -> set[str]:
    """Identities a false match could hide a new job with, so only unambiguous official IDs count."""
    ids = {f"{provider}:{m.group(1).lower()}" for provider, pattern in _JOB_IDS for m in pattern.finditer(text or "")}
    ids |= {f"comeet:{m.group(1).upper()}:{m.group(2).upper()}" for m in _COMEET_JOB.finditer(text or "")}
    ids |= {f"workday:{m.group(1).lower()}:{m.group(2).lower()}" for m in _WORKDAY_JOB.finditer(text or "")}
    return ids


def posting_identity(provider: str, board: str, provider_job_id: str) -> str:
    if provider == "comeet":
        return f"comeet:{board.split('/')[-1].upper()}:{provider_job_id.upper()}"
    return f"{provider}:{provider_job_id.lower()}"
