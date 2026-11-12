"""The website's JSON + streaming API (webapi.py), in demo mode with no network.

One LLM-mode test uses the fake Claude from fakes.py to check the session
limit, the live cost meter and the letter streaming."""

import importlib
import json
from datetime import date

import pytest
from fakes import SAMPLE_EXTRACTION, FakeClient, json_reply, reply, text, tool, usage
from fastapi.testclient import TestClient

from agent.config import Settings
from agent.cost import UsageMeter
from agent.loop import TenantAgent
from agent.runner import Session, run_turn
from webapi import SESSION_HEADER, SessionStore, build_digest, create_web_app, days_late, describe, short_citation

TODAY = date(2026, 11, 8)
DEMO_FORM = {"rera_average_rent": 100_000, "proposed_new_rent": 93_500, "notice_type": "rent_increase",
             "notice_date": "2026-11-01", "delivery_method": "email", "letter_language": "en"}


def settings(**overrides):
    base = dict(api_key=None, model="claude-haiku-5-5", effort="medium", max_tool_rounds=6)
    return Settings(**{**base, **overrides})


@pytest.fixture
def client():
    app = create_web_app(settings=settings(), agent=None, cache=None, today=lambda: TODAY)
    with TestClient(app) as c:
        yield c


def start(client):
    """A new tab: /api/meta hands out the session token."""
    r = client.get("/api/meta")
    assert r.status_code == 200
    token = r.json()["session"]
    assert r.headers[SESSION_HEADER] == token
    return {SESSION_HEADER: token}


def events_of(response):
    """Parse a text/event-stream body into (event, data) pairs."""
    out = []
    for block in response.text.split("\n\n"):
        name, data = None, ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if name:
            out.append((name, json.loads(data)))
    return out


# ---- the site ------------------------------------------------------------------

def test_index_serves_the_site_with_versioned_assets(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Your rent." in r.text and "__V__" not in r.text and "__MODE__" not in r.text
    assert 'data-mode="demo"' in r.text
    assert "frame-ancestors" not in r.headers["content-security-policy"]   # Spaces show the app in an iframe
    assert client.get("/static/css/site.css").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200


def test_meta_describes_demo_mode_limits_and_the_team(client):
    data = client.get("/api/meta").json()
    assert data["mode"] == "demo" and data["model"] is None
    assert data["limits"]["max_upload_mb"] == 5.0 and data["limits"]["max_pdf_pages"] == 10
    assert [a["name"] for a in data["roster"]][:2] == ["Orchestrator", "Contract Analyst"]
    assert data["law"]["in_force"] < data["law"]["total"]
    assert data["disclaimer"].startswith("Not legal advice")
    assert {d["value"] for d in data["delivery_methods"]} >= {"notary_public", "whatsapp_or_sms", "unknown"}


# ---- step 1: the contract ---------------------------------------------------------

def test_sample_contract_loads_with_the_demo_form(client):
    h = start(client)
    data = client.post("/api/contract/sample", headers=h).json()
    assert data["contract"]["label"].startswith("sample_contract.txt")
    assert data["contract"]["chars"] > 1000 and "Marina" in data["contract"]["preview"]
    assert data["form"]["proposed_new_rent"] == 93_500


def test_pasted_text_and_uploaded_txt(client, sample_text):
    h = start(client)
    r = client.post("/api/contract", headers=h, data={"text": sample_text})
    assert r.status_code == 200 and r.json()["contract"]["label"] == "pasted text"
    r = client.post("/api/contract", headers=h, files={"file": ("my lease.txt", sample_text.encode(), "text/plain")})
    assert r.status_code == 200 and r.json()["contract"]["label"] == "my lease.txt"


def test_contract_errors_are_plain_messages(client):
    h = start(client)
    r = client.post("/api/contract", headers=h, data={"text": "too short"})
    assert r.status_code == 400 and "couldn't find enough text" in r.json()["error"]
    r = client.post("/api/contract", headers=h, files={"file": ("lease.docx", b"x" * 500, "application/octet-stream")})
    assert r.status_code == 400 and "Unsupported file type" in r.json()["error"]
    r = client.post("/api/contract", headers=h, data={})
    assert r.status_code == 400 and "paste" in r.json()["error"]


def test_upload_size_limit_applies_before_and_after_reading(sample_text):
    app = create_web_app(settings=settings(max_upload_mb=0.01), agent=None, cache=None)
    with TestClient(app) as c:
        h = start(c)
        big = (sample_text * 40).encode()
        r = c.post("/api/contract", headers=h, files={"file": ("big.txt", big, "text/plain")})
        assert r.status_code == 413 and "too big" in r.json()["error"]


# ---- step 2: the notice -----------------------------------------------------------

def test_sample_notice_fills_the_form_and_reports_the_hidden_instruction(client):
    h = start(client)
    data = client.post("/api/notice/sample", headers=h).json()
    assert data["ok"] is True
    assert data["form"] == {"proposed_new_rent": 93_500.0, "notice_type": "rent_increase",
                            "notice_date": "2026-11-01", "delivery_method": "whatsapp_or_sms"}
    assert data["warnings"] and "AI assistant" in data["warnings"][0]
    assert data["image_url"] and client.get("/" + data["image_url"]).headers["content-type"] == "image/png"
    assert [e["agent"] for e in data["events"]] == ["Notice Reader", "Notice Reader"]


def test_other_screenshots_need_claude_vision_in_demo_mode(client):
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, format="PNG")
    h = start(client)
    data = client.post("/api/notice", headers=h, files={"file": ("shot.png", buf.getvalue(), "image/png")}).json()
    assert data["ok"] is False and "Claude vision" in data["html"]
    r = client.post("/api/notice", headers=h, files={"file": ("shot.png", b"not an image", "image/png")})
    assert r.json()["ok"] is False and "not an image" in r.json()["html"]


