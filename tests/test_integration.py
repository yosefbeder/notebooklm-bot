import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from studybot.app import App
from studybot.db import Draft, Job, Session, StudySet, Submission
from studybot.main import make_bot
from studybot.notebook import connect
from studybot.ui import UI
from studybot.worker import Worker


async def test_real_mcp_stdio_contract(database, bot, settings, monkeypatch, tmp_path):
    settings.mcp_command = sys.executable
    settings.mcp_args = [str(Path(__file__).with_name("fake_mcp.py"))]
    log = tmp_path / "mcp.jsonl"
    monkeypatch.setenv("MCP_SIMULATION_LOG", str(log))
    async with connect(settings) as adapter:
        notebook_id = await adapter.create("Test")
        path = tmp_path / "sources" / "sample.txt"
        path.write_text("The heart pumps blood.")
        assert await adapter.upload(notebook_id, path) == "test-source"
        assert (await adapter.share(notebook_id)).startswith("https://notebooklm.google.com/")
        artifact_id = await adapter.generate(
            notebook_id, "quiz", {"count": 30, "difficulty": "hard"}
        )
        await adapter.wait(notebook_id, artifact_id)
        raw = await adapter.download(notebook_id, artifact_id, "quiz", "study")
        assert raw["questions"][0]["answerOptions"][0]["isCorrect"] is True
        monkeypatch.setenv("MCP_SIMULATION_FAILURE", "share")
        # Child environment is captured at connect time; sharing errors are exercised below.
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    generation = next(call for call in calls if call["tool"] == "studio_create")
    assert generation["language"] == "en" and generation["count"] == 3
    assert "30 multiple-choice questions" in generation["prompt"]


async def test_sharing_failure_keeps_source_ready(database, bot, settings, monkeypatch):
    settings.mcp_command = sys.executable
    settings.mcp_args = [str(Path(__file__).with_name("fake_mcp.py"))]
    monkeypatch.setenv("MCP_SIMULATION_FAILURE", "share")
    async with database.sessions.begin() as db:
        sub = Submission(
            owner=7,
            chat_id=100,
            input_key="sharing",
            title="Notes",
            files=[{"file_id": "source-file", "name": "notes.txt", "size": 100}],
        )
        db.add(sub)
        await db.flush()
        job = Job(key="ingest:share", target=sub.id, kind="ingest")
        db.add(job)
        await db.flush()
    async with connect(settings) as adapter:
        await Worker(database, bot, adapter, UI(bot, database), settings).execute(job.id)
    async with database.sessions() as db:
        stored = await db.get(Submission, sub.id)
        assert stored.state == "Ready" and stored.sharing_error and not stored.notebook_url


@pytest.mark.skipif(not os.environ.get("SIM_BASE"), reason="Run the local Postman simulation first")
async def test_full_http_and_mcp_flow(database, settings, monkeypatch, tmp_path):
    settings.telegram_api_base = os.environ["SIM_BASE"].rstrip("/") + "/telegram"
    settings.mcp_command = sys.executable
    settings.mcp_args = [str(Path(__file__).with_name("fake_mcp.py"))]
    monkeypatch.setenv("MCP_SIMULATION_LOG", str(tmp_path / "mcp.jsonl"))
    async with httpx.AsyncClient() as client:
        await client.get(settings.telegram_api_base + "/__reset")
    bot = make_bot(settings)
    try:
        assert (await bot.get_me()).is_bot
        ui = UI(bot, database)
        app = App(database, bot, settings, ui)
        async with database.sessions.begin() as db:
            sub = Submission(
                owner=7,
                chat_id=100,
                input_key="http",
                title="Human heart",
                files=[{"file_id": "source-file", "name": "notes.txt", "size": 100}],
            )
            db.add(sub)
            await db.flush()
            job = Job(key=f"ingest:{sub.id}", target=sub.id, kind="ingest")
            db.add(job)
            await db.flush()
        async with connect(settings) as adapter:
            worker = Worker(database, bot, adapter, ui, settings)
            await worker.execute(job.id)
            for kind in ["quiz", "flashcards"]:
                await app.dispatch(["config", sub.id, kind], 7, 100)
                async with database.sessions() as db:
                    draft = await db.scalar(select(Draft).where(Draft.kind == kind))
                await app.dispatch(["generate", draft.id], 7, 100)
                async with database.sessions() as db:
                    study = await db.scalar(select(StudySet).where(StudySet.kind == kind))
                    generate_job = await db.scalar(select(Job).where(Job.target == study.id))
                await worker.execute(generate_job.id)
                await app.dispatch(["start", study.id], 7, 100)
                async with database.sessions() as db:
                    session = await db.scalar(select(Session).where(Session.state == "active"))
                if kind == "quiz":
                    await app.study.answer_poll(session.poll_id, 7, [0])
                else:
                    await app.study.rate(session.id, 0, True, 7)
                await app.dispatch(["pdf", study.id], 7, 100)
            # Exercise the actual SDK's handling of Telegram's flood-limit envelope.
            async with httpx.AsyncClient() as client:
                await client.get(settings.telegram_api_base + "/__fail-once")
            await bot.send_message(100, "Recovered after rate limit")
            async with httpx.AsyncClient() as client:
                calls = (await client.get(settings.telegram_api_base + "/__requests")).json()
            methods = [call["method"] for call in calls]
            assert "download" in methods and "sendPoll" in methods and "stopPoll" in methods
            assert methods.count("sendDocument") == 2
            assert (
                len(
                    [
                        call
                        for call in calls
                        if call.get("body", {}).get("text") == "Recovered after rate limit"
                    ]
                )
                == 2
            )
            async with database.sessions() as db:
                sessions = (await db.scalars(select(Session))).all()
                assert all(session.state == "completed" for session in sessions)
                assert all(list(session.answers.values())[0]["correct"] for session in sessions)
    finally:
        await bot.session.close()
