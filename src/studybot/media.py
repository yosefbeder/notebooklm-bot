import asyncio
import re
import shutil
import zipfile
from pathlib import Path

import pymupdf
from aiogram.types import Message

EXTENSIONS = frozenset(
    "pdf txt md docx csv pptx epub 3g2 3gp aac aif aifc aiff amr au avi "
    "m4a mp3 mp4 mpeg ogg opus ra snd wav wma avif bmp gif ico jp2 png webp "
    "tif tiff heic heif jpeg jpg jpe odt rtf hwp hwpx".split()
)
MAX_SOURCE_BYTES = 200 * 1024 * 1024


def incoming_file(message: Message) -> dict | None:
    for attribute, extension in [
        ("document", None),
        ("audio", "mp3"),
        ("voice", "ogg"),
        ("video", "mp4"),
        ("video_note", "mp4"),
        ("animation", "mp4"),
    ]:
        media = getattr(message, attribute)
        if media:
            name = getattr(media, "file_name", None) or f"{attribute}.{extension or 'bin'}"
            return {"file_id": media.file_id, "name": Path(name).name, "size": media.file_size or 0}
    if message.photo:
        media = message.photo[-1]
        return {"file_id": media.file_id, "name": "image.jpg", "size": media.file_size or 0}
    return None


def validate_file(path: Path):
    suffix = path.suffix.lower().lstrip(".")
    if suffix not in EXTENSIONS:
        raise ValueError("This source file type is not supported.")
    if not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
        raise ValueError("Sources must be non-empty and at most 200 MB.")
    with path.open("rb") as stream:
        header = stream.read(1024)
    if suffix == "pdf" and not header.lstrip().startswith(b"%PDF-"):
        raise ValueError("The file does not contain a valid PDF header.")
    if suffix in {"docx", "pptx", "epub", "odt", "hwpx"}:
        if not zipfile.is_zipfile(path):
            raise ValueError("Invalid document container.")
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > 500_000_000:
                raise ValueError("Expanded document is too large.")
    if suffix in {"txt", "md", "csv"} and b"\0" in header:
        raise ValueError("The file appears to be binary rather than text.")


async def download_source(bot, settings, file: dict, submission_id: str, index: int) -> Path:
    suffix = Path(file["name"]).suffix.lower()
    if suffix.lstrip(".") not in EXTENSIONS:
        raise ValueError("Unsupported file extension. Send the original file as a document.")
    if file["size"] > MAX_SOURCE_BYTES:
        raise ValueError("NotebookLM sources must be at most 200 MB.")
    if not settings.telegram_api_base and file["size"] > 20 * 1024 * 1024:
        raise ValueError(
            "This file needs the VPS Bot API. Configure TELEGRAM_API_BASE for files over 20 MB."
        )
    directory = settings.data_dir / "sources" / submission_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{index}{suffix}"
    if not path.exists():
        remote = await bot.get_file(file["file_id"])
        if not remote.file_path:
            raise ValueError("Telegram could not provide this file. Please upload it directly.")
        temporary = path.with_suffix(path.suffix + ".part")
        if settings.telegram_local_files:
            source = Path(remote.file_path)
            if settings.telegram_file_root:
                source = Path(settings.telegram_mount_root) / source.relative_to(
                    Path(settings.telegram_file_root)
                )
            await asyncio.to_thread(shutil.copyfile, source, temporary)
        else:
            await bot.download_file(remote.file_path, destination=temporary, timeout=600)
        temporary.replace(path)
    await asyncio.to_thread(validate_file, path)
    return path


def extract_assets(path: Path, root: Path) -> list[dict]:
    """Source-backed answer illustrations, with page context used for conservative matching."""
    root.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
        with pymupdf.open(path) as doc:
            target = root / f"{path.stem}-image.png"
            doc[0].get_pixmap(matrix=pymupdf.Matrix(1, 1)).save(target)
        return [{"path": str(target), "context": "", "standalone": True}]
    if path.suffix.lower() != ".pdf":
        return []
    result = []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc):
            if len(result) >= 40:
                break
            if page.get_images() or page.get_drawings():
                target = root / f"{path.stem}-page-{i + 1}.png"
                page.get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2)).save(target)
                result.append(
                    {"path": str(target), "context": page.get_text()[:8000], "standalone": False}
                )
    return result


def illustrate(items, assets):
    """Attach only answer-side source pages; never expose labeled answers on the question side."""
    stop = {"which", "what", "where", "these", "there", "their", "about", "would", "following"}
    for item in items:
        words = set(re.findall(r"[a-z]{5,}", (item.front + " " + item.back).lower())) - stop
        matches = []
        for asset in assets:
            context = set(re.findall(r"[a-z]{5,}", asset["context"].lower()))
            overlap = len(words & context)
            if overlap >= 4 and overlap / max(len(words), 1) >= 0.45:
                matches.append((overlap, asset["path"]))
        matches.sort(reverse=True)
        if matches and (len(matches) == 1 or matches[0][0] > matches[1][0]):
            item.back_image = matches[0][1]
        elif len(assets) == 1 and assets[0]["standalone"]:
            item.back_image = assets[0]["path"]
