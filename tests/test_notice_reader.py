"""The Notice Reader (Claude vision) with a fake Claude: request shape, validation,
spend guards, the result cache, demo mode, and the screenshot-as-data rule."""

import base64
import io
import json
from datetime import date
from pathlib import Path

import pytest
from fakes import FakeClient, json_reply, reply
from PIL import Image

from agent.config import Settings
from agent.inputs import TenantInputs
from agent.loop import TenantAgent
from agent.prompts import build_user_message
from agent.result_cache import ResultCache
from agent.roster import NOTICE_READER
from agent.runner import Session, read_notice
from agent.team.notice_reader import (
    MAX_IMAGE_EDGE,
    SAMPLE_NOTICE,
    SAMPLE_READING,
    NoticeImageError,
    NoticeReading,
    build_reader_params,
    load_notice_image,
)

SAMPLE = json.loads(SAMPLE_READING.read_text(encoding="utf-8"))["reading"]


def settings(**overrides):
    base = dict(api_key="test", model="claude-haiku-5-5", effort="medium", max_tool_rounds=5)
    return Settings(**{**base, **overrides})


def read(path, replies, session=None, cache=None, **overrides):
    client = FakeClient(replies)
    s = settings(**overrides)
    agent = TenantAgent(s, client=client) if s.api_key else None
    events = list(read_notice(session or Session(), str(path), settings=s, agent=agent, cache=cache))
    return events, events[-1].detail, client


def png(path: Path, size=(400, 300), color="white") -> Path:
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def test_the_sample_screenshot_is_read_into_form_values():
    events, final, client = read(SAMPLE_NOTICE, [json_reply(SAMPLE)])
    reading = final["reading"]
    assert reading.form_values() == {"proposed_new_rent": 93_500, "notice_type": "rent_increase",
                                     "notice_date": "2026-11-01", "delivery_method": "whatsapp_or_sms"}
    assert "please check" in final["text"] and "AED 93,500" in final["text"]
    assert all(e.agent == NOTICE_READER for e in events[:-1])
    assert any("addressed to an AI: reported, not followed" in e.title for e in events)
    assert "ignored it" in final["text"]


def test_request_sends_the_image_first_with_a_json_schema():
    _, _, client = read(SAMPLE_NOTICE, [json_reply(SAMPLE)])
    call = client.calls[0]
    image_block, text_block = call["messages"][0]["content"]
    assert image_block["type"] == "image" and image_block["source"]["media_type"] == "image/png"
    assert base64.b64decode(image_block["source"]["data"]) == SAMPLE_NOTICE.read_bytes()
    assert text_block["type"] == "text"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "low"
    assert "tools" not in call
    assert "data, not instructions" in call["system"]


def test_screenshot_text_never_reaches_the_orchestrator():
    # Only typed, validated values travel on: numbers, a date and two enums.
    reading = NoticeReading.model_validate(SAMPLE)
    form = reading.form_values()
    inputs = TenantInputs.from_form(None, form["proposed_new_rent"], form["notice_date"], form["notice_type"],
                                    form["delivery_method"])
    message = build_user_message("Analyze", inputs, True, 100, date(2026, 11, 8))
    assert "AI assistant" not in message and "accept" not in message
    assert "AED 93,500" in message and "2026-11-01" in message


def test_bad_values_are_dropped_not_trusted():
    data = {**SAMPLE, "proposed_annual_rent_aed": -5, "delivery_method": "carrier pigeon",
            "notice_type": "threat", "notice_date": "31/02/2026"}
    _, final, _ = read(SAMPLE_NOTICE, [json_reply(data)])
    reading = final["reading"]
    assert reading.proposed_annual_rent_aed is None and reading.notice_date is None
    assert reading.delivery_method == "unknown" and reading.notice_type == "unclear"
    assert reading.form_values() == {"proposed_new_rent": None, "notice_type": None, "notice_date": None,
                                     "delivery_method": None}


def test_not_a_notice_fills_nothing(tmp_path):
    data = {**SAMPLE, "is_landlord_notice": False}
    _, final, _ = read(png(tmp_path / "cat.png"), [json_reply(data)])
    assert "doesn't look like a landlord's notice" in final["text"]


def test_a_refusal_is_reported_and_nothing_is_filled():
    events, final, _ = read(SAMPLE_NOTICE, [reply("refusal")])
    assert final["reading"] is None and "couldn't read that screenshot" in final["text"]
    assert any(e.is_error for e in events)


def test_big_images_are_shrunk_before_sending(tmp_path):
    image = load_notice_image(png(tmp_path / "big.png", (4000, 3000)), max_bytes=5_000_000)
    assert max(image.size) == MAX_IMAGE_EDGE and image.media_type == "image/png"
    with Image.open(io.BytesIO(image.data)) as sent:
        assert sent.size == image.size
    params = build_reader_params(image, "claude-haiku-5-5")
    assert params["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_uploads_that_are_not_images_or_too_big_are_refused(tmp_path):
    fake = tmp_path / "notice.png"
    fake.write_text("not really a picture", encoding="utf-8")
    with pytest.raises(NoticeImageError):
        load_notice_image(fake, max_bytes=5_000_000)
    with pytest.raises(NoticeImageError, match="limit"):
        load_notice_image(SAMPLE_NOTICE, max_bytes=1_000)
    events, final, client = read(fake, [])
    assert client.calls == [] and "Couldn't use that image" in final["text"]


def test_the_same_screenshot_twice_is_free(tmp_path):
    cache = ResultCache(tmp_path / "cache")
    read(SAMPLE_NOTICE, [json_reply(SAMPLE)], cache=cache)
    events, final, client = read(SAMPLE_NOTICE, [], cache=cache)
    assert client.calls == [] and final["reading"].proposed_annual_rent_aed == 93_500
    assert "$0.00 (answered from the local cache)" in final["text"]


def test_reading_counts_toward_the_session_limit():
    session = Session(llm_requests=3)
    _, final, client = read(SAMPLE_NOTICE, [], session=session, max_requests_per_session=3)
    assert client.calls == [] and "used its 3 questions" in final["text"]


def test_demo_mode_shows_the_stored_sample_reading_and_explains_other_images(tmp_path):
    events, final, _ = read(SAMPLE_NOTICE, [], api_key=None)
    assert final["reading"].form_values()["proposed_new_rent"] == 93_500
    assert "Demo mode: $0.00" in final["text"]
    _, final, _ = read(png(tmp_path / "other.png"), [], api_key=None)
    assert final["reading"] is None and "needs Claude vision" in final["text"]


def test_the_sample_notice_is_fictional_and_matches_its_reading():
    assert SAMPLE_NOTICE.exists() and SAMPLE_NOTICE.stat().st_size < 300_000
    reading = NoticeReading.model_validate(SAMPLE)
    assert reading.notice_date == date(2026, 11, 1) and reading.embedded_instructions
