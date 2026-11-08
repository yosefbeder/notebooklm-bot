import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from studybot.app import App
from studybot.db import Draft, Job, Session, StudySet, Submission
from studybot.domain import GenerationSettings, Item, normalize_export, require_english
from studybot.main import reconcile
from studybot.study import Study
from studybot.ui import UI
from studybot.worker import Worker


async def seeded(database, kind="quiz", items=None):
    async with database.sessions.begin() as db:
        sub = Submission(
            owner=7,
            chat_id=100,
            input_key="1",
            title="Heart",
            state="Ready",
            files=[],
            notebook_id="notebook",
            notebook_url="https://notebooklm.google.com/notebook/test",
        )
        db.add(sub)
        await db.flush()
        study = StudySet(
            submission_id=sub.id,
            kind=kind,
            state="ready",
            settings=GenerationSettings().model_dump(),
            items=items
            or [
                Item(
                    front="Which organ pumps blood?",
                    back="The heart pumps blood.",
                    options=["Heart", "Lung"],
                    correct=0,
                ).model_dump(),
                Item(
                    front="Which organ exchanges gases?",
                    back="The lungs exchange gases.",
                    options=["Heart", "Lungs"],
                    correct=1,
                ).model_dump(),
            ],
        )
        db.add(study)
        await db.flush()
    return sub, study


def test_actual_export_shapes_and_ambiguous_answers():
    quiz = {
        "questions": [
            {
                "question": "Which chamber?",
                "answerOptions": [
                    {"text": "Left", "isCorrect": True, "rationale": "It is on the left."},
                    {"text": "Right", "isCorrect": False},
                ],
            }
        ]
    }
    assert normalize_export(quiz, "quiz")[0].correct == 0
    assert (
        normalize_export({"cards": [{"front": "Question", "back": "Answer"}]}, "flashcards")[0].back
        == "Answer"
    )
    quiz["questions"][0]["answerOptions"][1]["isCorrect"] = True
    with pytest.raises(ValueError):
        normalize_export(quiz, "quiz")
    with pytest.raises(ValueError):
        GenerationSettings(language="ar")
    with pytest.raises(ValueError):
        require_english([Item(front="ما هو القلب؟", back="القلب")])


async def test_quiz_duplicate_vote_leave_resume_and_wrong_user(database, bot):
    sub, study = await seeded(database)
    sessions = Study(database, bot, UI(bot, database))
    await sessions.start(study.id, 7, 100)
    async with database.sessions() as db:
        session = await db.scalar(select(Session))
        poll = session.poll_id
    await sessions.answer_poll(poll, 99, [0])
    await sessions.answer_poll(poll, 7, [0])
    await sessions.answer_poll(poll, 7, [0])
    async with database.sessions() as db:
        session = await db.get(Session, session.id)
        assert session.cursor == 1 and len(session.answers) == 1
        second_poll = session.poll_id
    await sessions.pause(session.id, 7)
    await sessions.answer_poll(second_poll, 7, [1])
    await sessions.resume(session.id, 7)
    async with database.sessions() as db:
        resumed = await db.get(Session, session.id)
        assert resumed.cursor == 1 and resumed.poll_id != second_poll
    await sessions.answer_poll(resumed.poll_id, 7, [1])
    async with database.sessions() as db:
        completed = await db.get(Session, session.id)
        assert completed.state == "completed"
        assert sum(a["correct"] for a in completed.answers.values()) == 2


async def test_flashcard_spoiler_images_duplicate_ratings_and_recovery(database, bot):
    items = [
        Item(
            front="Name the structure", back="Left ventricle", back_image="/tmp/asset.png"
        ).model_dump()
    ]
    _, study = await seeded(database, "flashcards", items)
    sessions = Study(database, bot, UI(bot, database))
    await sessions.start(study.id, 7, 100)
    assert any(call[0] == "photo" and call[2]["has_spoiler"] for call in bot.calls)
    assert any("<tg-spoiler>" in str(call[1]) for call in bot.calls)
    async with database.sessions() as db:
        session = await db.scalar(select(Session))
    await sessions.recover()
    with pytest.raises(ValueError):
        await sessions.rate(session.id, 0, True, 7)
    await sessions.resume(session.id, 7)
    await sessions.rate(session.id, 0, False, 7)
    with pytest.raises(ValueError):
        await sessions.rate(session.id, 0, True, 7)
    await sessions.start(study.id, 7, 100, missed_from=session.id)
    async with database.sessions() as db:
        active = await db.scalar(select(Session).where(Session.state == "active"))
        assert active.order == [0]


