"""Getting the contract into the app: PDF/TXT loading and clause splitting.

Two kinds of upload:
- A PDF (or TXT) with a usable English text layer: we keep the text. The
  regex tools can read it, and Claude reads it once in the extraction step.
- A scanned PDF (photos of paper) or an Arabic-only PDF: pypdf finds little
  or no English text. We keep the PDF bytes instead and, in LLM mode, send
  the PDF itself to Claude as a `document` block (Claude reads the pages,
  including Arabic). Demo mode can't read these and says so.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

# Contracts longer than this are almost certainly not a single tenancy
# contract (or the PDF extraction went wrong). We refuse rather than
# silently cutting the text, so the user knows what happened.
MAX_CONTRACT_CHARS = 60_000

# Fewer Latin letters than this in the extracted text means the PDF is
# scanned or (mostly) Arabic: regex can't use it, so Claude reads the PDF.
MIN_LATIN_LETTERS = 300


class ContractLoadError(Exception):
    """Raised when we can't get usable text out of an uploaded file."""


@dataclass(frozen=True)
class LoadedContract:
    """One uploaded contract, as the rest of the app sees it."""

    label: str
    raw_bytes: bytes          # exactly what was uploaded (used for cache keys)
    text: str | None          # usable text, or None for a scanned/Arabic PDF
    pdf_bytes: bytes | None   # kept when Claude has to read the PDF itself
    page_count: int | None = None

    @property
    def needs_document_block(self) -> bool:
        return self.text is None and self.pdf_bytes is not None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw_bytes).hexdigest()

    @classmethod
    def from_text(cls, text: str, label: str) -> "LoadedContract":
        clean = validate_contract_text(text)
        return cls(label=label, raw_bytes=clean.encode("utf-8"), text=clean, pdf_bytes=None)


def has_usable_text(text: str) -> bool:
    return len(re.findall(r"[A-Za-z]", text or "")) >= MIN_LATIN_LETTERS


def load_contract_upload(path: str | Path, max_bytes: int, max_pages: int) -> LoadedContract:
    """Load an upload with the spend guards applied (size and page limits).

    Returns text when the file has a usable English text layer, otherwise
    the PDF bytes so Claude can read the document itself.
    """
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise ContractLoadError(
            f"This file is {size / 1_048_576:.1f} MB; the limit is {max_bytes / 1_048_576:.1f} MB. "
            "Upload just the tenancy contract pages."
        )
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ""}:
        text = validate_contract_text(raw.decode("utf-8", errors="replace"))
        return LoadedContract(label=path.name, raw_bytes=raw, text=text, pdf_bytes=None)
    if suffix != ".pdf":
        raise ContractLoadError(f"Unsupported file type '{suffix}'. Upload a PDF or a .txt file.")

    reader = PdfReader(io.BytesIO(raw))
    pages = len(reader.pages)
    if pages > max_pages:
        raise ContractLoadError(
            f"This PDF has {pages} pages; the limit is {max_pages}. Upload just the tenancy contract pages."
        )
    text = normalize_text("\n\n".join(page.extract_text() or "" for page in reader.pages))
    if has_usable_text(text):
        return LoadedContract(path.name, raw, validate_contract_text(text), None, pages)
    return LoadedContract(label=path.name, raw_bytes=raw, text=None, pdf_bytes=raw, page_count=pages)


@dataclass(frozen=True)
class Clause:
    number: str          # "7", or "-" for text that isn't in a numbered clause
    text: str
    section: str | None  # the heading above it, e.g. "ADDITIONAL TERMS"


def read_pdf_text(path: str | Path) -> str:
    """Extract text from every page of a text-based PDF.

    Scanned PDFs (photos of paper) contain no text layer, so this returns
    an empty string for them; load_contract_upload() then keeps the PDF so
    Claude can read it as a document.
    """
    reader = PdfReader(str(path))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(pages)


def load_contract_file(path: str | Path) -> str:
    """Read a .pdf or .txt contract and return cleaned text."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = read_pdf_text(path)
    elif suffix in {".txt", ".md", ""}:
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        raise ContractLoadError(f"Unsupported file type '{suffix}'. Upload a PDF or a .txt file.")
    return validate_contract_text(text)


def validate_contract_text(text: str | None) -> str:
    """Normalise whitespace and make sure there is something to work with."""
    text = normalize_text(text or "")
    if len(text) < 200:
        raise ContractLoadError(
            "I couldn't find enough text in this contract. If it's a scanned or Arabic-only PDF, "
            "add an Anthropic API key so Claude can read the PDF itself, or paste the text."
        )
    if len(text) > MAX_CONTRACT_CHARS:
        raise ContractLoadError(
            f"This document has {len(text):,} characters, more than the {MAX_CONTRACT_CHARS:,} "
            "this MVP accepts. Upload just the tenancy contract pages."
        )
    return text


def normalize_text(text: str) -> str:
    """Tidy text from PDFs: unify line endings, curly quotes and extra spaces."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# A numbered clause starts a line: "7. The security deposit..." or
# "Clause 7: ..." or "7) ...".
_CLAUSE_START = re.compile(r"^\s*(?:clause\s+)?(\d{1,3})\s*[.):]\s+(.*)$", re.IGNORECASE)
# A heading is a short line in capitals, e.g. "ADDITIONAL TERMS".
_HEADING = re.compile(r"^[A-Z][A-Z &/\-]{3,60}$")


def split_clauses(text: str) -> list[Clause]:
    """Split a contract into numbered clauses (plus the section heading above each).

    Lines that are not part of a numbered clause are ignored here; the term
    extractor reads those "Label: value" lines separately.
    """
    clauses: list[Clause] = []
    current_number: str | None = None
    current_lines: list[str] = []
    current_section: str | None = None
    section_for_current: str | None = None

    def flush() -> None:
        if current_number is not None and current_lines:
            body = " ".join(line.strip() for line in current_lines).strip()
            clauses.append(Clause(number=current_number, text=body, section=section_for_current))

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            # A blank line ends the clause we are collecting.
            flush()
            current_number, current_lines = None, []
            continue
        if _HEADING.match(line):
            flush()
            current_number, current_lines = None, []
            current_section = line
            continue
        match = _CLAUSE_START.match(line)
        if match:
            flush()
            current_number = match.group(1)
            current_lines = [match.group(2)]
            section_for_current = current_section
        elif current_number is not None:
            current_lines.append(line)  # continuation of a wrapped clause
    flush()
    return clauses
