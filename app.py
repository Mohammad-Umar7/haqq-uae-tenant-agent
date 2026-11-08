"""Haqq - Dubai Tenant Rights Agent (Gradio UI).

Run locally:   python app.py   then open http://127.0.0.1:7860
Hugging Face Spaces runs this same file.

This file is only UI wiring. The agent lives in agent/loop.py, the tools in
agent/tools/, and the no-API-key fallback in agent/demo.py.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import gradio as gr

from agent.config import load_settings
from agent.contract import ContractLoadError, LoadedContract, load_contract_upload
from agent.cost import format_usd
from agent.inputs import TenantInputs
from agent.knowledge import DISCLAIMER
from agent.loop import TenantAgent
from agent.prompts import ANALYZE_REQUEST
from agent.result_cache import ResultCache
from agent.roster import ROSTER
from agent.runner import Session, read_notice, run_turn
from agent.team.arabic import ARABIC_SOURCES
from agent.team.notice_reader import SAMPLE_NOTICE
from agent.trace import TraceEvent, latest_letter, render_trace_markdown

SAMPLE_PATH = Path(__file__).parent / "samples" / "sample_contract.txt"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
settings = load_settings()
agent = TenantAgent(settings) if settings.llm_enabled else None
cache = ResultCache(settings.cache_dir, enabled=settings.cache_enabled) if settings.llm_enabled else None


# ---------------------------------------------------------------- contract --

def _loaded_status(contract: LoadedContract) -> str:
    if contract.needs_document_block:
        return (f"**Contract loaded:** {contract.label} ({contract.page_count} page(s), scanned or Arabic: "
                "Claude will read the PDF itself, once). Click **Analyze my contract**.")
    return f"**Contract loaded:** {contract.label} ({len(contract.text or ''):,} characters). Click **Analyze my contract**."


def load_contract(file_path: str | None, pasted: str, session: Session):
    """Load from the uploaded file if there is one, otherwise from the pasted text."""
    try:
        if file_path:
            contract = load_contract_upload(
                file_path,
                max_bytes=int(settings.max_upload_mb * 1_048_576),
                max_pages=settings.max_pdf_pages,
            )
            if contract.needs_document_block and agent is None:
                raise ContractLoadError(
                    "This PDF has no usable English text (it looks scanned or Arabic-only). Demo mode can only "
                    "read text: paste the text, or add an ANTHROPIC_API_KEY so Claude can read the PDF."
                )
        elif pasted and pasted.strip():
            contract = LoadedContract.from_text(pasted, "pasted text")
        else:
            return session, "_Upload a PDF/TXT or paste the contract text first._", *(gr.update(),) * 5
    except ContractLoadError as exc:
        return session, f"**Couldn't load the contract:** {exc}", *(gr.update(),) * 5
    except Exception as exc:  # e.g. a corrupt PDF
        return session, f"**Couldn't read that file:** {type(exc).__name__}: {exc}", *(gr.update(),) * 5

    # A new contract starts a fresh conversation (the request count carries over).
    new_session = Session(contract=contract, llm_requests=session.llm_requests)
    return new_session, _loaded_status(contract), [], render_trace_markdown([]), *_no_letter()


def load_sample(session: Session):
    """Load the fictional sample contract and fill the form with a demo scenario."""
    contract = LoadedContract.from_text(SAMPLE_PATH.read_text(encoding="utf-8"), "sample_contract.txt (fictional)")
    new_session = Session(contract=contract, llm_requests=session.llm_requests)
    return (
        new_session,
        _loaded_status(contract),
        [],
        render_trace_markdown([]),
        *_no_letter(),     # no letter yet
        100_000,           # RERA average for a similar 1BR (made-up demo figure)
        93_500,            # landlord asks for +10%
        "rent_increase",
        "2026-11-01",      # notice arrived 74 days before expiry (late)
        "email",
    )


# ------------------------------------------------------- landlord's notice --

def _keep_or_set(value):
    return gr.update(value=value) if value is not None else gr.update()


def read_notice_image(image_path: str | None, session: Session):
    """The Notice Reader: screenshot -> form values the tenant then checks."""
    unchanged = (gr.update(),) * 4
    if not image_path:
        yield session, "_Upload a screenshot of the landlord's message first._", *unchanged, gr.update()
        return
    yield session, "_The Notice Reader is reading the screenshot..._", *unchanged, gr.update()
    events = list(read_notice(session, image_path, settings=settings, agent=agent, cache=cache))
    final = events[-1].detail
    trace = render_trace_markdown(events[:-1])
    reading = final["reading"]
    if reading is None or not reading.is_landlord_notice:
        yield session, final["text"], *unchanged, trace
        return
    form = reading.form_values()
    yield (session, final["text"], _keep_or_set(form["proposed_new_rent"]), _keep_or_set(form["notice_type"]),
           _keep_or_set(form["notice_date"]), _keep_or_set(form["delivery_method"]), trace)


def load_sample_notice(session: Session):
    """Show the fictional sample screenshot, then read it."""
    path = str(SAMPLE_NOTICE)
    for update in read_notice_image(path, session):
        yield path, *update


# ------------------------------------------------------------------- letter --

ARABIC_NOTE = (
    "**The Arabic text of Dubai's laws is the original and prevails.** Haqq quotes the official English "
    "translation; the Arabic letter's citations were turned into Arabic by code (law number, year and article), "
    "not by the AI. Check them against the Arabic originals: "
    + ", ".join(f"[{name}]({url})" for name, url in ARABIC_SOURCES.items())
    + ". Not legal advice."
)


def _no_letter():
    """Values for the three letter components: English box, Arabic box, Arabic note."""
    return "", gr.update(value="", visible=False), gr.update(value="", visible=False)


def _letter_view(letter: dict, language: str):
    """Show the team's letter: English, Arabic (right-to-left) or both."""
    arabic = letter.get("body_arabic")
    if arabic:
        english = gr.update(value=letter["body"], visible=language != "ar")
        return english, gr.update(value=arabic, visible=True), gr.update(value=ARABIC_NOTE, visible=True)
    if language != "en":
        why = ("Arabic letters are written by the Drafter (Claude), so they need an ANTHROPIC_API_KEY."
               if agent is None else "The Drafter could not produce a verified Arabic version this time.")
        return (gr.update(value=letter["body"], visible=True), gr.update(value="", visible=False),
                gr.update(value=f"_{why} The English letter is shown above._", visible=True))
    return gr.update(value=letter["body"], visible=True), gr.update(visible=False), gr.update(visible=False)


