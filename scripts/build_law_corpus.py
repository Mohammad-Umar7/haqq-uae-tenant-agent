"""Build agent/knowledge_base/law_articles.json from the official law PDFs.

The PDFs are the official English translations published on the Dubai
Legislation Portal (dlp.dubai.gov.ae, run by the Supreme Legislation
Committee). This script:

1. reads each PDF with pypdf (no OCR, no LLM),
2. removes the running page header and the copyright footnote,
3. splits the text at every "Article (N)" line,
4. writes one JSON record per article with its citation and source URL.

The article text is copied as printed. The only edits are repairs of
PDF-extraction artifacts (a space inside a word such as "specifi c", or
"first -degree"), listed in TEXT_FIXES and recorded in the JSON file. Never
add words or paraphrase here: the agent quotes this text as the law.

Usage (from the project folder):
    python scripts/build_law_corpus.py --download downloads/law_pdfs
    python scripts/build_law_corpus.py --pdf-dir downloads/law_pdfs

The first form downloads the three PDFs (about 1 MB) into that folder first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pypdf import PdfReader

PROJECT = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT / "agent" / "knowledge_base" / "law_articles.json"

LAW26 = "Law No. (26) of 2007 Regulating the Relationship between Landlords and Tenants in the Emirate of Dubai"
LAW33 = "Law No. (33) of 2008 Amending Law No. (26) of 2007 Regulating the Relationship between Landlords and Tenants in the Emirate of Dubai"
DECREE43 = "Decree No. (43) of 2013 Determining Rent Increase for Real Property in the Emirate of Dubai"

DLP = "https://dlp.dubai.gov.ae/Legislation%20Reference"
SOURCES = {
    "law26_2007": {
        "file": "law26_2007.pdf",
        "title": LAW26,
        "url": f"{DLP}/2007/Law%20No.%20(26)%20of%202007.pdf",
    },
    "law33_2008": {
        "file": "law33_2008.pdf",
        "title": LAW33,
        "url": f"{DLP}/2009/Law%20No.%20(33)%20of%202008%20Amending%20Law%20No.%20(26)%20of%202007.pdf",
    },
    "decree43_2013": {
        "file": "decree43_2013.pdf",
        "title": DECREE43,
        "url": f"{DLP}/2013/Decree%20No.%20(43)%20of%202013%20Determining%20Rent%20Increase%20for%20Real%20Property.pdf",
    },
}

# Law No. 33 of 2008, Art. 1: "Articles (2), (3), (4), (9), (13), (14), (15),
# (25), (26), (29), and (36) of the Original Law are hereby superseded".
SUPERSEDED_BY_2008 = {"2", "3", "4", "9", "13", "14", "15", "25", "26", "29", "36"}

# Repairs of PDF-extraction artifacts only (pattern, replacement).
TEXT_FIXES: list[tuple[str, str]] = [
    (r"\bspecifi c\b", "specific"),
    (r"\binclude s\b", "includes"),
    (r"\brenovat ed\b", "renovated"),
    (r"\brefurbish ed\b", "refurbished"),
    (r"\bdamage s\b", "damages"),
    (r"\bDispute s\b", "Disputes"),
    (r"\bh is\b", "his"),
    (r"(\w) -(\w)", r"\1-\2"),          # "first -degree" -> "first-degree"
    (r" +([,.;:)])", r"\1"),             # "Contract ," -> "Contract,"
    (r"[‘’]", "'"),            # curly quotes -> straight
    (r"[“”]", '"'),
    (r"[ \t]{2,}", " "),
]

# Plain-English search hints for the BM25 index. These are NOT law text and
# are never shown as such; they only help a tenant's wording ("sell the
# flat", "cut off my AC") find the right article.
SEARCH_KEYWORDS: dict[str, list[str]] = {
    "law26_art2": ["definitions", "what counts as a notice", "notice by email or WhatsApp"],
    "law26_art4": ["Ejari", "registration", "register the contract"],
    "law26_art6": ["automatic renewal", "auto renew", "stay after expiry"],
    "law26_art7": ["early termination", "break the lease", "end the contract early"],
    "law26_art12": ["cheques", "instalments", "payment dates"],
    "law26_art13": ["rent at renewal", "negotiate the rent", "fair rent"],
    "law26_art14": ["rent increase notice", "change the terms at renewal", "not renewing"],
    "law26_art16": ["maintenance", "repairs", "who pays for repairs"],
    "law26_art17": ["wear and tear", "defects"],
    "law26_art20": ["security deposit", "deposit refund", "non-refundable deposit"],
    "law26_art24": ["sublet", "subletting"],
    "law26_art25": ["eviction", "evict", "vacate", "move out", "sale", "own use",
                    "12 months notice", "notary public", "registered mail"],
    "law26_art26": ["re-let after eviction for own use", "compensation"],
    "law26_art28": ["new owner", "ownership transfer"],
    "law26_art34": ["cut off electricity", "disconnect water", "air conditioning", "harassment"],
    "decree43_art1": ["rent increase", "maximum increase", "cap", "percentage", "renewal"],
    "decree43_art3": ["RERA rental index", "average rent", "calculator", "market rent"],
}

_PAGE_LINE = re.compile(r"^Page \d+ of \d+$")
_ARTICLE_LINE = re.compile(r"^Article \((\d+)\)$")
_LIST_START = re.compile(r"^(?:\d{1,2}\.|[a-i]\.)\s")
_SIGNATURE = "Mohammed bin Rashid Al Maktoum"
_ISSUED = re.compile(r"^Issued in Dubai on (.+?)\s*$")


@dataclass
class RawArticle:
    number: str
    title: str | None
    chapter: str | None
    lines: list[str]


def _body_lines(page_text: str) -> list[str]:
    """Drop the running header (up to 'Page X of Y') and the copyright footnote."""
    lines = page_text.splitlines()
    for i, line in enumerate(lines):
        if _PAGE_LINE.match(line.strip()):
            lines = lines[i + 1:]
            break
    body: list[str] = []
    for line in lines:
        if line.strip().startswith("©"):
            break
        if line.strip():
            body.append(line.strip())
    return body


def _is_heading(line: str) -> bool:
    return (
        len(line) <= 60
        and line[0].isupper()
        and line[-1] not in ".,;:"
        and not _LIST_START.match(line)
    )


def _split_articles(lines: list[str]) -> tuple[list[RawArticle], str | None]:
    articles: list[RawArticle] = []
    chapter: str | None = None
    issued: str | None = None
    current: RawArticle | None = None
    for i, line in enumerate(lines):
        if m := _ISSUED.match(line):
            issued = m.group(1)
        if line == _SIGNATURE:
            current = None  # end of the operative text
            continue
        if m := _ARTICLE_LINE.match(line):
            title = None
            # A heading printed directly above the article belongs to it.
            if current is not None and current.lines and _is_heading(current.lines[-1]):
                title = current.lines.pop()
            elif articles == [] and i > 0 and _is_heading(lines[i - 1]):
                title = lines[i - 1]
            if title:
                chapter = title
            current = RawArticle(number=m.group(1), title=title, chapter=chapter, lines=[])
            articles.append(current)
            continue
        if current is not None:
            current.lines.append(line)
    return articles, issued


def _join(lines: list[str]) -> str:
    """Re-flow wrapped lines; keep list items and sentences that start a line."""
    paragraphs: list[str] = []
    for line in lines:
        if not paragraphs:
            paragraphs.append(line)
            continue
        prev = paragraphs[-1]
        if prev.endswith("-") and not prev.endswith(" -"):
            paragraphs[-1] = prev + line          # "sub-" + "lease" across a line break
        elif _LIST_START.match(line) or (prev[-1] in ".;:" and line[0].isupper()):
            paragraphs.append(line)
        else:
            paragraphs[-1] = prev + " " + line
    text = "\n".join(paragraphs)
    for pattern, replacement in TEXT_FIXES:
        text = re.sub(pattern, replacement, text)
    return text.strip()


def _read(pdf: Path) -> tuple[list[RawArticle], str | None, int]:
    reader = PdfReader(str(pdf))
    lines: list[str] = []
    for page in reader.pages:
        lines.extend(_body_lines(page.extract_text() or ""))
    articles, issued = _split_articles(lines)
    return articles, issued, len(reader.pages)


def _record(**fields) -> dict:
    record = {
        "id": fields["id"],
        "law": fields["law"],
        "law_short": fields["law_short"],
        "article": fields["article"],
        "title": fields.get("title"),
        "chapter": fields.get("chapter"),
        "version": fields["version"],
        "in_force": fields["in_force"],
        "superseded_by": fields.get("superseded_by"),
        "citation": fields["citation"],
        "source_url": fields["source_url"],
        "text": fields["text"],
        "search_keywords": SEARCH_KEYWORDS.get(fields["id"], []),
    }
    return record


def build(pdf_dir: Path) -> dict:
    meta, parsed = {}, {}
    for law_id, src in SOURCES.items():
        pdf = pdf_dir / src["file"]
        articles, issued, pages = _read(pdf)
        parsed[law_id] = articles
        meta[law_id] = {
            "law_id": law_id,
            "title": src["title"],
            "url": src["url"],
            "issued": issued,
            "pages": pages,
            "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        }

    records: list[dict] = []
    chapters_2007: dict[str, str | None] = {}

    # Law No. 26 of 2007: original text. Articles replaced in 2008 are kept for
    # transparency but marked in_force = False (search never returns them).
    for a in parsed["law26_2007"]:
        chapters_2007[a.number] = a.chapter
        superseded = a.number in SUPERSEDED_BY_2008
        records.append(_record(
            id=f"law26_art{a.number}" + ("_2007" if superseded else ""),
            law=LAW26,
            law_short="Dubai Law No. 26 of 2007",
            article=a.number,
            title=a.title,
            chapter=a.chapter,
            version="original 2007 text (superseded by Law No. 33 of 2008)" if superseded else "2007 text, not amended by Law No. 33 of 2008",
            in_force=not superseded,
            superseded_by="Law No. (33) of 2008, Art. 1" if superseded else None,
            citation=(f"Dubai Law No. 26 of 2007, Art. {a.number} (original 2007 text, superseded by Law No. 33 of 2008)"
                      if superseded else f"Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. {a.number}"),
            source_url=SOURCES["law26_2007"]["url"],
            text=_join(a.lines),
        ))

    # Law No. 33 of 2008: its own Art. 1 (the amending clause) and last
    # article (commencement); everything in between is replacement text for
    # articles of Law No. 26 of 2007.
    l33 = parsed["law33_2008"]
    own_first, replacements, own_last = l33[0], l33[1:-1], l33[-1]
    assert {a.number for a in replacements} == SUPERSEDED_BY_2008, "unexpected Law 33 structure"
    for own in (own_first, own_last):
        records.append(_record(
            id=f"law33_art{own.number}",
            law=LAW33,
            law_short="Dubai Law No. 33 of 2008",
            article=own.number,
            version="2008 amending law",
            in_force=True,
            citation=f"Dubai Law No. 33 of 2008, Art. {own.number}",
            source_url=SOURCES["law33_2008"]["url"],
            text=_join(own.lines),
        ))
    for a in replacements:
        records.append(_record(
            id=f"law26_art{a.number}",
            law=LAW26,
            law_short="Dubai Law No. 26 of 2007",
            article=a.number,
            title=None,
            chapter=chapters_2007.get(a.number),
            version="as replaced by Law No. (33) of 2008",
            in_force=True,
            citation=f"Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. {a.number}",
            source_url=SOURCES["law33_2008"]["url"],
            text=_join(a.lines),
        ))

    for a in parsed["decree43_2013"]:
        records.append(_record(
            id=f"decree43_art{a.number}",
            law=DECREE43,
            law_short="Dubai Decree No. 43 of 2013",
            article=a.number,
            title=a.title,
            chapter=a.chapter,
            version="2013 text",
            in_force=True,
            citation=f"Dubai Decree No. 43 of 2013, Art. {a.number}",
            source_url=SOURCES["decree43_2013"]["url"],
            text=_join(a.lines),
        ))

    order = {"law26": 0, "law33": 1, "decree43": 2}
    records.sort(key=lambda r: (order[r["id"].split("_")[0]], int(r["article"]), r["in_force"]))
    ids = [r["id"] for r in records]
    assert len(ids) == len(set(ids)), "duplicate article ids"

    return {
        "_about": (
            "Article-level chunks of Dubai's tenancy legislation, copied from the official English translations "
            "on the Dubai Legislation Portal (Supreme Legislation Committee). 'text' is the official wording; "
            "the only edits are the PDF-extraction repairs listed in text_cleanup. The English text is a "
            "translation: for interpretation the original Arabic text prevails. 'search_keywords' are curated "
            "search hints, not law. Articles of Law No. 26 of 2007 replaced by Law No. 33 of 2008 are kept with "
            "in_force=false and are never returned by search. Rebuild with scripts/build_law_corpus.py."
        ),
        "built_on": date.today().isoformat(),
        "sources": list(meta.values()),
        "text_cleanup": [f"{p} -> {r}" for p, r in TEXT_FIXES],
        "articles": records,
    }


def download(pdf_dir: Path) -> None:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for src in SOURCES.values():
        target = pdf_dir / src["file"]
        print(f"Downloading {src['url']}")
        with urllib.request.urlopen(src["url"], timeout=60) as response:  # noqa: S310 - fixed official URLs
            target.write_bytes(response.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf-dir", type=Path, help="folder that already holds the three PDFs")
    group.add_argument("--download", type=Path, help="download the PDFs into this folder first")
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args()

    pdf_dir = args.download or args.pdf_dir
    if args.download:
        download(pdf_dir)
    corpus = build(pdf_dir)
    args.out.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    in_force = sum(1 for a in corpus["articles"] if a["in_force"])
    print(f"Wrote {len(corpus['articles'])} articles ({in_force} in force) to {args.out}")


if __name__ == "__main__":
    main()
