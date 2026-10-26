"""Where to apply, in order of preference: the employer's own site or ATS, then LinkedIn, then a job board.

The official boards the search reads are the first step already. For a record or lead whose link is LinkedIn
or a job board, the employer's own posting is looked up and suggested next to it; the original link is always
kept. Only postings on the employer's official board at the latest check are suggested, and only for the same
company with the same job ID or a very similar title. Agencies are skipped: their end client is usually not named.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from search import agencies

EMPLOYER, LINKEDIN, JOB_BOARD = "employer", "linkedin", "job board"
CHANNEL_ORDER = {EMPLOYER: 0, LINKEDIN: 1, JOB_BOARD: 2}
CHANNEL_LABELS = {EMPLOYER: "Employer site", LINKEDIN: "LinkedIn", JOB_BOARD: "Job board"}
JOB_BOARD_HOSTS = ("indeed.", "tsenta.com", "drushim.co.il", "alljobs.co.il", "jobmaster.co.il",
                   "glassdoor.", "echojobs.io", "jobnet.co.il", "jobtime.co.il", "gotfriends.co.il", "jooble.",
                   "talent.com", "builtin.com", "wellfound.com", "comeet.co/jobs/search")
AGENCY_SITES = ("gotfriends.co.il",)  # a staffing agency's own job list: ranked with job boards, named as what it is
TITLE_MATCH = 0.5
LINKEDIN_ISRAEL = "101620260"  # LinkedIn's location ID for Israel


@dataclass
class OfficialMatch:
    posting: sqlite3.Row
    company: str
    reason: str


def channel(url: str | None) -> str:
    """employer (its own site or ATS), linkedin, or job board (a third-party aggregator)."""
    host = (urlsplit(url or "").hostname or "").casefold()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return LINKEDIN
    if any(a in host or a in (url or "").casefold() for a in JOB_BOARD_HOSTS):
        return JOB_BOARD
    return EMPLOYER


def is_aggregator(url: str | None) -> bool:
    """LinkedIn or a job board: not the employer's own site."""
    return channel(url) != EMPLOYER


def ordered_links(urls: list[str]) -> list[str]:
    """Distinct links, best first: employer, LinkedIn, job board; within each, a link that names a specific
    job (a number or ID in its path) before a general careers page; otherwise in the order given."""
    distinct = list(dict.fromkeys(u.rstrip(".") for u in urls))
    specific = lambda u: bool(re.search(r"\d", urlsplit(u).path + urlsplit(u).query))
    return sorted(distinct, key=lambda u: (CHANNEL_ORDER[channel(u)], not specific(u)))


ATS_NAMES = {"greenhouse.io": "Greenhouse", "lever.co": "Lever", "ashbyhq.com": "Ashby", "comeet.com": "Comeet",
             "comeet.co": "Comeet", "myworkdayjobs.com": "Workday", "breezy.hr": "Breezy", "smartrecruiters.com": "SmartRecruiters"}


def link_label(url: str) -> str:
    """'Employer ATS · Greenhouse', 'Employer site · spotower.com', 'LinkedIn' or 'Job board · drushim.co.il'."""
    ch = channel(url)
    host = (urlsplit(url or "").hostname or "").casefold().removeprefix("www.")
    if ch == LINKEDIN:
        return "LinkedIn"
    if ch == JOB_BOARD:
        return f"{'Agency site' if any(host.endswith(a) for a in AGENCY_SITES) else 'Job board'} · {host}"
    ats = next((name for suffix, name in ATS_NAMES.items() if host == suffix or host.endswith("." + suffix)), None)
    return f"Employer ATS · {ats}" if ats else f"Employer site · {host or 'link'}"


def linkedin_search(company: str, title: str) -> str:
    """A LinkedIn Jobs search you open yourself (the next step when no employer posting is found)."""
    role = re.split(r"\s[—–-]\s|\(", title or "", maxsplit=1)[0].strip()
    company = (company or "").split(" / ")[0].strip()
    return "https://www.linkedin.com/jobs/search/?" + urlencode({"keywords": f"{company} {role}".strip(),
                                                                 "geoId": LINKEDIN_ISRAEL})


# Aggregators left out of the employer search: the point is the employer's own page.
_NOT_EMPLOYER = ["linkedin.com", "drushim.co.il", "alljobs.co.il", "jobify360.co.il", "glassdoor.com", "indeed.com",
                 "jobmaster.co.il", "builtin.com"]


def employer_search(company: str, title: str) -> str:
    """A web search for the job on the employer's own site or ATS, opened by you in your browser (nothing is
    fetched by the console): the first step when no board the console reads lists the job."""
    role = re.split(r"\s[—–-]\s|\(", title or "", maxsplit=1)[0].strip()
    company = (company or "").split(" / ")[0].strip()
    query = f'"{role}" "{company}" careers ' + " ".join(f"-site:{s}" for s in _NOT_EMPLOYER)
    return "https://www.google.com/search?" + urlencode({"q": query})


def _title_words(title: str) -> set[str]:
    # the part before an em dash or "(" is the role; the rest is often location or notes in the tracker
    core = re.split(r"\s[—–-]\s|\(", title or "", maxsplit=1)[0]
    return {w for w in re.findall(r"[^\W\d_]{2,}", core.casefold())} - {"and", "the", "for", "of", "in", "at"}


def title_similarity(a: str, b: str) -> float:
    wa, wb = _title_words(a), _title_words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


class OfficialIndex:
    """Postings listed now on official boards, grouped by company, for quick lookups."""

    def __init__(self, conn: sqlite3.Connection, agency_terms: list[str]):
        self.agency_terms = agency_terms
        self.by_company: dict[str, list[sqlite3.Row]] = {}
        for p in conn.execute("""
                SELECT p.*, s.company FROM postings p JOIN search_sources s ON s.id = p.source_id
                WHERE p.last_run_id = (SELECT c.run_id FROM source_checks c WHERE c.source_id = p.source_id AND c.ok = 1
                                       ORDER BY c.id DESC LIMIT 1)"""):
            self.by_company.setdefault(agencies.company_key(p["company"], []), []).append(p)

    def find(self, company: str, title: str, job_ids: list[str] | None = None, limit: int = 3) -> list[OfficialMatch]:
        if agencies.agency_key(company, self.agency_terms):
            return []
        found = []
        for p in self.by_company.get(agencies.company_key(company, []), []):
            ids = agencies.vacancy_numbers(" ".join(filter(None, [p["title"], p["requisition_id"] and f"req {p['requisition_id']}"])))
            if job_ids and set(job_ids) & set(ids):
                found.append((2.0, OfficialMatch(p, p["company"], f"same job ID #{(set(job_ids) & set(ids)).pop()}")))
                continue
            score = title_similarity(title, p["title"])
            if score >= TITLE_MATCH:
                found.append((score, OfficialMatch(p, p["company"], f"similar title ({round(score * 100)}% of words)")))
        return [m for _, m in sorted(found, key=lambda pair: -pair[0])[:limit]]
