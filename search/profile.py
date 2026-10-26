"""Search profile and the deterministic gates: keyword relevance, Israel geography, freshness.

No AI and no scoring model: every verdict comes with the exact term or rule that produced it, so a
decision can be read, questioned and overridden.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import datetime, timezone

# Default profile, translated from the screening rules in the tracker (see docs/SEARCH.md):
# "Software-only discovery": software QA for web, mobile, API, SaaS, AI, fintech; never hardware,
# embedded, IoT, firmware, HIL, manufacturing quality or medical-device regulatory QA.
# "Junior/manual-only filter": low priority, not excluded.
DEFAULT_PROFILE = {
    # "verification" alone is not QA (user, 2026-10-16: verification engineers are hardware or network roles);
    # a title with it and a QA word still matches through the QA word.
    "include_terms": ["qa", "quality assurance", "quality", "test", "tester", "testing", "sdet", "automation", "validation",
                      "בודק", "בודקת", "בדיקות", "אבטחת איכות", "אוטומציה", "тестировщик", "тестирование",
                      "автоматизация"],  # "automation" in all three languages (Hebrew was missing until 2026-10-25)
    "exclude_terms": ["hardware", "embedded", "firmware", "iot", "hil", "fpga", "asic", "rtl", "silicon",
                      "semiconductor", "chip", "soc", "design verification", "manufacturing", "production quality", "process quality",
                      "quality control", "qc", "regulatory", "medical device", "pharma", "mechanical", "electrical",
                      "pcb", "rf", "technician", "supplier quality", "electromechanical"],
    "low_priority_terms": ["junior", "entry level", "intern", "internship", "student", "manual"],
    # Medical-device / pharma regulatory QA and manufacturing (hardware) quality show in the description even when
    # the title looks neutral ("Quality Engineer"). One mention is common boilerplate ("FDA-cleared"), so it only
    # marks; two or more exclude (never a software role, see software_title_terms).
    "exclude_description_terms": ["fda", "mdr", "ivdr", "iso 13485", "13485", "62304", "14971", "62366", "21 cfr",
                                  "gmp", "gxp", "510(k)", "medical device", "design controls", "capa",
                                  "notified body", "post-market surveillance",
                                  "spc", "8d", "fmea", "doe", "ncr", "mtbf", "mttr", "semiconductor", "iso 9001",
                                  "failure analysis", "process capability", "supplier audit", "supplier audits",
                                  "first article inspection", "incoming inspection", "production line", "ipc-a-610",
                                  "cleanroom", "clean room"],
    "description_min_hits": 2,
    # Medicine is fine, medical devices are not (user, 2026-10-15): for a software role (e.g. QA automation at a
    # medical-imaging AI company) regulatory terms in the description are only marked, never a reason to hide it.
    "software_title_terms": ["software", "sw", "automation", "sdet", "web", "mobile", "api", "cloud", "saas", "data",
                             "ai", "frontend", "backend", "full stack", "developer", "devops", "digital"],
    # Staffing agencies and contractors: a vacancy is its number, never its (generic) title.
    "agency_companies": ["gotfriends", "comblack", "logon", "log-on", "quality-ai", "quality ai", "elad", "ness", "tesnet",
                    "matrix", "next point", "sqlink", "ethosia", "experis", "manpower", "jobtime", "job-time", "dialog",
                    "spotower", "niloosoft", "hunter hr", "adam milo", "nisha", "infinity labs", "bynet", "malam team",
                    "john bryce", "one technologies"],
    # Your level (user, 2026-10-15): middle management — team lead, QA lead, manager, head of QA in a small company.
    # Engineer roles and director-level roles are marked, never hidden; your level is listed first.
    "focus_level_terms": ["lead", "leader", "leadership", "team lead", "tech lead", "manager", "head", "ראש צוות",
                          "מנהל", "מנהלת", "מוביל", "מובילה"],
    "above_level_terms": ["director", "vp", "vice president", "chief", "cto", "cqo", "svp", "evp"],
    "junior_level_terms": ["junior", "graduate", "intern", "internship", "student", "entry level"],
    # Requirements that identify a job across recruiters: an employer gives every recruiter the same list, so
    # records sharing several rare ones are possibly one position (user, 2026-10-15). Edit for another field.
    "requirement_terms": ["playwright", "selenium", "cypress", "appium", "detox", "react native", "cucumber", "gherkin",
                          "bdd", "java", "python", "typescript", "javascript", "c#", ".net", "kotlin", "swift", "ios",
                          "android", "postman", "graphql", "sql", "kubernetes", "docker", "aws", "azure devops", "azure",
                          "vsts", "gcp", "jenkins", "github actions", "ci/cd", "jira", "xray", "testrail", "jmeter", "k6",
                          "gatling", "load testing", "security testing", "salesforce", "sap", "oracle", "etl", "dwh",
                          "machine learning", "llm", "embedded", "linux", "networking", "microservices", "kafka",
                          "banking", "fintech", "payments", "insurance", "gaming", "healthcare", "e-commerce", "b2b",
                          "iot", "firmware", "government", "defense", "erp", "crm", "rest"],
    # Overqualified (user, 2026-10-15: 10+ years as a QA engineer, 4 of management): a job that asks at most this
    # many years of QA / engineering experience is mid-level and likely below you. Years asked for management or
    # leadership do not count (2–3 years of management is a normal lead requirement). Marked, never hidden.
    "overqualified_max_years": 3,
    # Speed matters (user, 2026-10-14): fresh up to a week. Older than two weeks is not shown as new at all
    # (user, 2026-10-15): kept, and shown again only when the employer publishes it again.
    "fresh_days": 7,
    "max_age_days": 14,
    # Places too far for you (a job only there is held back; several places or "Israel" never are).
    "exclude_places": [],
    # Regions you do not want (keys of REGIONS): a job whose places are all in them is held back.
    "exclude_regions": [],
    # Filters you turned off on the board (keys of FILTERS): the jobs they hold back are shown, marked with the rule.
    "filters_off": [],
}

# The rules that hold a relevant job back from New jobs (user, 2026-10-15: what the console filters out must be
# visible on the board, and each rule can be turned off). Your own decisions and boards you turned off are not
# here: they are your choices, undone where you made them.
FILTERS = {  # key: (name on the board, what it holds back, short mark on a job it let through when off)
    "title": ("Excluded word in the title", "a search term, but also hardware, embedded, QC, manufacturing… "
                                            "(Search settings → Exclude)", "excluded title word"),
    "description": ("Regulated or manufacturing description", "2+ medical-device or manufacturing-quality terms in "
                                                              "the description; software titles are never held back",
                    "regulated description"),
    "outside": ("Outside Israel", "the location is in another country", "outside Israel"),
    "place": ("A region or place you excluded", "in Israel, but only where it is too far for you "
                                                    "(Search settings → Regions and Places)", "excluded region"),
    "geo_unclear": ("Location unclear", "the board does not say where the job is", "location unclear"),
    "older": ("Older than {max_age} days", "published before the window of new jobs", "older"),
    "known": ("Already known", "in your tracker, or the same position as a job you decided", "already known"),
    "junior": ("Asks 1–2 years of QA", "a junior position for you: at most 2 years of QA / engineering experience "
                                       "asked, and no years of managing or leading", "asks 1–2 years"),
}

ISRAEL_TERMS = [
    "israel", "ישראל", "израиль", "tel aviv", "tel-aviv", "tel aviv-yafo", "תל אביב", "herzliya", "herzelia", "הרצליה",
    "haifa", "חיפה", "jerusalem", "ירושלים", "ra'anana", "raanana", "רעננה", "petah tikva", "petach tikva",
    "petah-tikva", "פתח תקווה", "netanya", "נתניה", "ramat gan", "רמת גן", "be'er sheva", "beer sheva", "beersheba",
    "באר שבע", "rehovot", "רחובות", "yokneam", "yokneam ilit", "יקנעם", "hod hasharon", "הוד השרון", "kfar saba",
    "כפר סבא", "ness ziona", "nes ziona", "נס ציונה", "caesarea", "קיסריה", "modiin", "מודיעין", "rishon lezion",
    "rishon le zion", "ראשון לציון", "holon", "חולון", "bnei brak", "בני ברק", "or yehuda", "אור יהודה",
    "airport city", "rosh haayin", "rosh ha'ayin", "ראש העין", "givatayim", "גבעתיים", "bat yam", "ashdod", "אשדוד",
    "ashkelon", "אשקלון", "migdal haemek", "nazareth", "yavne", "יבנה", "lod", "ramla", "kiryat gat", "kiryat ono",
    "kiryat shmona", "sderot", "omer", "matam", "petah tiqwa", "jaffa", "yafo", "glilot", "גלילות",
    "ramat hasharon", "רמת השרון", "karmiel", "כרמיאל", "tirat carmel",
]
# Israel's regions as you would pick them (Search settings → Regions): the towns in each, and the district names
# boards write ("Center District, Israel"). A town near a border is in both regions (Herzliya: Tel Aviv and Sharon).
REGIONS = {
    "tel_aviv": ("Tel Aviv & Gush Dan", [
        "tel aviv", "tel-aviv", "tel aviv-yafo", "tel aviv district", "תל אביב", "jaffa", "yafo", "ramat gan", "רמת גן",
        "givatayim", "גבעתיים", "bnei brak", "בני ברק", "holon", "חולון", "bat yam", "בת ים", "or yehuda", "אור יהודה",
        "kiryat ono", "קריית אונו", "herzliya", "herzelia", "הרצליה", "ramat hasharon", "רמת השרון", "glilot", "גלילות",
        "gush dan", "גוש דן"]),
    "sharon": ("Sharon & Hadera", [
        "raanana", "ra'anana", "רעננה", "kfar saba", "כפר סבא", "hod hasharon", "הוד השרון", "netanya", "נתניה",
        "herzliya", "הרצליה", "ramat hasharon", "even yehuda", "kadima", "caesarea", "קיסריה", "hadera", "חדרה",
        "or akiva", "hasharon", "השרון", "sharon"]),
    "center": ("Center & Shfela", [
        "center district", "central district", "petah tikva", "petach tikva", "petah-tikva", "petah tiqwa", "פתח תקווה",
        "rosh haayin", "rosh ha'ayin", "ראש העין", "airport city", "lod", "לוד", "ramla", "רמלה", "rehovot", "רחובות",
        "ness ziona", "nes ziona", "נס ציונה", "rishon lezion", "rishon le zion", "ראשון לציון", "yavne", "יבנה",
        "modiin", "מודיעין", "shoham", "beer yaakov", "באר יעקב", "yehud", "יהוד", "shfela", "השפלה"]),
    "jerusalem": ("Jerusalem", [
        "jerusalem", "ירושלים", "jerusalem district", "beit shemesh", "בית שמש", "mevaseret", "מבשרת"]),
    "judea_samaria": ("Judea & Samaria", [
        "judea", "samaria", "שומרון", "יהודה ושומרון", "ariel", "אריאל", "eli", "עלי", "gush etzion", "גוש עציון",
        "efrat", "אפרת", "ma'ale adumim", "maale adumim", "מעלה אדומים", "modiin illit", "barkan", "ברקן",
        "tekoa", "תקוע"]),
    "south": ("South", [
        "south district", "southern district", "beer sheva", "be'er sheva", "beersheba", "באר שבע", "ashdod", "אשדוד",
        "ashkelon", "אשקלון", "kiryat gat", "קריית גת", "sderot", "שדרות", "omer", "עומר", "dimona", "eilat", "אילת",
        "netivot", "ofakim", "דרום"]),
    "north": ("North & Haifa", [
        "north district", "northern district", "haifa district", "haifa", "חיפה", "yokneam", "יקנעם", "migdal haemek",
        "מגדל העמק", "nazareth", "נצרת", "karmiel", "כרמיאל", "tirat carmel", "nesher", "kiryat shmona", "tiberias",
        "טבריה", "kinneret", "כנרת", "afula", "עפולה", "galilee", "גליל", "matam", "manof", "beit shean", "בית שאן",
        "צפון"]),
}

ISRAEL_COUNTRY_CODES = {"IL", "ISR"}
WORLDWIDE_TERMS = ["worldwide", "anywhere", "global", "work from anywhere", "any location", "all locations"]
REGIONAL_TERMS = ["emea", "europe", "european union", "eu", "apac", "asia", "middle east", "mena", "americas",
                  "latam", "north america", "cet", "gmt"]
OUTSIDE_TERMS = [
    "united states", "usa", "us", "u.s.", "us only", "us-remote", "remote us", "remote - us", "remote (us)", "canada",
    "united kingdom", "uk", "england", "germany", "deutschland", "france", "poland", "india", "spain", "portugal",
    "netherlands", "ireland", "romania", "ukraine", "bulgaria", "serbia", "cyprus", "greece", "italy", "australia",
    "singapore", "japan", "brazil", "mexico", "argentina", "philippines", "vietnam", "china", "hong kong", "taiwan",
    "korea", "sweden", "denmark", "norway", "finland", "switzerland", "austria", "belgium", "czech", "hungary",
    "estonia", "latvia", "lithuania", "uae", "dubai", "south africa", "egypt", "turkey", "colombia", "chile",
    "costa rica", "new york", "nyc", "san francisco", "bay area", "boston", "seattle", "austin", "chicago",
    "los angeles", "denver", "atlanta", "miami", "toronto", "vancouver", "montreal", "london", "berlin", "munich",
    "paris", "amsterdam", "dublin", "madrid", "barcelona", "lisbon", "warsaw", "krakow", "bucharest", "kyiv",
    "sofia", "belgrade", "limassol", "athens", "milan", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai",
    "delhi", "gurgaon", "noida", "chennai", "sydney", "melbourne", "tokyo", "são paulo", "sao paulo",
    "mexico city", "buenos aires", "manila", "zurich", "stockholm", "copenhagen", "oslo", "helsinki", "vienna",
    "prague", "budapest", "tallinn", "riga", "vilnius", "brussels",
]
# ", IL" is deliberately absent: some Israeli boards write "Tel Aviv, IL". An unknown town with ", IL"
# therefore goes to geo review instead of being hidden as Illinois.
US_STATE_SUFFIX = re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|"
                             r"NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b")
RESTRICTION_PHRASES = ["us only", "u.s. only", "must be based in the us", "must reside in the us", "us citizens",
                       "authorized to work in the united states", "uk only", "eu only", "must be located in"]
# Some boards prefix locations with an ISO-3166 alpha-3 code, e.g. "IND-Remote", "USA-Remote".
ISO3_PREFIX = re.compile(r"^\s*([A-Z]{3})\s*[-–]")
ISO3_TO_ISO2 = {"ISR": "IL", "IND": "IN", "USA": "US", "GBR": "GB", "DEU": "DE", "FRA": "FR", "POL": "PL", "CAN": "CA",
                "AUS": "AU", "BRA": "BR", "MEX": "MX", "ESP": "ES", "NLD": "NL", "IRL": "IE", "ROU": "RO", "UKR": "UA",
                "PRT": "PT", "ITA": "IT", "SWE": "SE", "JPN": "JP", "SGP": "SG", "CHN": "CN", "ARG": "AR", "CZE": "CZ",
                "BGR": "BG", "SRB": "RS", "CYP": "CY", "GRC": "GR", "CHE": "CH", "AUT": "AT", "BEL": "BE", "DNK": "DK",
                "NOR": "NO", "FIN": "FI", "PHL": "PH", "VNM": "VN", "KOR": "KR", "TWN": "TW", "ARE": "AE", "ZAF": "ZA",
                "TUR": "TR", "COL": "CO", "CHL": "CL", "CRI": "CR", "HUN": "HU", "EST": "EE", "LVA": "LV", "LTU": "LT"}

GENERIC_LOCATIONS = {"", "remote", "hybrid", "on-site", "onsite", "office", "hq", "headquarters", "multiple locations",
                     "various", "flexible", "tbd"}

GEO_ELIGIBLE = ("ISRAEL", "WORLDWIDE_REMOTE")


# Hebrew job titles write both genders: "ראש/ת צוות", "בודק.ת תוכנה", "מנהל/ית". Read as the base word.
_HEBREW_GENDER = re.compile(r"(?<=[\u05d0-\u05ea])[/.](?:ית|ת|ה)(?![\u05d0-\u05ea])")


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", _HEBREW_GENDER.sub("", (text or "").casefold())).strip()


@lru_cache(maxsize=4096)
def _term_pattern(term: str) -> re.Pattern:
    """Whole-word match for Latin terms (so 'test' does not match 'contest'); plain substring otherwise.
    Numbers only need digit boundaries, so '13485' also matches 'ISO13485'."""
    t = re.escape(term.casefold())
    if not term.isascii():
        return re.compile(t)
    left = r"(?<!\d)" if term[0].isdigit() else r"(?<![a-z0-9])"
    right = r"(?!\d)" if term[-1].isdigit() else r"(?![a-z0-9])"
    return re.compile(left + t + right)


def find_terms(text: str, terms: list[str]) -> list[str]:
    t = _norm(text)
    return [term for term in terms if term.strip() and _term_pattern(term.strip()).search(t)]


@dataclass(frozen=True)
class Verdict:
    value: str
    reason: str


def relevance(title: str, profile: dict, description: str = "") -> tuple[Verdict, list[str], list[str], list[str]]:
    """(verdict, matched search terms, low-priority terms, description mentions below the exclusion threshold)."""
    excluded = find_terms(title, profile["exclude_terms"])
    included = find_terms(title, profile["include_terms"])
    low = find_terms(title, profile.get("low_priority_terms", []))
    if excluded:
        return Verdict("EXCLUDED", f"title has excluded term: {', '.join(excluded)}"), included, low, []
    if not included:
        return Verdict("NO_MATCH", "title has none of the search terms"), included, low, []
    # descriptions are long: only read them for titles that already match
    mentions = find_terms(description, profile.get("exclude_description_terms", [])) if description else []
    if len(mentions) >= profile.get("description_min_hits", 2):
        software = find_terms(title, profile.get("software_title_terms", DEFAULT_PROFILE["software_title_terms"]))
        if not software:
            return (Verdict("EXCLUDED", f"description has {len(mentions)} regulatory or manufacturing-quality terms: "
                                        f"{', '.join(mentions)}"), included, low, mentions)
        return (Verdict("MATCH", f"title matches: {', '.join(included)}; a software role ({', '.join(software)}), "
                                 f"so the description terms are only marked"), included, low, mentions)
    return Verdict("MATCH", f"title matches: {', '.join(included)}"), included, low, mentions


# "3+ years", "2-3 years", "3 to 5 yrs", "minimum of 5 years"; Hebrew "3 שנות ניסיון", "ניסיון של 3 שנים"
_YEARS = re.compile(r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*)?(?:years?|yrs)\b", re.I)
_YEARS_HE = re.compile(r"(\d{1,2})\s*\+?\s*שנ(?:ות|ים)")
_EXPERIENCE = re.compile(r"experience|background|hands-on|track record|ניסיון|\b(?:years?|yrs)\s+(?:in|of|as|working)\b", re.I)
_OPTIONAL = re.compile(r"ideally|advantage|nice to have|a plus|\bplus\b|preferred|preferably|יתרון", re.I)  # not required
_MANAGEMENT = re.compile(r"\b(?:manag\w*|lead\w*|people|supervis\w*|mentor\w*)\b|ניהול", re.I)


def experience_asked(description: str | None) -> tuple[list[int], list[int]]:
    """Years of experience a description asks for: (QA / engineering, management or leadership). Each line is read
    on its own; a number counts only on a required line about experience, and goes to management when the words
    right after it are about managing or leading."""
    general, management = [], []
    for line in re.split(r"[\n•·;]|(?<=[.!?])\s", description or ""):
        if not _EXPERIENCE.search(line) or _OPTIONAL.search(line):
            continue
        for m in [*_YEARS.finditer(line), *_YEARS_HE.finditer(line)]:
            # a range counts by its top: "2-3 years" is mid-level, "3-5 years" reaches senior
            years = int(m.group(2) if m.re is _YEARS and m.group(2) else m.group(1))
            # what the years are of: the words right after them ("3+ years leading QA engineers on top of
            # 5+ years of hands-on QA" is 3 of management and 5 of QA)
            about = line[m.start():m.end() + 45]
            if 0 < years <= 30:
                (management if _MANAGEMENT.search(about) else general).append(years)
    return general, management


def likely_overqualified(description: str | None, profile: dict) -> tuple[bool, list[int]]:
    """(True when the most the job asks of QA / engineering experience is at most overqualified_max_years, the years asked)."""
    general, _ = experience_asked(description)
    limit = profile.get("overqualified_max_years", DEFAULT_PROFILE["overqualified_max_years"])
    return bool(general) and max(general) <= limit, general


# Your rule (2026-10-20): 1–2 years of QA is a junior position, you are senior; 1–2 years of management is fine.
JUNIOR_MAX_YEARS = 2


def asks_junior_years(description: str | None) -> bool:
    """The job asks at most JUNIOR_MAX_YEARS of QA / engineering experience and no management or leadership years."""
    general, management = experience_asked(description)
    return bool(general) and max(general) <= JUNIOR_MAX_YEARS and not management


NARROW_TERMS = {"quality", "verification", "validation"}  # alone in a title they are often hardware or physics


# "מוביל/ה טכנולוגי/ת QA" is a lead; "בודק/ת תוכנה לארגון ממשלתי מוביל" is a tester at a leading organization
ADJECTIVE_LEAD = {"מוביל", "מובילה"}
_LEAD_FIRST = re.compile(r"^[\W\d_-]*(?:דרוש\S*\s+)?מוביל")


def level(title: str, profile: dict) -> str:
    """ABOVE (director level), FOCUS (your level: lead, manager, head), JUNIOR or ENGINEER, read from the title.
    Two postings of different levels are never one position, however alike their text."""
    if find_terms(title, profile.get("above_level_terms", DEFAULT_PROFILE["above_level_terms"])):
        return "ABOVE"
    focus = find_terms(title, profile.get("focus_level_terms", DEFAULT_PROFILE["focus_level_terms"]))
    if focus and (set(focus) - ADJECTIVE_LEAD or _LEAD_FIRST.match(title or "")):
        return "FOCUS"
    if find_terms(title, profile.get("junior_level_terms", DEFAULT_PROFILE["junior_level_terms"])):
        return "JUNIOR"
    return "ENGINEER"


def regions_of(location: str | None) -> set[str]:
    """The regions (keys of REGIONS) a location names; none for "Israel", "Remote" or "Israel (3 locations)"."""
    return {key for key, (_, places) in REGIONS.items() if find_terms(location or "", places)}


def excluded_place(location: str | None, profile: dict) -> list[str]:
    """Why this location is too far for you: the places you excluded that it names, or its regions when all of them
    are regions you do not want. A job listed in several places ("Israel (3 locations)") names none of them."""
    found = find_terms(location or "", profile.get("exclude_places", []))
    regions, off = regions_of(location), set(profile.get("exclude_regions", []))
    if regions and regions <= off:
        found += [REGIONS[r][0] for r in sorted(regions)]
    return found


def narrow_match(matched: list[str]) -> bool:
    """The title matched only on 'quality', 'validation' or 'verification': check that it is software QA."""
    return bool(matched) and {t.casefold() for t in matched} <= NARROW_TERMS


def geography(location: str, countries: list[str] | None = None, remote: bool | None = None,
              description: str = "") -> Verdict:
    """Strict Israel gate. Only ISRAEL and WORLDWIDE_REMOTE are shown; OUTSIDE is hidden with its reason;
    UNKNOWN goes to 'Needs geo review'. A recruiter's or company's own location is never evidence."""
    loc = _norm(location)
    codes = {c.upper() for c in (countries or []) if c}
    if (m := ISO3_PREFIX.match(location or "")) and m.group(1) in ISO3_TO_ISO2:
        codes.add(ISO3_TO_ISO2[m.group(1)])
    hits = find_terms(loc, ISRAEL_TERMS)
    if hits or codes & ISRAEL_COUNTRY_CODES:
        return Verdict("ISRAEL", f"location: {hits[0] if hits else 'country ' + sorted(codes & ISRAEL_COUNTRY_CODES)[0]}")
    if codes:
        return Verdict("OUTSIDE", f"country: {', '.join(sorted(codes))}")
    outside = find_terms(loc, OUTSIDE_TERMS)
    if outside or US_STATE_SUFFIX.search(location or ""):
        return Verdict("OUTSIDE", f"location: {', '.join(outside[:3]) if outside else location}")
    worldwide = find_terms(loc, WORLDWIDE_TERMS)
    is_remote = bool(remote or "remote" in loc or worldwide)
    if is_remote and worldwide:
        restricted = find_terms(description, RESTRICTION_PHRASES)  # only matters here; descriptions are long
        if restricted:
            return Verdict("UNKNOWN", f"worldwide remote, but the description says: {restricted[0]}")
        return Verdict("WORLDWIDE_REMOTE", f"location: {worldwide[0]}")
    regional = find_terms(loc, REGIONAL_TERMS)
    if regional:
        return Verdict("UNKNOWN", f"regional location ({regional[0]}) does not say whether Israel is included")
    if loc in GENERIC_LOCATIONS or (is_remote and loc.replace("remote", "").strip(" -,()") == ""):
        return Verdict("UNKNOWN", "location does not name a country")
    return Verdict("UNKNOWN", f"location '{location}' not recognised")


@dataclass(frozen=True)
class Freshness:
    label: str  # Fresh / Aging / Older than N days / Updated (no posting date) / Date unknown
    tone: str   # pr / n
    days: int | None
    basis: str  # what the age is measured from


def freshness(posted_at: str | None, profile: dict, now: datetime | None = None,
              updated_at: str | None = None) -> Freshness:
    """How old a job is. Fresh up to fresh_days; older than max_age_days it is not shown as new (kept).
    Without a published date (Comeet) the employer's last update is the only date there is."""
    stamp = posted_at or updated_at
    if not stamp:
        return Freshness("Date unknown", "n", None, "no date from the source")
    now = now or datetime.now(timezone.utc)
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = max(0, (now - when).days)
    if not posted_at:
        return Freshness("Updated", "n", days, f"updated {when.date().isoformat()}; the source publishes no posting date")
    basis = f"posted {when.date().isoformat()}"
    if days <= profile["fresh_days"]:
        return Freshness("Fresh", "pr", days, basis)
    max_age = profile.get("max_age_days", 14)
    if days <= max_age:
        return Freshness("Aging", "n", days, basis)
    return Freshness(f"Older than {max_age} days", "n", days, basis)


