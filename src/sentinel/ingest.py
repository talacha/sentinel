"""Turn an uploaded PDF / text file into a `Document` that can locate verbatim quotes.

The locator is what makes verdicts auditable: any quote the model cites must be found in the
source text (modulo whitespace, case, and typographic punctuation) or the policy layer
downgrades the verdict.
"""

from __future__ import annotations

import hashlib
import io
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .models import DocumentInfo, Evidence

DEFAULT_MAX_CHARS = 300_000

_PUNCT = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
    }
)


class IngestError(ValueError):
    """The document could not be turned into text."""


@dataclass(frozen=True)
class Page:
    number: int  # 1-based
    start: int  # offset into Document.text
    end: int


def _normalize(text: str) -> tuple[str, list[int]]:
    """Normalize for matching; return (normalized, map from normalized idx -> original idx)."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # drop leading whitespace
    for i, ch in enumerate(text):
        piece = unicodedata.normalize("NFKC", ch).translate(_PUNCT).casefold()
        for c in piece:
            if c.isspace():
                if prev_space:
                    continue
                out.append(" ")
                prev_space = True
            else:
                out.append(c)
                prev_space = False
            idx.append(i)
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


@dataclass
class Document:
    filename: str
    text: str
    pages: list[Page]
    sha256: str
    _norm: str = field(init=False, repr=False)
    _map: list[int] = field(init=False, repr=False)
    _starts: list[int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._norm, self._map = _normalize(self.text)
        self._starts = [p.start for p in self.pages]

    def info(self) -> DocumentInfo:
        return DocumentInfo(
            filename=self.filename,
            sha256=self.sha256,
            pages=len(self.pages),
            chars=len(self.text),
        )

    def page_at(self, offset: int) -> int:
        i = max(bisect_right(self._starts, offset) - 1, 0)
        return self.pages[i].number

    def locate(self, quote: str | None) -> tuple[int, int] | None:
        """Return (start, end) offsets of `quote` in the original text, or None."""
        if not quote:
            return None
        needle, _ = _normalize(quote)
        if not needle:
            return None
        pos = self._norm.find(needle)
        if pos < 0:
            return None
        return self._map[pos], self._map[pos + len(needle) - 1] + 1

    def evidence_for(self, quote: str | None, fact: str | None = None) -> Evidence | None:
        """Build located evidence for a quote, using the *document's* text as the quote."""
        span = self.locate(quote)
        if span is None:
            return None
        start, end = span
        return Evidence(
            quote=self.text[start:end], page=self.page_at(start), start=start, end=end, fact=fact
        )

    def render_for_prompt(self) -> str:
        """Document text with page markers, for the model. Markers are not part of `text`."""
        parts = [f"[page {p.number}]\n{self.text[p.start : p.end]}" for p in self.pages]
        return "\n\n".join(parts)


def _build(filename: str, page_texts: list[str], max_chars: int) -> Document:
    text_parts: list[str] = []
    pages: list[Page] = []
    offset = 0
    for n, page_text in enumerate(page_texts, start=1):
        if n > 1:
            text_parts.append("\n\n")
            offset += 2
        pages.append(Page(number=n, start=offset, end=offset + len(page_text)))
        text_parts.append(page_text)
        offset += len(page_text)
    text = "".join(text_parts)
    if not text.strip():
        raise IngestError(
            "no extractable text found (scanned/image-only documents need OCR, "
            "which is not supported)"
        )
    if len(text) > max_chars:
        raise IngestError(f"document too large: {len(text)} chars (limit {max_chars})")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Document(filename=filename, text=text, pages=pages, sha256=digest)


def ingest_bytes(data: bytes, filename: str, max_chars: int = DEFAULT_MAX_CHARS) -> Document:
    """Ingest raw bytes; PDF is detected by magic bytes, everything else is treated as text."""
    if data[:5] == b"%PDF-":
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise IngestError("encrypted PDFs are not supported")
            page_texts = [(page.extract_text() or "").strip() for page in reader.pages]
        except PyPdfError as exc:
            raise IngestError(f"could not read PDF: {exc}") from exc
        return _build(filename, page_texts, max_chars)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    page_texts = [p.strip() for p in text.split("\f")] if "\f" in text else [text.strip()]
    return _build(filename, page_texts, max_chars)


def ingest_path(path: Path | str, max_chars: int = DEFAULT_MAX_CHARS) -> Document:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise IngestError(f"cannot read {path}: {exc}") from exc
    return ingest_bytes(data, path.name, max_chars)
