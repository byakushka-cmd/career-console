"""Your CV: the PDFs you upload, and copies of them for one job with only the header lines changed.

The rest of the page stays as it was. The old lines are taken out of the page itself, not covered up, so a
recruiter's software does not read them; the new ones take their place, in the same size and colour.
The files are kept next to the database, private.
"""
from __future__ import annotations

import io
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from pypdf.generic import ContentStream, FloatObject, NameObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen import canvas

from app import db
from app.config import ensure_private_dir

MAX_UPLOAD = 5 * 1024 * 1024
TOP_LINES = 14         # the lines of the page offered as its header
MIN_SCALE = 0.75       # a longer line may shrink this far; beyond that it has to be shortened
SAME_BASELINE = 0.6    # points
FONT_DIRS = ("/System/Library/Fonts/Supplemental", "/System/Library/Fonts", "/Library/Fonts", "~/Library/Fonts",
             "/Applications/LibreOffice.app/Contents/Resources/fonts/truetype", "/usr/share/fonts", "~/.fonts",
             "C:/Windows/Fonts")
SANS, SERIF = ("arimo", "liberationsans", "arial", "helvetica", "dejavusans", "notosans"), \
    ("notoserif", "georgia", "timesnewroman", "liberationserif", "dejavuserif")
_SERIF_NAME = re.compile(r"serif|times|georgia|garamond|cambria|minion|merriweather|lora|playfair|palatino", re.I)
_CONTACT = re.compile(r"@|https?:|www\.|linkedin|\+?\d[\d\s()-]{6,}", re.I)
_RIGHT_TO_LEFT = re.compile(r"[\u0590-\u06ff]")
_NUMBERED_COPY = re.compile(r"[-_ ]*\(?\d{1,2}\)?$")   # "CV-1", "CV (2)": the browser's mark on a second download


@dataclass(frozen=True)
class Line:
    index: int
    x: float
    y: float
    size: float
    font: str
    text: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _times(m, n):
    a, b, c, d, e, f = m
    g, h, i, j, k, l = n
    return a * g + b * i, a * h + b * j, c * g + d * i, c * h + d * j, e * g + f * i + k, e * h + f * j + l


def page_lines(data: bytes) -> list[Line]:
    """The text lines of the first page, top to bottom."""
    found = []

    def visit(text, cm, tm, font, size):
        if text.strip():
            _, _, _, d, x, y = _times(tm, cm)
            found.append((x, y, round((size or 0) * abs(d or 1), 2), str((font or {}).get("/BaseFont") or ""),
                          " ".join(text.replace("\xa0", " ").split("\n")).strip()))

    PdfReader(io.BytesIO(data)).pages[0].extract_text(visitor_text=visit)
    ordered = sorted(found, key=lambda f: (-round(f[1]), f[0]))
    return [Line(i, *f) for i, f in enumerate(ordered)]


def header_guess(lines: list[Line]) -> list[int]:
    """The lines right under your name (the largest text at the top), without your contacts."""
    if not lines:
        return []
    name, out = max(lines[:10], key=lambda l: l.size), []
    for line in lines[name.index + 1:]:
        if name.y - line.y > name.size * 3 or len(out) == 2:
            break
        if abs(line.x - name.x) <= 3 and not _CONTACT.search(line.text):
            out.append(line.index)
    return out


@dataclass(frozen=True)
class Shown:
    """One text-showing operation of a page, and the state it runs in."""
    op: int
    x: float
    y: float
    scale: float     # what a font size is multiplied by on the page
    font: list       # the operands of the Tf in force
    spacing: tuple   # character and word spacing in force


