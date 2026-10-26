"""Find official job boards for a list of companies (one-off, on request).

For each company a few likely board names are tried on Greenhouse, Lever and Ashby. A board is added only
when it currently lists at least one job in Israel: that both confirms it is the right company (not a
namesake) and that it is worth checking. Comeet boards need the company's internal ID, which cannot be
guessed: the known ones are listed in COMEET_BOARDS (taken from public Comeet job pages), and any other is
added by pasting one of its job links.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from app import db
from search import profile as gates
from search import sources

# Israeli tech companies (public knowledge, no personal data). Extra board names where the obvious one differs.
CANDIDATES: dict[str, list[str]] = {
    "Wix": [], "monday.com": ["mondaycom", "monday"], "Fiverr": [], "Lemonade": [], "Riskified": [], "Payoneer": [],
    "JFrog": [], "Snyk": [], "Taboola": [], "Outbrain": [], "Gong": ["gongio"], "HiBob": ["hibob"],
    "Papaya Global": [], "Cato Networks": [], "Armis": ["armissecurity"], "Wiz": ["wizinc"],
    "Transmit Security": [], "Pagaya": [], "Melio": ["meliopayments"], "Tipalti": [], "Rapyd": [], "Similarweb": [],
    "AppsFlyer": [], "Yotpo": [], "CyberArk": [], "Check Point": ["checkpoint"], "SentinelOne": [],
    "Cybereason": [], "Orca Security": [], "Axonius": [], "Claroty": [], "Pentera": [], "Cymulate": [],
    "Island": ["islandio"], "Apiiro": [], "Aqua Security": [], "Lightricks": [], "Playtika": [], "Plarium": [],
    "eToro": [], "Nayax": [], "WalkMe": [], "Sisense": [], "Optimove": [], "HoneyBook": [], "Guesty": [],
    "Lusha": [], "Via": ["ridewithvia"], "Gett": [], "Dynamic Yield": [], "Forter": [], "Next Insurance": [],
    "Justt": [], "Bluevine": [], "Zesty": ["zestytech"], "Firebolt": [], "Coralogix": [], "Komodor": [],
    "Port": ["getport"], "Tabnine": [], "Run:ai": ["runai"], "Hailo": [], "D-ID": ["did"], "Healthy.io": [],
    "Kaltura": [], "Zencity": [], "Viz.ai": [], "Hunters": ["huntersai"], "Cyera": [], "Noname Security": [],
    "Salt Security": [], "Oligo Security": ["oligo"], "Upwind": ["upwindsecurity"], "Torq": [], "BigID": [],
    "VAST Data": [], "Tufin": [], "Riverside": ["riversidefm"], "Hippo": [],
}
PROVIDERS = ("greenhouse", "lever", "ashby")

# Comeet (shown as "Spark Hire Recruit" on some pages) is the most common ATS among Israeli companies. Board =
# company page name / internal ID, as in https://www.comeet.com/jobs/vastdata/43.001 (public pages, 2026-10-15).
COMEET_BOARDS: dict[str, str] = {
    "VAST Data": "vastdata/43.001", "Claroty": "Claroty/F2.004", "Quantum Machines": "quantummachines/d6.000",
    "Dream": "dreamgroup/99.002", "Paragon": "paragon/76.006", "SafeBreach": "safebreach/53.004",
    "Kaltura": "kaltura/E2.00D", "Blink": "blinkops/C7.004", "Coralogix": "coralogix/06.004",
    "ScyllaDB": "scylladb/E4.006", "Vega": "vega/C9.009", "Global-e": "global-e/62.002",
    "Discount Bank": "dbank/F8.004", "INFINIDAT": "infinidat/D6.003", "SolarEdge": "solaredge/71.00A",
    "Checkmarx": "checkmarx/C0.008", "Cognyte": "cognyte/F2.009", "PTC": "ptc/32.005",
    "ONE ZERO Bank": "onezerobank/36.00A", "Kela": "kelasys/2A.007", "Keshet 12": "keshet/26.009",
    "abra": "abra_rnd/15.007", "eToro": "etoro/41.009", "KPMG Somekh Chaikin": "somekhchaikin/F3.007",
    "Deloitte Israel": "deloitte/F7.00B", "Push Operations": "pushoperations/3A.006", "Globalbit": "globalbit/D5.006",
    "888": "888jobs/E2.001", "Noma Security": "noma_security/3A.005", "Zenity": "zenity/19.000",
    "Upwind Security": "upwind/49.004", "OX Security": "ox_security/f8.006", "UVeye": "uveye/92.00F",
    "BioCatch": "biocatch/03.00E", "DRS RADA": "rada/73.009", "Fetcherr": "fetcherr/68.006",
    "Majestic Labs": "majesticlabs/AA.004", "Genpact Israel": "genpact/B9.008", "Nova": "nova/A5.007",
    "Surecomp": "Surecomp/24.00E", "Team8": "team8/61.003", "Guideline": "guideline/89.009",
    "Rapid Medical": "rapidmedical/5A.003", "Augmedics": "augmedics/26.00C",
}


@dataclass
class Found:
    company: str
    provider: str | None = None
    board: str | None = None
    jobs: int = 0
    israel_jobs: int = 0
    note: str = ""


def board_names(company: str, extra: list[str]) -> list[str]:
    plain = re.sub(r"[^a-z0-9]", "", company.casefold())
    dashed = re.sub(r"[^a-z0-9]+", "-", company.casefold()).strip("-")
    names = []
    for name in [plain, *extra, dashed]:
        if name and name not in names and sources.BOARD_PATTERN.fullmatch(name):
            names.append(name)
    return names


def probe(company: str, extra: list[str], get=sources.http_get, sleep=time.sleep, delay: float = 0.3) -> Found:
    best = Found(company, note="no public Greenhouse/Lever/Ashby board found")
    for name in board_names(company, extra):
        for provider in PROVIDERS:
            sleep(delay)
            try:
                postings = sources.FETCHERS[provider](name, get)
            except sources.SourceError:
                continue
            if not postings:
                continue
            israel = sum(1 for p in postings
                         if gates.geography(p.location, p.countries, p.remote).value in gates.GEO_ELIGIBLE)
            found = Found(company, provider, name, len(postings), israel)
            if israel:
                found.note = "added"
                return found
            if best.provider is None:
                best = Found(company, provider, name, len(postings), 0, "board found, but no jobs in Israel now")
    return best


def probe_comeet(company: str, board: str, get=sources.http_get, sleep=time.sleep, delay: float = 0.3) -> Found:
    sleep(delay)
    try:
        postings = sources.fetch_comeet(board, get)
    except sources.SourceError as e:
        return Found(company, note=f"Comeet board not readable: {e}"[:80])
    israel = sum(1 for p in postings if gates.geography(p.location, p.countries, p.remote).value in gates.GEO_ELIGIBLE)
    return Found(company, "comeet", board, len(postings), israel,
                 "added" if israel else "board found, but no jobs in Israel now")


def discover(conn: sqlite3.Connection, candidates: dict[str, list[str]] | None = None, get=sources.http_get,
             sleep=time.sleep, dry_run: bool = False, comeet: dict[str, str] | None = None) -> list[Found]:
    """Try every candidate company; add the boards that list jobs in Israel now. With no arguments: the built-in
    company list (Greenhouse, Lever, Ashby) and the known Comeet boards."""
    if candidates is None and comeet is None:
        comeet = COMEET_BOARDS
    known = {(r["provider"], r["board"].casefold()) for r in conn.execute("SELECT provider, board FROM search_sources")}
    known_companies = {r["company"].casefold() for r in conn.execute("SELECT company FROM search_sources")}
    todo = [(company, lambda c=company, e=extra: probe(c, e, get, sleep))
            for company, extra in (CANDIDATES if candidates is None else candidates).items()]
    todo += [(company, lambda c=company, b=board: probe_comeet(c, b, get, sleep)) for company, board in (comeet or {}).items()]
    results = []
    for company, run_probe in todo:
        if company.casefold() in known_companies:
            results.append(Found(company, note="already on your list"))
            continue
        found = run_probe()
        if found.israel_jobs and (found.provider, found.board.casefold()) in known:
            found.note = "already on your list"
        elif found.israel_jobs and not dry_run:
            with db.transaction(conn):
                conn.execute("""INSERT OR IGNORE INTO search_sources(provider, board, company, enabled, origin, added_at_utc)
                                VALUES (?, ?, ?, 1, 'found: has jobs in Israel', ?)""",
                             (found.provider, found.board, company, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            known.add((found.provider, found.board.casefold()))
            known_companies.add(company.casefold())
        elif found.israel_jobs:
            found.note = "would be added"
        results.append(found)
    return results
