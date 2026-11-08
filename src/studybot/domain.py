import json
import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Kind = Literal["quiz", "flashcards"]


class GenerationSettings(BaseModel):
    count: int = Field(default=20, ge=1, le=100)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    language: Literal["en"] = "en"


class Item(BaseModel):
    front: str = Field(min_length=1, max_length=20000)
    back: str = Field(default="", max_length=20000)
    options: list[str] = Field(default_factory=list, max_length=12)
    correct: int | None = None
    front_image: str | None = None
    back_image: str | None = None

    @model_validator(mode="after")
    def valid_answer(self):
        if self.options and (
            len(self.options) < 2
            or self.correct is None
            or not 0 <= self.correct < len(self.options)
            or any(not option.strip() for option in self.options)
        ):
            raise ValueError("Invalid quiz answer mapping.")
        return self


def normalize_export(raw: object, kind: Kind) -> list[Item]:
    """Accept explicit observed/legacy export shapes; fail closed on unknown answer mappings."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        raw = raw.get(
            "questions" if kind == "quiz" else "flashcards", raw.get("cards", raw.get("items"))
        )
    if not isinstance(raw, list) or not raw or len(raw) > 200:
        raise ValueError("NotebookLM returned an unsupported or empty study set.")
    result = []
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError("Unsupported study item.")
        if kind == "flashcards":
            result.append(
                Item(
                    front=value.get("front", value.get("question", "")),
                    back=value.get("back", value.get("answer", "")),
                )
            )
            if not result[-1].back:
                raise ValueError("Flashcard has no answer.")
            continue
        options = value.get("options", value.get("answerOptions", []))
        correct = value.get("correct_answer_index", value.get("correctAnswerIndex"))
        explanation = value.get("explanation", "")
        if options and isinstance(options[0], dict):
            marked = [i for i, option in enumerate(options) if option.get("isCorrect") is True]
            if len(marked) != 1:
                raise ValueError("Expected exactly one correct answer.")
            correct = marked[0]
            explanation = explanation or options[correct].get("rationale", "")
            options = [option.get("text", "") for option in options]
        if correct is None:
            answer = value.get("answer", value.get("correct_answer"))
            if isinstance(answer, str) and answer in options and options.count(answer) == 1:
                correct = options.index(answer)
        if not options or type(correct) is not int:
            raise ValueError("Missing explicit quiz answer mapping.")
        result.append(
            Item(
                front=value.get("question", ""), back=explanation, options=options, correct=correct
            )
        )
    return result


@lru_cache(maxsize=1)
def _detector():
    from lingua import LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_all_languages().build()


def require_english(items: list[Item]) -> None:
    # Check prose in batches; individual medical terms often have no meaningful language.
    from lingua import Language

    text = " ".join(item.front + " " + item.back + " " + " ".join(item.options) for item in items)
    if re.search(r"[\u0600-\u06ff\u4e00-\u9fff\u0400-\u04ff]", text):
        raise ValueError("Generated content was not English. Please retry generation.")
    if len(text.split()) >= 12 and _detector().detect_language_of(text[:12000]) != Language.ENGLISH:
        raise ValueError("Generated content was not English. Please retry generation.")