def _shown_text(operations) -> list[Shown]:
    ctm, tm, line_m, leading, font, spacing, saved, out = (1, 0, 0, 1, 0, 0), None, None, 0.0, [], (0.0, 0.0), [], []
    for i, (operands, operator) in enumerate(operations):
        if operator == b"q":
            saved.append((ctm, font, spacing))
        elif operator == b"Q" and saved:
            ctm, font, spacing = saved.pop()
        elif operator == b"cm":
            ctm = _times(tuple(float(o) for o in operands), ctm)
        elif operator == b"Tf":
            font = operands
        elif operator == b"Tc":
            spacing = (float(operands[0]), spacing[1])
        elif operator == b"Tw":
            spacing = (spacing[0], float(operands[0]))
        elif operator == b"BT":
            tm = line_m = (1, 0, 0, 1, 0, 0)
        elif operator == b"TL":
            leading = float(operands[0])
        elif operator in (b"Td", b"TD"):
            tx, ty = float(operands[0]), float(operands[1])
            leading = -ty if operator == b"TD" else leading
            tm = line_m = _times((1, 0, 0, 1, tx, ty), line_m)
        elif operator == b"Tm":
            tm = line_m = tuple(float(o) for o in operands)
        elif operator in (b"T*", b"'", b'"'):
            tm = line_m = _times((1, 0, 0, 1, 0, -leading), line_m)
        if operator in (b"Tj", b"TJ", b"'", b'"') and tm is not None:
            _, _, _, d, x, y = _times(tm, ctm)
            out.append(Shown(i, x, y, abs(d) or 1.0, font, spacing))
    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.rsplit("+", 1)[-1].casefold())


@lru_cache(maxsize=1)
def _installed_fonts() -> dict[str, Path]:
    fonts = {}
    for folder in FONT_DIRS:
        root = Path(folder).expanduser()
        if root.is_dir():
            for path in root.rglob("*.ttf"):
                fonts.setdefault(_norm(path.stem), path)
    return fonts


def _font_for(original: str) -> str:
    """The original font when it is on this computer, else the closest one of the same kind."""
    name = _norm(original)
    bold, italic = "bold" in name, "italic" in name or "oblique" in name
    style = ("bold" if bold else "") + ("italic" if italic else "")
    serif = bool(_SERIF_NAME.search(name)) and "sans" not in name
    installed = _installed_fonts()
    similar = [family + ending for family in (SERIF if serif else SANS) for ending in ([style] if style else ["", "regular"])]
    for candidate in (name, *similar):
        if candidate in installed:
            try:
                registered = f"cv-{candidate}"
                if registered not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont(registered, str(installed[candidate])))
                return registered
            except (TTFError, OSError):
                continue
    if serif:
        return {"bold": "Times-Bold", "italic": "Times-Italic", "bolditalic": "Times-BoldItalic"}.get(style, "Times-Roman")
    return {"bold": "Helvetica-Bold", "italic": "Helvetica-Oblique",
            "bolditalic": "Helvetica-BoldOblique"}.get(style, "Helvetica")


def _check_letters(text: str, font: str) -> None:
    if _RIGHT_TO_LEFT.search(text):
        raise ValueError("Hebrew in the header is not supported yet: write these lines in English")
    face = getattr(pdfmetrics.getFont(font), "face", None)
    glyphs = getattr(face, "charToGlyph", None)
    if glyphs is not None:
        missing = sorted({c for c in text if ord(c) not in glyphs})
    else:
        missing = sorted({c for c in text if not c.encode("cp1252", "ignore")})
    if missing:
        raise ValueError(f"The font of this CV has no {' '.join(missing)}: use other characters")


def _fitted_size(line: Line, text: str, font: str, lines: list[Line], right_edge: float) -> float:
    """The line's own size, or a little smaller when the new text would run into what stands to its right."""
    top, bottom = line.y + line.size * 0.8, line.y - line.size * 0.25
    limits = [other.x - 8 for other in lines if other.x > line.x and other is not line
              and other.y + other.size * 0.8 > bottom and other.y - other.size * 0.25 < top]
    room = min([right_edge - line.x, *limits]) - line.x if limits else right_edge - 2 * line.x
    width = pdfmetrics.stringWidth(text, font, line.size)
    if width <= room:
        return line.size
    if width * MIN_SCALE > room:
        keep = max(1, int(len(text) * room / (width * MIN_SCALE)))
        raise ValueError(f"“{text}” is too long for its place in the CV: keep it to about {keep} characters")
    return math.floor(line.size * room / width * 20) / 20


