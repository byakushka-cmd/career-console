"""My CV: only the header lines of a copy change. The CV here is generated, for a fictional person."""
import io
import json

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from pypdf.generic import ContentStream
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas

from app import cv, db
from app.cli import main
from app.web import create_app

SAME = {"Origin": "http://testserver"}
TITLE, TAGLINE = "HEAD OF QA", "Automation  |  CI/CD  |  Quality Strategy"
BODY = ["PROFILE", "Led a QA team at Acme Cloud for eight years.", "EXPERIENCE", "Initech — QA Engineer"]


def make_pdf(pages=1) -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(612, 792))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(30, 745, "ALEX SAMPLE")
    c.setFillColor(Color(0.08, 0.25, 0.35))
    c.setFont("Helvetica-Bold", 12.5)
    c.drawString(30, 725, TITLE)
    c.setFillColor(Color(0.44, 0.44, 0.44))
    c.setFont("Helvetica", 8.5)
    c.drawString(30, 711, TAGLINE)
    c.setFont("Helvetica", 9)
    c.drawString(470, 728, "alex@example.com")
    c.drawString(470, 716, "054-0000000")
    c.setFillColor(Color(0, 0, 0))
    for i, text in enumerate(BODY):
        c.drawString(30, 660 - i * 14, text)
    for _ in range(pages - 1):
        c.showPage()
        c.setFont("Helvetica", 9)
        c.drawString(30, 700, "Second page text")
    c.save()
    return out.getvalue()


def texts(data):
    return [line.text for line in cv.page_lines(data)]


def test_reads_the_lines_and_guesses_the_header():
    lines = cv.page_lines(make_pdf())
    assert lines[0].text == "ALEX SAMPLE" and lines[0].size == 22
    assert [lines[i].text for i in cv.header_guess(lines)] == [TITLE, TAGLINE]   # not the contacts on the right


def test_a_contact_line_under_the_name_is_not_header():
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(612, 792))
    c.setFont("Helvetica-Bold", 22)
    c.drawString(30, 745, "ALEX SAMPLE")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(30, 727, TITLE)
    c.setFont("Helvetica", 8)
    c.drawString(30, 716, "Israel | 054-0000000 | alex@example.com")
    c.save()
    lines = cv.page_lines(out.getvalue())
    assert [lines[i].text for i in cv.header_guess(lines)] == [TITLE]


def test_a_copy_changes_only_the_header_and_the_old_text_is_gone():
    data = make_pdf(pages=2)
    head = cv.header_guess(cv.page_lines(data))
    out = cv.retitle(data, {head[0]: "QA TEAM LEAD / PLAYWRIGHT", head[1]: "Playwright  |  API  |  Fintech"})
    after = texts(out)
    assert "QA TEAM LEAD / PLAYWRIGHT" in after and "Playwright  |  API  |  Fintech" in after
    full_text = " ".join(page.extract_text() for page in PdfReader(io.BytesIO(out)).pages)
    assert TITLE not in full_text and "Quality Strategy" not in full_text    # taken out, not covered up
    for kept in ["ALEX SAMPLE", "alex@example.com", "054-0000000", *BODY, "Second page text"]:
        assert kept in full_text
    assert len(PdfReader(io.BytesIO(out)).pages) == 2


def test_a_new_line_keeps_the_place_and_size_of_the_old_one():
    data = make_pdf()
    before = cv.page_lines(data)
    head = cv.header_guess(before)
    after = {line.text: line for line in cv.page_lines(cv.retitle(data, {head[0]: "QA LEAD"}))}
    assert after["QA LEAD"].x == pytest.approx(before[head[0]].x, abs=0.1)
    assert after["QA LEAD"].y == pytest.approx(before[head[0]].y, abs=0.1)
    assert after["QA LEAD"].size == before[head[0]].size


def test_the_new_lines_stay_in_the_order_of_the_page_with_their_colour():
    data = make_pdf()
    head = cv.header_guess(cv.page_lines(data))
    out = cv.retitle(data, {head[0]: "QA TEAM LEAD", head[1]: "Playwright  |  API"})
    words = PdfReader(io.BytesIO(out)).pages[0].extract_text()
    assert words.index("ALEX SAMPLE") < words.index("QA TEAM LEAD") < words.index("Playwright") < words.index("PROFILE")
    operations = ContentStream(PdfReader(io.BytesIO(out)).pages[0].get_contents(), None).operations
    fills = [tuple(round(float(v), 2) for v in operands) for operands, operator in operations if operator == b"rg"]
    assert (0.08, 0.25, 0.35) in fills and (0.44, 0.44, 0.44) in fills


