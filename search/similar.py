"""When two postings are one position, and when they are only alike.

Job ads are written from templates: an employer's "about us", benefits and legal text repeat in all its ads, and
an ad is often reused for a similar role (another city, a junior or a lead version). So what is compared is the
role itself — the responsibilities and requirements sections, found by their headings — in runs of five words,
and the decision also needs the same level and a similar title (common practice for job-ad deduplication:
section-aware text, word shingles, title and level kept as separate fields).

- same_position: the same employer, the same level, and the role text the same (or very close with a similar
  title): your decision on one covers the others.
- link_similar: "possibly the same position" — only linked and marked, so you can compare.
"""
from __future__ import annotations

import re
from functools import lru_cache

POSSIBLY_SAME = 0.6   # share of distinct words in common (Jaccard) at which two descriptions are linked
SAME_COMPANY = 0.75   # within one company: the template is left out, but its ads still share a vocabulary
MIN_WORDS = 40        # shorter texts say too little to compare
SAME_TEXT = 0.95      # role text this close: one position listed several times (other cities, other titles)
SAME_ROLE = 0.7       # role text this close plus a similar title (and the same level): one position
SAME_TITLE = 0.6      # title words in common for SAME_ROLE ("QA Automation" vs "QA Performance" is not enough)
SHINGLE = 5           # words per compared run

# Headings that open the role part of an ad, and headings that close it (the employer's template)
_ROLE = re.compile(r"^\s*(about (the|this) (role|position|job)|the role|the position|role overview|job description|"
                   r"description|what you('ll| will) (do|be doing)|responsibilities|key responsibilities|your role|"
                   r"in this role|the job|what we('re| are) looking for|who you are|requirements|qualifications|"
                   r"must have|nice to have|you('ll| will) (bring|need)|skills|experience|תיאור התפקיד|תיאור משרה|"
                   r"דרישות|דרישות התפקיד)\b", re.I)
_TEMPLATE = re.compile(r"^\s*(about us|about [\w .&-]{2,30}$|who we are|why (join|work)|benefits|perks|what we offer|"
                       r"equal (employment )?opportunit|our (offices|culture|values)|life at|join us|קצת עלינו|על החברה)\b", re.I)


@lru_cache(maxsize=8192)
def role_part(sha: str, text: str) -> str:
    """The responsibilities and requirements of an ad, without the employer's template; the whole text when the ad
    has no such headings."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    kept, inside = [], False
    for line in lines:
        if len(line) < 70 and _TEMPLATE.match(line):
            inside = False
        elif len(line) < 70 and _ROLE.match(line):
            inside = True
        elif inside:
            kept.append(line)
    role = " ".join(kept)
    return role if len(role.split()) >= MIN_WORDS else (text or "")


@lru_cache(maxsize=8192)
def _shingles(sha: str, text: str) -> frozenset:
    words = re.findall(r"[^\W_]+", role_part(sha, text).casefold())
    return frozenset(" ".join(words[i:i + SHINGLE]) for i in range(max(0, len(words) - SHINGLE + 1)))


def role_similarity(a_sha: str, a_text: str, b_sha: str, b_text: str) -> float:
    """Share of five-word runs the two role parts have in common (Jaccard), 0 when either says too little."""
    if len((a_text or "").split()) < MIN_WORDS or len((b_text or "").split()) < MIN_WORDS:
        return 0.0
    if a_sha == b_sha:
        return 1.0
    a, b = _shingles(a_sha, a_text), _shingles(b_sha, b_text)
    return len(a & b) / len(a | b) if a and b else 0.0


def same_position(role_score: float, title_score: float) -> bool:
    """Given the same employer and the same level: is it one position?"""
    return role_score >= SAME_TEXT or (role_score >= SAME_ROLE and title_score >= SAME_TITLE)


@lru_cache(maxsize=8192)
def _words(sha: str, text: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[^\W\d_]{3,}", role_part(sha, text).casefold()))


def similarity(a_sha: str, a_text: str, b_sha: str, b_text: str) -> float:
    a, b = _words(a_sha, a_text), _words(b_sha, b_text)
    if len(a) < MIN_WORDS or len(b) < MIN_WORDS:
        return 0.0
    return 1.0 if a_sha == b_sha else len(a & b) / len(a | b)


def link_similar(views: list, pool: list) -> None:
    """Set view.similar = [(other view, score), …] for each view, comparing with the pool (never itself)."""
    for view in views:
        p = view.posting
        found = []
        for other in pool:
            o = other.posting
            if o["id"] == p["id"]:
                continue
            score = similarity(p["description_sha"], p["description"] or "", o["description_sha"], o["description"] or "")
            if score >= (SAME_COMPANY if other.company.casefold() == view.company.casefold() else POSSIBLY_SAME):
                found.append((other, round(score, 2)))
        view.similar = sorted(found, key=lambda pair: -pair[1])[:6]
