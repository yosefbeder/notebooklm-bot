import asyncio
import contextlib
import logging
import shutil
import time

from sqlalchemy import select

from studybot.db import Draft, Job, Session, StudySet, Submission
from studybot.domain import normalize_export, require_english
from studybot.media import download_source, extract_assets, illustrate

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, db, bot, notebook, ui, settings):
        self.db, self.bot, self.notebook, self.ui, self.settings = db, bot, notebook, ui, settings

    async def checkpoint(self, job_id: str, pending: str | None):
        async with self.db.sessions.begin() as db:
            job = await db.get(Job, job_id)
            job.pending_write = pending

    async def stage(self, sub_id: str, state: str):
        async with self.db.sessions.begin() as db:
            sub = await db.get(Submission, sub_id)
            sub.state, sub.error = state, None
        await self.ui.render(sub_id)

    async def heartbeat(self, job_id: str):
        while True:
            await asyncio.sleep(15)
            async with self.db.sessions.begin() as db:
                job = await db.get(Job, job_id)
                job.lease_until = time.time() + 60

    async def run(self):
        while True:
            job_id = await self.db.claim_job()
            if not job_id:
                await asyncio.sleep(1)
                continue
            heartbeat = asyncio.create_task(self.heartbeat(job_id))
            try:
                await self.execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Worker operation failed (job %s)", job_id)
                await self.fail(job_id, exc)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def execute(self, job_id: str):
        async with self.db.sessions() as db:
            job = await db.get(Job, job_id)
        if job.pending_write:
            await self.fail(job_id)
            return
        if job.kind == "ingest":
            await self.ingest(job)
        elif job.kind == "generate":
            await self.generate(job)
        elif job.kind == "share":
            await self.share(job.target)
        elif job.kind == "delete":
            await self.delete(job)
        else:
            raise ValueError("Unknown job type.")
        async with self.db.sessions.begin() as db:
            stored = await db.get(Job, job_id)
            stored.state, stored.lease_until = "done", 0

    async def fail(self, job_id: str, error: Exception | None = None):
        async with self.db.sessions.begin() as db:
            job = await db.get(Job, job_id)
            uncertain = bool(job.pending_write)
            if (
                not uncertain
                and error is not None
                and not isinstance(error, ValueError)
                and job.attempts < 3
            ):
                job.state, job.lease_until = "queued", 0
                job.run_at = time.time() + 5 * (2 ** max(job.attempts - 1, 0))
                return  # Only safe, checkpointed reads/downloads are automatically retried.
            job.state, job.lease_until = "needs_review" if uncertain else "failed", 0
            job.error = (
                "The upstream request may have completed. Operator reconciliation is required."
                if uncertain
                else "Operation failed. Check the server logs and retry."
            )
            if not uncertain and isinstance(error, ValueError):
                job.error = str(error)[:300]
            if job.kind == "generate":
                study = await db.get(StudySet, job.target)
                study.state, study.error = job.state, job.error
                sub_id = study.submission_id
            else:
                sub_id = job.target
                sub = await db.get(Submission, sub_id)
                if job.kind == "share":
                    sub.sharing_error = True
                elif job.kind == "delete":
                    sub.state, sub.error = "Delete failed", job.error
                else:
                    sub.state = "Needs operator review" if uncertain else "Failed"
                    sub.error = job.error
        with contextlib.suppress(Exception):
            await self.ui.render(sub_id)

    async def share(self, sub_id: str):
        async with self.db.sessions() as db:
            sub = await db.get(Submission, sub_id)
        try:
            url = await self.notebook.share(sub.notebook_id)
        except Exception:
            log.warning("Notebook sharing unavailable for submission %s", sub_id)
            async with self.db.sessions.begin() as db:
                stored = await db.get(Submission, sub_id)
                stored.sharing_error = True
        else:
            async with self.db.sessions.begin() as db:
                stored = await db.get(Submission, sub_id)
                stored.notebook_url, stored.sharing_error = url, False
        await self.ui.render(sub_id)

    async def ingest(self, job):
        async with self.db.sessions() as db:
            sub = await db.get(Submission, job.target)
        paths = []
        await self.stage(sub.id, "Downloading")
        for i, file in enumerate(sub.files):
            paths.append(await download_source(self.bot, self.settings, file, sub.id, i))
        if not sub.notebook_id:
            await self.stage(sub.id, "Creating notebook")
            await self.checkpoint(job.id, "create_notebook")
            notebook_id = await self.notebook.create(f"{sub.title[:100]} [{sub.id}]")
            async with self.db.sessions.begin() as db:
                stored = await db.get(Submission, sub.id)
                stored.notebook_id = notebook_id
                stored_job = await db.get(Job, job.id)
                stored_job.pending_write = None
            sub.notebook_id = notebook_id
        for i, path in enumerate(paths):
            if sub.files[i].get("source_id"):
                continue
            await self.stage(sub.id, f"Uploading and processing source {i + 1}/{len(paths)}")
            await self.checkpoint(job.id, f"upload:{i}")
            source_id = await self.notebook.upload(sub.notebook_id, path)
            if not source_id:
                raise ValueError("NotebookLM did not return a source ID.")
            sub.files = [dict(file) for file in sub.files]
            sub.files[i]["source_id"] = source_id
            async with self.db.sessions.begin() as db:
                stored = await db.get(Submission, sub.id)
                stored.files = sub.files
                stored_job = await db.get(Job, job.id)
                stored_job.pending_write = None
            if not sub.notebook_url:
                await self.share(sub.id)
                async with self.db.sessions() as db:
                    sub = await db.get(Submission, sub.id)
        await self.stage(sub.id, "Ready")

    async def generate(self, job):
        async with self.db.sessions.begin() as db:
            study = await db.get(StudySet, job.target)
            sub = await db.get(Submission, study.submission_id)
            study.state = "generating"
        await self.ui.render(sub.id)
        if not study.artifact_id:
            await self.checkpoint(job.id, "generate")
            artifact_id = await self.notebook.generate(sub.notebook_id, study.kind, study.settings)
            async with self.db.sessions.begin() as db:
                stored = await db.get(StudySet, study.id)
                stored.artifact_id = artifact_id
                stored_job = await db.get(Job, job.id)
                stored_job.pending_write = None
            study.artifact_id = artifact_id
        await self.notebook.wait(sub.notebook_id, study.artifact_id)
        raw = await self.notebook.download(sub.notebook_id, study.artifact_id, study.kind, study.id)
        try:
            items = normalize_export(raw, study.kind)
            await asyncio.to_thread(require_english, items)
        except ValueError:
            # A known completed but invalid artifact needs a fresh, user-confirmed generation.
            async with self.db.sessions.begin() as db:
                stored = await db.get(StudySet, study.id)
                stored.artifact_id = None
            raise
        if study.kind == "flashcards":
            assets = []
            for path in sorted((self.settings.data_dir / "sources" / sub.id).glob("*")):
                if path.suffix == ".part":
                    continue
                try:
                    assets += await asyncio.to_thread(
                        extract_assets, path, self.settings.data_dir / "assets" / sub.id
                    )
                except Exception:
                    log.warning("Image extraction failed for submission %s", sub.id)
            illustrate(items, assets)
        async with self.db.sessions.begin() as db:
            stored = await db.get(StudySet, study.id)
            stored.items, stored.state, stored.error = (
                [item.model_dump() for item in items],
                "ready",
                None,
            )
        await self.ui.render(sub.id)

    async def delete(self, job):
        async with self.db.sessions() as db:
            sub = await db.get(Submission, job.target)
        # Deletion is an explicit user-confirmed operation, never an automatic retention job.
        if sub.notebook_id:
            await self.notebook.delete(sub.notebook_id)
        async with self.db.sessions.begin() as db:
            sets = (
                await db.scalars(select(StudySet).where(StudySet.submission_id == sub.id))
            ).all()
            for study in sets:
                sessions = (
                    await db.scalars(select(Session).where(Session.set_id == study.id))
                ).all()
                for session in sessions:
                    await db.delete(session)
                for folder, suffix in [("exports", ".json"), ("pdf", ".pdf")]:
                    (self.settings.data_dir / folder / f"{study.id}{suffix}").unlink(
                        missing_ok=True
                    )
                await db.delete(study)
            drafts = (await db.scalars(select(Draft).where(Draft.submission_id == sub.id))).all()
            for draft in drafts:
                await db.delete(draft)
            stored = await db.get(Submission, sub.id)
            stored.state, stored.notebook_url, stored.files = "Deleted", None, []
        for folder in ["sources", "assets"]:
            await asyncio.to_thread(shutil.rmtree, self.settings.data_dir / folder / sub.id, True)
        await self.ui.render(sub.id)