def test_a_line_in_a_block_with_other_lines_leaves_their_font_alone():
    """Some programs write several lines in one text block: the next line must keep its own font and spacing."""
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 20 Tf 30 740 Td (ALEX SAMPLE) Tj /F1 12 Tf 0.5 Tc 0 -20 Td (HEAD OF QA) Tj 0 -20 Td (Next line) Tj ET")
    page[NameObject("/Contents")] = w._add_object(content)
    buffer = io.BytesIO()
    w.write(buffer)
    lines = cv.page_lines(buffer.getvalue())
    number = next(l.index for l in lines if l.text.replace(" ", "") == "HEADOFQA")
    out = cv.retitle(buffer.getvalue(), {number: "QA LEAD"})
    after = cv.page_lines(out)
    assert any(l.text == "QA LEAD" and l.size == 12 for l in after)
    assert any(l.text.replace(" ", "") == "Nextline" and l.size == 12 and l.font.endswith("Helvetica") for l in after)
    operations = ContentStream(PdfReader(io.BytesIO(out)).pages[0].get_contents(), None).operations
    assert [float(o[0]) for o, op in operations if op == b"Tc"] == [0.5, 0, 0.5]     # off for the new line, then back


def test_a_long_line_shrinks_a_little_and_stops_before_the_contacts():
    data = make_pdf()
    head = cv.header_guess(cv.page_lines(data))
    text = "QA TEAM LEAD / QUALITY ENGINEERING / AUTOMATION / API / CLOUD"
    line = next(l for l in cv.page_lines(cv.retitle(data, {head[0]: text})) if l.text == text)
    assert line.size < 12.5
    with pytest.raises(ValueError, match="too long"):
        cv.retitle(data, {head[0]: text * 2})


def test_hebrew_in_the_header_is_refused_in_words():
    data = make_pdf()
    with pytest.raises(ValueError, match="Hebrew"):
        cv.retitle(data, {cv.header_guess(cv.page_lines(data))[0]: "ראש צוות QA"})


@pytest.fixture
def client(imported_db):
    return TestClient(create_app(imported_db, allowed_hosts=("testserver",)))


def upload(client, data=None, name="Alex_Sample-1.pdf"):
    return client.post("/cv/upload", headers=SAME, files={"file": (name, data or make_pdf(), "application/pdf")},
                       follow_redirects=False)


def added_job(client):
    r = client.post("/added", headers=SAME, data={"company": "Acme Cloud", "role": "QA Lead", "state": "DISCOVERED"},
                    follow_redirects=False)
    assert r.status_code == 303
    return r.headers["location"].split("?")[0]


def test_upload_lists_the_cv_and_keeps_the_file_private_on_disk(client, imported_db):
    data = make_pdf()
    assert upload(client, data).status_code == 303
    page = client.get("/cv").text
    assert "Alex_Sample-1.pdf" in page and TITLE in page
    conn = db.connect(imported_db, create=False)
    row = cv.files(conn)[0]
    assert (cv.folder(imported_db) / row["path"]).read_bytes() == data     # byte for byte, multipart or not
    assert client.get(f"/cv/{row['id']}/file").content == data


def test_upload_refuses_what_it_cannot_use(client):
    assert upload(client, b"not a pdf at all").status_code == 400
    assert "does not read as a PDF" in upload(client, b"not a pdf at all").text
    empty = io.BytesIO()
    c = canvas.Canvas(empty)
    c.showPage()
    c.save()
    assert "no text" in upload(client, empty.getvalue()).text


def test_upload_is_refused_from_another_site(client):
    r = client.post("/cv/upload", headers={"Origin": "http://evil.example"}, files={"file": ("a.pdf", make_pdf())})
    assert r.status_code == 403


def test_same_file_twice_is_one_cv(client, imported_db):
    data = make_pdf()
    upload(client, data)
    upload(client, data)
    assert len(cv.files(db.connect(imported_db, create=False))) == 1


def copy_form(conn, job):
    f = cv.files(conn)[0]
    return {"cv_id": f["id"], "ref": "AD 1", "company": "Acme Cloud / Initech", "role": "QA Lead", "href": job}, f


