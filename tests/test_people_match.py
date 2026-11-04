"""People matching, ported from the watchdog's resolver tests (aliases, honorifics, collisions, thresholds)
plus the two-tier order (Contacts with a TelegramUserID first, then the rest)."""
from t6_notion_reports.contacts import Contact
from t6_notion_reports.people_match import MatchSettings, match_person

S = MatchSettings()


def c(page_id, name, aliases="", tg=None):
    return Contact(page_id, name, None, tg, aliases)


def who(name, contacts, settings=S):
    got = match_person(name, contacts, settings)
    return got.page_id if got else None


CONTACTS = [
    c("majo", "Maria Josefa Cruz", "Majo, Ate Majo", tg=1),
    c("deb", "Debbie Santos", "Debbie,  Debz , KUYA debs,,", tg=2),
    c("paul", "Paul Reyes", tg=3),
    c("kim1", "Kimberly Lim", "Kim", tg=4),
    c("kim2", "Kim Tan", "kim", tg=5),
]


def test_defaults_match_the_watchdog():
    assert (S.fuzzy_min_score, S.fuzzy_min_lead, S.honorifics) == (90, 8, ("ate", "kuya", "sir"))


def test_alias_with_honorifics_on_both_sides():
    for raw in ("Majo", "ate Majo", "Ate MAJO", "sir majo", "kuya  Majo", "Debz", "kuya debs", "sir Debbie"):
        assert who(raw, CONTACTS) in {"majo", "deb"}, raw
    assert who("ate Majo", CONTACTS) == "majo"
    assert who("kuya debs", CONTACTS) == "deb"


def test_alias_collision_is_unresolved_and_never_guessed():
    assert who("Kim", CONTACTS) is None


def test_fuzzy_pass():
    assert who("Paul", CONTACTS + [c("x", "Zed Quill")]) == "paul"
    assert who("Pual Reyes", CONTACTS) == "paul"


def test_fuzzy_fails_on_low_score():
    assert who("Zzyzx", CONTACTS) is None


def test_fuzzy_fails_on_insufficient_lead():
    crowd = [c("a", "Paul Garcia", tg=1), c("b", "Paula Garcia", tg=2)]
    assert who("Paul Garcia", crowd) is None


def test_thresholds_are_configurable():
    assert who("Pual Reyes", CONTACTS, MatchSettings(fuzzy_min_score=99)) is None
    crowd = [c("a", "Paul Garcia", tg=1), c("b", "Paula Garcia", tg=2)]
    assert who("Paul Garcia", crowd, MatchSettings(fuzzy_min_lead=0)) == "a"


def test_honorifics_are_configurable():
    cs = [c("a", "Boss Dan", "Dan")]
    assert who("boss dan", cs, MatchSettings(honorifics=("boss",))) == "a"


def test_empty_and_blank_names_are_unresolved():
    assert who("", CONTACTS) is None
    assert who("  ate  ", CONTACTS) is None
    assert who("Majo", []) is None


def test_onboarded_wins_over_namesake_for_alias_and_fuzzy():
    cs = [c("off", "Paul Reyes", "Paulo"), c("on", "Paul Reyes", "Paulo", tg=9)]
    assert who("Paulo", cs) == "on"
    assert who("Paul Reyes", cs) == "on"


def test_no_onboarded_match_falls_through_with_same_thresholds():
    cs = [c("on", "Debbie Santos", tg=1), c("off", "Alec Gaba", "Alec")]
    assert who("Alec", cs) == "off"
    assert who("Alec Gabba", cs) == "off"
    assert who("Alec Gabba", cs, MatchSettings(fuzzy_min_score=99)) is None
    assert who("Zzyzx", cs) is None


def test_ambiguous_onboarded_alias_does_not_fall_through_to_a_namesake():
    cs = [c("a", "Kim Lim", "kim", tg=1), c("b", "Kim Tan", "kim", tg=2), c("c", "Kim Off", "kim")]
    assert who("Kim", cs) is None
