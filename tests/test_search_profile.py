"""The Israel geography gate and keyword gates are tested on their own, as the spec requires."""
from datetime import datetime, timezone

import pytest

from search.profile import DEFAULT_PROFILE, find_terms, freshness, geography, level, relevance, validate_profile


@pytest.mark.parametrize("location, countries, remote, description, expected", [
    ("Tel Aviv-Yafo, Tel Aviv District, Israel", None, None, "", "ISRAEL"),
    ("Herzliya", None, None, "", "ISRAEL"),
    ("תל אביב", None, None, "", "ISRAEL"),
    ("Hybrid", ["IL"], None, "", "ISRAEL"),                       # structured country beats vague text
    ("Tel Aviv, IL", None, None, "", "ISRAEL"),                   # IL here means Israel, not Illinois
    ("New York, NY / Tel Aviv", None, None, "", "ISRAEL"),         # multi-location including Israel
    ("Remote - Worldwide", None, True, "", "WORLDWIDE_REMOTE"),
    ("Anywhere", None, None, "", "WORLDWIDE_REMOTE"),
    ("Remote - Worldwide", None, True, "This role is US only.", "UNKNOWN"),  # restriction in the text
    ("New York, NY", None, None, "", "OUTSIDE"),
    ("Remote, US", None, True, "", "OUTSIDE"),
    ("Austin, TX", None, None, "", "OUTSIDE"),
    ("London", None, None, "", "OUTSIDE"),
    ("Remote", ["US"], True, "", "OUTSIDE"),
    ("Remote - EMEA", None, True, "", "UNKNOWN"),                 # region without saying Israel
    ("Remote", None, True, "", "UNKNOWN"),
    ("", None, None, "", "UNKNOWN"),
    ("Springfield, IL", None, None, "", "UNKNOWN"),               # never hidden as 'outside' on a guess
    ("Kibbutz Somewhere", None, None, "", "UNKNOWN"),
    ("IND-Remote", None, None, "", "OUTSIDE"),                    # ISO-3 country prefix
    ("ISR-Remote", None, None, "", "ISRAEL"),
    ("ABC-Remote", None, None, "", "UNKNOWN"),                    # unknown code: review, not hidden
])
def test_israel_gate(location, countries, remote, description, expected):
    assert geography(location, countries, remote, description).value == expected


def test_israeli_tech_hubs_are_israel():
    for loc in ("Glilot", "Glilot; Tel Aviv", "Ramat HaSharon", "גלילות"):
        assert geography(loc).value == "ISRAEL", loc   # Island's "Test Development Engineer - Glilot"


def test_geo_verdicts_always_explain_themselves():
    for loc in ("Haifa", "Berlin", "Remote", "Remote - Worldwide"):
        assert geography(loc).reason


@pytest.mark.parametrize("title, expected, low", [
    ("Senior QA Automation Engineer", "MATCH", []),
    ("QA Team Lead", "MATCH", []),
    ("SDET - Payments API", "MATCH", []),
    ("בודק/ת תוכנה", "MATCH", []),
    ("Тестировщик", "MATCH", []),
    ("Junior Manual QA", "MATCH", ["junior", "manual"]),          # low priority, never hidden for it
    ("Embedded Software QA Engineer", "EXCLUDED", []),
    ("Firmware Validation & Test Engineer", "EXCLUDED", []),
    ("Design Verification Engineer", "EXCLUDED", []),
    ("Quality Control Inspector", "EXCLUDED", []),
    ("QA Regulatory Affairs Specialist", "EXCLUDED", []),
    ("Backend Engineer", "NO_MATCH", []),
    ("Contest Manager", "NO_MATCH", []),                          # whole words: 'test' ≠ 'contest'
    ("System Validation Engineer", "MATCH", []),
    ("Process Quality Engineer", "EXCLUDED", []),                 # manufacturing quality
])
def test_relevance(title, expected, low):
    verdict, _, low_terms, _ = relevance(title, DEFAULT_PROFILE)
    assert verdict.value == expected
    assert low_terms == low


def test_freshness_marks_but_never_guesses():
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert freshness("2026-10-09T09:00:00-04:00", DEFAULT_PROFILE, now).label == "Fresh"      # 4 days
    assert freshness("2026-10-01T00:00:00Z", DEFAULT_PROFILE, now).label == "Aging"           # 13 days
    old = freshness("2026-09-20T00:00:00Z", DEFAULT_PROFILE, now)                             # 24 days: > 2 weeks
    assert (old.label, old.days) == ("Older than 14 days", 24)
    updated = freshness(None, DEFAULT_PROFILE, now, updated_at="2026-10-12T10:12:38Z")         # Comeet: no posting date
    assert updated.label == "Updated" and updated.days == 1 and "updated 2026-10-12" in updated.basis
    assert freshness(None, DEFAULT_PROFILE, now).label == "Date unknown"


