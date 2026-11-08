import contextlib
import time
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InputPollOption
from sqlalchemy import select

from studybot.db import Session, StudySet, Submission
from studybot.domain import Item
from studybot.ui import action


class Study:
    """All entry points run under App.lock; one polling process handles a deployment."""

    def __init__(self, db, bot, ui):
        self.db, self.bot, self.ui = db, bot, ui

    async def recover(self):
        # Telegram sends cannot be atomically committed with our DB. Pause after restart;
        # /resume invalidates the old poll and sends a fresh attempt for the current cursor.
        async with self.db.sessions.begin() as db:
            sessions = (await db.scalars(select(Session).where(Session.state == "active"))).all()
            for session in sessions:
                session.state = "paused"

    async def start(self, set_id: str, owner: int, chat_id: int, missed_from: str | None = None):
        async with self.db.sessions() as db:
            study = await db.get(StudySet, set_id)
            sub = await db.get(Submission, study.submission_id) if study else None
            if not sub or sub.owner != owner or study.state != "ready" or sub.state != "Ready":
                raise ValueError("This study set is unavailable.")
            previous = (
                await db.scalars(
                    select(Session).where(
                        Session.owner == owner, Session.state.in_(["active", "paused"])
                    )
                )
            ).all()
        for session in previous:
            await self.pause(session.id, owner, show=False)
        order = list(range(len(study.items)))
        if missed_from:
            async with self.db.sessions() as db:
                old = await db.get(Session, missed_from)
                if not old or old.owner != owner or old.set_id != set_id:
                    raise ValueError("Review session unavailable.")
                order = [int(key) for key, value in old.answers.items() if not value["correct"]]
            if not order:
                raise ValueError("There are no missed items to review.")
        async with self.db.sessions.begin() as db:
            session = Session(owner=owner, chat_id=chat_id, set_id=set_id, order=order)
            db.add(session)
            await db.flush()
        await self.send_item(session.id)

    async def control(self, session, study):
        answered = len(session.answers)
        correct = sum(answer["correct"] for answer in session.answers.values())
        completed = session.cursor >= len(session.order)
        title = "Exam" if study.kind == "quiz" else "Flashcards"
        text = (
            f"{title} {'complete' if completed else 'paused' if session.state == 'paused' else 'in progress'}\n"
            f"Answered {answered}/{len(session.order)} · "
            f"{'Correct' if study.kind == 'quiz' else 'Got it'} {correct}\n"
            f"Elapsed: {int(time.time() - session.started)} seconds"
        )
        if completed:
            rows = [
                [
                    action(
                        "Review incorrect" if study.kind == "quiz" else "Review missed",
                        f"missed:{session.id}",
                    ),
                    action("Restart", f"start:{study.id}"),
                ]
            ]
        elif session.state == "paused":
            rows = [
                [
                    action(
                        "Resume exam" if study.kind == "quiz" else "Resume review",
                        f"resume:{session.id}",
                    )
                ]
            ]
        else:
            text += f"\n{'Question' if study.kind == 'quiz' else 'Card'} {session.cursor + 1}/{len(session.order)}"
            rows = [
                [
                    action(
                        "Leave exam" if study.kind == "quiz" else "Leave review",
                        f"leave:{session.id}",
                    )
                ]
            ]
        rows.append([action("Back to notebook", f"home:{study.submission_id}")])
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        if session.control_message_id:
            try:
                await self.bot.edit_message_text(
                    text,
                    chat_id=session.chat_id,
                    message_id=session.control_message_id,
                    reply_markup=markup,
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
                if "message to edit not found" not in str(exc):
                    raise
        message = await self.bot.send_message(session.chat_id, text, reply_markup=markup)
        async with self.db.sessions.begin() as db:
            stored = await db.get(Session, session.id)
            stored.control_message_id = message.message_id

    async def text(self, chat_id: int, text: str, spoiler=False):
        # Split before escaping so HTML entities cannot be cut; honor Telegram UTF-16 lengths.
        for index in range(0, len(text), 1500):
            escaped = escape(text[index : index + 1500])
            await self.bot.send_message(
                chat_id,
                f"<tg-spoiler>{escaped}</tg-spoiler>" if spoiler else escaped,
                parse_mode="HTML",
            )

    async def send_item(self, session_id: str):
        async with self.db.sessions() as db:
            session = await db.get(Session, session_id)
            study = await db.get(StudySet, session.set_id)
        if session.cursor >= len(session.order):
            async with self.db.sessions.begin() as db:
                stored = await db.get(Session, session_id)
                stored.state = "completed"
            session.state = "completed"
            await self.control(session, study)
            return
        await self.control(session, study)
        item = Item.model_validate(study.items[session.order[session.cursor]])
        if study.kind == "quiz":
            question = item.front
            options = item.options
            if len(question.encode("utf-16-le")) // 2 > 280:
                await self.text(session.chat_id, item.front)
                question = f"Question {session.cursor + 1}: choose the correct answer."
            if any(len(option.encode("utf-16-le")) // 2 > 90 for option in options):
                await self.text(
                    session.chat_id,
                    "\n".join(f"{chr(65 + i)}. {option}" for i, option in enumerate(options)),
                )
                options = [chr(65 + i) for i in range(len(options))]
            # Follow the installed SDK's parameter naming; the VPS API must match that SDK.
            from aiogram.methods import SendPoll

            correct_parameter = (
                {"correct_option_ids": [item.correct]}
                if ("correct_option_ids" in SendPoll.model_fields)
                else {"correct_option_id": item.correct}
            )
            message = await self.bot.send_poll(
                session.chat_id,
                question,
                options=[InputPollOption(text=text) for text in options],
                type="quiz",
                is_anonymous=False,
                allows_revoting=False,
                **correct_parameter,
            )
            async with self.db.sessions.begin() as db:
                stored = await db.get(Session, session.id)
                stored.poll_id, stored.poll_message_id = message.poll.id, message.message_id
        else:
            if item.front_image:
                await self.bot.send_photo(session.chat_id, FSInputFile(item.front_image))
            await self.text(
                session.chat_id, f"Card {session.cursor + 1}/{len(session.order)}\n\n{item.front}"
            )
            await self.text(session.chat_id, f"Answer: {item.back}", spoiler=True)
            if item.back_image:
                await self.bot.send_photo(
                    session.chat_id, FSInputFile(item.back_image), has_spoiler=True
                )
            token = f"{session.id}:{session.cursor}"
            message = await self.bot.send_message(
                session.chat_id,
                "How did you do?",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            action("✅ Got it", f"rate:{token}:1"),
                            action("❌ Missed it", f"rate:{token}:0"),
                        ],
                        [action("Leave review", f"leave:{session.id}")],
                    ]
                ),
            )
            async with self.db.sessions.begin() as db:
                stored = await db.get(Session, session.id)
                stored.card_message_id = message.message_id

    async def answer_poll(self, poll_id: str, owner: int, options: list[int]):
        if len(options) != 1:
            return
        async with self.db.sessions.begin() as db:
            session = await db.scalar(select(Session).where(Session.poll_id == poll_id))
            if not session or session.owner != owner or session.state != "active":
                return
            study = await db.get(StudySet, session.set_id)
            index = session.order[session.cursor]
            if str(index) in session.answers:
                return
            item = Item.model_validate(study.items[index])
            if options[0] < 0 or options[0] >= len(item.options):
                return
            session.answers = {
                **session.answers,
                str(index): {"answer": options[0], "correct": options[0] == item.correct},
            }
            session.cursor += 1
            session.poll_id = None
        with contextlib.suppress(TelegramBadRequest):
            await self.bot.stop_poll(session.chat_id, session.poll_message_id)
        if item.back:
            await self.text(session.chat_id, item.back)
        await self.send_item(session.id)

    async def rate(self, session_id: str, cursor: int, correct: bool, owner: int):
        async with self.db.sessions.begin() as db:
            session = await db.get(Session, session_id)
            if (
                not session
                or session.owner != owner
                or session.state != "active"
                or session.cursor != cursor
            ):
                raise ValueError("This card is no longer active.")
            study = await db.get(StudySet, session.set_id)
            if study.kind != "flashcards":
                raise ValueError("Invalid review action.")
            index = session.order[cursor]
            session.answers = {**session.answers, str(index): {"correct": correct}}
            session.cursor += 1
        with contextlib.suppress(TelegramBadRequest):
            await self.bot.edit_message_reply_markup(
                chat_id=session.chat_id, message_id=session.card_message_id
            )
        await self.send_item(session.id)

    async def pause(self, session_id: str, owner: int, show=True):
        async with self.db.sessions.begin() as db:
            session = await db.get(Session, session_id)
            if not session or session.owner != owner:
                raise ValueError("Session unavailable.")
            if session.state == "completed":
                return
            session.state = "paused"
            session.poll_id = None
            study = await db.get(StudySet, session.set_id)
        if session.poll_message_id:
            with contextlib.suppress(TelegramBadRequest):
                await self.bot.stop_poll(session.chat_id, session.poll_message_id)
        if session.card_message_id:
            with contextlib.suppress(TelegramBadRequest):
                await self.bot.edit_message_reply_markup(
                    chat_id=session.chat_id, message_id=session.card_message_id
                )
        if show:
            await self.control(session, study)

    async def resume(self, session_id: str, owner: int):
        async with self.db.sessions() as db:
            session = await db.get(Session, session_id)
            if not session or session.owner != owner or session.state not in {"paused", "active"}:
                raise ValueError("This session cannot be resumed.")
            active = (
                await db.scalars(
                    select(Session).where(Session.owner == owner, Session.state == "active")
                )
            ).all()
        for other in active:
            await self.pause(other.id, owner, show=False)
        # Close any orphaned poll before publishing its replacement.
        await self.pause(session_id, owner, show=False)
        async with self.db.sessions.begin() as db:
            session = await db.get(Session, session_id)
            session.state = "active"
        await self.send_item(session_id)
