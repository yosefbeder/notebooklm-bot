from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from studybot.domain import Item


def render_pdf(
    title: str, kind: str, items: list[Item], output: Path, font_path: Path | None = None
):
    font = "Helvetica"
    if font_path:
        if "StudyFont" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("StudyFont", str(font_path)))
        font = "StudyFont"
    body = ParagraphStyle(
        "StudyBody",
        fontName=font,
        fontSize=10.5,
        leading=16,
        textColor=colors.HexColor("#243247"),
        spaceAfter=10,
        alignment=TA_LEFT,
    )
    heading = ParagraphStyle("StudyHeading", parent=body, fontSize=18, leading=24, spaceAfter=18)
    label = ParagraphStyle(
        "StudyLabel", parent=body, fontSize=11, spaceBefore=10, textColor=colors.HexColor("#166575")
    )

    def paragraph(value, style=body):
        # Exported content is untrusted, including ReportLab's XML markup.
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)

    def image(path):
        if path:
            element = Image(path)
            scale = min(6.5 * inch / element.imageWidth, 5 * inch / element.imageHeight, 1)
            element.drawWidth = element.imageWidth * scale
            element.drawHeight = element.imageHeight * scale
            return [element, Spacer(1, 12)]
        return []

    story = [
        paragraph(title[:160], heading),
        paragraph(
            f"{'MCQs' if kind == 'quiz' else 'Flashcards'} | {len(items)} items | English", label
        ),
        Spacer(1, 12),
    ]
    for i, item in enumerate(items, 1):
        story += [
            paragraph(f"{'Question' if kind == 'quiz' else 'Card'} {i}", label),
            paragraph(item.front),
            *image(item.front_image),
        ]
        for j, option in enumerate(item.options):
            story.append(paragraph(f"{chr(65 + j)}. {option}"))
        story.append(Spacer(1, 10))
    story += [PageBreak(), paragraph("Answers and explanations", heading)]
    for i, item in enumerate(items, 1):
        answer = (
            f"{chr(65 + item.correct)}. {item.options[item.correct]}"
            if kind == "quiz"
            else item.back
        )
        story += [paragraph(f"{i}. {answer}", label)]
        if kind == "quiz" and item.back:
            story.append(paragraph(item.back))
        story += image(item.back_image)

    def footer(canvas, document):
        canvas.setFont(font, 9)
        canvas.setFillColor(colors.HexColor("#627080"))
        canvas.drawString(54, 30, "NotebookLM Study Bot")
        canvas.drawRightString(document.pagesize[0] - 54, 30, str(document.page))

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".part.pdf")
    SimpleDocTemplate(
        str(temporary), leftMargin=54, rightMargin=54, topMargin=48, bottomMargin=54, title=title
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    temporary.replace(output)