def validate_profile(raw: dict) -> dict:
    """Normalise a profile edited in the UI; raises ValueError on nonsense."""
    out = {}
    for key in ("include_terms", "exclude_terms", "low_priority_terms", "exclude_description_terms", "agency_companies",
                "software_title_terms", "focus_level_terms", "above_level_terms", "junior_level_terms",
                "requirement_terms", "exclude_places"):
        terms = raw.get(key, [])
        if isinstance(terms, str):
            terms = terms.splitlines()
        cleaned = []
        for t in terms:
            t = re.sub(r"\s+", " ", t).strip()
            if t and t.casefold() not in (c.casefold() for c in cleaned):
                if len(t) > 60:
                    raise ValueError(f"Term too long: {t[:30]}…")
                cleaned.append(t)
        out[key] = cleaned
    if not out["include_terms"]:
        raise ValueError("At least one search term is needed")
    for key in ("agency_companies", "software_title_terms", "focus_level_terms", "above_level_terms",
                "junior_level_terms", "requirement_terms", "exclude_places"):
        if key not in raw:
            out[key] = list(DEFAULT_PROFILE[key])  # older forms did not have the field
    try:
        out["fresh_days"] = int(raw.get("fresh_days", DEFAULT_PROFILE["fresh_days"]))
        out["max_age_days"] = int(raw.get("max_age_days", DEFAULT_PROFILE["max_age_days"]))
    except (TypeError, ValueError):
        raise ValueError("Freshness limits must be whole numbers of days")
    if not 1 <= out["fresh_days"] <= out["max_age_days"] <= 365:
        raise ValueError("Need 1 ≤ fresh days ≤ days shown ≤ 365")
    try:
        out["description_min_hits"] = int(raw.get("description_min_hits") or 2)  # empty field: default
    except (TypeError, ValueError):
        raise ValueError("The description threshold must be a whole number")
    if not 1 <= out["description_min_hits"] <= 20:
        raise ValueError("The description threshold must be between 1 and 20")
    try:
        out["overqualified_max_years"] = int(raw.get("overqualified_max_years") or DEFAULT_PROFILE["overqualified_max_years"])
    except (TypeError, ValueError):
        raise ValueError("Overqualified: a whole number of years")
    if not 0 <= out["overqualified_max_years"] <= 30:
        raise ValueError("Overqualified: between 0 and 30 years")
    off = raw.get("filters_off", [])
    out["filters_off"] = [k for k in FILTERS if k in (off.split() if isinstance(off, str) else off)]
    regions = raw.get("exclude_regions", [])
    out["exclude_regions"] = [k for k in REGIONS if k in (regions.split() if isinstance(regions, str) else regions)]
    return out
