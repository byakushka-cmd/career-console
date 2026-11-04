"""Your CV: the files you upload, and copies of them for one job with only the header lines changed.

A PDF: the old lines are taken out of the page itself, not covered up, so a recruiter's software does not read them;
the new ones take their place, in the same size and colour. A Word file (.docx): only the text of those paragraphs
changes, every other part of the file stays byte for byte, and LibreOffice, when installed, makes the PDF.
The files are kept next to the database, private.
"""
from __future__ import annotations

import io
import json
import math
import re
import shutil
import sqlite3
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from lxml import etree
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError
from pypdf.generic import ContentStream, FloatObject, NameObject
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen import canvas

from app import db
from app.config import ensure_private_dir
from search.sources import first_requirements

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
_LANGUAGE = re.compile(r"[-_ ](EN|HE|RU)$", re.I)
_MASTER = re.compile(r"[-_ ]?(?:master|original)\b", re.I)
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
SOFFICE = ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice")


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


def is_word(data: bytes) -> bool:
    try:
        return "word/document.xml" in zipfile.ZipFile(io.BytesIO(data)).namelist()
    except zipfile.BadZipFile:
        return False


def _paragraphs(root) -> list:
    return list(root.iter(W + "p"))


def _text(paragraph) -> str:
    return "".join(t.text or "" for t in paragraph.iter(W + "t"))


def word_lines(data: bytes) -> list[tuple[int, str]]:
    """The paragraphs with text, in the file's order (table cells included), by their position in the document."""
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    return [(i, " ".join(_text(p).split())) for i, p in enumerate(_paragraphs(root)) if _text(p).strip()]


def word_header_guess(lines: list[tuple[int, str]]) -> list[int]:
    """The line under your name: the title a recruiter reads first."""
    return next(([i] for i, text in lines[1:4] if not _CONTACT.search(text)), [])


def word_retitle(data: bytes, changes: dict[int, str]) -> bytes:
    """A copy of the Word file with these paragraphs' text replaced, in the formatting of their first run."""
    source = zipfile.ZipFile(io.BytesIO(data))
    root = etree.fromstring(source.read("word/document.xml"))
    paragraphs = _paragraphs(root)
    for index, text in changes.items():
        runs = list(paragraphs[index].iter(W + "t"))
        if not runs:
            raise ValueError("This line cannot be changed in this Word file")
        runs[0].text = text
        runs[0].set(XML_SPACE, "preserve")
        for run in runs[1:]:
            run.text = ""
    changed = etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for item in source.infolist():
            target.writestr(item, changed if item.filename == "word/document.xml" else source.read(item.filename))
    return out.getvalue()


def _soffice() -> str | None:
    return next((found for name in SOFFICE if (found := shutil.which(name) or (name if Path(name).is_file() else None))), None)


