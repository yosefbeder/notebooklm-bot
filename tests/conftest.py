from types import SimpleNamespace

import pytest

from studybot.config import Settings
from studybot.db import Database


@pytest.fixture
def settings(tmp_path):
    config = Settings(
        _env_file=None,
        bot_token="123456:test-token",
        data_dir=tmp_path,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.sqlite",
    )
    for folder in ["sources", "assets", "exports", "pdf"]:
        (tmp_path / folder).mkdir()
    return config


@pytest.fixture
async def database(settings):
    db = Database(settings.database_url)
    await db.initialize()
    yield db
    await db.close()


class FakeBot:
    def __init__(self):
        self.calls = []
        self.sequence = 0

    async def send_message(self, chat_id, text, **kwargs):
        self.sequence += 1
        self.calls.append(("message", text, kwargs))
        return SimpleNamespace(message_id=self.sequence)

    async def edit_message_text(self, text, **kwargs):
        self.calls.append(("edit", text, kwargs))

    async def send_poll(self, chat_id, question, **kwargs):
        self.sequence += 1
        self.calls.append(("poll", question, kwargs))
        return SimpleNamespace(
            message_id=self.sequence, poll=SimpleNamespace(id=f"poll-{self.sequence}")
        )

    async def stop_poll(self, *args, **kwargs):
        self.calls.append(("stop", args, kwargs))

    async def edit_message_reply_markup(self, **kwargs):
        self.calls.append(("markup", "", kwargs))

    async def send_photo(self, chat_id, photo, **kwargs):
        self.calls.append(("photo", photo, kwargs))
        return SimpleNamespace(message_id=1)

    async def send_document(self, chat_id, document, **kwargs):
        self.calls.append(("document", document, kwargs))

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="sources/sample.txt")

    async def download_file(self, path, destination, **kwargs):
        destination.write_text(
            "The heart pumps blood through the lungs and the rest of the human body."
        )


@pytest.fixture
def bot():
    return FakeBot()
