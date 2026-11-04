from datetime import date
from t6_notion_reports.briefing import Settings, build_briefing
from t6_notion_reports.contacts import Contact
from t6_notion_reports.diff import ProjectChange, FieldChange, Person

SUNDAY = date(2026, 11, 15)

def test_multiple_digest_recipients():
    cyril = Contact("c-cyril", "Cyril", telegram_id=999)
    jam = Contact("c-jam", "Jam", telegram_id=777)
    leizette = Contact("c-leiz", "Leizette", telegram_id=888)
    pio = Contact("c-pio", "Pio", telegram_id=666)

    settings = Settings(
        cyril=cyril,
        group_chat_id=-1001,
        group_topic_id=None,
        digest_recipients=(cyril, jam, leizette, pio),
    )

    changes = [
        ProjectChange(
            page_id="p1",
            code="RM",
            title="Project RM",
            name="RM",
            coordinators=[Person("c-cyril", "Cyril")],
            health="On Track",
            kind="changed",
            group="in_progress",
            changes=[FieldChange("Stage", "Offline", "Online")],
            last_updated=SUNDAY,
        )
    ]
    coordinators = {"c-cyril": "Cyril"}
    contacts = {"c-cyril": cyril, "c-jam": jam, "c-leiz": leizette, "c-pio": pio}

    msgs = build_briefing(changes, coordinators, contacts, settings, SUNDAY)

    # 1 coordinator DM (Cyril) + 4 digest DMs (Cyril, Jam, Leizette, Pio)
    assert len(msgs) == 5
    recipients = {m.to for m in msgs}
    assert recipients == {999, 777, 888, 666}

    digest_msgs = [m for m in msgs if "psr-digest" in m.key]
    assert len(digest_msgs) == 4
    assert {m.to for m in digest_msgs} == {999, 777, 888, 666}
