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
    client(t).notify(100000001, "x", "k")
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


# ---- delivery status (GET /notify/{ref}) --------------------------------------------------------

@pytest.mark.parametrize("state", ["queued", "sent", "failed"])
def test_delivery_status_returns_the_gateways_state(state):
    t = FakeTransport((200, {"state": state}))
    assert client(t).delivery_status(5531) == state


def test_delivery_status_accepts_a_status_key_too():
    assert client(FakeTransport((200, {"status": "failed"}))).delivery_status(1) == "failed"


def test_delivery_status_is_a_get_with_bearer_and_no_body():
    t = FakeTransport((200, {"state": "sent"}))
    client(t).delivery_status(5531)
    url, headers, body = t.calls[0]
    assert url == "http://gw:8780/notify/5531"
    assert headers["Authorization"] == "Bearer s3cret"
    assert body is None  # no body means GET


@pytest.mark.parametrize("reply", [
    ConnectionError("down s3cret"),
    (404, None), (500, {"state": "sent"}), (202, {"state": "sent"}),
    (200, None), (200, {}), (200, {"state": "delivered"}), (200, {"state": None}), (200, {"state": ["sent"]}),
])
def test_delivery_status_never_raises_and_is_unknown_on_any_problem(reply):
    t = FakeTransport(reply)
    assert client(t).delivery_status(1) == "unknown"
    assert len(t.calls) == 1  # no retries: the caller polls
