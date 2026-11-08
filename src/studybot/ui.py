from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton as Button
from aiogram.types import InlineKeyboardMarkup as Keyboard
from sqlalchemy import select

from studybot.db import Draft, StudySet, Submission


def action(text: str, data: str) -> Button:
    return Button(text=text, callback_data=data)


def notebook_row(sub: Submission) -> list[list[Button]]:
    return [[Button(text="Open Notebook", url=sub.notebook_url)]] if sub.notebook_url else []


class UI:
    def __init__(self, bot, db):
        self.bot, self.db = bot, db

    async def edit(self, sub, text: str, rows):
        markup = Keyboard(inline_keyboard=rows)
        if sub.message_id:
            try:
                await self.bot.edit_message_text(
                    text,
                    chat_id=sub.chat_id,
                    message_id=sub.message_id,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                return
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc):
                    return
                if "message to edit not found" not in str(
                    exc
                ) and "message can't be edited" not in str(exc):
                    raise
        message = await self.bot.send_message(
            sub.chat_id, text, reply_markup=markup, parse_mode="HTML"
        )
        async with self.db.sessions.begin() as db:
            stored = await db.get(Submission, sub.id)
            stored.message_id = message.message_id

    async def render(self, submission_id: str):
        async with self.db.sessions() as db:
            sub = await db.get(Submission, submission_id)
            sets = (
                await db.scalars(select(StudySet).where(StudySet.submission_id == submission_id))
            ).all()
        rows = notebook_row(sub)
        text = f"📘 <b>{escape(sub.title[:180])}</b>\n{escape(sub.state)}"
        if sub.error:
            text += f"\n{escape(sub.error)}"
        if sub.sharing_error:
            text += "\nNotebook link unavailable."
            rows.append([action("Retry notebook link", f"share:{sub.id}")])
        if sub.state == "Ready":
            for kind in ["quiz", "flashcards"]:
                study = next((s for s in sets if s.kind == kind), None)
                title = "MCQs" if kind == "quiz" else "flashcards"
                if study and study.state == "ready":
                    text += f"\n{title.capitalize()}: {len(study.items)} items · {study.settings['difficulty']}"
                    rows.append(
                        [
                            action(f"Start {title}", f"start:{study.id}"),
                            action(
                                f"{title.capitalize() if kind != 'quiz' else title} PDF",
                                f"pdf:{study.id}",
                            ),
                        ]
                    )
                elif study and study.state in {"queued", "generating"}:
                    text += f"\nGenerating {title}…"
                elif study and study.state == "needs_review":
                    text += f"\n{title}: awaiting operator reconciliation."
                else:
                    if study and study.error:
                        text += f"\n{title}: {escape(study.error)}"
                    rows.append([action(f"Generate {title}", f"config:{sub.id}:{kind}")])
            rows.append([action("Delete notebook and files", f"delete:{sub.id}")])
        elif sub.state == "Failed":
            rows.append([action("Retry upload", f"retry:{sub.id}")])
        elif sub.state == "Delete failed":
            rows.append([action("Retry deletion", f"delete:{sub.id}")])
        await self.edit(sub, text, rows)

    async def draft(self, draft_id: str):
        async with self.db.sessions() as db:
            draft = await db.get(Draft, draft_id)
            sub = await db.get(Submission, draft.submission_id)
            ready = (
                await db.scalars(
                    select(StudySet).where(
                        StudySet.submission_id == sub.id, StudySet.state == "ready"
                    )
                )
            ).all()
        settings = draft.settings
        title = "MCQs" if draft.kind == "quiz" else "flashcards"
        label = "Questions" if draft.kind == "quiz" else "Cards"
        text = (
            f"<b>Generate {title}</b>\nNumber of {label}: {settings['count']}\n"
            f"Level of Difficulty: {settings['difficulty'].capitalize()}\nLanguage: English\n\n"
            "NotebookLM treats count as a target; the actual number may differ."
        )
        if draft.kind == "quiz":
            amount = (
                "fewer"
                if settings["count"] <= 10
                else "standard"
                if settings["count"] <= 20
                else "more"
            )
            text += f"\nAmount preset: {amount}, with your requested count in the instructions."
        rows = [
            [
                action(f"{n}{' ✓' if settings['count'] == n else ''}", f"count:{draft.id}:{n}")
                for n in [10, 20, 30, 50]
            ],
            [action("Custom count", f"custom:{draft.id}")],
            [
                action(
                    f"{level.capitalize()}{' ✓' if settings['difficulty'] == level else ''}",
                    f"level:{draft.id}:{level}",
                )
                for level in ["easy", "medium", "hard"]
            ],
            [action("Generate", f"generate:{draft.id}"), action("Back", f"back:{draft.id}")],
        ]
        for study in ready:
            rows.append(
                [
                    action(
                        f"Start {'MCQs' if study.kind == 'quiz' else 'flashcards'}",
                        f"start:{study.id}",
                    ),
                    action("PDF", f"pdf:{study.id}"),
                ]
            )
        await self.edit(sub, text, rows + notebook_row(sub))
