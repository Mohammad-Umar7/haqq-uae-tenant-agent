"""Arabic letters: citation forms written by code, and text normalisation.

The Drafter (Claude) translates the letter, but it does not translate the
citations. Code turns each English citation into its Arabic form here, and
the Drafter is told to copy those forms exactly. A legal citation is the one
place where a creative translation would be a bug.

The forms name each law by its number and year, as the Dubai Legislation
Portal titles them ("قانون رقم (26) لسنة 2007"), so they can't misquote a
long title. The official text of Dubai's laws is the Arabic one: the English
text in law_articles.json is an official translation, and where they
differ, the Arabic prevails. The UI says so next to the Arabic letter and
links the Arabic originals below.
"""

from __future__ import annotations

import re

# The Arabic originals on the Dubai Legislation Portal (checked 8 November 2026).
ARABIC_SOURCES = {
    "قانون رقم (26) لسنة 2007": "https://dlp.dubai.gov.ae/Legislation%20Ar%20Reference/2007/"
                                "%D9%82%D8%A7%D9%86%D9%88%D9%86%20%D8%B1%D9%82%D9%85%20(26)%20%D9%84%D8%B3%D9%86%D8%A9%202007.html",
    "قانون رقم (33) لسنة 2008": "https://dlp.dubai.gov.ae/Legislation%20Ar%20Reference/2009/"
                                "%D9%82%D8%A7%D9%86%D9%88%D9%86%20%D8%B1%D9%82%D9%85%20(33)%20%D9%84%D8%B3%D9%86%D8%A9%202008.html",
}

# Phrases in our curated citations that have no article number.
_DESCRIPTORS = {
    "rent increase caps": "نسب الزيادة في بدل الإيجار",
    "measured against the RERA Rental Index": "وفق مؤشر الإيجارات المعتمد من مؤسسة التنظيم العقاري",
}
_FIXED = {
    "Dubai Rental Disputes Settlement Centre (RDSC)": "مركز فض المنازعات الإيجارية في دبي",
    "Ejari registration (RERA / Dubai Land Department)":
        "التسجيل في نظام إيجاري (مؤسسة التنظيم العقاري / دائرة الأراضي والأملاك في دبي)",
}
_KIND = {"Law": "القانون", "Decree": "المرسوم"}

_CITATION = re.compile(
    r"^Dubai (?P<kind>Law|Decree) No\. (?P<no>\d+) of (?P<year>\d{4})"
    r"(?: \(as amended by Law No\. (?P<amend_no>\d+) of (?P<amend_year>\d{4})\))?"
    r"(?:, Art\. (?P<art>\d+)(?:\((?P<clause>\d+)\))?| \((?P<desc>[^)]+)\))?$"
)


def arabic_citation(english: str) -> str | None:
    """'Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 25(2)' ->
    'القانون رقم (26) لسنة 2007 (المعدَّل بالقانون رقم (33) لسنة 2008)، المادة (25)، البند (2)'.
    Returns None for a citation it doesn't know (the Drafter then may not use it in Arabic)."""
    english = " ".join(english.split())
    if english in _FIXED:
        return _FIXED[english]
    m = _CITATION.match(english)
    if not m:
        return None
    text = f"{_KIND[m['kind']]} رقم ({m['no']}) لسنة {m['year']}"
    if m["amend_no"]:
        text += f" (المعدَّل بالقانون رقم ({m['amend_no']}) لسنة {m['amend_year']})"
    if m["art"]:
        text += f"، المادة ({m['art']})"
        if m["clause"]:
            text += f"، البند ({m['clause']})"
    elif m["desc"]:
        if m["desc"] not in _DESCRIPTORS:
            return None
        text += f" ({_DESCRIPTORS[m['desc']]})"
    return text


def citation_pairs(citations: list[str]) -> list[tuple[str, str]]:
    """(English, Arabic) for every citation that has an Arabic form."""
    return [(c, ar) for c in citations if (ar := arabic_citation(c))]


# ---- normalisation for the automatic check ---------------------------------------

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٬٫٪", "01234567890123456789,.%")


def normalize(text: str) -> str:
    """Arabic-Indic digits and separators -> ASCII, so one set of patterns reads both languages."""
    return text.translate(_DIGITS)


# Common English words that should never survive a translation into Arabic.
# (Found in a real run: "...استئجار العقار، but أعتقد...".)
_ENGLISH_LEFTOVERS = re.compile(
    r"(?<![A-Za-z])(but|and|the|of|to|for|with|is|are|that|this|not|however|please|dear|regards)(?![A-Za-z])",
    re.IGNORECASE,
)


def untranslated_words(arabic_letter: str) -> list[str]:
    """English words left inside an Arabic letter: a slip any reader would notice."""
    return sorted({m.group(1).lower() for m in _ENGLISH_LEFTOVERS.finditer(arabic_letter)})


ARABIC_MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "إبريل": 4, "مايو": 5, "يونيو": 6,
    "يوليو": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12,
}
