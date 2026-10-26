"""Build small synthetic XLSX files with the stdlib, including the edge cases the real snapshot has."""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from app.xlsx_snapshot import col_letters

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass
class Rich:
    """A shared string made of rich-text runs."""
    parts: list[str]


@dataclass
class Inline:
    text: str


def build_xlsx(path: Path, sheets: dict[str, list[list]], hyperlinks: dict[str, dict[str, str]] | None = None,
               extra_cells: dict[str, list[tuple[str, str]]] | None = None) -> Path:
    """Values: str -> shared string ("" makes an EMPTY shared string cell), int/float -> number, None -> no cell.

    Worksheet files are numbered in reverse so a reader that relies on position instead of names fails.
    """
    hyperlinks, extra_cells = hyperlinks or {}, extra_cells or {}
    shared: list[str] = []
    index: dict[str, int] = {}

    def sst(xml: str) -> int:
        if xml not in index:
            index[xml] = len(shared)
            shared.append(xml)
        return index[xml]

    names = list(sheets)
    files = {name: f"sheet{len(names) - i}.xml" for i, name in enumerate(names)}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(f'<Override PartName="/xl/worksheets/{f}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for f in files.values())
        z.writestr("[Content_Types].xml", f'<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>')
        z.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        sheet_xml = "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rS{i + 1}"/>' for i, n in enumerate(names))
        z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{MAIN}" xmlns:r="{R}"><sheets>{sheet_xml}</sheets></workbook>')
        rels = "".join(f'<Relationship Id="rS{i + 1}" Type="{R}/worksheet" Target="worksheets/{files[n]}"/>' for i, n in enumerate(names))
        rels += f'<Relationship Id="rSST" Type="{R}/sharedStrings" Target="sharedStrings.xml"/>'
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG}">{rels}</Relationships>')
        for name in names:
            rows_xml = []
            for r, row in enumerate(sheets[name], 1):
                cells = []
                for c, v in enumerate(row):
                    ref = f"{col_letters(c)}{r}"
                    if v is None:
                        continue
                    if isinstance(v, bool):
                        cells.append(f'<c r="{ref}" t="b"><v>{int(v)}</v></c>')
                    elif isinstance(v, (int, float)):
                        cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                    elif isinstance(v, Inline):
                        cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v.text)}</t></is></c>')
                    elif isinstance(v, Rich):
                        runs = "".join(f"<r><t xml:space=\"preserve\">{escape(p)}</t></r>" for p in v.parts)
                        cells.append(f'<c r="{ref}" t="s"><v>{sst(runs)}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="s"><v>{sst(f"<t xml:space=\"preserve\">{escape(v)}</t>")}</v></c>')
                for ref, xml in extra_cells.get(name, []):
                    if int("".join(ch for ch in ref if ch.isdigit())) == r:
                        cells.append(xml)
                rows_xml.append(f'<row r="{r}">{"".join(cells)}</row>')
            links = hyperlinks.get(name, {})
            link_xml = ""
            if links:
                link_xml = "<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rH{i}"/>' for i, ref in enumerate(links)) + "</hyperlinks>"
                hrels = "".join(f'<Relationship Id="rH{i}" Type="{R}/hyperlink" Target="{escape(t)}" TargetMode="External"/>' for i, t in enumerate(links.values()))
                z.writestr(f"xl/worksheets/_rels/{files[name]}.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{PKG}">{hrels}</Relationships>')
            z.writestr(f"xl/worksheets/{files[name]}", f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{MAIN}" xmlns:r="{R}"><sheetData>{"".join(rows_xml)}</sheetData>{link_xml}</worksheet>')
        items = "".join(f"<si>{x}</si>" for x in shared)
        z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{MAIN}" count="{len(shared)}" uniqueCount="{len(shared)}">{items}</sst>')
    return path
