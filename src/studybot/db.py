import time
import uuid

from sqlalchemy import JSON, BigInteger, Float, Integer, String, UniqueConstraint, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return uuid.uuid4().hex[:16]


class Base(DeclarativeBase):
    pass


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (UniqueConstraint("chat_id", "input_key"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    input_key: Mapped[str] = mapped_column(String)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="Queued")
    notebook_id: Mapped[str | None] = mapped_column(String)
    notebook_url: Mapped[str | None] = mapped_column(String)
    sharing_error: Mapped[bool] = mapped_column(default=False)
    files: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String)


class StudySet(Base):
    __tablename__ = "study_sets"
    __table_args__ = (UniqueConstraint("submission_id", "kind"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    submission_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="queued")
    settings: Mapped[dict] = mapped_column(JSON)
    artifact_id: Mapped[str | None] = mapped_column(String)
    items: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String)


class Draft(Base):
    __tablename__ = "drafts"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner: Mapped[int] = mapped_column(BigInteger, index=True)
    submission_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    settings: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String, default="editing")
    prompt_message_id: Mapped[int | None] = mapped_column(BigInteger)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="queued", index=True)
    run_at: Mapped[float] = mapped_column(Float, default=time.time)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Checkpointed before non-idempotent upstream writes. A crashed write is never blindly replayed.
    pending_write: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    owner: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    set_id: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="active")
    order: Mapped[list] = mapped_column(JSON)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    poll_id: Mapped[str | None] = mapped_column(String, index=True)
    poll_message_id: Mapped[int | None] = mapped_column(BigInteger)
    control_message_id: Mapped[int | None] = mapped_column(BigInteger)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger)
    started: Mapped[float] = mapped_column(Float, default=time.time)


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self):
        await self.engine.dispose()

    async def claim_job(self) -> str | None:
        from sqlalchemy import update

        now = time.time()
        async with self.sessions.begin() as db:
            candidate = await db.scalar(
                select(Job.id)
                .where(
                    Job.state.in_(["queued", "running"]), Job.run_at <= now, Job.lease_until < now
                )
                .order_by(Job.run_at)
                .limit(1)
            )
            if not candidate:
                return None
            claimed = await db.execute(
                update(Job)
                .where(
                    Job.id == candidate, Job.lease_until < now, Job.state.in_(["queued", "running"])
                )
                .values(state="running", lease_until=now + 60, attempts=Job.attempts + 1)
            )
            return candidate if claimed.rowcount else None
