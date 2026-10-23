"""Модерация по списку запрещённых слов (TXT + синхронизация с таблицей BlockedWord)."""
from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_LAT2CYR = str.maketrans("aceopxykmthb", "асеорхукмтнв")
_CYR = re.compile(r"[а-я]")
_LAT = re.compile(r"[a-z]")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)
_SPACED = re.compile(r"(?<!\w)(?:\w[\s.\-*]){2,}\w(?!\w)")


def _fix_mixed_script(text: str) -> str:
    def fix(m: re.Match) -> str:
        tok = m.group(0)
        return tok.translate(_LAT2CYR) if _CYR.search(tok) and _LAT.search(tok) else tok
    return re.sub(r"\w+", fix, text)


def normalize_text(text: str) -> str:
    """NFKC, без невидимых символов, регистр, ё→е, подмена латиницы в смешанных словах, без пунктуации."""
    t = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).casefold().replace("ё", "е")
    t = _fix_mixed_script(t)
    return _NON_WORD.sub(" ", t).strip()


def _collapse_spaced_letters(text: str) -> str:
    """'п р и м е р' -> 'пример' (только для проверки, не меняет исходный текст)."""
    return _NON_WORD.sub(" ", _SPACED.sub(lambda m: re.sub(r"[\s.\-*]", "", m.group(0)), text)).strip()


@dataclass(frozen=True)
class BlockedTerm:
    normalized: str
    match_type: str  # exact | word | phrase
    reason: str | None = None
    id: int | None = None


@dataclass(frozen=True)
class Violation:
    term: str
    match_type: str
    reason: str | None = None


def _default_type(norm: str) -> str:
    return "phrase" if " " in norm else "word"


class ModerationService:
    def __init__(self, terms: list[BlockedTerm] | None = None, path: str | Path | None = None):
        self._terms: list[BlockedTerm] = terms or []
        self._path = Path(path) if path else None
        self._mtime: float | None = None
        self._lock = threading.Lock()

    # --- загрузка ---
    @staticmethod
    def parse_lines(lines: list[str]) -> list[BlockedTerm]:
        seen: dict[str, BlockedTerm] = {}
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            norm = normalize_text(line)
            if norm and norm not in seen:
                seen[norm] = BlockedTerm(norm, _default_type(norm))
        return list(seen.values())

    def load_file(self) -> None:
        if not self._path or not self._path.exists():
            return
        with self._lock:
            self._terms = self.parse_lines(self._path.read_text(encoding="utf-8").splitlines())
            self._mtime = self._path.stat().st_mtime

    def reload_if_changed(self) -> bool:
        if not self._path or not self._path.exists():
            return False
        if self._mtime != self._path.stat().st_mtime:
            self.load_file()
            return True
        return False

    def set_terms(self, terms: list[BlockedTerm]) -> None:
        with self._lock:
            self._terms = list(terms)

    @property
    def terms(self) -> list[BlockedTerm]:
        return list(self._terms)

    # --- проверка ---
    @staticmethod
    def _matches(term: BlockedTerm, text: str) -> bool:
        if term.match_type == "exact":
            return text == term.normalized
        # word/phrase: совпадение только по границам слов, не по подстроке
        return re.search(rf"(?<!\w){re.escape(term.normalized)}(?!\w)", text) is not None

    def check(self, text: str | None) -> list[Violation]:
        if not text:
            return []
        norm = normalize_text(text)
        variants = {norm, _collapse_spaced_letters(unicodedata.normalize("NFKC", text).casefold())}
        found: list[Violation] = []
        for term in self._terms:
            if any(self._matches(term, v) for v in variants if v):
                found.append(Violation(term.normalized, term.match_type, term.reason))
        return found

    def check_fields(self, fields: dict[str, str | None]) -> dict[str, list[Violation]]:
        result = {}
        for name, value in fields.items():
            v = self.check(value)
            if v:
                result[name] = v
        return result

    @staticmethod
    def user_message(violations_by_field: dict[str, list[Violation]]) -> str:
        if not violations_by_field:
            return ""
        fields = ", ".join(violations_by_field)
        return f"Объявление не прошло проверку. Исправьте поля: {fields} — и отправьте повторно."


async def sync_blocked_words(session, service: ModerationService) -> int:
    """Добавляет в таблицу BlockedWord слова из TXT, которых там ещё нет. Возвращает число добавленных."""
    from sqlalchemy import select

    from mysite.database.ai_models import BlockedWord

    existing = set((await session.execute(select(BlockedWord.normalized_word))).scalars())
    added = 0
    for t in service.terms:
        if t.normalized not in existing:
            session.add(BlockedWord(word=t.normalized, normalized_word=t.normalized, match_type=t.match_type))
            added += 1
    await session.commit()
    return added


async def load_terms_from_db(session) -> list[BlockedTerm]:
    from sqlalchemy import select

    from mysite.database.ai_models import BlockedWord

    rows = (await session.execute(select(BlockedWord).where(BlockedWord.is_active.is_(True)))).scalars()
    return [BlockedTerm(r.normalized_word, r.match_type, r.reason, r.id) for r in rows]
