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
    c("vedo", "Velma Odette Belk", "Vedo, Ate Vedo", tg=1),
    c("tib", "Tibbie Varnel", "Tibbie,  Tibz , KUYA tibs,,", tg=2),
    c("ravn", "Ravn Pardo", tg=3),
    c("lor1", "Lorraine Ung", "Lor", tg=4),
    c("lor2", "Lor Dov", "lor", tg=5),
]


def test_defaults_match_the_watchdog():
    assert (S.fuzzy_min_score, S.fuzzy_min_lead, S.honorifics) == (90, 8, ("ate", "kuya", "sir"))


def test_alias_with_honorifics_on_both_sides():
    for raw in ("Vedo", "ate Vedo", "Ate VEDO", "sir vedo", "kuya  Vedo", "Tibz", "kuya tibs", "sir Tibbie"):
        assert who(raw, CONTACTS) in {"vedo", "tib"}, raw
    assert who("ate Vedo", CONTACTS) == "vedo"
    assert who("kuya tibs", CONTACTS) == "tib"


def test_alias_collision_is_unresolved_and_never_guessed():
    assert who("Lor", CONTACTS) is None


def test_fuzzy_pass():
    assert who("Ravn", CONTACTS + [c("x", "Zed Quill")]) == "ravn"
    assert who("Rvan Pardo", CONTACTS) == "ravn"


def test_fuzzy_fails_on_low_score():
    assert who("Zzyzx", CONTACTS) is None


def test_fuzzy_fails_on_insufficient_lead():
    crowd = [c("a", "Ravn Hollis", tg=1), c("b", "Ravna Hollis", tg=2)]
    assert who("Ravn Hollis", crowd) is None


def test_thresholds_are_configurable():
    assert who("Rvan Pardo", CONTACTS, MatchSettings(fuzzy_min_score=99)) is None
    crowd = [c("a", "Ravn Hollis", tg=1), c("b", "Ravna Hollis", tg=2)]
    assert who("Ravn Hollis", crowd, MatchSettings(fuzzy_min_lead=0)) == "a"


def test_honorifics_are_configurable():
    cs = [c("a", "Boss Gus", "Gus")]
    assert who("boss gus", cs, MatchSettings(honorifics=("boss",))) == "a"


def test_empty_and_blank_names_are_unresolved():
    assert who("", CONTACTS) is None
    assert who("  ate  ", CONTACTS) is None
    assert who("Vedo", []) is None


def test_onboarded_wins_over_namesake_for_alias_and_fuzzy():
    cs = [c("off", "Ravn Pardo", "Ravno"), c("on", "Ravn Pardo", "Ravno", tg=9)]
    assert who("Ravno", cs) == "on"
    assert who("Ravn Pardo", cs) == "on"


def test_no_onboarded_match_falls_through_with_same_thresholds():
    cs = [c("on", "Tibbie Varnel", tg=1), c("off", "Ivor Mora", "Ivor")]
    assert who("Ivor", cs) == "off"
    assert who("Ivor Morra", cs) == "off"
    assert who("Ivor Morra", cs, MatchSettings(fuzzy_min_score=99)) is None
    assert who("Zzyzx", cs) is None


def test_ambiguous_onboarded_alias_does_not_fall_through_to_a_namesake():
    cs = [c("a", "Lor Ung", "lor", tg=1), c("b", "Lor Dov", "lor", tg=2), c("c", "Lor Off", "lor")]
    assert who("Lor", cs) is None
