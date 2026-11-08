import argparse
import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramRetryAfter
from filelock import FileLock

from studybot.app import App
from studybot.config import Settings
from studybot.db import Database, Job, StudySet, Submission
from studybot.notebook import connect
from studybot.ui import UI
from studybot.worker import Worker


def make_bot(settings):
    api = (
        TelegramAPIServer.from_base(settings.telegram_api_base.rstrip("/"))
        if (settings.telegram_api_base)
        else TelegramAPIServer.from_base("https://api.telegram.org")
    )
    # File copying is handled explicitly so container mount mappings work too.
    session = AiohttpSession(api=api, timeout=120)

    @session.middleware()
    async def rate_limit(make_request, bot, method):
        for attempt in range(4):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt == 3:
                    raise
                await asyncio.sleep(exc.retry_after + 0.1)

    return Bot(settings.bot_token.get_secret_value(), session=session)


async def reconcile(database, job_id, upstream_id, absent):
    async with database.sessions.begin() as db:
        job = await db.get(Job, job_id)
        if not job or job.state != "needs_review" or not job.pending_write:
            raise ValueError("This job does not need reconciliation.")
        if not upstream_id and not absent:
            raise ValueError("Provide a verified upstream ID or --confirmed-absent.")
        if upstream_id:
            if job.pending_write == "create_notebook":
                sub = await db.get(Submission, job.target)
                sub.notebook_id = upstream_id
            elif job.pending_write.startswith("upload:"):
                sub = await db.get(Submission, job.target)
                index = int(job.pending_write.split(":")[1])
                files = [dict(file) for file in sub.files]
                files[index]["source_id"] = upstream_id
                sub.files = files
            elif job.pending_write == "generate":
                study = await db.get(StudySet, job.target)
                study.artifact_id = upstream_id
        job.pending_write, job.state, job.run_at, job.lease_until = None, "queued", time.time(), 0


async def run(args):
    settings = Settings()
    for folder in ["sources", "exports", "assets", "pdf"]:
        (settings.data_dir / folder).mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_url)
    await database.initialize()
    try:
        if args.command == "reconcile":
            await reconcile(database, args.job_id, args.upstream_id, args.confirmed_absent)
            print("Job reconciled and queued.")
            return
        bot = make_bot(settings)
        try:
            ui = UI(bot, database)
            app = App(database, bot, settings, ui)
            async with connect(settings) as notebook:
                if args.command == "check":
                    await bot.get_me()
                    print(
                        "Telegram and MCP tools are reachable. Google authentication still needs a live notebook operation."
                    )
                    return
                await app.study.recover()
                worker = asyncio.create_task(Worker(database, bot, notebook, ui, settings).run())
                dispatcher = Dispatcher()
                dispatcher.include_router(app.router)
                polling = asyncio.create_task(
                    dispatcher.start_polling(
                        bot,
                        allowed_updates=["message", "callback_query", "poll_answer"],
                        handle_as_tasks=False,
                        close_bot_session=False,
                    )
                )
                try:
                    done, _ = await asyncio.wait(
                        [worker, polling], return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        task.result()
                finally:
                    worker.cancel()
                    polling.cancel()
                    await asyncio.gather(worker, polling, return_exceptions=True)
        finally:
            await bot.session.close()
    finally:
        await database.close()


def main():
    parser = argparse.ArgumentParser(description="NotebookLM Telegram study bot")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("check", help="Check Telegram connectivity and required MCP tools")
    recover = subcommands.add_parser("reconcile", help="Resolve an ambiguous NotebookLM write")
    recover.add_argument("job_id")
    group = recover.add_mutually_exclusive_group(required=True)
    group.add_argument("--upstream-id")
    group.add_argument("--confirmed-absent", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Request URLs can contain Telegram bot tokens; never enable SDK HTTP debug logging.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    if args.command is None:
        settings = Settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        # One polling process and one worker per deployment; locks/session state are process-local.
        with FileLock(settings.data_dir / "bot.lock", timeout=0):
            asyncio.run(run(args))
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