def _written_with(text: str, font: str, size: float, writer: PdfWriter, fonts, used: list) -> list:
    """The operations that write this text in this font; the font goes into the page's fonts."""
    drawn = io.BytesIO()
    sheet = canvas.Canvas(drawn)
    sheet.setFont(font, size)
    sheet.drawString(0, 0, text)
    sheet.save()
    reader = PdfReader(io.BytesIO(drawn.getvalue()))
    source = reader.pages[0]
    current, operations = None, []
    for operands, operator in ContentStream(source.get_contents(), reader).operations:
        if operator == b"Tf":
            current = operands[0]
        elif operator == b"Tj":
            name = NameObject(f"/CVHeader{len(used)}")
            used.append(name)
            cloned = source["/Resources"]["/Font"][current].get_object().clone(writer)
            fonts[name] = cloned.indirect_reference or writer._add_object(cloned)
            operations += [([name, FloatObject(size)], b"Tf"), (operands, b"Tj")]
    return operations


def retitle(data: bytes, changes: dict[int, str]) -> bytes:
    """A copy of the PDF with these lines of the first page (by line number) rewritten; nothing else changes.

    Each new line is written where the old one was in the page's own order, so software that reads a CV from top to
    bottom still finds your title right under your name, and it keeps the old line's colour."""
    lines = page_lines(data)
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    page = writer.pages[0]
    resources = page["/Resources"].get_object()
    if "/Font" not in resources:
        raise ValueError("This PDF has no fonts to change")
    fonts, used = resources["/Font"].get_object(), []
    stream = ContentStream(page.get_contents(), writer)
    operations = stream.operations
    shown = _shown_text(operations)
    remove, replace = set(), {}
    for index, text in sorted(changes.items()):
        line = lines[index]
        font = _font_for(line.font)
        _check_letters(text, font)
        next_x = min((o.x for o in lines if o is not line and o.x > line.x and abs(o.y - line.y) < SAME_BASELINE),
                     default=math.inf)
        hits = [s for s in shown if abs(s.y - line.y) < SAME_BASELINE and line.x - 1 <= s.x < next_x]
        if not hits:
            raise ValueError(f"The line “{line.text}” cannot be changed in this PDF")
        first = min(hits, key=lambda s: (s.x, s.op))
        remove.update(s.op for s in hits)
        size = _fitted_size(line, text, font, lines, float(page.mediabox.right)) / first.scale
        restore = [(first.font, b"Tf")] if first.font else []
        for value, operator in zip(first.spacing, (b"Tc", b"Tw")):
            if value:
                restore.append(([FloatObject(value)], operator))
        no_spacing = [([FloatObject(0)], operator) for value, operator in zip(first.spacing, (b"Tc", b"Tw")) if value]
        replace[first.op] = no_spacing + _written_with(text, font, size, writer, fonts, used) + restore
    new_operations = []
    for i, op in enumerate(operations):
        new_operations += replace.get(i, []) if i in remove else [op]
    stream.operations = new_operations
    page.replace_contents(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def folder(db_path: Path | str) -> Path:
    return ensure_private_dir(Path(db_path).parent / "cv")


def _clean_lines(text: str) -> str:
    return " ".join(text.split("\n")).strip()


def add(conn: sqlite3.Connection, db_path: Path | str, filename: str, data: bytes) -> int:
    """Keep an uploaded CV. The lines under the name are taken as its header until you say otherwise."""
    if len(data) > MAX_UPLOAD:
        raise ValueError("This file is too large: a CV should be under 5 MB")
    try:
        lines = page_lines(data)
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, OSError):
        raise ValueError("This does not read as a PDF. Export your CV from Word or Google Docs as a PDF.") from None
    if not lines:
        raise ValueError("This PDF has no text in it (a scan or a picture), so its header cannot be changed. "
                         "Export it from Word or Google Docs as a PDF with text.")
    digest = sha256(data).hexdigest()
    known = conn.execute("SELECT id FROM cv_files WHERE sha256=?", (digest,)).fetchone()
    if known:
        with db.transaction(conn):
            conn.execute("UPDATE cv_files SET removed_at_utc=NULL WHERE id=?", (known["id"],))
        return known["id"]
    name = f"cv-{digest[:16]}.pdf"
    (folder(db_path) / name).write_bytes(data)
    with db.transaction(conn):
        return conn.execute("""INSERT INTO cv_files(uploaded_at_utc, name, path, sha256, top_lines, header_lines)
                               VALUES (?,?,?,?,?,?)""",
                            (_now(), Path(filename.replace("\\", "/")).name[:200] or "CV.pdf", name, digest,
                             json.dumps([[l.index, l.text] for l in lines[:TOP_LINES]], ensure_ascii=False),
                             json.dumps(header_guess(lines)))).lastrowid


