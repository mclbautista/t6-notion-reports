from fake_notion import FakeNotion, contact, rich, select
from t6_notion_reports.contacts import load_contacts


def test_reads_id_handle_and_name_from_number_or_text():
    pio = contact("c1", "Pio")
    pio["properties"].update({"TelegramUserID": {"type": "number", "number": 274983989}, "TelegramHandle": rich("@pio_h")})
    ana = contact("c2", "Ana")
    ana["properties"].update({"TelegramUserID": rich(" 555 "), "TelegramHandle": rich("")})
    angel = contact("c3", "Angel")
    angel["properties"].update({"TelegramUserID": {"type": "number", "number": None}, "TelegramHandle": rich("heygelica")})
    junk = contact("c4", "Bad")
    junk["properties"].update({"TelegramUserID": rich("not-a-number")})
    got = load_contacts(FakeNotion(contacts=[pio, ana, angel, junk]), "contacts-ds")
    assert (got["c1"].telegram_id, got["c1"].handle) == (274983989, "pio_h")
    assert (got["c2"].telegram_id, got["c2"].handle) == (555, None)
    assert (got["c3"].telegram_id, got["c3"].handle) == (None, "heygelica")
    assert got["c4"].telegram_id is None
