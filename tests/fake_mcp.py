"""Local MCP contract stand-in. Never talks to Google; production never imports this module."""

import json
import os
from pathlib import Path

from fastmcp import FastMCP

server = FastMCP("NotebookLM test contract")


def record(tool, **arguments):
    if log := os.environ.get("MCP_SIMULATION_LOG"):
        with Path(log).open("a") as stream:
            stream.write(json.dumps({"tool": tool, **arguments}) + "\n")


@server.tool
def notebook_create(title: str) -> dict:
    record("notebook_create", title=title)
    return {"status": "success", "notebook_id": "test-notebook"}


@server.tool
def source_add(
    notebook_id: str, source_type: str, file_path: str, wait: bool, wait_timeout: float
) -> dict:
    record("source_add", wait=wait, path=file_path)
    assert Path(file_path).is_file()
    return {"status": "success", "ready": True, "source_id": "test-source"}


@server.tool
def notebook_share_public(notebook_id: str, is_public: bool) -> dict:
    record("notebook_share_public", is_public=is_public)
    if os.environ.get("MCP_SIMULATION_FAILURE") == "share":
        return {"status": "error", "error": "Sharing unavailable"}
    return {
        "status": "success",
        "public_link": "https://notebooklm.google.com/notebook/test-notebook",
    }


@server.tool
def studio_create(
    notebook_id: str,
    artifact_type: str,
    confirm: bool,
    language: str,
    difficulty: str,
    question_count: int,
    focus_prompt: str,
) -> dict:
    record(
        "studio_create",
        kind=artifact_type,
        language=language,
        difficulty=difficulty,
        count=question_count,
        prompt=focus_prompt,
    )
    assert confirm and language == "en"
    return {"status": "success", "artifact_id": f"test-{artifact_type}"}


@server.tool
def studio_status(notebook_id: str, artifact_id: str) -> dict:
    record("studio_status", artifact_id=artifact_id)
    return {"status": "success", "artifacts": [{"artifact_id": artifact_id, "status": "completed"}]}


@server.tool
def download_artifact(
    notebook_id: str, artifact_id: str, artifact_type: str, output_format: str, output_path: str
) -> dict:
    record("download_artifact", artifact_id=artifact_id)
    data = (
        {
            "questions": [
                {
                    "question": "Which organ pumps blood around the human body?",
                    "answerOptions": [
                        {
                            "text": "Heart",
                            "isCorrect": True,
                            "rationale": "The heart pumps blood through the circulation.",
                        },
                        {"text": "Lungs", "isCorrect": False},
                    ],
                }
            ]
        }
        if artifact_type == "quiz"
        else {
            "cards": [
                {
                    "front": "What is the function of the heart in the human body?",
                    "back": "The heart pumps blood through the circulatory system.",
                }
            ]
        }
    )
    path = Path(os.environ["NOTEBOOKLM_DOWNLOAD_DIR"]) / output_path
    path.write_text(json.dumps(data))
    return {"status": "success", "path": str(path)}


@server.tool
def notebook_delete(notebook_id: str, confirm: bool) -> dict:
    record("notebook_delete", confirm=confirm)
    return {"status": "success"}


if __name__ == "__main__":
    server.run(transport="stdio", show_banner=False)