# ---- step 3: the streamed analysis --------------------------------------------------

def test_analysis_needs_a_contract(client):
    h = start(client)
    r = client.post("/api/analyze", headers=h, json=DEMO_FORM)
    assert r.status_code == 400 and "Load a contract first" in r.json()["error"]


def test_analysis_streams_every_step_then_the_findings(client):
    h = start(client)
    client.post("/api/contract/sample", headers=h)
    r = client.post("/api/analyze", headers=h, json=DEMO_FORM)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = events_of(r)
    names = [n for n, _ in events]
    assert names[0] == "start" and names[-1] == "final" and "letter" in names
    steps = [d for n, d in events if n == "step"]
    agents = {s["agent"] for s in steps}
    assert {"Contract Analyst", "Calculators", "Law Researcher", "Drafter", "Critic"} <= agents
    assert all(s["line"] and s["t"] is not None for s in steps)
    rent = next(s for s in steps if s["kind"] == "tool_result" and s["title"] == "rera_rent_increase_check")
    assert rent["line"] == "10.0% asked, 5% allowed: AED 4,250 over the cap"

    final = events[-1][1]
    assert final["rent"]["excess_over_cap"] == 4250 and final["notice"]["days_late"] == 16
    assert len(final["flags"]) == 3 and final["terms"]["annual_rent_aed"] == 85_000
    assert {a["article_id"] for a in final["law"]} >= {"decree43_art1", "law26_art14", "law26_art20"}
    assert all(a["source_url"].startswith("https://dlp.dubai.gov.ae/") for a in final["law"])
    assert final["letter"]["body"].startswith("Date:") and final["letter"]["body_arabic"] is None
    assert "demo mode" in final["letter"]["arabic_unavailable"]
    assert final["cost"]["usd"] == 0.0 and "<table>" in final["answer_html"]


def test_follow_up_question_streams_a_law_search_answer(client):
    h = start(client)
    client.post("/api/contract/sample", headers=h)
    r = client.post("/api/ask", headers=h, json={**DEMO_FORM, "message": "Can my landlord keep my deposit?"})
    final = events_of(r)[-1][1]
    assert "Art. 20" in final["answer_html"] and final["law"][0]["article_id"] == "law26_art20"
    assert client.post("/api/ask", headers=h, json={"message": "  "}).status_code == 400
    assert client.post("/api/ask", headers=h, json={"message": "x" * 2001}).status_code == 400


def test_markdown_from_the_model_cannot_inject_html():
    from webapi import markdown_html

    out = markdown_html('<script>alert(1)</script> [x](javascript:alert(1)) **ok**')
    assert "<script>" not in out and "&lt;script&gt;" in out      # shown as text, never run
    assert 'href="javascript' not in out and "<strong>ok</strong>" in out


# ---- sessions --------------------------------------------------------------------------

def test_sessions_are_separate_and_a_new_contract_resets_the_chat(client, sample_text):
    a, b = start(client), start(client)
    assert a != b
    client.post("/api/contract/sample", headers=a)
    assert client.get("/api/meta", headers=a).json()["contract"]["chars"] > 0
    assert client.get("/api/meta", headers=b).json()["contract"] is None
    store = client.app.state.sessions
    _, entry = store.get(a[SESSION_HEADER])
    entry.session.llm_requests = 7
    entry.session.messages.append({"role": "user", "content": "hi"})
    client.post("/api/contract", headers=a, data={"text": sample_text})
    _, entry = store.get(a[SESSION_HEADER])
    assert entry.session.messages == [] and entry.session.llm_requests == 7   # the count carries over
    assert client.post("/api/reset", headers=a).json()["ok"] is True


def test_unknown_token_gets_a_fresh_session(client):
    r = client.get("/api/meta", headers={SESSION_HEADER: "made-up"})
    assert r.json()["session"] != "made-up"