# -------------------------------------------------------------------- agent --

def _run_turn(user_text: str, display_text: str, analyze: bool, session: Session, chat: list, inputs: TenantInputs):
    """Run one turn (LLM loop or demo pipeline) and stream UI updates."""
    chat = list(chat or []) + [
        {"role": "user", "content": display_text},
        {"role": "assistant", "content": "_Working..._"},
    ]
    events: list[TraceEvent] = []
    letter_out = (gr.update(),) * 3   # the Letter panel changes only when the team writes a letter
    stream = run_turn(
        session, user_text, analyze=analyze, inputs=inputs, today=date.today(),
        settings=settings, agent=agent, cache=cache,
    )

    for event in stream:
        if event.kind == "final":
            chat[-1]["content"] = event.detail["text"]
            usage = event.detail.get("usage")
            if usage:
                events.append(TraceEvent("info", "Token usage", detail=", ".join(f"{k}: {v:,}" for k, v in usage.items())))
        else:
            events.append(event)
            calls = sum(1 for e in events if e.kind == "tool_call")
            chat[-1]["content"] = f"_Working... {calls} step(s) so far (latest: {event.agent}, {event.title})_"
            if (letter := latest_letter([event])) is not None:
                letter_out = _letter_view(letter, inputs.letter_language)
        yield chat, render_trace_markdown(events), session, *letter_out


def analyze(session, chat, rera, proposed, notice_type, notice_date, method, language):
    inputs = TenantInputs.from_form(rera, proposed, notice_date, notice_type, method, language)
    yield from _run_turn(ANALYZE_REQUEST, "Analyze my contract.", True, session, chat, inputs)


