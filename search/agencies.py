"""Vacancy numbers (any company) and staffing agencies.

Wherever a vacancy number is written it is kept and shown, and the same company + the same number is the same
vacancy. Staffing agencies and contractors (Gotfriends, Ness, Elad, …) list hundreds of vacancies with the same
generic titles, so for them a title match alone never hides a vacancy.
"""
from __future__ import annotations

import re
from functools import lru_cache

from search import profile as gates
from search.profile import find_terms

DEFAULT_AGENCIES = gates.DEFAULT_PROFILE["agency_companies"]

# With a marker word: "req 145006", "Req #144671", "Position 23231", "Job ID: 1234", "משרה מס' 1234", "LinkedIn 4474177887"
_MARKED = re.compile(r"(?:\b(?:req(?:uisition)?|position|job\s*(?:id|no\.?|number)?|vacancy|linkedin)\s*(?:no\.?|number)?\s*[#:]?\s*"
                     r"|משרה\s*(?:מס['׳]?|מספר)?\s*[#:]?\s*|מס['׳]?\s*משרה\s*[#:]?\s*)(\d{3,10})\b", re.I)
# Without one, only in titles: "#598", "AllJobs #8847984", "(37477)" (closed brackets, so "(100% remote)" is not one)
_BARE = re.compile(r"#\s*(\d{3,10})\b|\((\d{4,10})\)")


def agency_key(company: str, agencies: list[str] | None = None) -> str | None:
    """The agency a company name belongs to ('Gotfriends / confidential client' -> 'gotfriends')."""
    return _agency_key(company or "", tuple(agencies if agencies is not None else DEFAULT_AGENCIES))


@lru_cache(maxsize=2048)
def _agency_key(company: str, agencies: tuple[str, ...]) -> str | None:
    hits = find_terms(company, list(agencies))
    return hits[0].casefold() if hits else None


def vacancy_numbers(text: str, marked_only: bool = False) -> list[str]:
    """Vacancy numbers written in the text, in order; years such as 2026 are not numbers.
    marked_only: only numbers next to a marker word (for long descriptions, where "#1 platform" occurs)."""
    found = [(m.start(), m.group(1)) for m in _MARKED.finditer(text or "")]
    if not marked_only:
        found += [(m.start(), m.group(1) or m.group(2)) for m in _BARE.finditer(text or "")]
    out = []
    for _, number in sorted(found):
        if (len(number) == 4 and 1990 <= int(number) <= 2100) or number in out:
            continue
        out.append(number)
    return out


# Words that do not name an employer ("NESS / confidential financial client", "Bank Acme" -> acme)
_GENERIC = {"bank", "group", "ltd", "inc", "llc", "the", "and", "for", "systems", "system", "technologies", "technology",
            "tech", "solutions", "software", "services", "israel", "israeli", "global", "labs", "company", "client",
            "clients", "employer", "end", "via", "ventures", "holdings", "international", "industries", "projects",
            "security", "cyber", "medical", "health", "healthcare", "innovations", "innovation", "lab", "networks",
            "network", "data", "cloud", "digital", "mobile", "games", "gaming", "payments", "capital", "energy",
            "robotics", "analytics", "bio", "pharma", "insurance", "finance", "fintech", "saas", "platform", "apps",
            "studio", "media", "research", "development", "consulting", "partners"}
_UNNAMED = re.compile(r"confidential|undisclosed|unnamed|stealth|חסוי", re.I)
_AD_WORDS = {"req", "requisition", "position", "official", "linkedin", "remote", "hybrid", "onsite", "full", "time",
             "part", "temporary", "senior", "junior", "lead", "manager", "engineer", "automation", "manual", "possible",
             "confirmed", "not", "drushim", "alljobs", "jobtime", "gotfriends", "greenhouse", "lever", "comeet", "ashby"}


def client_words(company: str, role: str = "", agencies: list[str] | None = None) -> frozenset[str]:
    """Words naming the employer a job is for. An agency's job: its end client, as written after " / "
    ("NESS / Bank Acme" -> acme). A name with its parent in brackets is the unit, not the parent: "Pepper (Bank
    Acme)" -> pepper, a different employer from Bank Acme. Brackets alone name the client only when nothing else
    does. An unnamed client ("confidential fintech") gives nothing. An employer's own job: the employer."""
    company, role = company or "", role or ""
    agency = agency_key(company, agencies)
    head, _, tail = company.partition(" / ")
    if not agency:
        parts = [head]
    else:  # brackets only here: elsewhere they hold cities, requisitions and notes
        parts = [tail] + role.split(" / ")[1:]
    places = {w for term in gates.ISRAEL_TERMS for w in term.split()}
    drop = _GENERIC | _AD_WORDS | places | {agency or ""} | set((agency or "").split())

    def named(parts):
        words = set()
        for part in parts:
            if part and not _UNNAMED.search(part):
                words |= set(re.findall(r"[^\W\d_]{3,}", re.sub(r"\([^)]*\)", " ", part).casefold()))
        return words - drop

    words = named(parts)
    if not words and agency:  # only the brackets say who the client is: "QA Lead (Bank Acme)"
        words = named(re.findall(r"\(([^)]{3,40})\)", f"{company} {role}"))
    return frozenset(words)


def company_key(company: str, agencies: list[str] | None = None) -> str:
    """Who numbers the vacancy: the agency ('Gotfriends / client' -> 'gotfriends'), else the company name
    before any ' / ' ('Dialog / confidential fintech' -> 'dialog')."""
    agency = agency_key(company, agencies)
    if agency:
        return agency
    return re.sub(r"\s+", " ", (company or "").split(" / ")[0].casefold()).strip()
