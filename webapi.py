"""The website's server: the hand-built site in web/ plus a small JSON API.

app.py builds the agent (settings, TenantAgent, result cache) and the classic
Gradio UI. This module serves the same runner functions over HTTP for the
site, so both front ends share one backend:

    GET  /                     the website (web/index.html)
    GET  /api/meta             mode, limits, the team, and this tab's session token
    POST /api/contract/sample  load the fictional sample contract (+ its demo form values)
    POST /api/contract         upload a PDF/TXT, or paste the text
    POST /api/notice           read a screenshot of the landlord's notice (Notice Reader)
    POST /api/notice/sample    the same, with the bundled sample screenshot
    POST /api/analyze          the full analysis, streamed as server-sent events
    POST /api/ask              a follow-up question about the same contract, streamed
    POST /api/reset            new conversation, same contract

Sessions: every browser tab gets a random token (from /api/meta) that it
sends back in the X-Haqq-Session header. The token maps to the same Session
object the Gradio UI keeps in gr.State, so the per-session question limit
works the same way. A header and not a cookie, because Hugging Face shows a
Space inside an iframe, where browsers block third-party cookies.

Streaming: run_turn() is a plain generator that blocks on Claude calls. A
worker thread runs it and hands each TraceEvent to the response, which sends
it to the page at once (and a keep-alive comment while Claude is thinking).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from starlette.datastructures import MutableHeaders
from pydantic import BaseModel, Field

from agent.config import Settings
from agent.contract import ContractLoadError, LoadedContract, load_contract_upload
from agent.cost import UsageMeter, format_usd
from agent.inputs import LETTER_LANGUAGES, TenantInputs
from agent.knowledge import DISCLAIMER
from agent.law_search import LAW_PATH
from agent.prompts import ANALYZE_REQUEST
from agent.result_cache import ResultCache
from agent.roster import CRITIC, ROSTER
from agent.runner import Session, read_notice, run_turn
from agent.team.arabic import ARABIC_SOURCES
from agent.team.notice_reader import DELIVERY_LABELS, SAMPLE_NOTICE
from agent.trace import TraceEvent, latest_letter

log = logging.getLogger("haqq.web")

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
SAMPLE_CONTRACT = ROOT / "samples" / "sample_contract.txt"
SESSION_HEADER = "X-Haqq-Session"

# The fictional sample case (the same figures the classic UI fills in).
SAMPLE_FORM = {
    "rera_average_rent": 100_000,   # made-up RERA average for a similar 1BR
    "proposed_new_rent": 93_500,    # the landlord asks for +10%
    "notice_type": "rent_increase",
    "notice_date": "2026-11-01",    # 74 days before expiry: late
    "delivery_method": "email",
}
NOTICE_TYPES = [
    ("none", "No notice yet / not sure"),
    ("rent_increase", "Rent increase"),
    ("change_terms", "Other change to the terms"),
    ("eviction", "Eviction (own use, sale, demolition, renovation)"),
]
DELIVERY_METHODS = [
    ("unknown", "Not sure"),
    ("notary_public", "Notary public"),
    ("registered_mail", "Registered mail"),
    ("email", "Email"),
    ("whatsapp_or_sms", "WhatsApp / SMS"),
    ("hand_delivered", "Handed to me"),
    ("other", "Other"),
]
MAX_QUESTION_CHARS = 2_000
BUSY_TIMEOUT_S = 600          # a run that never finished stops blocking its session after this
KEEPALIVE_S = 10              # comment line while Claude thinks, so proxies keep the stream open

_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def markdown_html(text: str | None) -> str:
    """Markdown -> HTML. Raw HTML in the text is escaped and unsafe link schemes are dropped."""
    return _MD.render(text or "")


# ------------------------------------------------------------------ sessions --

@dataclass
class _Entry:
    session: Session
    last_seen: float
    busy_since: float | None = None


class SessionStore:
    """Token -> Session, in memory. Idle sessions expire; the oldest go first when full."""

    def __init__(self, max_sessions: int = 300, ttl_s: float = 3 * 3600, clock: Callable[[], float] = time.monotonic):
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self.max_sessions = max_sessions
        self.ttl_s = ttl_s
        self.clock = clock

    def get(self, token: str | None) -> tuple[str, _Entry]:
        now = self.clock()
        with self._lock:
            self._expire(now)
            entry = self._entries.get(token or "")
            if entry is None:
                token = secrets.token_urlsafe(18)
                entry = self._entries[token] = _Entry(Session(), now)
            entry.last_seen = now
            return token, entry

    def _expire(self, now: float) -> None:
        for token in [t for t, e in self._entries.items() if now - e.last_seen > self.ttl_s and not e.busy_since]:
            del self._entries[token]
        if len(self._entries) >= self.max_sessions:
            idle = sorted((e.last_seen, t) for t, e in self._entries.items() if not e.busy_since)
            for _, token in idle[: len(self._entries) - self.max_sessions + 1]:
                del self._entries[token]

    def __len__(self) -> int:
        return len(self._entries)


# ------------------------------------------------------------ trace events --

def jsonable(value: Any, max_str: int = 6_000) -> Any:
    """Tool output as plain JSON; very long strings (thinking summaries) are cut."""
    if isinstance(value, str):
        return value if len(value) <= max_str else value[:max_str].rstrip() + " ..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): jsonable(v, max_str) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v, max_str) for v in value]
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json"), max_str)
    return str(value)


def _aed(x: Any) -> str:
    try:
        return f"AED {float(x):,.0f}"
    except (TypeError, ValueError):
        return "AED ?"


def _day(value: Any) -> date | None:
    """'2026-10-16 (Fri 16 Oct 2026)' -> date(2026, 10, 16)."""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    return date(int(match[1]), int(match[2]), int(match[3])) if match else None


def _nice(day: date | None) -> str:
    return f"{day.day} {day.strftime('%b %Y')}" if day else "?"


_CITE = re.compile(r"Dubai (Law|Decree) No\. (\d+) of (\d{4}).*?(?:, Art\. ([\w()]+))?$")


def short_citation(citation: str) -> str:
    """'Dubai Law No. 26 of 2007 (as amended ...), Art. 20' -> 'Law 26/2007, Art. 20'."""
    match = _CITE.match(citation or "")
    if not match:
        return citation
    kind, number, year, article = match.groups()
    return f"{kind} {number}/{year}" + (f", Art. {article}" if article else "")


def days_late(notice: dict[str, Any]) -> int | None:
    """How many days after the last valid date the notice came (None if not late or unknown)."""
    sent, last = _day(notice.get("notice_date")), _day(notice.get("last_valid_notice_date"))
    if notice.get("notice_on_time") is False and sent and last:
        return (sent - last).days
    return None


def _first_sentence(text: str, limit: int = 150) -> str:
    text = " ".join(str(text or "").split())
    cut = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return cut if len(cut) <= limit else cut[: limit - 1].rstrip() + "…"


def _humanize(value: str) -> str:
    return str(value or "").replace("_", " ")


def describe(ev: TraceEvent) -> str:
    """One plain line for the case file: what this step did or found."""
    d = ev.detail if isinstance(ev.detail, dict) else {}
    if ev.kind == "tool_call":
        lines = {
            "read_contract_once": "Reading the whole contract once, into structured JSON",
            "extract_contract_terms": "Reading the key terms: parties, rent, dates, deposit",
            "clause_risk_scan": "Scanning every clause for red flags",
            "critic_review": "Reading draft 1 as the landlord's lawyer, looking for weak points",
            "read_notice_image": "Reading the screenshot with Claude vision",
        }
        if ev.title in lines:
            return lines[ev.title]
        if ev.title == "rera_rent_increase_check":
            asked = f"{_aed(d['proposed_new_rent'])} " if d.get("proposed_new_rent") else "the increase "
            return f"Checking {asked}against the RERA cap"
        if ev.title == "notice_deadline_check":
            return f"Counting the days: {_humanize(d.get('notice_type', 'notice'))} notice before {d.get('contract_end_date', '?')}"
        if ev.title == "search_tenancy_law":
            return f"Searching the official law for “{_humanize(d.get('query', ''))}”"
        if ev.title == "draft_letter":
            return f"Drafting a letter: {_humanize(d.get('letter_type', ''))}"
        if ev.title == "revise_letter":
            parts = []
            if d.get("objections_to_answer"):
                parts.append(f"answering {d['objections_to_answer']} objection(s)")
            if d.get("arabic_version"):
                parts.append("writing the Arabic version")
            return "Revising the letter once" + (": " + " and ".join(parts) if parts else "")
        return f"Calling {ev.title}"
    if ev.kind == "tool_result":
        if ev.is_error or "error" in d:
            return f"Problem: {_first_sentence(d.get('error') or ev.detail)}"
        if ev.title in ("extract_contract_terms", "read_contract_once"):
            t = d.get("terms") or {}
            bits = []
            if t.get("annual_rent_aed"):
                bits.append(f"rent {_aed(t['annual_rent_aed'])} a year")
            if t.get("end_date"):
                bits.append(f"ends {_nice(_day(t['end_date']))}")
            if t.get("security_deposit_aed"):
                bits.append(f"deposit {_aed(t['security_deposit_aed'])}")
            return ("Found " + ", ".join(bits)) if bits else "Read the contract"
        if ev.title == "clause_risk_scan":
            n = d.get("flag_count", 0)
            sev = ", ".join(f"{v} {k}" for k, v in (d.get("by_severity") or {}).items())
            return f"{n} red flag{'s' if n != 1 else ''}" + (f": {sev}" if sev else "")
        if ev.title == "rera_rent_increase_check":
            if d.get("is_within_cap") is False:
                return (f"{d.get('proposed_increase_percent')}% asked, {d.get('max_increase_percent')}% allowed: "
                        f"{_aed(d.get('excess_over_cap'))} over the cap")
            if d.get("is_within_cap") is True:
                return f"Within the {d.get('max_increase_percent')}% cap"
            return f"Cap for this rent: {d.get('max_increase_percent')}% ({_aed(d.get('max_allowed_new_rent'))} at most)"
        if ev.title == "notice_deadline_check":
            late = days_late(d)
            if late is not None:
                return f"Notice late by {late} day{'s' if late != 1 else ''}: the last valid date was {_nice(_day(d.get('last_valid_notice_date')))}"
            if d.get("notice_on_time") is True:
                return f"Notice on time: {d.get('notice_days_before_expiry')} days before expiry"
            return f"Last valid notice date: {_nice(_day(d.get('last_valid_notice_date')))}"
        if ev.title == "search_tenancy_law":
            found = [short_citation(a.get("citation", "")) for a in d.get("articles", [])]
            return ("Found " + "; ".join(found)) if found else "Nothing in the law matched"
        if ev.title == "draft_letter":
            version = str(d.get("version") or "letter ready")
            return "Letter ready: " + version + (" + Arabic" if d.get("body_arabic") else "")
        if ev.title == "critic_review":
            objections = d.get("objections")
            if isinstance(objections, list) and objections:
                return f"{len(objections)} objection{'s' if len(objections) != 1 else ''}, the first: {_humanize(objections[0].get('kind'))}"
            return "No objections: the Critic found the letter solid"
        if ev.title == "revise_letter":
            n = len(d.get("changes_made") or [])
            return f"{n} change{'s' if n != 1 else ''} made"
        if ev.title == "read_notice_image":
            bits = []
            if d.get("proposed_annual_rent_aed"):
                bits.append(f"new rent {_aed(d['proposed_annual_rent_aed'])}")
            if d.get("notice_date"):
                bits.append(f"sent {_nice(_day(d['notice_date']))}")
            if d.get("delivery_method"):
                bits.append(f"by {DELIVERY_LABELS.get(d['delivery_method'], d['delivery_method'])}")
            return ("Read: " + ", ".join(bits)) if bits else "Read the screenshot"
        return f"Result from {ev.title}"
    if ev.kind == "thinking":
        return _first_sentence(ev.detail)
    if ev.kind == "assistant_text":
        return _first_sentence(ev.detail)
    if ev.kind == "error":
        return ev.title
    if ev.title.startswith("Round ") and isinstance(ev.detail, str):
        cost = re.search(r"cost (\$[\d.]+)", ev.detail)
        return ev.title.split(" (")[0] + (f" · {cost[1]}" if cost else "")
    if ev.title == "Cost" and isinstance(ev.detail, str):
        return ev.detail
    return ev.title


def serialize(ev: TraceEvent, t: float | None = None, cost_usd: float | None = None) -> dict[str, Any]:
    return {
        "kind": ev.kind,
        "title": ev.title,
        "agent": ev.agent,
        "line": describe(ev),
        "is_error": ev.is_error,
        "rule_ids": list(ev.rule_ids),
        "duration_ms": ev.duration_ms,
        "detail": jsonable(ev.detail),
        "t": None if t is None else round(t, 2),
        "cost_usd": cost_usd,
    }


# ------------------------------------------------------------------- digest --

def _letter_payload(letter: dict[str, Any] | None, language: str, llm: bool) -> dict[str, Any] | None:
    if not letter:
        return None
    arabic = letter.get("body_arabic")
    reason = None
    if not arabic:
        if not llm:
            reason = "Arabic letters are written by the Drafter (Claude), so they need the AI team, which is off in demo mode."
        elif language == "en":
            reason = "Choose Arabic or Both under “Write my letter in” and run the check again for an Arabic version."
        else:
            reason = "The Drafter could not produce a verified Arabic version this time."
    review = letter.get("review") or {}
    return {
        "subject": letter.get("subject"),
        "letter_type": letter.get("letter_type"),
        "body": letter.get("body"),
        "body_arabic": arabic,
        "arabic_unavailable": reason,
        "version": letter.get("version"),
        "objections": review.get("objections", []),
        "unverified": review.get("unverified_in_final_letter", []),
        "changes_made": letter.get("changes_made", []),
        "revision_rejected_because": letter.get("revision_rejected_because", []),
        "cited_rules": letter.get("cited_rules", []),
        "rule_ids": letter.get("rule_ids", []),
    }


def build_digest(events: list[TraceEvent], final: TraceEvent, *, language: str, llm: bool) -> dict[str, Any]:
    """Everything the results view needs, pulled from one turn's events."""
    results: dict[str, dict[str, Any]] = {}
    articles: dict[str, dict[str, Any]] = {}
    checks: list[dict[str, Any]] = []
    rules: list[str] = []
    for ev in events:
        for r in ev.rule_ids:
            if r not in rules:
                rules.append(r)
        if ev.kind == "tool_result" and not ev.is_error and isinstance(ev.detail, dict) and "error" not in ev.detail:
            results[ev.title] = ev.detail
            if ev.title == "search_tenancy_law":
                for a in ev.detail.get("articles", []):
                    if a.get("article_id") and a["article_id"] not in articles:
                        text = " ".join(str(a.get("text", "")).split())
                        articles[a["article_id"]] = {
                            "article_id": a["article_id"],
                            "citation": a.get("citation"),
                            "short": short_citation(a.get("citation", "")),
                            "title": a.get("title"),
                            "text": text,
                            "source_url": a.get("source_url"),
                            "rule_ids": a.get("related_rule_ids", []),
                        }
        if ev.kind == "info" and ev.agent == CRITIC and ev.title.startswith("Automatic check"):
            checks.append({"title": ev.title, "detail": ev.detail})

    terms_out = results.get("extract_contract_terms") or results.get("read_contract_once") or {}
    notice = results.get("notice_deadline_check")
    detail = final.detail if isinstance(final.detail, dict) else {}
    cost = detail.get("cost") or {}
    critic = results.get("critic_review") or {}
    return {
        "answer_html": markdown_html(detail.get("text")),
        "answer_text": detail.get("text", ""),
        "cached": bool(detail.get("cached")),
        "cost": {
            "usd": cost.get("cost_usd", 0.0) if llm else 0.0,
            "summary": detail.get("cost_summary") or ("Demo mode: $0.00 (no API calls)." if not llm else ""),
            "tokens": sum(cost.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens")),
            "requests": cost.get("requests", 0),
            "cache_share": cost.get("cache_share"),
            "by_agent": cost.get("by_agent", {}),
        },
        "terms": terms_out.get("terms"),
        "derived": terms_out.get("derived"),
        "rent": results.get("rera_rent_increase_check"),
        "notice": {**notice, "days_late": days_late(notice)} if notice else None,
        "flags": (results.get("clause_risk_scan") or {}).get("flags", []),
        "law": list(articles.values()),
        "rules": rules,
        "letter": _letter_payload(latest_letter(events), language, llm),
        "critic": {"objections": critic.get("objections") if isinstance(critic.get("objections"), list) else [],
                   "ran": "critic_review" in results},
        "checks": checks,
        "steps": sum(1 for e in events if e.kind == "tool_call"),
        "agents": list(dict.fromkeys(e.agent for e in events)),
    }


# --------------------------------------------------------------------- app --

class FormIn(BaseModel):
    rera_average_rent: float | None = None
    proposed_new_rent: float | None = None
    notice_type: str | None = None
    notice_date: str | None = None
    delivery_method: str | None = None
    letter_language: str | None = "en"

    def inputs(self) -> TenantInputs:
        return TenantInputs.from_form(self.rera_average_rent, self.proposed_new_rent, self.notice_date,
                                      self.notice_type, self.delivery_method, self.letter_language)


class AskIn(FormIn):
    message: str = Field(default="")


class _UploadGuard:
    """Plain ASGI middleware (it never buffers a stream): refuses an oversized upload before its
    body is read (the loaders check the exact size again) and adds two safe headers."""

    UPLOADS = ("/api/contract", "/api/notice")

    def __init__(self, app: Any, max_body: int, limit_mb: float) -> None:
        self.app, self.max_body, self.limit_mb = app, max_body, limit_mb

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "POST" and scope["path"] in self.UPLOADS:
            length = dict(scope["headers"]).get(b"content-length", b"")
            if length.isdigit() and int(length) > self.max_body:
                response = JSONResponse({"error": f"That file is too big: the limit is {self.limit_mb:.0f} MB."},
                                        status_code=413)
                await response(scope, receive, send)
                return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode("utf-8")


def _contract_info(contract: LoadedContract) -> dict[str, Any]:
    if contract.needs_document_block:
        status = (f"{contract.page_count} page(s), scanned or Arabic: Claude will read the PDF itself, once.")
    else:
        status = f"{len(contract.text or ''):,} characters of text."
    return {
        "label": contract.label,
        "chars": len(contract.text or ""),
        "pages": contract.page_count,
        "scanned": contract.needs_document_block,
        "status": status,
        "preview": (contract.text or "")[:2_400],
    }


def _safe_name(filename: str | None, default: str) -> str:
    name = Path(filename or "").name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")[:80]
    return name or default


def _web_version(web_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(web_dir.rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def create_web_app(
    *,
    settings: Settings,
    agent: Any = None,
    cache: ResultCache | None = None,
    web_dir: Path = WEB_DIR,
    today: Callable[[], date] = date.today,
    store: SessionStore | None = None,
) -> FastAPI:
    """The FastAPI app: the website, its API, and nothing else (app.py mounts the classic UI)."""
    app = FastAPI(title="Haqq", docs_url=None, redoc_url=None, openapi_url=None)
    store = store or SessionStore()
    app.state.sessions = store
    llm = agent is not None
    max_upload = int(settings.max_upload_mb * 1_048_576)

    page_cache: dict[str, Any] = {}

    def index_html() -> str:
        """index.html with ?v=<hash of web/> on every asset URL, rebuilt only when a file changes."""
        stamp = tuple((str(p), p.stat().st_mtime_ns) for p in sorted(web_dir.rglob("*")) if p.is_file())
        if page_cache.get("stamp") != stamp:
            page_cache["stamp"] = stamp
            page_cache["html"] = ((web_dir / "index.html").read_text(encoding="utf-8")
                                  .replace("__V__", _web_version(web_dir)).replace("__MODE__", "llm" if llm else "demo"))
        return page_cache["html"]

    law = json.loads(LAW_PATH.read_text(encoding="utf-8"))["articles"]
    law_stats = {"total": len(law), "in_force": sum(1 for a in law if a.get("in_force"))}

    def session_for(request: Request) -> tuple[str, _Entry]:
        return store.get(request.headers.get(SESSION_HEADER))

    def reply(token: str, body: dict[str, Any], status: int = 200) -> JSONResponse:
        return JSONResponse({**body, "session": token}, status_code=status, headers={SESSION_HEADER: token})

    def error(token: str, message: str, status: int = 400) -> JSONResponse:
        return reply(token, {"error": message}, status)

    def busy(entry: _Entry) -> bool:
        return entry.busy_since is not None and time.monotonic() - entry.busy_since < BUSY_TIMEOUT_S

    app.add_middleware(_UploadGuard, max_body=max_upload + 65_536, limit_mb=settings.max_upload_mb)

    # ---- the site ----

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(index_html(), headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
                "img-src 'self' data: blob:; connect-src 'self'; base-uri 'self'; form-action 'self'"
            ),
        })

    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        return {"ok": True, "mode": "llm" if llm else "demo", "sessions": len(store)}

    # ---- meta ----

    @app.get("/api/meta")
    def meta(request: Request) -> JSONResponse:
        token, entry = session_for(request)
        contract = entry.session.contract
        return reply(token, {
            "mode": "llm" if llm else "demo",
            "model": settings.model if llm else None,
            "effort": settings.effort if llm else None,
            "limits": {
                "max_upload_mb": settings.max_upload_mb,
                "max_pdf_pages": settings.max_pdf_pages,
                "max_requests_per_session": settings.max_requests_per_session,
                "max_cost_per_run_usd": settings.max_cost_per_run_usd,
                "max_cost_per_run": format_usd(settings.max_cost_per_run_usd),
                "max_question_chars": MAX_QUESTION_CHARS,
            },
            "requests_used": entry.session.llm_requests,
            "roster": [{"name": name, "how": how, "job": job} for name, (how, job) in ROSTER.items()],
            "disclaimer": DISCLAIMER,
            "arabic_sources": [{"name": name, "url": url} for name, url in ARABIC_SOURCES.items()],
            "law": law_stats,
            "notice_types": [{"value": v, "label": label} for v, label in NOTICE_TYPES],
            "delivery_methods": [{"value": v, "label": label} for v, label in DELIVERY_METHODS],
            "letter_languages": [{"value": v, "label": label} for v, label in LETTER_LANGUAGES.items()],
            "contract": _contract_info(contract) if contract else None,
        })

    # ---- step 1: the contract ----

    def _set_contract(entry: _Entry, contract: LoadedContract) -> None:
        # A new contract starts a fresh conversation (the request count carries over).
        entry.session = Session(contract=contract, llm_requests=entry.session.llm_requests)

    @app.post("/api/contract/sample")
    def contract_sample(request: Request) -> JSONResponse:
        token, entry = session_for(request)
        if busy(entry):
            return error(token, "Haqq is still working on your last request.", 409)
        contract = LoadedContract.from_text(SAMPLE_CONTRACT.read_text(encoding="utf-8"), "sample_contract.txt (fictional)")
        _set_contract(entry, contract)
        return reply(token, {"contract": _contract_info(contract), "form": SAMPLE_FORM})

    @app.post("/api/contract")
    async def contract_upload(request: Request, file: UploadFile | None = File(default=None),
                              text: str | None = Form(default=None)) -> JSONResponse:
        token, entry = session_for(request)
        if busy(entry):
            return error(token, "Haqq is still working on your last request.", 409)
        try:
            if file is not None and file.filename:
                data = await file.read(max_upload + 1)
                if len(data) > max_upload:
                    raise ContractLoadError(f"This file is larger than the {settings.max_upload_mb:.0f} MB limit. "
                                            "Upload just the tenancy contract pages.")
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / _safe_name(file.filename, "contract.pdf")
                    path.write_bytes(data)
                    contract = load_contract_upload(path, max_bytes=max_upload, max_pages=settings.max_pdf_pages)
                if contract.needs_document_block and not llm:
                    raise ContractLoadError(
                        "This PDF has no usable English text (it looks scanned or Arabic-only). Demo mode can only "
                        "read text: paste the text instead.")
            elif text and text.strip():
                contract = LoadedContract.from_text(text, "pasted text")
            else:
                return error(token, "Upload a PDF or TXT file, or paste the contract text first.")
        except ContractLoadError as exc:
            return error(token, str(exc))
        except Exception as exc:  # e.g. a corrupt PDF
            log.warning("contract upload failed: %s", exc)
            return error(token, f"Couldn't read that file ({type(exc).__name__}). Try another file or paste the text.")
        _set_contract(entry, contract)
        return reply(token, {"contract": _contract_info(contract)})

    # ---- step 2: the landlord's notice ----

    def _notice_reply(token: str, events: list[TraceEvent], image_url: str | None = None) -> JSONResponse:
        final = events[-1].detail
        reading = final.get("reading")
        ok = reading is not None and reading.is_landlord_notice
        return reply(token, {
            "ok": ok,
            "html": markdown_html(final.get("text")),
            "reading": jsonable(reading) if reading is not None else None,
            "form": reading.form_values() if ok else None,
            "warnings": list(reading.embedded_instructions) if reading is not None else [],
            "events": [serialize(e) for e in events[:-1]],
            "cost": next((e.detail for e in reversed(events) if e.title == "Cost"), None),
            "image_url": image_url,
        })

    def _read(entry: _Entry, path: str) -> list[TraceEvent]:
        try:
            return list(read_notice(entry.session, path, settings=settings, agent=agent, cache=cache))
        finally:
            entry.busy_since = None

    @app.post("/api/notice/sample")
    async def notice_sample(request: Request) -> JSONResponse:
        token, entry = session_for(request)
        if busy(entry):
            return error(token, "Haqq is still working on your last request.", 409)
        entry.busy_since = time.monotonic()
        events = await asyncio.to_thread(_read, entry, str(SAMPLE_NOTICE))
        return _notice_reply(token, events, image_url="static-sample/notice.png")

    @app.get("/static-sample/notice.png", include_in_schema=False)
    def notice_sample_image() -> FileResponse:
        return FileResponse(SAMPLE_NOTICE, media_type="image/png")

    @app.post("/api/notice")
    async def notice_upload(request: Request, file: UploadFile = File(...)) -> JSONResponse:
        token, entry = session_for(request)
        if busy(entry):
            return error(token, "Haqq is still working on your last request.", 409)
        data = await file.read(max_upload + 1)
        if len(data) > max_upload:
            return error(token, f"This image is larger than the {settings.max_upload_mb:.0f} MB limit.")
        entry.busy_since = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / _safe_name(file.filename, "notice.png")
            path.write_bytes(data)
            events = await asyncio.to_thread(_read, entry, str(path))
        return _notice_reply(token, events)

    # ---- step 3: the analysis and follow-up questions (streamed) ----

    def _turn(entry: _Entry, user_text: str, analyze: bool, inputs: TenantInputs) -> Iterator[bytes]:
        meter = UsageMeter(settings.model) if llm else None
        started = time.perf_counter()
        events: list[TraceEvent] = []
        yield _sse("start", {"mode": "llm" if llm else "demo", "analyze": analyze})
        stream = run_turn(entry.session, user_text, analyze=analyze, inputs=inputs, today=today(),
                          settings=settings, agent=agent, cache=cache, meter=meter)
        for ev in stream:
            elapsed = time.perf_counter() - started
            if ev.kind == "final":
                digest = build_digest(events, ev, language=inputs.letter_language, llm=llm)
                digest["elapsed_s"] = round(elapsed, 1)
                digest["requests_used"] = entry.session.llm_requests
                yield _sse("final", digest)
                return
            events.append(ev)
            yield _sse("step", serialize(ev, elapsed, meter.cost_usd if meter else 0.0))
            if (letter := latest_letter([ev])) is not None:
                yield _sse("letter", _letter_payload(letter, inputs.letter_language, llm))

    async def _stream(entry: _Entry, user_text: str, analyze: bool, inputs: TenantInputs):
        """Run the turn in a worker thread; send each event as soon as it exists."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def put(item: bytes | None) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:   # the server is shutting down
                pass

        def worker() -> None:
            try:
                for chunk in _turn(entry, user_text, analyze, inputs):
                    put(chunk)
            except Exception as exc:  # never leave the page waiting
                log.exception("turn failed")
                put(_sse("error", {"message": f"Something went wrong ({type(exc).__name__}). Please try again."}))
            finally:
                entry.busy_since = None
                put(None)

        threading.Thread(target=worker, name="haqq-turn", daemon=True).start()
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                yield b": still working\n\n"
                continue
            if chunk is None:
                return
            yield chunk

    def _start(token: str, entry: _Entry, user_text: str, analyze: bool, inputs: TenantInputs):
        if busy(entry):
            return error(token, "Haqq is still working on your last request.", 409)
        entry.busy_since = time.monotonic()
        return StreamingResponse(_stream(entry, user_text, analyze, inputs), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", SESSION_HEADER: token,
        })

    @app.post("/api/analyze")
    def analyze(request: Request, form: FormIn):
        token, entry = session_for(request)
        if entry.session.contract is None:
            return error(token, "Load a contract first: upload a PDF or TXT, paste the text, or use the sample.")
        return _start(token, entry, ANALYZE_REQUEST, True, form.inputs())

    @app.post("/api/ask")
    def ask(request: Request, body: AskIn):
        token, entry = session_for(request)
        message = (body.message or "").strip()
        if not message:
            return error(token, "Type a question first.")
        if len(message) > MAX_QUESTION_CHARS:
            return error(token, f"Please keep questions under {MAX_QUESTION_CHARS:,} characters.")
        return _start(token, entry, message, False, body.inputs())

    @app.post("/api/reset")
    def reset(request: Request) -> JSONResponse:
        token, entry = session_for(request)
        if busy(entry):
            return error(token, "Haqq is still working on your last request.", 409)
        entry.session = entry.session.fresh_conversation()
        return reply(token, {"ok": True})

    return app