def test_profile_validation():
    p = validate_profile({"include_terms": "QA\n qa \n\nSDET", "exclude_terms": "", "low_priority_terms": "",
                          "fresh_days": "5", "max_age_days": "21"})
    assert p["include_terms"] == ["QA", "SDET"] and (p["fresh_days"], p["max_age_days"]) == (5, 21)
    assert validate_profile({"include_terms": "qa"})["max_age_days"] == 14              # older forms: the default
    for bad in ({"include_terms": ""}, {"include_terms": "qa", "fresh_days": 30, "max_age_days": 14},
                {"include_terms": "qa", "fresh_days": "x"}):
        with pytest.raises(ValueError):
            validate_profile(bad)


REGULATORY = ("Strong knowledge of FDA requirements, MDR regulation, ISO 13485, ISO 62304, ISO 14971 and IEC 62366.")


def test_regulatory_description_excludes_a_neutral_title():
    verdict, _, _, mentions = relevance("Senior Quality Specialist", DEFAULT_PROFILE, REGULATORY)
    assert verdict.value == "EXCLUDED" and "manufacturing-quality terms" in verdict.reason
    assert {"fda", "mdr", "13485", "62304", "14971", "62366"} <= set(mentions)


def test_single_regulatory_mention_only_marks():
    boilerplate = "Our company has a record number of FDA-cleared solutions. You will automate API tests."
    verdict, _, _, mentions = relevance("QA Automation Engineer", DEFAULT_PROFILE, boilerplate)
    assert verdict.value == "MATCH" and mentions == ["fda"]


def test_mdr_alone_is_not_enough():  # MDR also means Managed Detection & Response in security
    verdict, _, _, mentions = relevance("QA Engineer, MDR platform", DEFAULT_PROFILE, "Test our MDR detection pipeline.")
    assert verdict.value == "MATCH" and mentions == ["mdr"]


def test_standards_without_spaces_are_recognised():
    _, _, _, mentions = relevance("QA Engineer", DEFAULT_PROFILE, "Experience with ISO13485 and IEC62304:2006")
    assert {"13485", "62304"} <= set(mentions)


def test_description_threshold_is_configurable():
    strict = validate_profile({**DEFAULT_PROFILE, "description_min_hits": "1"})
    verdict, *_ = relevance("Quality Engineer", strict, "FDA-cleared products")   # not a software title
    assert verdict.value == "EXCLUDED"
    with pytest.raises(ValueError):
        validate_profile({**DEFAULT_PROFILE, "description_min_hits": "0"})


def test_role_summary_skips_the_about_us_opening():
    from search.sources import role_summary
    text = "Acme is the leading platform for everything.\nWe raised a lot.\nAbout the role\nYou will own API test automation."
    assert role_summary(text).startswith("About the role You will own API test automation")
    assert role_summary("Short text without headings") == "Short text without headings"
    assert role_summary("word " * 300).endswith("…")


def test_apply_destination_says_where_the_button_goes():
    from search.sources import apply_destination
    assert apply_destination("https://job-boards.greenhouse.io/island/jobs/1", "Island", "greenhouse") == "Island's own form (Greenhouse)"
    assert apply_destination("https://www.catonetworks.com/careers/x?gh_jid=1", "Cato Networks", "greenhouse") == "catonetworks.com"


def test_medicine_is_fine_medical_devices_are_not():
    text = "Our AI is FDA-cleared; you will test software built under IEC 62304 and ISO 13485."
    verdict, _, _, mentions = relevance("QA Automation Engineer", DEFAULT_PROFILE, text)   # software role at a medical company
    assert verdict.value == "MATCH" and "only marked" in verdict.reason and "fda" in mentions
    assert relevance("Senior Quality Specialist", DEFAULT_PROFILE, text)[0].value == "EXCLUDED"  # regulatory quality role
    assert relevance("Digital Health SW QA Engineer", DEFAULT_PROFILE, text)[0].value == "MATCH"
    assert relevance("Medical Device QA Engineer", DEFAULT_PROFILE, "")[0].value == "EXCLUDED"     # devices: the title says so


def test_manufacturing_quality_is_excluded_by_its_description():
    text = ("Lead yield and defect reduction: SPC, process capability, 8D, FMEA, DOE; drive NCR and CAPA; "
            "supplier audits; MTBF and failure analysis in semiconductor manufacturing.")
    verdict, *_ = relevance("Quality & Reliability Lead", DEFAULT_PROFILE, text)
    assert verdict.value == "EXCLUDED" and "spc" in verdict.reason
    software = relevance("QA Automation Engineer", DEFAULT_PROFILE, "Own failure analysis of flaky tests.")[0]
    assert software.value == "MATCH"   # one mention in a software job: nothing happens


