"""Read-only XLSX snapshot reader, standard library only.

Replaces the proprietary `artifact_tool` reader used in Phase 2; it reproduces the Phase 2
legacy rows byte for byte. Behaviour that matters for the migration:

- a cell that points at an *empty* shared string is read as None (the 174-cell edge case),
- numbers keep their int/float type (dates stay Excel serials; conversion happens later),
- sheets are found by name, never by position,
- native hyperlink targets are returned separately, so a URL hidden behind display text is not lost,
- a non-empty cell outside the expected columns is an error, never silently dropped.

The file is opened read-only and never written.
"""
from __future__ import annotations

import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_REF = re.compile(r"([A-Z]+)(\d+)")


class SnapshotFormatError(ValueError):
    pass


@dataclass
class Sheet:
    name: str
    rows: list[list]  # rectangular: n_rows x n_cols, padded with None; rows[0] is source row 1
    hyperlinks: dict[str, str] = field(default_factory=dict)  # "O53" -> target
    empty_shared_string_cells: list[str] = field(default_factory=list)
    formula_cells: list[str] = field(default_factory=list)


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def split_ref(ref: str) -> tuple[int, int]:
    """'O53' -> (52, 14): zero-based row and column."""
    m = _REF.fullmatch(ref)
    if not m:
        raise SnapshotFormatError(f"Not an A1 cell reference: {ref!r}")
    return int(m.group(2)) - 1, col_index(m.group(1))


def col_letters(index: int) -> str:
    out, n = "", index + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _part(base_dir: str, target: str) -> str:
    return target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base_dir, target))


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    rels_path = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if rels_path not in z.namelist():
        return {}
    return {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read(rels_path))}


def _shared_strings(z: zipfile.ZipFile, part: str | None) -> list[str]:
    if not part or part not in z.namelist():
        return []
    out = []
    for si in ET.fromstring(z.read(part)).findall("x:si", NS):
        # Plain <t> plus rich-text runs <r><t>; phonetic hints (<rPh>) are not cell text.
        texts = [si.find("x:t", NS)] + [r.find("x:t", NS) for r in si.findall("x:r", NS)]
        out.append("".join((t.text or "") for t in texts if t is not None))
    return out


def _cell_value(c: ET.Element, shared: list[str]):
    kind = c.get("t", "n")
    if kind == "inlineStr":
        texts = c.findall(".//x:t", NS)
        return "".join(t.text or "" for t in texts) or None
    v = c.find("x:v", NS)
    if v is None or v.text is None:
        return None
    if kind == "s":
        return shared[int(v.text)] or None
    if kind == "n":
        number = float(v.text)
        return int(number) if number.is_integer() else number
    if kind == "b":
        return v.text == "1"
    return v.text or None  # str (formula result), e (error), d (ISO date)


def read_workbook(path: Path | str, columns: dict[str, int]) -> dict[str, Sheet]:
    """Read the named sheets. `columns` maps sheet name -> expected column count."""
    with zipfile.ZipFile(path) as z:
        wb_part = "xl/workbook.xml"
        wb_rels = _rels(z, wb_part)
        shared_part = next((_part("xl", t) for t in wb_rels.values() if t.endswith("sharedStrings.xml")), None)
        shared = _shared_strings(z, shared_part)
        sheet_parts = {s.get("name"): _part("xl", wb_rels[s.get(DOC_REL + "id")])
                       for s in ET.fromstring(z.read(wb_part)).findall("x:sheets/x:sheet", NS)}
        missing = set(columns) - set(sheet_parts)
        if missing:
            raise SnapshotFormatError(f"Snapshot is missing sheets: {sorted(missing)}")
        return {name: _read_sheet(z, name, sheet_parts[name], ncols, shared) for name, ncols in columns.items()}


def _read_sheet(z: zipfile.ZipFile, name: str, part: str, ncols: int, shared: list[str]) -> Sheet:
    root = ET.fromstring(z.read(part))
    cells: dict[tuple[int, int], object] = {}
    sheet = Sheet(name=name, rows=[])
    for c in root.findall("x:sheetData/x:row/x:c", NS):
        m = _REF.fullmatch(c.get("r", ""))
        if not m:
            raise SnapshotFormatError(f"{name}: cell without an A1 reference")
        col, row = col_index(m.group(1)), int(m.group(2)) - 1
        if c.find("x:f", NS) is not None:
            sheet.formula_cells.append(c.get("r"))
        value = _cell_value(c, shared)
        if c.get("t") == "s" and value is None and c.find("x:v", NS) is not None:
            sheet.empty_shared_string_cells.append(c.get("r"))
        if value is None:
            continue
        if col >= ncols:
            raise SnapshotFormatError(f"{name}!{c.get('r')} has data outside the expected {ncols} columns")
        cells[(row, col)] = value
    nrows = max((r for r, _ in cells), default=0) + 1
    sheet.rows = [[cells.get((r, c)) for c in range(ncols)] for r in range(nrows)]
    rels = _rels(z, part)
    for h in root.findall("x:hyperlinks/x:hyperlink", NS):
        target = rels.get(h.get(DOC_REL + "id")) if h.get(DOC_REL + "id") else "#" + (h.get("location") or "")
        if target:
            sheet.hyperlinks[h.get("ref")] = target
    return sheet