def _file(row: sqlite3.Row) -> dict:
    header = json.loads(row["header_lines"])
    top = json.loads(row["top_lines"])
    return {"id": row["id"], "name": row["name"], "uploaded_at_utc": row["uploaded_at_utc"], "path": row["path"],
            "top": top, "header": header, "header_text": {i: text for i, text in top if i in header}}


def files(conn: sqlite3.Connection) -> list[dict]:
    return [_file(r) for r in conn.execute("SELECT * FROM cv_files WHERE removed_at_utc IS NULL ORDER BY id DESC")]


def get_file(conn: sqlite3.Connection, cv_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cv_files WHERE id=?", (cv_id,)).fetchone()
    return _file(row) if row else None


def set_header(conn: sqlite3.Connection, cv_id: int, numbers: list[int]) -> None:
    cv = get_file(conn, cv_id)
    if cv is None:
        raise ValueError("Unknown CV")
    known = {i for i, _ in cv["top"]}
    with db.transaction(conn):
        conn.execute("UPDATE cv_files SET header_lines=? WHERE id=?",
                     (json.dumps(sorted(set(numbers) & known)), cv_id))


def remove(conn: sqlite3.Connection, cv_id: int) -> None:
    """Off the list; the copies already made from it stay."""
    with db.transaction(conn):
        conn.execute("UPDATE cv_files SET removed_at_utc=? WHERE id=?", (_now(), cv_id))


def versions(conn: sqlite3.Connection, ref: str | None = None) -> list[dict]:
    rows = conn.execute("""SELECT v.*, f.name AS cv_name FROM cv_versions v JOIN cv_files f ON f.id = v.cv_id
                           WHERE (? IS NULL OR v.record_ref = ?) ORDER BY v.id DESC""", (ref, ref)).fetchall()
    return [{**dict(r), "lines": {int(k): v for k, v in json.loads(r["lines"]).items()}} for r in rows]


def get_version(conn: sqlite3.Connection, version_id: int) -> dict | None:
    return next((v for v in versions(conn) if v["id"] == version_id), None)


def make_copy(conn: sqlite3.Connection, db_path: Path | str, cv_id: int, ref: str, company: str, role: str,
              job_href: str, new_lines: dict[int, str], origin: str = "web") -> int:
    """A copy of this CV for one job. Only the lines marked as its header can change."""
    cv = get_file(conn, cv_id)
    if cv is None:
        raise ValueError("Unknown CV")
    wanted = {i: _clean_lines(t) for i, t in new_lines.items() if i in cv["header"] and _clean_lines(t)}
    changed = {i: t for i, t in wanted.items() if t != cv["header_text"][i]}
    if not changed:
        raise ValueError("Nothing to change: edit at least one line of the header")
    data = retitle((folder(db_path) / cv["path"]).read_bytes(), changed)
    name = f"job-{sha256(data).hexdigest()[:16]}.pdf"
    (folder(db_path) / name).write_bytes(data)
    with db.transaction(conn):
        return conn.execute("""INSERT INTO cv_versions(created_at_utc, cv_id, record_ref, company, role, job_href, lines,
                               path, origin) VALUES (?,?,?,?,?,?,?,?,?)""",
                            (_now(), cv_id, ref, company, role, job_href,
                             json.dumps({**cv["header_text"], **wanted}, ensure_ascii=False), name, origin)).lastrowid


def download_name(version: dict) -> str:
    """Your CV's own name with the company after it."""
    stem = _NUMBERED_COPY.sub("", Path(version["cv_name"]).stem) or "CV"
    company = re.sub(r"[^\w]+", "_", version["company"].split(" / ")[0]).strip("_")[:40]
    return f"{stem}_{company}.pdf" if company else f"{stem}.pdf"
