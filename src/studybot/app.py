import asyncio
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import ForceReply, FSInputFile, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from studybot.db import Draft, Job, Session, StudySet, Submission
from studybot.domain import GenerationSettings, Item
from studybot.media import EXTENSIONS, MAX_SOURCE_BYTES, incoming_file
from studybot.pdf import render_pdf
from studybot.study import Study
from studybot.ui import action, notebook_row

log = logging.getLogger(__name__)


class App:
    def __init__(self, db, bot, settings, ui):
        self.db, self.bot, self.settings, self.ui = db, bot, settings, ui
        self.lock = asyncio.Lock()
        self.study = Study(db, bot, ui)
        self.router = Router()
        self.router.message.register(self.start, Command("start", "help"))
        self.router.message.register(self.list_notebooks, Command("notebooks"))
        self.router.message.register(self.resume_list, Command("resume"))
        self.router.message.register(self.message)
        self.router.callback_query.register(self.callback, F.data)
        self.router.poll_answer.register(self.poll_answer)

    def allowed(self, event):
        return event.from_user and self.settings.permits(event.from_user.id)

    async def start(self, message):
        if message.chat.type != "private" or not self.allowed(message):
            return
        await message.answer(
            "Forward or upload a document, image, audio recording, or video. "
            "I will create a NotebookLM notebook, then help you study with MCQs or flashcards.\n\n"
            "Choose item count and difficulty before generation. Everything is in English. "
            "Notebook links use read-only sharing: anyone with the link and a Google account "
            "can view the notebook and its sources.\n\n"
            "/notebooks - your uploads\n/resume - interrupted study sessions\n"
            "Send images as documents to preserve their quality."
        )

    async def list_notebooks(self, message):
        if message.chat.type != "private" or not self.allowed(message):
            return
        async with self.db.sessions() as db:
            subs = (
                await db.scalars(
                    select(Submission)
                    .where(Submission.owner == message.from_user.id, Submission.state != "Deleted")
                    .order_by(Submission.id.desc())
                    .limit(20)
                )
            ).all()
        rows = [[action(sub.title[:50], f"home:{sub.id}")] for sub in subs]
        await message.answer(
            "Your recent notebooks" if rows else "Upload a source to get started.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def resume_list(self, message):
        if message.chat.type != "private" or not self.allowed(message):
            return
        async with self.db.sessions() as db:
            sessions = (
                await db.scalars(
                    select(Session)
                    .where(
                        Session.owner == message.from_user.id,
                        Session.state.in_(["paused", "active"]),
                    )
                    .order_by(Session.started.desc())
                    .limit(10)
                )
            ).all()
        rows = [
            [action(f"Resume session ({s.cursor}/{len(s.order)})", f"resume:{s.id}")]
            for s in sessions
        ]
        await message.answer(
            "Paused study sessions" if rows else "No paused study sessions.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def message(self, message):
        if message.chat.type != "private" or not self.allowed(message):
            return
        async with self.lock:
            if message.text and message.reply_to_message:
                if await self.custom_count(message):
                    return
            file = incoming_file(message)
            if not file:
                await message.answer("Please forward or upload a supported source file.")
                return
            from pathlib import Path

            if Path(file["name"]).suffix.lower().lstrip(".") not in EXTENSIONS:
                await message.answer(
                    "Unsupported file type. Please send the original source as a document."
                )
                return
            if file["size"] > MAX_SOURCE_BYTES:
                await message.answer("NotebookLM supports uploaded files up to 200 MB.")
                return
            if not self.settings.telegram_api_base and file["size"] > 20 * 1024 * 1024:
                await message.answer("Files over 20 MB need the VPS Bot API endpoint configured.")
                return
            file["message_id"] = message.message_id
            key = (
                f"album:{message.media_group_id}"
                if message.media_group_id
                else str(message.message_id)
            )
            try:
                async with self.db.sessions.begin() as db:
                    sub = await db.scalar(
                        select(Submission).where(
                            Submission.chat_id == message.chat.id, Submission.input_key == key
                        )
                    )
                    if sub:
                        if any(f["message_id"] == message.message_id for f in sub.files):
                            return
                        if sub.state != "Queued":
                            await message.answer(
                                "This album is already processing. Send this file separately."
                            )
                            return
                        sub.files = [*sub.files, file]
                        sub.title = f"Album ({len(sub.files)} sources)"
                        job = await db.scalar(select(Job).where(Job.key == f"ingest:{sub.id}"))
                        job.run_at = time.time() + 3
                    else:
                        sub = Submission(
                            owner=message.from_user.id,
                            chat_id=message.chat.id,
                            input_key=key,
                            title=file["name"],
                            files=[file],
                        )
                        db.add(sub)
                        await db.flush()
                        db.add(
                            Job(
                                key=f"ingest:{sub.id}",
                                kind="ingest",
                                target=sub.id,
                                run_at=time.time() + 3,
                            )
                        )
                await self.ui.render(sub.id)
            except IntegrityError:
                return  # Redelivered input or another handler already persisted this source.

    async def custom_count(self, message):
        async with self.db.sessions.begin() as db:
            draft = await db.scalar(
                select(Draft).where(
                    Draft.owner == message.from_user.id,
                    Draft.prompt_message_id == message.reply_to_message.message_id,
                    Draft.state == "editing",
                )
            )
            if not draft:
                return False
            try:
                settings = GenerationSettings(**{**draft.settings, "count": int(message.text)})
            except ValueError:
                await message.answer("Reply to the count prompt with a whole number from 1 to 100.")
                return True
            draft.settings, draft.prompt_message_id = settings.model_dump(), None
        await self.ui.draft(draft.id)
        return True

    async def own_submission(self, db, sub_id, owner):
        sub = await db.get(Submission, sub_id)
        if not sub or sub.owner != owner or sub.state == "Deleted":
            raise ValueError("This notebook is unavailable.")
        return sub

    async def own_draft(self, db, draft_id, owner):
        draft = await db.get(Draft, draft_id)
        if not draft or draft.owner != owner or draft.state != "editing":
            raise ValueError("These settings are no longer active.")
        await self.own_submission(db, draft.submission_id, owner)
        return draft

    async def callback(self, query):
        if not self.allowed(query) or not query.message or query.message.chat.type != "private":
            await query.answer("This action is unavailable.", show_alert=True)
            return
        await query.answer()
        async with self.lock:
            try:
                await self.dispatch(
                    query.data.split(":"), query.from_user.id, query.message.chat.id
                )
            except ValueError as exc:
                await self.bot.send_message(query.message.chat.id, str(exc))
            except Exception:
                log.exception("Telegram action failed")
                await self.bot.send_message(
                    query.message.chat.id,
                    "This action could not finish. Use /resume for interrupted study sessions, or try again.",
                )

    async def dispatch(self, parts, owner, chat_id):
        name, target = parts[:2]
        if name in {"home", "config", "share", "retry", "delete", "confirmdelete"}:
            async with self.db.sessions() as db:
                sub = await self.own_submission(db, target, owner)
            if name == "home":
                await self.ui.render(sub.id)
            elif name == "config":
                kind = parts[2]
                if kind not in {"quiz", "flashcards"} or sub.state != "Ready":
                    raise ValueError("Wait for the source to finish processing.")
                async with self.db.sessions.begin() as db:
                    existing = await db.scalar(
                        select(StudySet).where(
                            StudySet.submission_id == sub.id, StudySet.kind == kind
                        )
                    )
                    if existing and existing.state != "failed":
                        raise ValueError("This study set already exists or is being generated.")
                    drafts = (
                        await db.scalars(
                            select(Draft).where(
                                Draft.owner == owner,
                                Draft.submission_id == sub.id,
                                Draft.kind == kind,
                                Draft.state == "editing",
                            )
                        )
                    ).all()
                    for old in drafts:
                        old.state = "cancelled"
                    draft = Draft(
                        owner=owner,
                        submission_id=sub.id,
                        kind=kind,
                        settings=GenerationSettings().model_dump(),
                    )
                    db.add(draft)
                    await db.flush()
                await self.ui.draft(draft.id)
            elif name in {"share", "retry"}:
                kind = "share" if name == "share" else "ingest"
                async with self.db.sessions.begin() as db:
                    job = await db.scalar(select(Job).where(Job.key == f"{kind}:{sub.id}"))
                    if job and job.state == "needs_review":
                        raise ValueError(
                            "An operator must reconcile the upstream request before retrying."
                        )
                    if job and job.state in {"running", "queued"}:
                        return
                    if name == "retry" and sub.state != "Failed":
                        raise ValueError("The upload is not awaiting retry.")
                    if job:
                        job.state, job.run_at, job.lease_until = "queued", time.time(), 0
                    else:
                        db.add(Job(key=f"{kind}:{sub.id}", kind=kind, target=sub.id))
            elif name == "delete":
                await self.ui.edit(
                    sub,
                    "Delete this notebook, its sources, and generated study sets?",
                    [
                        [
                            action("Delete permanently", f"confirmdelete:{sub.id}"),
                            action("Keep notebook", f"home:{sub.id}"),
                        ]
                    ]
                    + notebook_row(sub),
                )
            else:
                async with self.db.sessions.begin() as db:
                    busy = await db.scalar(
                        select(Job).where(
                            Job.state.in_(["queued", "running", "needs_review"]),
                            (
                                (Job.target == sub.id)
                                | Job.target.in_(
                                    select(StudySet.id).where(StudySet.submission_id == sub.id)
                                )
                            ),
                        )
                    )
                    if busy:
                        raise ValueError(
                            "Wait for this notebook's current operation to finish before deleting."
                        )
                    sets = (
                        await db.scalars(select(StudySet).where(StudySet.submission_id == sub.id))
                    ).all()
                    sessions = (
                        await db.scalars(
                            select(Session).where(
                                Session.set_id.in_([s.id for s in sets]), Session.state == "active"
                            )
                        )
                    ).all()
                for session in sessions:
                    await self.study.pause(session.id, owner, show=False)
                async with self.db.sessions.begin() as db:
                    stored = await db.get(Submission, sub.id)
                    stored.state = "Deleting"
                    job = await db.scalar(select(Job).where(Job.key == f"delete:{sub.id}"))
                    if job:
                        job.state, job.run_at = "queued", time.time()
                    else:
                        db.add(Job(key=f"delete:{sub.id}", kind="delete", target=sub.id))
                await self.ui.render(sub.id)
            return
        if name in {"count", "level", "custom", "back", "generate"}:
            async with self.db.sessions.begin() as db:
                draft = await self.own_draft(db, target, owner)
                if name in {"count", "level"}:
                    key = "count" if name == "count" else "difficulty"
                    value = int(parts[2]) if name == "count" else parts[2]
                    draft.settings = GenerationSettings(
                        **{**draft.settings, key: value}
                    ).model_dump()
                elif name == "back":
                    draft.state = "cancelled"
                elif name == "custom":
                    prompt = await self.bot.send_message(
                        chat_id,
                        "Reply with the number of items to request (1-100).",
                        reply_markup=ForceReply(selective=True),
                    )
                    draft.prompt_message_id = prompt.message_id
                elif name == "generate":
                    sub = await self.own_submission(db, draft.submission_id, owner)
                    if sub.state != "Ready":
                        raise ValueError("This source is not ready for generation.")
                    study = await db.scalar(
                        select(StudySet).where(
                            StudySet.submission_id == sub.id, StudySet.kind == draft.kind
                        )
                    )
                    if study and study.state != "failed":
                        raise ValueError("This study set already exists or is being generated.")
                    if study:
                        study.state, study.error = "queued", None
                        # Settings changes create a new generation, not a retry of the old artifact.
                        if study.settings != draft.settings:
                            study.artifact_id = None
                        study.settings = draft.settings
                        job = await db.scalar(select(Job).where(Job.key == f"generate:{study.id}"))
                        job.state, job.run_at, job.lease_until = "queued", time.time(), 0
                    else:
                        study = StudySet(
                            submission_id=sub.id, kind=draft.kind, settings=draft.settings
                        )
                        db.add(study)
                        await db.flush()
                        db.add(Job(key=f"generate:{study.id}", kind="generate", target=study.id))
                    draft.state = "submitted"
            if name in {"back", "generate"}:
                await self.ui.render(draft.submission_id)
            elif name != "custom":
                await self.ui.draft(draft.id)
            return
        if name == "start":
            await self.study.start(target, owner, chat_id)
        elif name == "pdf":
            async with self.db.sessions() as db:
                study = await db.get(StudySet, target)
                if not study or study.state != "ready":
                    raise ValueError("This PDF is not ready.")
                sub = await self.own_submission(db, study.submission_id, owner)
            path = self.settings.data_dir / "pdf" / f"{study.id}.pdf"
            if not path.exists():
                await asyncio.to_thread(
                    render_pdf,
                    sub.title,
                    study.kind,
                    [Item.model_validate(item) for item in study.items],
                    path,
                    self.settings.pdf_font_path,
                )
            await self.bot.send_document(
                chat_id,
                FSInputFile(
                    path,
                    filename=f"{'MCQs' if study.kind == 'quiz' else 'Flashcards'}-{study.id}.pdf",
                ),
            )
        elif name == "leave":
            await self.study.pause(target, owner)
        elif name == "resume":
            await self.study.resume(target, owner)
        elif name == "rate":
            if parts[3] not in {"0", "1"}:
                raise ValueError("Invalid rating.")
            await self.study.rate(target, int(parts[2]), parts[3] == "1", owner)
        elif name == "missed":
            async with self.db.sessions() as db:
                session = await db.get(Session, target)
                if not session or session.owner != owner:
                    raise ValueError("Session unavailable.")
            await self.study.start(session.set_id, owner, chat_id, missed_from=target)
        else:
            raise ValueError("Unknown action.")

    async def poll_answer(self, answer):
        if not answer.user or not self.settings.permits(answer.user.id):
            return
        async with self.lock:
            try:
                await self.study.answer_poll(answer.poll_id, answer.user.id, answer.option_ids)
            except Exception:
                log.exception("Could not advance exam")
