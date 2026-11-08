import asyncio
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from studybot.config import Settings


class NotebookError(RuntimeError):
    pass


class NotebookMCP:
    def __init__(self, session: ClientSession, settings: Settings):
        self.session = session
        self.settings = settings

    async def call(self, name: str, *, _timeout: float | None = None, **arguments) -> dict:
        result = await self.session.call_tool(
            name,
            arguments=arguments,
            read_timeout_seconds=_timeout or float(self.settings.mcp_timeout_seconds),
        )
        payload = result.structured_content
        if payload is None:
            blocks = [block.text for block in result.content if block.type == "text"]
            try:
                payload = json.loads("\n".join(blocks))
            except (ValueError, TypeError) as exc:
                raise NotebookError(f"Unsupported response from {name}.") from exc
        if not isinstance(payload, dict):
            raise NotebookError(f"Unsupported response from {name}.")
        if result.is_error or payload.get("status") in {"error", "failed"} or payload.get("error"):
            # Upstream details can include file paths/session information; do not expose them in chat.
            raise NotebookError(
                f"NotebookLM {name} failed. Check authentication, quotas, and server logs."
            )
        return payload

    async def verify_tools(self):
        available = {tool.name for tool in (await self.session.list_tools()).tools}
        needed = {
            "notebook_create",
            "source_add",
            "notebook_share_public",
            "studio_create",
            "studio_status",
            "download_artifact",
            "notebook_delete",
        }
        if missing := needed - available:
            raise NotebookError(
                f"MCP server is missing required tools: {', '.join(sorted(missing))}"
            )

    async def create(self, title: str) -> str:
        data = await self.call("notebook_create", title=title)
        return data["notebook_id"]

    async def upload(self, notebook_id: str, path: Path) -> str:
        data = await self.call(
            "source_add",
            _timeout=float(self.settings.source_timeout_seconds + 30),
            notebook_id=notebook_id,
            source_type="file",
            file_path=str(path),
            wait=True,
            wait_timeout=float(self.settings.source_timeout_seconds),
        )
        if data.get("ready") is not True:
            raise NotebookError("Source processing did not report ready.")
        source = data.get("source", data)
        return source.get("source_id", source.get("id", ""))

    async def share(self, notebook_id: str) -> str:
        from urllib.parse import urlparse

        data = await self.call("notebook_share_public", notebook_id=notebook_id, is_public=True)
        link = data.get("public_link")
        parsed = urlparse(link or "")
        if parsed.scheme != "https" or parsed.hostname not in {
            "notebooklm.google.com",
            "notebook.google.com",
            "gemini.google.com",
        }:
            raise NotebookError("NotebookLM did not return a recognized public notebook URL.")
        return link

    async def generate(self, notebook_id: str, kind: str, settings: dict) -> str:
        count = settings["count"]
        # The upstream quiz field is an amount preset (1=fewer, 2=standard, 3=more),
        # not a guaranteed literal count. Flashcards use the focus prompt for count.
        data = await self.call(
            "studio_create",
            notebook_id=notebook_id,
            artifact_type=kind,
            confirm=True,
            language="en",
            difficulty=settings["difficulty"],
            question_count=1 if count <= 10 else 2 if count <= 20 else 3,
            focus_prompt=(
                f"Create {count} {'multiple-choice questions' if kind == 'quiz' else 'flashcards'}. "
                "Write every question, answer, option, and explanation in English only, "
                "regardless of the language of the sources. Use only the uploaded sources. "
                "For quizzes use exactly one correct option per question."
            ),
        )
        return data["artifact_id"]

    async def wait(self, notebook_id: str, artifact_id: str):
        async with asyncio.timeout(self.settings.generation_timeout_seconds):
            while True:
                data = await self.call(
                    "studio_status", notebook_id=notebook_id, artifact_id=artifact_id
                )
                artifacts = data.get("artifacts", [])
                artifact = next((a for a in artifacts if a.get("artifact_id") == artifact_id), None)
                if artifact and artifact.get("status") == "completed":
                    return
                if artifact and artifact.get("status") == "failed":
                    raise NotebookError("NotebookLM generation failed. Try again later.")
                await asyncio.sleep(5)

    async def download(self, notebook_id: str, artifact_id: str, kind: str, set_id: str):
        name = f"{set_id}.json"
        await self.call(
            "download_artifact",
            notebook_id=notebook_id,
            artifact_id=artifact_id,
            artifact_type=kind,
            output_format="json",
            output_path=name,
        )
        path = self.settings.data_dir / "exports" / name
        if path.stat().st_size > 10_000_000:
            raise NotebookError("Study export exceeds the supported size.")
        return await asyncio.to_thread(lambda: json.loads(path.read_text()))

    async def delete(self, notebook_id: str):
        await self.call("notebook_delete", notebook_id=notebook_id, confirm=True)


@asynccontextmanager
async def connect(settings: Settings):
    env = {
        **os.environ,
        "NOTEBOOKLM_DOWNLOAD_DIR": str(settings.data_dir / "exports"),
        "NOTEBOOKLM_ALLOWED_FILE_DIRS": str(settings.data_dir / "sources"),
        "NOTEBOOKLM_HL": "en",
        "NOTEBOOKLM_MCP_CLI_PATH": str(
            (settings.notebooklm_mcp_cli_path or settings.data_dir / "notebooklm").resolve()
        ),
    }
    command = settings.mcp_command
    if command == "notebooklm-mcp" and not shutil.which(command):
        command = str(Path(sys.executable).parent / command)
    async with stdio_client(
        StdioServerParameters(command=command, args=settings.mcp_args, env=env)
    ) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            adapter = NotebookMCP(session, settings)
            await adapter.verify_tools()
            yield adapter
