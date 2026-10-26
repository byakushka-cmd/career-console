"""Staffing agencies: a vacancy is title + number; same text from several sources is linked, never hidden."""
import pytest

from search import agencies, pipeline, similar


@pytest.mark.parametrize("text, numbers", [
    ("Automation Team Lead — req 145006", ["145006"]),
    ("QA Team Lead (Req 94512)", ["94512"]),
    ("QA Team Lead – Digital (Position 23231)", ["23231"]),
    ("Senior QA Engineer #550", ["550"]),
    ("QA Domain Manager (37477)", ["37477"]),
    ("Digital QA Tester (LinkedIn 4474177887)", ["4474177887"]),
    ("משרה מס' 12345 בודק/ת", ["12345"]),
    ("Joined in 2026 (2026), Req 95212", ["95212"]),     # years are not vacancy numbers
    ("QA Engineer (100% remote)", []),                  # open bracket + percent is not a number
    ("QA Engineer", []),
])
def test_vacancy_numbers(text, numbers):
    assert agencies.vacancy_numbers(text) == numbers


def test_descriptions_only_count_numbers_next_to_a_marker_word():
    text = "We are the #1 platform with #500 customers. Job ID: 48213. Apply now."
    assert agencies.vacancy_numbers(text, marked_only=True) == ["48213"]


def test_company_key_groups_agency_clients_and_plain_companies():
    assert agencies.company_key("Gotfriends / confidential hotel") == "gotfriends"
    assert agencies.company_key("Acme Cloud / confidential") == "acme cloud"


def test_any_company_same_number_is_the_same_vacancy():
    h = pipeline.History()
    hit = pipeline.TrackerHit(1, "Master Tracker", 40, "APPLIED")
    h.by_identity["vac:acme cloud:4821"] = [hit]
    verdict, _, _ = h.classify("greenhouse:9", "Acme Cloud", "Platform QA Engineer", None, None, ["4821"])
    assert verdict == "HISTORY"
    verdict, _, _ = h.classify("greenhouse:9", "Acme Cloud", "Backend Developer", None, None, ["4822"])
    assert verdict == "NEW"  # a different number at the same company is a different vacancy


def test_agency_names_match_as_words():
    assert agencies.agency_key("Gotfriends / confidential client") == "gotfriends"
    assert agencies.agency_key("NESS / Bank Acme") == "ness"
    assert agencies.agency_key("Business Applications Ltd") is None   # 'ness' inside a word does not count
    assert agencies.agency_key("Hunters") is None


def history_with(state):
    h = pipeline.History()
    hit = pipeline.TrackerHit(1, "Master Tracker", 113, state)
    h.by_title[("gotfriends", "qa team leader")] = [hit]
    h.by_identity["vac:gotfriends:151117"] = [hit]
    return h


def test_agency_title_match_alone_never_hides_a_new_vacancy():
    verdict, _, note = history_with("REJECTED").classify("comeet:x:1", "Gotfriends", "QA Team Leader", None,
                                                          "gotfriends", ["160001"])
    assert verdict == "AGENCY_SIMILAR" and "compare the job ID" in note


def test_agency_same_number_is_the_same_vacancy():
    verdict, _, _ = history_with("REJECTED").classify("comeet:x:1", "Gotfriends", "QA Team Leader", None,
                                                       "gotfriends", ["151117"])
    assert verdict == "HISTORY"


def test_employer_title_match_still_counts_as_possible_history():
    verdict, _, _ = history_with("REJECTED").classify("greenhouse:1", "Gotfriends", "QA Team Leader", None)
    assert verdict == "POSSIBLE_HISTORY"  # without the agency flag: the employer rule applies


class View:
    def __init__(self, pid, company, text):
        self.company = company
        self.posting = {"id": pid, "description": text, "description_sha": str(hash(text))}
        self.similar = []


