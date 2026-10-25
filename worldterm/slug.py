"""Slug helpers: turn an article title into a filesystem/URL-safe slug."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime

_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 60) -> str:
    """Lower-case ASCII slug with single hyphens; never empty."""
    normalised = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_WORD.sub("-", ascii_text).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0] if "-" in slug[:max_len] else slug[:max_len]
        slug = slug.strip("-")
    return slug or "untitled"


def note_filename(title: str, when: datetime | None = None) -> str:
    """`YYYYMMDD-<slug>.md` using *when* (defaults to now)."""
    when = when or datetime.now()
    return f"{when.strftime('%Y%m%d')}-{slugify(title)}.md"