def test_a_busy_session_refuses_a_second_run(client):
    h = start(client)
    client.post("/api/contract/sample", headers=h)
    _, entry = client.app.state.sessions.get(h[SESSION_HEADER])
    entry.busy_since = __import__("time").monotonic()
    r = client.post("/api/analyze", headers=h, json=DEMO_FORM)
    assert r.status_code == 409 and "still working" in r.json()["error"]


def test_session_store_expires_idle_sessions_and_caps_its_size():
    now = [0.0]
    store = SessionStore(max_sessions=3, ttl_s=100, clock=lambda: now[0])
    first, _ = store.get(None)
    now[0] = 50
    tokens = [store.get(None)[0] for _ in range(3)]
    assert len(store) == 3 and first not in store._entries   # the oldest made room
    now[0] = 500
    store.get(None)
    assert len(store) == 1 and not set(tokens) & set(store._entries)


# ---- LLM mode with a fake Claude -------------------------------------------------------

def test_llm_mode_question_limit_is_enforced_without_calling_claude():
    s = settings(api_key="test", max_requests_per_session=2)
    fake = FakeClient([])   # any call would fail the test
    app = create_web_app(settings=s, agent=TenantAgent(s, client=fake), cache=None, today=lambda: TODAY)
    with TestClient(app) as c:
        h = start(c)
        assert c.get("/api/meta", headers=h).json()["mode"] == "llm"
        c.post("/api/contract/sample", headers=h)
        _, entry = app.state.sessions.get(h[SESSION_HEADER])
        entry.session.llm_requests = 2
        events = events_of(c.post("/api/analyze", headers=h, json=DEMO_FORM))
        assert any(d.get("title") == "Session request limit reached" for n, d in events if n == "step")
        assert "used its 2 questions" in events[-1][1]["answer_text"]
        assert fake.calls == []


def test_llm_mode_streams_a_live_cost_and_the_answer(sample_text):
    s = settings(api_key="test")
    fake = FakeClient([
        json_reply(SAMPLE_EXTRACTION, u=usage(1_500, 900)),
        reply("tool_use", tool("t1", "extract_contract_terms", {}), u=usage(200, 80, w=3_000)),
        reply("end_turn", text("The increase is above the cap [Dubai Decree No. 43 of 2013, Art. 1]."),
              u=usage(50, 400, r=3_000, w=2_000)),
    ])
    app = create_web_app(settings=s, agent=TenantAgent(s, client=fake), cache=None, today=lambda: TODAY)
    with TestClient(app) as c:
        h = start(c)
        c.post("/api/contract", headers=h, data={"text": sample_text})
        events = events_of(c.post("/api/analyze", headers=h, json=DEMO_FORM))
    costs = [d["cost_usd"] for n, d in events if n == "step"]
    assert costs == sorted(costs) and costs[-1] > 0          # the ticker only goes up
    final = events[-1][1]
    assert final["cost"]["usd"] == pytest.approx((175 + 30 + 625 + 690) / 1e6)
    assert final["cost"]["usd"] == pytest.approx(costs[-1])
    assert "above the cap" in final["answer_text"] and final["requests_used"] == 1


def test_run_turn_fills_a_meter_the_caller_passes(sample_text):
    from agent.contract import LoadedContract
    from agent.inputs import TenantInputs

    s = settings(api_key="test")
    fake = FakeClient([json_reply(SAMPLE_EXTRACTION), reply("end_turn", text("Done."))])
    meter = UsageMeter(s.model)
    session = Session(contract=LoadedContract.from_text(sample_text, "sample"))
    list(run_turn(session, "Hi", analyze=True, inputs=TenantInputs(), today=TODAY, settings=s,
                  agent=TenantAgent(s, client=fake), meter=meter))
    assert meter.requests == 2 and meter.cost_usd > 0


# ---- small helpers -----------------------------------------------------------------

def test_helpers():
    assert short_citation("Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 25(2)") == "Law 26/2007, Art. 25(2)"
    assert short_citation("Dubai Decree No. 43 of 2013 (rent increase caps)") == "Decree 43/2013"
    assert days_late({"notice_on_time": False, "notice_date": "2026-11-01 (Sun)", "last_valid_notice_date": "2026-10-16 (Fri)"}) == 16
    assert days_late({"notice_on_time": True}) is None
    from agent.trace import TraceEvent

    assert describe(TraceEvent("thinking", "reasoning", detail="First idea. Second.")) == "First idea."
    digest = build_digest([], TraceEvent("final", "answer", detail={"text": "Hello"}), language="en", llm=False)
    assert digest["letter"] is None and digest["answer_html"] == "<p>Hello</p>\n"


def test_app_mounts_the_classic_gradio_ui(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-your-key-placeholder")   # demo mode: no real client
    import app as app_module

    app_module = importlib.reload(app_module)
    assert app_module.agent is None
    with TestClient(app_module.build_server()) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/meta").json()["mode"] == "demo"
        classic = c.get("/classic/")
        assert classic.status_code == 200 and "gradio" in classic.text.lower()