def chat_submit(message, session, chat, rera, proposed, notice_type, notice_date, method, language):
    if not message or not message.strip():
        yield chat, gr.update(), session, *(gr.update(),) * 3, ""
        return
    inputs = TenantInputs.from_form(rera, proposed, notice_date, notice_type, method, language)
    for update in _run_turn(message, message, False, session, chat, inputs):
        yield *update, ""


def new_conversation(session: Session):
    return session.fresh_conversation(), [], render_trace_markdown([]), *_no_letter()


# ----------------------------------------------------------------------- UI --

if settings.llm_enabled:
    MODE_MD = (
        f"**LLM mode** - model `{settings.model}`, effort `{settings.effort}`. "
        "The Orchestrator (Claude) decides which specialist to ask; every letter is attacked by the Critic "
        "(the landlord's lawyer) and revised once. Open *What the agents did* to watch, including what each "
        f"agent cost. Spend limit: {format_usd(settings.max_cost_per_run_usd)} per analysis (all agents together), "
        f"{settings.max_requests_per_session} questions per session."
    )
else:
    MODE_MD = (
        "**Demo mode - the LLM is off.** No `ANTHROPIC_API_KEY` was found, so the code-only specialists "
        "run in a fixed order (the Critic does its automatic check only) and chat searches the official "
        "law text (BM25, no AI). Add a key to `.env` to enable the Claude agents."
    )

CSS = """
.disclaimer {border-left: 4px solid #d97706; padding: 6px 12px; background: rgba(217,119,6,0.08); border-radius: 4px;}
.mode {border-left: 4px solid #2563eb; padding: 6px 12px; background: rgba(37,99,235,0.08); border-radius: 4px;}
"""

TEAM_MD = "| Agent | How it works | Job |\n|---|---|---|\n" + "\n".join(
    f"| **{name}** | {how} | {job} |" for name, (how, job) in ROSTER.items()
)

NOTICE_TYPES = [
    ("No notice yet / not sure", "none"),
    ("Rent increase", "rent_increase"),
    ("Other change to the contract terms", "change_terms"),
    ("Eviction (owner's own use, sale, demolition, renovation)", "eviction"),
]
DELIVERY = [
    ("Not sure", "unknown"),
    ("Notary public", "notary_public"),
    ("Registered mail", "registered_mail"),
    ("Email", "email"),
    ("WhatsApp / SMS", "whatsapp_or_sms"),
    ("Handed to me", "hand_delivered"),
    ("Other", "other"),
]