def word_to_pdf(docx: Path) -> Path | None:
    """The PDF of a Word file, made by LibreOffice next to it; None when LibreOffice is not installed."""
    soffice = _soffice()
    if soffice is None:
        return None
    profile = ensure_private_dir(docx.parent / ".libreoffice")
    try:
        subprocess.run([soffice, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--convert-to", "pdf",
                        "--outdir", str(docx.parent), str(docx)], check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise ValueError("LibreOffice could not make a PDF of this CV") from None
    pdf = docx.with_suffix(".pdf")
    if not pdf.is_file() or not pdf.stat().st_size:
        raise ValueError("LibreOffice could not make a PDF of this CV")
    return pdf


def _same_layout(new_pdf: Path, master_pdf: Path | None) -> None:
    """A new title must not push the page over: the same number of pages and of lines as your master CV."""
    if master_pdf is None or not master_pdf.is_file():
        return
    new_pages, old_pages = len(PdfReader(new_pdf).pages), len(PdfReader(master_pdf).pages)
    if new_pages > old_pages:
        raise ValueError("With this title the CV no longer fits its pages: shorten it")
    baselines = lambda pdf: len({round(line.y) for line in page_lines(pdf.read_bytes())})   # text pieces share a line
    if baselines(new_pdf) > baselines(master_pdf):
        raise ValueError("The title runs onto a second line: shorten it")


def folder(db_path: Path | str) -> Path:
    return ensure_private_dir(Path(db_path).parent / "cv")


def _clean_lines(text: str) -> str:
    return " ".join(text.split("\n")).strip()


def add(conn: sqlite3.Connection, db_path: Path | str, filename: str, data: bytes) -> int:
    """Keep an uploaded CV. The lines under the name are taken as its header until you say otherwise."""
    if len(data) > MAX_UPLOAD:
        raise ValueError("This file is too large: a CV should be under 5 MB")
    if is_word(data):
        try:
            top = word_lines(data)
        except (etree.XMLSyntaxError, KeyError, zipfile.BadZipFile):
            raise ValueError("This Word file cannot be read. Save it again from Word as .docx.") from None
        if not top:
            raise ValueError("This Word file has no text in it")
        header, kind = word_header_guess(top), "docx"
    else:
        try:
            lines = page_lines(data)
        except (PyPdfError, ValueError, TypeError, KeyError, IndexError, OSError):
            raise ValueError("This does not read as a PDF or a Word file. Upload your CV as .pdf or .docx.") from None
        if not lines:
            raise ValueError("This PDF has no text in it (a scan or a picture), so its header cannot be changed. "
                             "Export it from Word or Google Docs as a PDF with text.")
        top, header, kind = [(l.index, l.text) for l in lines], header_guess(lines), "pdf"
    digest = sha256(data).hexdigest()
    known = conn.execute("SELECT id FROM cv_files WHERE sha256=?", (digest,)).fetchone()
    if known:
        with db.transaction(conn):
            conn.execute("UPDATE cv_files SET removed_at_utc=NULL WHERE id=?", (known["id"],))
        return known["id"]
    name = f"cv-{digest[:16]}.{kind}"
    (folder(db_path) / name).write_bytes(data)
    pdf = word_to_pdf(folder(db_path) / name) if kind == "docx" else None
    with db.transaction(conn):
        return conn.execute("""INSERT INTO cv_files(uploaded_at_utc, name, path, sha256, top_lines, header_lines, pdf_path)
                               VALUES (?,?,?,?,?,?,?)""",
                            (_now(), Path(filename.replace("\\", "/")).name[:200] or f"CV.{kind}", name, digest,
                             json.dumps([list(t) for t in top[:TOP_LINES]], ensure_ascii=False),
                             json.dumps(header), pdf.name if pdf else None)).lastrowid


def _file(row: sqlite3.Row) -> dict:
    header = json.loads(row["header_lines"])
    top = json.loads(row["top_lines"])
    return {"id": row["id"], "name": row["name"], "uploaded_at_utc": row["uploaded_at_utc"], "path": row["path"],
            "kind": "docx" if row["path"].endswith(".docx") else "pdf", "pdf_path": row["pdf_path"],
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
    return [{**dict(r), "lines": {int(k): v for k, v in json.loads(r["lines"]).items()},
             "pdf": r["pdf_path"] or (r["path"] if r["path"].endswith(".pdf") else None),
             "word": r["path"] if r["path"].endswith(".docx") else None} for r in rows]


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
    here = folder(db_path)
    source = (here / cv["path"]).read_bytes()
    pdf_name = None
    if cv["kind"] == "docx":
        data = word_retitle(source, changed)
        name = f"job-{sha256(data).hexdigest()[:16]}.docx"
        (here / name).write_bytes(data)
        try:
            pdf = word_to_pdf(here / name)
            if pdf:
                _same_layout(pdf, here / cv["pdf_path"] if cv["pdf_path"] else None)
                pdf_name = pdf.name
        except ValueError:
            for leftover in (here / name, (here / name).with_suffix(".pdf")):
                leftover.unlink(missing_ok=True)
            raise
    else:
        data = retitle(source, changed)
        name = f"job-{sha256(data).hexdigest()[:16]}.pdf"
        (here / name).write_bytes(data)
    with db.transaction(conn):
        return conn.execute("""INSERT INTO cv_versions(created_at_utc, cv_id, record_ref, company, role, job_href, lines,
                               path, origin, pdf_path) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (_now(), cv_id, ref, company, role, job_href,
                             json.dumps({**cv["header_text"], **wanted}, ensure_ascii=False), name, origin,
                             pdf_name)).lastrowid


_ROLE_NOISE = re.compile(r"\s+[—–]\s+(?:linkedin|drushim|alljobs|jobtime|official|employer|greenhouse|comeet|lever|ashby|"
                         r"workday|req|#).*$|\s*\((?=[^)]*\d)[^)]*\)|#\s*\d+|\b(?:req|job|position)\.?\s*(?:id\s*)?#?\s*\d+|^\d{3,}\s*-\s*|"
                         r"\s*\|.*$|,\s*(?:israel|tel aviv|remote|hybrid)\b.*$", re.I)


def suggest_title(role: str, current: str = "") -> str:
    """The employer's own name for the role, without job numbers and places: a recruiter's first question is "what
    role?". Written in capitals when your CV's title is."""
    if _RIGHT_TO_LEFT.search(current or "") and not _RIGHT_TO_LEFT.search(role or ""):
        return ""   # a Hebrew CV keeps its own title until you write one: the role is in another language
    title = " ".join(_ROLE_NOISE.sub("", role or "").split()).strip(" -|/")
    if current and current == current.upper() and current != current.lower():
        title = title.upper()
    return title


def requirements_of(text: str, count: int = 7) -> list[str]:
    """The first requirements of a job's text; when it has no requirements heading, its first substantial lines."""
    found = first_requirements(text, count)
    if found:
        return found
    lines = [" ".join(l.split()) for l in (text or "").splitlines()]
    return [l.lstrip("•*-–· ") for l in lines if len(l) >= 12][:count]


def job_text(conn: sqlite3.Connection, ref: str) -> dict | None:
    row = conn.execute("SELECT * FROM cv_jobs WHERE record_ref=?", (ref,)).fetchone()
    return dict(row) if row else None


def save_job_text(conn: sqlite3.Connection, ref: str, company: str, role: str, text: str) -> None:
    """The text of a job you pasted, kept with it."""
    with db.transaction(conn):
        conn.execute("""INSERT INTO cv_jobs(record_ref, company, role, text, updated_at_utc) VALUES (?,?,?,?,?)
                        ON CONFLICT(record_ref) DO UPDATE SET text=excluded.text, updated_at_utc=excluded.updated_at_utc""",
                     (ref, company[:300], role[:300], text.strip()[:60_000], _now()))


def new_job(conn: sqlite3.Connection, company: str, role: str, text: str) -> int:
    """A job that is not in the console: its text pasted on My CV, to make a CV for it."""
    if not role.strip() or not text.strip():
        raise ValueError("Write the role and paste the job's text")
    with db.transaction(conn):
        job_id = conn.execute("INSERT INTO cv_jobs(company, role, text, updated_at_utc) VALUES (?,?,?,?)",
                              (company.strip()[:300], role.strip()[:300], text.strip()[:60_000], _now())).lastrowid
        conn.execute("UPDATE cv_jobs SET record_ref=? WHERE id=?", (f"JOB {job_id}", job_id))
    return job_id


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM cv_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def download_name(version: dict, extension: str = "pdf") -> str:
    """Your CV's own name with the company after it, and its language last: Name_CV_Acme_EN.pdf."""
    stem = _NUMBERED_COPY.sub("", Path(version["cv_name"]).stem) or "CV"
    language = _LANGUAGE.search(stem)
    stem = _MASTER.sub("", _LANGUAGE.sub("", stem)).strip("_- ") or "CV"
    company = re.sub(r"[^\w]+", "_", version["company"].split(" / ")[0]).strip("_")[:40]
    parts = [stem, company, language.group(1).upper() if language else ""]
    return "_".join(p for p in parts if p) + f".{extension}"
