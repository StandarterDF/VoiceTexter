"""Постобработка распознанного текста перед вставкой.

Whisper не всегда расставляет заглавные буквы и конечную точку, поэтому перед
вставкой приводим фразу к аккуратному виду:

- первая буква — заглавная (даже если перед ней стоит кавычка/скобка);
- если в конце нет знака препинания — добавляем точку (кавычки/скобки в
  конце при проверке пропускаются, чтобы не получить ``«Привет!».``).

Функция чистая и без зависимостей, поэтому её легко покрыть тестами.
"""

from __future__ import annotations

# Знаки, которые закрывают предложение: после них точку не ставим.
_SENTENCE_END = ".!?…"
# Прочие знаки препинания: запятая/тире и т.п. — тоже считаются «знаком в конце».
_OTHER_PUNCT = ",;:—–-"
# Закрывающие кавычки и скобки: точку ставим ПЕРЕД ними.
_CLOSERS = "»”’\"'`)]}"


def _capitalize_first_letter(text: str) -> str:
    """Сделать заглавной первую букву (пропуская кавычки, скобки, цифры)."""
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1 :]
    return text


def _ensure_ending(text: str) -> str:
    """Добавить точку, если фраза не заканчивается знаком препинания.

    Закрывающие кавычки/скобки пропускаем при проверке: ``«Привет!»`` уже
    закончено, а ``«Привет»`` — нет, и точка ставится в самом конце фразы.
    """
    stripped = text.rstrip()
    if not stripped:
        return text

    index = len(stripped) - 1
    while index >= 0 and stripped[index] in _CLOSERS:
        index -= 1
    if index < 0 or stripped[index] in _SENTENCE_END or stripped[index] in _OTHER_PUNCT:
        return text
    return stripped + "."


def format_transcript(text: str | None) -> str:
    """Привести распознанную фразу к виду «С большой буквы и с точкой в конце»."""
    text = (text or "").strip()
    if not text:
        return text
    return _capitalize_first_letter(_ensure_ending(text))
