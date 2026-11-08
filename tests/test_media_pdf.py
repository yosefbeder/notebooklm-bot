import pymupdf
import pytest

from studybot.domain import Item
from studybot.media import download_source, extract_assets, illustrate, validate_file
from studybot.pdf import render_pdf


def test_illustrated_pdf_and_export(tmp_path):
    source = tmp_path / "source.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=400, height=400)
        page.insert_text((30, 40), "The heart pumps oxygenated blood through systemic circulation.")
        page.draw_rect(pymupdf.Rect(100, 100, 220, 220), color=(0.1, 0.4, 0.6), fill=(0.8, 0.9, 1))
        page.insert_text((110, 160), "Left ventricle")
        document.save(source)
    assets = extract_assets(source, tmp_path / "assets")
    assert len(assets) == 1
    items = [
        Item(
            front="Which chamber pumps oxygenated blood through systemic circulation?",
            back="The left ventricle pumps oxygenated blood through systemic circulation.",
        )
    ]
    illustrate(items, assets)
    assert items[0].back_image == assets[0]["path"]
    target = tmp_path / "flashcards.pdf"
    render_pdf("Human heart", "flashcards", items, target)
    with pymupdf.open(target) as result:
        assert len(result) >= 2
        assert "Which chamber" in result[0].get_text()
        assert "Answers and explanations" in result[1].get_text()
        assert result[1].get_images()
    # Markup is printed literally rather than interpreted by the PDF engine.
    render_pdf(
        "<title>",
        "quiz",
        [
            Item(
                front="Is x < 5?",
                back="An explanation & evidence.",
                options=["Yes", "No"],
                correct=0,
            )
        ],
        tmp_path / "quiz.pdf",
    )
    with pymupdf.open(tmp_path / "quiz.pdf") as result:
        assert "Is x < 5?" in result[0].get_text()
        assert "1. A. Yes" in result[1].get_text()


async def test_local_api_download_mount_mapping_and_validation(settings, bot, tmp_path):
    root = tmp_path / "telegram-volume"
    root.mkdir()
    (root / "source.txt").write_text("This is the original source text.")

    async def local_file(file_id):
        from types import SimpleNamespace

        return SimpleNamespace(file_path="/api/storage/source.txt")

    bot.get_file = local_file
    settings.telegram_api_base = "http://localhost:8081"
    settings.telegram_local_files = True
    settings.telegram_file_root = "/api/storage"
    settings.telegram_mount_root = str(root)
    path = await download_source(
        bot, settings, {"file_id": "original", "name": "notes.txt", "size": 100}, "submission", 0
    )
    assert path.read_text() == "This is the original source text."
    fake = tmp_path / "fake.pdf"
    fake.write_text("This is not a PDF.")
    with pytest.raises(ValueError, match="PDF header"):
        validate_file(fake)


async def test_default_api_large_file_is_rejected(settings, bot):
    with pytest.raises(ValueError, match="VPS Bot API"):
        await download_source(
            bot,
            settings,
            {"file_id": "large", "name": "lecture.mp3", "size": 30_000_000},
            "submission",
            0,
        )