def test_a_job_page_makes_a_copy_and_offers_it_for_download(client, imported_db):
    upload(client)
    job = added_job(client)
    assert "CV for this job" in client.get(job).text
    conn = db.connect(imported_db, create=False)
    form, f = copy_form(conn, job)
    head = f["header"]
    form.update({f"line_{head[0]}": "QA TEAM LEAD", f"line_{head[1]}": TAGLINE})
    r = client.post("/cv/copy", headers=SAME, data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"{job}#cv"
    version = cv.versions(conn)[0]
    assert version["lines"][head[0]] == "QA TEAM LEAD" and version["lines"][head[1]] == TAGLINE
    assert "QA TEAM LEAD" in client.get("/cv").text
    pdf = client.get(f"/cv/copies/{version['id']}")
    assert pdf.headers["content-type"] == "application/pdf"
    assert 'filename="Alex_Sample_Acme_Cloud.pdf"' in pdf.headers["content-disposition"]   # no "-1", no second company
    assert "QA TEAM LEAD" in texts(pdf.content) and TITLE not in texts(pdf.content)


def test_a_line_that_is_not_header_cannot_be_changed(client, imported_db):
    upload(client)
    job = added_job(client)
    conn = db.connect(imported_db, create=False)
    form, f = copy_form(conn, job)
    name_line = next(i for i, text in f["top"] if text == "ALEX SAMPLE")
    form[f"line_{name_line}"] = "SOMEONE ELSE"
    form[f"line_{f['header'][0]}"] = "QA TEAM LEAD"
    assert client.post("/cv/copy", headers=SAME, data=form, follow_redirects=False).status_code == 303
    assert "ALEX SAMPLE" in texts(client.get(f"/cv/copies/{cv.versions(conn)[0]['id']}").content)


def test_nothing_changed_or_too_long_keeps_what_you_typed(client, imported_db):
    upload(client)
    job = added_job(client)
    conn = db.connect(imported_db, create=False)
    form, f = copy_form(conn, job)
    head = f["header"]
    r = client.post("/cv/copy", headers=SAME, data={**form, f"line_{head[0]}": TITLE})
    assert r.status_code == 400 and "Nothing to change" in r.text
    long_line = "VERY LONG TITLE " * 12
    r = client.post("/cv/copy", headers=SAME, data={**form, f"line_{head[0]}": long_line.strip()})
    assert r.status_code == 400 and "too long" in r.text and "VERY LONG TITLE VERY" in r.text
    assert cv.versions(conn) == []


def test_you_choose_which_lines_are_the_header(client, imported_db):
    upload(client)
    conn = db.connect(imported_db, create=False)
    f = cv.files(conn)[0]
    r = client.post(f"/cv/{f['id']}/header", headers=SAME, data={"line": [str(f["header"][0])]}, follow_redirects=False)
    assert r.status_code == 303
    assert cv.files(conn)[0]["header"] == [f["header"][0]]


def test_removed_cv_leaves_the_list_but_its_copies_stay(client, imported_db):
    data = make_pdf()
    upload(client, data)
    job = added_job(client)
    conn = db.connect(imported_db, create=False)
    form, f = copy_form(conn, job)
    client.post("/cv/copy", headers=SAME, data={**form, f"line_{f['header'][0]}": "QA LEAD"})
    client.post(f"/cv/{f['id']}/remove", headers=SAME)
    assert cv.files(conn) == []
    assert client.get(f"/cv/copies/{cv.versions(conn)[0]['id']}").status_code == 200
    upload(client, data)    # uploading it again brings it back
    assert len(cv.files(conn)) == 1


def test_a_job_page_without_a_cv_points_to_my_cv(client):
    assert "upload it in My CV" in client.get(added_job(client)).text


def test_copy_from_the_command_line(client, imported_db, tmp_path, capsys):
    path = tmp_path / "Alex_Sample.pdf"
    path.write_bytes(make_pdf())
    added_job(client)
    conn = db.connect(imported_db, create=False)
    ref = conn.execute("SELECT max(id) AS id FROM added_records").fetchone()["id"]
    main(["cv", "upload", str(path), "--db", str(imported_db)])
    main(["cv", "copy", "--cv", "1", "--ref", f"AD {ref}", "--line", "QA TEAM LEAD", "--line", TAGLINE, "--db", str(imported_db)])
    assert f"AD {ref}" in capsys.readouterr().out
    version = cv.versions(conn, f"AD {ref}")[0]
    assert version["origin"] == "cli" and version["company"] == "Acme Cloud"
    with pytest.raises(SystemExit):
        main(["cv", "copy", "--cv", "1", "--ref", f"AD {ref}", "--line", "only one", "--db", str(imported_db)])
    with pytest.raises(SystemExit):
        main(["cv", "copy", "--cv", "1", "--ref", "AD 99999", "--line", "a", "--line", "b", "--db", str(imported_db)])


def test_a_job_page_shows_the_first_requirements():
    from search.sources import first_requirements
    text = "Description\nGreat team.\nRequirements\n7+ years in automation\n\nLeadership role\nFrameworks from scratch\nAgile\nPython\nAdvantage"
    assert first_requirements(text) == ["7+ years in automation", "Leadership role", "Frameworks from scratch", "Agile"]
    assert first_requirements("דרישות התפקיד:\nניסיון של 5 שנים") == ["ניסיון של 5 שנים"]
    assert first_requirements("Requirements for a good day are many.\nCoffee") == []
    assert first_requirements("") == []

