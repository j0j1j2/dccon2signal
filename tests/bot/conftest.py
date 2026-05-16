import pytest


class FakeClock:
    """Manually-advanced monotonic clock for testing throttling."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


class FakeBot:
    """Minimal stand-in for telegram.Bot capturing edit_message_text calls."""

    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.sent: list[dict] = []

    async def edit_message_text(self, text, chat_id, message_id, **kw) -> None:
        self.edits.append({"text": text, "chat_id": chat_id, "message_id": message_id, **kw})

    async def send_message(self, chat_id, text, **kw) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, **kw})


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()
