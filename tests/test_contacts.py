from fake_notion import FakeNotion, contact, rich, select


def test_reads_id_handle_and_name_from_number_or_text():
    oto = contact("c1", "Oto")
    oto["properties"].update({"TelegramUserID": {"type": "number", "number": 100000001}, "TelegramHandle": rich("@oto_h")})
    ida = contact("c2", "Ida")
    ida["properties"].update({"TelegramUserID": rich(" 555 "), "TelegramHandle": rich("")})
    juno = contact("c3", "Juno")
    juno["properties"].update({"TelegramUserID": {"type": "number", "number": None}, "TelegramHandle": rich("heyjunebug")})
    junk = contact("c4", "Bad")
    junk["properties"].update({"TelegramUserID": rich("not-a-number")})
    got = FakeNotion(contacts=[oto, ida, juno, junk]).read_contacts()
    assert (got["c1"].telegram_id, got["c1"].handle) == (100000001, "oto_h")
    assert (got["c2"].telegram_id, got["c2"].handle) == (555, None)
    assert (got["c3"].telegram_id, got["c3"].handle) == (None, "heyjunebug")
    assert got["c4"].telegram_id is None


def test_loads_aliases():
    oto = contact("c1", "Oto")
    oto["properties"]["Aliases"] = rich("Oto, Otolo")
    ida = contact("c2", "Ida")
    got = FakeNotion(contacts=[oto, ida]).read_contacts()
    assert (got["c1"].aliases, got["c2"].aliases) == ("Oto, Otolo", "")