def words(prefix, n):  # distinct letter-only words (the comparison ignores digits)
    return " ".join(prefix + chr(97 + i // 26) + chr(97 + i % 26) for i in range(n))


JOB = words("job", 60) + " playwright api automation lead"


def test_same_vacancy_from_several_agencies_is_linked_not_hidden():
    a, b = View(1, "Gotfriends", JOB + " gotfriends"), View(2, "Ethosia", JOB + " ethosia apply now")
    other_role = View(3, "Gotfriends", words("other", 60))
    similar.link_similar([a], [a, b, other_role])
    assert [o.posting["id"] for o, _ in a.similar] == [2]


def test_within_one_company_only_near_identical_texts_are_linked():
    about = words("about", 50)
    a = View(1, "Island", about + " " + words("net", 12))
    b = View(2, "Island", about + " " + words("qat", 12))
    similar.link_similar([a], [b])
    assert a.similar == []  # shared "about us" text alone is not the same vacancy


def test_the_end_client_of_an_agency_job_is_read_from_its_name():
    from search.agencies import client_words, DEFAULT_AGENCIES as T
    assert client_words("NESS / Bank Acme", "QA Team Lead — Lod (NESS possible req #12345)", T) == {"acme"}
    assert client_words("Nisha Pro", "QA Automation Lead / Pepper (Bank Acme)", T) == {"pepper"}   # a unit of the bank
    assert client_words("Ethosia", "QA Team Lead (Bank Acme)", T) == {"acme"}                    # brackets name the client
    assert client_words("Gotfriends / confidential hotel procurement marketplace", "QA Engineer", T) == set()
    assert client_words("NiCE", "Quality Director — Req #10834 (Raanana)", T) == {"nice"}     # an employer: itself


def test_the_same_client_through_two_recruiters_is_linked_not_merged():
    from app import web
    from search.profile import DEFAULT_PROFILE
    row = lambda i, company, role, state: web.Record(i, web.MASTER, i, [company, role] + [None] * 15, state, None, 0, 0)
    a = row(1, "Nisha Pro", "QA Automation Lead / Pepper (Bank Acme)", "CONTACT_ONLY")
    b = row(2, "Matrix / Pepper", "QA Team Lead", "APPLIED")
    c = row(3, "NESS / Bank Acme", "QA Team Lead — Lod", "INTERVIEW")       # the bank itself: another employer
    d = row(4, "Gotfriends / confidential bank", "QA Team Lead", "APPLIED")   # unnamed client
    e = row(5, "Matrix / Pepper", "Senior QA Engineer", "APPLIED")           # same client, another level
    web.link_possibly_same([a, b, c, d, e], list(DEFAULT_PROFILE["agency_companies"]), DEFAULT_PROFILE)
    assert [r.id for r, _ in a.possibly_same] == [2] and [r.id for r, _ in b.possibly_same] == [1]
    assert "same end client: pepper" == a.possibly_same[0][1]
    assert c.possibly_same == [] and d.possibly_same == [] and e.possibly_same == []


def test_unnamed_clients_are_linked_by_their_rare_requirements():
    from app import web
    from search.profile import DEFAULT_PROFILE
    def row(i, company, role, note, state="APPLIED"):
        values = [company, role] + [None] * 15
        values[13] = note
        return web.Record(i, web.MASTER, i, values, state, None, 0, 0)
    pepper = "Mobile automation: React Native, Appium or Detox, Cucumber/Gherkin, CI/CD."
    a = row(1, "Nisha Pro", "QA Automation Lead", "Recruiter call. " + pepper, "CONTACT_ONLY")
    b = row(2, "Matrix R&D Services / confidential financial client", "QA Automation Lead", "Drushim ad. " + pepper)
    c = row(3, "Elad Software Systems", "QA Automation Lead", "Web: Selenium, Java, SQL, Jenkins.")
    d = row(4, "Gotfriends / confidential", "Senior QA Engineer", pepper)          # same needs, another level
    others = [row(10 + i, f"Employer {i}", "QA Engineer", "Web testing, Jira, SQL, Selenium.") for i in range(30)]
    web.link_possibly_same([a, b, c, d] + others, list(DEFAULT_PROFILE["agency_companies"]), DEFAULT_PROFILE)
    assert [r.id for r, _ in a.possibly_same] == [2] and "react native" in a.possibly_same[0][1]
    assert c.possibly_same == [] and d.possibly_same == []


def test_an_engineer_job_at_an_agency_you_applied_to_as_a_lead_is_marked():
    from search.profile import DEFAULT_PROFILE
    h = pipeline.History(agency_terms=list(DEFAULT_PROFILE["agency_companies"]), profile=dict(DEFAULT_PROFILE))
    lead = pipeline.TrackerHit(1, "Master Tracker", 113, "APPLIED", "QA Team Leader — EdTech (Req 151117)", "2026-10-07")
    rejected = pipeline.TrackerHit(2, "Master Tracker", 128, "REJECTED", "QA Team Lead — Req #147776", "2026-10-09")
    not_sent = pipeline.TrackerHit(3, "Master Tracker", 96, "EXPIRED", "Automation Team Lead — req 145006")
    engineer = pipeline.TrackerHit(4, "Master Tracker", 131, "APPLIED", "Senior QA Engineer", "2026-10-01")
    h.by_agency["gotfriends"] = [lead, rejected, not_sent, engineer]
    assert h.applied_as_lead("Gotfriends / confidential cloud", "ENGINEER") == [rejected, lead]   # latest first
    assert h.applied_as_lead("Gotfriends", "FOCUS") == []       # a lead job there is your level: nothing to mark
    assert h.applied_as_lead("Island", "ENGINEER") == []        # an employer, not an agency


def test_agency_records_below_your_level_list_your_lead_applications_there():
    from app import web
    from search.profile import DEFAULT_PROFILE
    def row(i, company, role, state, agency):
        return web.Record(i, web.MASTER, i, [company, role, f"2026-10-0{i}"] + [None] * 14, state, None, 0, 0,
                          agency=agency)
    lead = row(1, "Gotfriends", "QA Team Leader — EdTech", "APPLIED", "gotfriends")
    engineer = row(2, "Gotfriends / confidential cloud", "Senior QA & Automation Engineer", "HOLD", "gotfriends")
    elsewhere = row(3, "Ethosia", "QA Engineer", "HOLD", "ethosia")
    web.link_agency_leads([lead, engineer, elsewhere], DEFAULT_PROFILE)
    assert engineer.lead_here == [lead] and lead.lead_here == [] and elsewhere.lead_here == []
