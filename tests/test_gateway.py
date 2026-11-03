import pytest

from t6_notion_reports.gateway import GatewayClient, GatewayError


class FakeTransport:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        reply = self.replies.pop(0) if self.replies else (202, {"message_ref": 1})
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(transport, sleeps=None):
    return GatewayClient("http://gw:8780/", "s3cret", transport, sleep=(sleeps if sleeps is not None else []).append)


def test_body_bearer_and_url():
    t = FakeTransport((202, {"message_ref": 5531}))
    assert client(t).notify(-1003, "<b>hi</b>", "psr-digest-2026-11-15", topic=7) == 5531
    url, headers, body = t.calls[0]
    assert url == "http://gw:8780/notify"
    assert headers["Authorization"] == "Bearer s3cret"
    assert body == {"to": -1003, "topic": 7, "text": "<b>hi</b>", "format": "html",
                    "idempotency_key": "psr-digest-2026-11-15"}


def test_topic_omitted_for_direct_messages():
    t = FakeTransport()
    client(t).notify(274983989, "x", "k")
    assert "topic" not in t.calls[0][2]


def test_retries_three_times_with_backoff_then_succeeds():
    sleeps = []
    t = FakeTransport((503, None), ConnectionError("down"), (500, None), (202, {"message_ref": 9}))
    assert client(t, sleeps).notify(1, "x", "k") == 9
    assert len(t.calls) == 4 and sleeps == [2.0, 4.0, 8.0]


def test_gives_up_after_three_retries_without_leaking_secret_or_text():
    t = FakeTransport(*[(500, None)] * 10)
    with pytest.raises(GatewayError) as err:
        client(t).notify(1, "private text", "k1")
    assert len(t.calls) == 4
    assert "s3cret" not in str(err.value) and "private text" not in str(err.value)


def test_a_200_without_message_ref_is_not_success():
    t = FakeTransport(*[(200, {})] * 4)
    with pytest.raises(GatewayError):
        client(t).notify(1, "x", "k")