with gr.Blocks(title="Haqq - Dubai Tenant Rights Agent") as demo:
    session_state = gr.State(Session())

    gr.Markdown(
        "# Haqq - Dubai Tenant Rights Agent\n"
        "Upload your tenancy contract (and, if you like, a screenshot of your landlord's notice) and chat "
        "with a team of agents that checks rent increases, notice deadlines and risky clauses against the "
        "official text of Dubai's tenancy law, cites the articles it relies on, and drafts a letter to your "
        "landlord in English or Arabic, after a Critic playing the landlord's lawyer has attacked it."
    )
    gr.Markdown(f"**{DISCLAIMER}**", elem_classes="disclaimer")
    gr.Markdown(MODE_MD, elem_classes="mode")

    with gr.Row():
        with gr.Column(scale=2):
            gr.Markdown("### 1. Your contract")
            file_in = gr.File(label="Upload contract (PDF or TXT)", file_types=[".pdf", ".txt"], type="filepath")
            pasted_in = gr.Textbox(label="...or paste the contract text", lines=5)
            with gr.Row():
                use_btn = gr.Button("Use pasted text")
                sample_btn = gr.Button("Load sample contract")
            contract_status = gr.Markdown("_No contract loaded._")

            gr.Markdown("### 2. Details from your landlord (optional)")
            notice_img = gr.Image(
                type="filepath", sources=["upload", "clipboard"], height=280,
                label="The landlord's notice: a WhatsApp, SMS or email screenshot (optional)",
            )
            with gr.Row():
                read_btn = gr.Button("Read the notice")
                sample_notice_btn = gr.Button("Load sample notice")
            notice_status = gr.Markdown(
                "_Upload a screenshot and the Notice Reader (Claude vision) fills in the form below. "
                "Check every value: the screenshot is treated as data, never as instructions._"
            )
            rera_in = gr.Number(
                label="RERA Rental Index average rent for similar units (AED/year)",
                info="From the official DLD/RERA Rental Index calculator.",
                minimum=0,
            )
            proposed_in = gr.Number(label="Landlord's proposed new rent (AED/year)", minimum=0)
            notice_type_in = gr.Dropdown(NOTICE_TYPES, value="none", label="What did the landlord's notice say?")
            notice_date_in = gr.Textbox(label="Date you received the notice (YYYY-MM-DD)", placeholder="2026-11-01")
            method_in = gr.Dropdown(DELIVERY, value="unknown", label="How was the notice delivered?")
            language_in = gr.Radio(
                [("English", "en"), ("العربية (Arabic)", "ar"), ("Both", "both")], value="en",
                label="Write the letter to my landlord in",
                info="Arabic letters are translated by the Drafter; their legal citations are converted by code.",
            )
            gr.Markdown(
                "_The official DLD/RERA Rental Index calculator is the source of truth for the average "
                "rent. Dubai updated its rental index in 2025, so use a current figure._"
            )

        with gr.Column(scale=3):
            gr.Markdown("### 3. Ask Haqq")
            analyze_btn = gr.Button("Analyze my contract", variant="primary")
            chatbot = gr.Chatbot(label="Haqq", height=520)
            msg_in = gr.Textbox(
                show_label=False,
                placeholder="Ask a question, e.g. 'Can my landlord keep my deposit?' and press Enter",
            )
            gr.Examples(
                examples=[
                    "Is my landlord's rent increase legal?",
                    "Can my landlord keep my security deposit?",
                    "My landlord wants me out to sell the flat. How much notice do they need?",
                    "Draft an RDSC complaint summary about the rent increase.",
                ],
                inputs=msg_in,
            )
            letter_box = gr.Textbox(
                label="Letter to your landlord (draft: check every fact before sending)",
                lines=12, buttons=["copy"], interactive=True,
                placeholder="When the team drafts a letter (Drafter, then the Critic's review, then one revision), it appears here.",
            )
            letter_ar_box = gr.Textbox(
                label="الرسالة إلى المالك (مسودة) - Arabic letter (draft)", lines=12, buttons=["copy"],
                interactive=True, rtl=True, text_align="right", visible=False,
            )
            arabic_note = gr.Markdown(visible=False)
            with gr.Accordion("What the agents did", open=False):
                trace_md = gr.Markdown(render_trace_markdown([]))
            with gr.Accordion("Meet the team", open=False):
                gr.Markdown(TEAM_MD)
            reset_btn = gr.Button("New conversation", size="sm")

    # --- wiring: which function runs on which click, with which inputs/outputs ---
    form_inputs = [rera_in, proposed_in, notice_type_in, notice_date_in, method_in, language_in]
    no_file = gr.State(None)  # "Use pasted text" ignores any uploaded file
    letter_outputs = [letter_box, letter_ar_box, arabic_note]

    loaded = [session_state, contract_status, chatbot, trace_md, *letter_outputs]
    file_in.upload(load_contract, [file_in, pasted_in, session_state], loaded)
    use_btn.click(load_contract, [no_file, pasted_in, session_state], loaded)
    sample_btn.click(
        load_sample, [session_state],
        [*loaded, rera_in, proposed_in, notice_type_in, notice_date_in, method_in],
    )
    notice_outputs = [session_state, notice_status, proposed_in, notice_type_in, notice_date_in, method_in, trace_md]
    read_btn.click(read_notice_image, [notice_img, session_state], notice_outputs)
    sample_notice_btn.click(load_sample_notice, [session_state], [notice_img, *notice_outputs])
    analyze_btn.click(analyze, [session_state, chatbot, *form_inputs], [chatbot, trace_md, session_state, *letter_outputs])
    msg_in.submit(chat_submit, [msg_in, session_state, chatbot, *form_inputs],
                  [chatbot, trace_md, session_state, *letter_outputs, msg_in])
    reset_btn.click(new_conversation, [session_state], [session_state, chatbot, trace_md, *letter_outputs])


if __name__ == "__main__":
    demo.launch(theme=gr.themes.Soft(), css=CSS)