@pytest.mark.parametrize("title, expected", [
    ("QA & Automation Team Lead", "FOCUS"), ("QA Manager", "FOCUS"), ("Head of QA", "FOCUS"),
    ("Automation Infrastructure Tech Lead", "FOCUS"), ("ראש צוות בדיקות אוטומציה", "FOCUS"),
    ("Senior QA Automation Engineer", "ENGINEER"), ("Director of Quality Engineering", "ABOVE"), ("VP Quality", "ABOVE"),
    ("Graduate QA Automation Engineer", "JUNIOR"), ("Junior QA Engineer", "JUNIOR"),
])
def test_level_is_read_from_the_title(title, expected):
    from search.profile import level
    assert level(title, DEFAULT_PROFILE) == expected


def test_quality_or_validation_alone_is_flagged_for_a_check():
    from search.profile import narrow_match
    assert narrow_match(["validation"]) and narrow_match(["quality"]) and not narrow_match(["qa", "quality"])


@pytest.mark.parametrize("text, expected", [
    ("2-3 years of experience in QA", True),                         # mid-level: below 10+ years
    ("3+ years of hands-on experience with Selenium", True),
    ("ניסיון של 3 שנים בבדיקות תוכנה", True),
    ("5+ years of experience in software testing", False),
    ("3 to 5 years of experience", False),                           # a range counts by its top
    ("7 years of QA experience\n2+ years of management experience", False),
    ("3+ years of experience leading a team", False),                 # management years never count
    ("We grew 3x in 2 years.", False),                                # not about experience
    ("No years mentioned at all", False),
    ("5+ years in QA with a strong focus on test automation.\nHands-on experience with Cursor (ideally 1+ year).", False),
    ("2 years in QA automation", True),                              # "years in" is about experience too
    ("3+ years of experience leading QA engineers on top of 5+ years of hands-on QA.\n"
     "Minimum 3 years of application testing across web systems.", False),   # 5 of QA asked: not mid-level
])
def test_likely_overqualified_reads_the_years_asked(text, expected):
    from search.profile import likely_overqualified
    assert likely_overqualified(text, DEFAULT_PROFILE)[0] is expected


@pytest.mark.parametrize("title, expected", [
    ("ראש/ת צוות QA", "FOCUS"),                     # both genders written with a slash
    ("ראש צוות בדיקות אוטומציה", "FOCUS"),
    ("בודק.ת תוכנה לארגון ביטחוני", "ENGINEER"),
    ("General application / QA leadership", "FOCUS"),
    ("8073 מוביל/ה טכנולוגי/ת QA & Automation + AI", "FOCUS"),   # a tech lead, in Hebrew
    ("Senior QA Engineer", "ENGINEER"),
])
def test_level_reads_hebrew_gender_forms_and_leadership(title, expected):
    assert level(title, DEFAULT_PROFILE) == expected


def test_hebrew_gender_forms_still_match_search_terms():
    assert find_terms("דרוש/ה בודק/ת תוכנה", ["בודק"]) == ["בודק"]
    assert find_terms("מהנדס/ת בדיקות תוכנה", ["בדיקות"]) == ["בדיקות"]


@pytest.mark.parametrize("location, regions", [
    ("Kinneret", {"north"}), ("Haifa District, Israel", {"north"}), ("Caesarea, Haifa District, Israel", {"sharon", "north"}),
    ("Center District, Israel", {"center"}), ("Tel Aviv-Yafo", {"tel_aviv"}), ("Herzliya", {"tel_aviv", "sharon"}),
    ("ירושלים", {"jerusalem"}), ("Israel", set()), ("Israel (3 locations)", set()), ("Remote", set()),
])
def test_regions_of_israeli_locations(location, regions):
    from search.profile import regions_of
    assert regions_of(location) == regions


def test_a_job_is_held_back_only_when_all_its_regions_are_unwanted():
    from search.profile import excluded_place
    prof = {**DEFAULT_PROFILE, "exclude_regions": ["north"]}
    assert excluded_place("Yokneam Ilit", prof) == ["North & Haifa"]
    assert excluded_place("Caesarea, Haifa District, Israel", prof) == []      # also Sharon, which you want
    assert excluded_place("Israel (2 locations)", prof) == []
    assert validate_profile({**DEFAULT_PROFILE, "exclude_regions": ["north", "mars"]})["exclude_regions"] == ["north"]
