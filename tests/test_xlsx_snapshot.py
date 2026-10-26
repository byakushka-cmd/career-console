import pytest

from app.xlsx_snapshot import SnapshotFormatError, read_workbook, split_ref
from tests.xlsx_factory import Inline, Rich, build_xlsx


def test_reads_by_sheet_name_with_types_and_blanks(tmp_path):
    path = build_xlsx(tmp_path / "w.xlsx", {
        "First": [["text", 46265, 1.5, "", None, True]],
        "Second": [["only"]],
    })
    book = read_workbook(path, {"Second": 1, "First": 6})
    assert book["Second"].rows == [["only"]]  # second file on disk is sheet1.xml: lookup is by name
    assert book["First"].rows == [["text", 46265, 1.5, None, None, True]]
    assert isinstance(book["First"].rows[0][1], int)
    assert book["First"].empty_shared_string_cells == ["D1"]


def test_rich_text_and_inline_strings(tmp_path):
    path = build_xlsx(tmp_path / "w.xlsx", {"S": [[Rich(["Senior ", "QA"]), Inline("inline")]]})
    assert read_workbook(path, {"S": 2})["S"].rows == [["Senior QA", "inline"]]


def test_hyperlink_targets_are_returned(tmp_path):
    path = build_xlsx(tmp_path / "w.xlsx", {"S": [["Acme", "https://a.example/jobs/1"]]},
                      hyperlinks={"S": {"A1": "http://acme.example", "B1": "https://a.example/jobs/1"}})
    assert read_workbook(path, {"S": 2})["S"].hyperlinks == {"A1": "http://acme.example", "B1": "https://a.example/jobs/1"}


def test_data_beyond_expected_columns_is_an_error_not_dropped(tmp_path):
    path = build_xlsx(tmp_path / "w.xlsx", {"S": [["a", "b", "c"]]})
    with pytest.raises(SnapshotFormatError, match="outside the expected 2 columns"):
        read_workbook(path, {"S": 2})


def test_blank_cell_beyond_expected_columns_is_ignored(tmp_path):
    path = build_xlsx(tmp_path / "w.xlsx", {"S": [["a", "b", ""]]})
    assert read_workbook(path, {"S": 2})["S"].rows == [["a", "b"]]


def test_missing_sheet_is_an_error(tmp_path):
    path = build_xlsx(tmp_path / "w.xlsx", {"S": [["a"]]})
    with pytest.raises(SnapshotFormatError, match="missing sheets"):
        read_workbook(path, {"S": 1, "Other": 1})


def test_split_ref():
    assert split_ref("O53") == (52, 14)
    assert split_ref("AA1") == (0, 26)