async def test_settings_are_validated_confirmed_once_and_owned(database, bot, settings):
    sub, _ = await seeded(database)
    app = App(database, bot, settings, UI(bot, database))
    await app.dispatch(["config", sub.id, "flashcards"], 7, 100)
    async with database.sessions() as db:
        draft = await db.scalar(select(Draft))
    with pytest.raises(ValueError):
        await app.dispatch(["count", draft.id, "50"], 99, 100)
    await app.dispatch(["count", draft.id, "30"], 7, 100)
    await app.dispatch(["level", draft.id, "hard"], 7, 100)
    with pytest.raises(ValueError):
        await app.dispatch(["count", draft.id, "-1"], 7, 100)
    await app.dispatch(["generate", draft.id], 7, 100)
    with pytest.raises(ValueError):
        await app.dispatch(["generate", draft.id], 7, 100)
    async with database.sessions() as db:
        job = await db.scalar(select(Job))
        study = await db.get(StudySet, job.target)
        assert study.settings == {"count": 30, "difficulty": "hard", "language": "en"}
        assert len((await db.scalars(select(Job))).all()) == 1
    with pytest.raises(ValueError):
        await app.dispatch(["pdf", study.id], 99, 100)


async def test_crashed_write_requires_reconciliation_not_duplicate_create(database, bot, settings):
    sub, _ = await seeded(database)
    async with database.sessions.begin() as db:
        job = Job(
            key="ingest:test",
            kind="ingest",
            target=sub.id,
            state="running",
            pending_write="create_notebook",
            lease_until=0,
        )
        db.add(job)
        await db.flush()
    notebook = SimpleNamespace(create=None)
    worker = Worker(database, bot, notebook, UI(bot, database), settings)
    assert await database.claim_job() == job.id
    assert await database.claim_job() is None
    await worker.execute(job.id)
    async with database.sessions() as db:
        failed = await db.get(Job, job.id)
        assert failed.state == "needs_review"
    await reconcile(database, job.id, "verified-notebook", False)
    async with database.sessions() as db:
        stored = await db.get(Submission, sub.id)
        queued = await db.get(Job, job.id)
        assert stored.notebook_id == "verified-notebook"
        assert queued.pending_write is None and queued.state == "queued"


async def test_long_question_and_options_keep_answer_mapping(database, bot):
    _, study = await seeded(
        database,
        items=[
            Item(
                front="Clinical details. " * 50,
                options=["Long answer. " * 20, "Second answer. " * 20],
                correct=1,
            ).model_dump()
        ],
    )
    sessions = Study(database, bot, UI(bot, database))
    await sessions.start(study.id, 7, 100)
    poll = next(call for call in bot.calls if call[0] == "poll")
    assert [option.text for option in poll[2]["options"]] == ["A", "B"]
    assert poll[2]["correct_option_ids"] == [1]
    assert poll[2]["is_anonymous"] is False


async def test_album_and_redelivery_are_one_notebook(database, bot, settings):
    app = App(database, bot, settings, UI(bot, database))
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private", id=100),
        from_user=SimpleNamespace(id=7),
        text=None,
        media_group_id="album",
        message_id=1,
        document=SimpleNamespace(file_id="file-1", file_size=100, file_name="source.pdf"),
        audio=None,
        voice=None,
        video=None,
        video_note=None,
        animation=None,
        photo=None,
    )
    await app.message(message)
    await app.message(message)
    message.message_id = 2
    await app.message(message)
    async with database.sessions() as db:
        subs = (await db.scalars(select(Submission))).all()
        assert len(subs) == 1 and len(subs[0].files) == 2
        job = await db.scalar(select(Job))
        assert job.run_at > time.time()
