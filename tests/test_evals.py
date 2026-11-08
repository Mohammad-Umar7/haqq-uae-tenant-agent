"""The eval harness: cases are well-formed, the committed scorecard is current,
and LLM mode drives the Message Batches API correctly (mocked)."""

import copy
from datetime import date
from types import SimpleNamespace as NS

import pytest
from fakes import SAMPLE_EXTRACTION, json_reply, reply, usage

from agent.tools.clause_scan import RED_FLAGS
from evals.run_eval import (
    DETERMINISTIC_NOTES,
    RESULTS_PATH,
    load_cases,
    render_markdown,
    run_deterministic,
    run_llm,
)

CASES, TODAY = load_cases()


def test_cases_cover_every_band_both_notice_outcomes_and_every_flag():
    expected = [c.expected for c in CASES]
    assert 9 <= len(CASES) <= 15
    assert {e["max_increase_percent"] for e in expected} >= {0, 5, 10, 15, 20}
    assert {e["notice_on_time"] for e in expected} >= {True, False}
    assert {e["delivery_ok"] for e in expected} >= {True, False}
    all_flags = {rf.id for rf in RED_FLAGS} | {"ejari_not_mentioned"}
    for flag in all_flags:
        assert any(flag in e["red_flags"] for e in expected), f"no case with {flag}"
        assert any(flag not in e["red_flags"] for e in expected), f"no case without {flag}"
    for case in CASES:
        text = case.contract_path.read_text(encoding="utf-8")
        assert text.startswith("FICTIONAL"), case.id


def test_deterministic_scorecard():
    card = run_deterministic()
    assert card["verdict_accuracy"] == 1.0 and card["band_accuracy"] == 1.0 and card["terms_accuracy"] == 1.0
    assert card["red_flag_counts"] == {"true_positive": 12, "false_positive": 1, "false_negative": 3}


def test_committed_results_md_is_up_to_date():
    rendered = render_markdown(run_deterministic(), "Eval results: deterministic pipeline (no LLM)", DETERMINISTIC_NOTES)
    assert RESULTS_PATH.read_text(encoding="utf-8") == rendered, "run: python -m evals.run_eval --write evals/RESULTS.md"


# ---- LLM mode with a fake Message Batches API ---------------------------------------

def perfect_extraction(case):
    """What a perfect reader would return for one eval case."""
    data = copy.deepcopy(SAMPLE_EXTRACTION)
    exp = case.expected
    flags = [f for f in exp["red_flags"] if f != "ejari_not_mentioned"]
    data.update(
        annual_rent_aed=exp["annual_rent_aed"], end_date=exp["end_date"],
        mentions_ejari="ejari_not_mentioned" not in exp["red_flags"],
        red_flags=[{"flag_id": f, "clause_number": None, "quote": "..."} for f in flags],
    )
    return data


class FakeBatches:
    def __init__(self, results):
        self._results = results
        self.statuses = ["in_progress", "in_progress", "ended"]
        self.created = None

    def create(self, requests):
        self.created = list(requests)
        return NS(id="msgbatch_test", processing_status="in_progress")

    def retrieve(self, batch_id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return NS(id=batch_id, processing_status=status)

    def results(self, batch_id):
        return iter(self._results)


def fake_client(results):
    return NS(messages=NS(batches=FakeBatches(results)))


def test_llm_mode_submits_one_batch_and_scores_results_by_custom_id():
    # Results arrive in reverse order; one case errors, one is half wrong.
    results = []
    for case in reversed(CASES):
        if case.id == "c09_alnahda_above_average":     # 4 expected flags, all lost
            results.append(NS(custom_id=case.id, result=NS(type="errored", error=NS(type="api_error"))))
            continue
        data = perfect_extraction(case)
        if case.id == "c02_jlt_band5":
            data["annual_rent_aed"] = 58_000          # misread rent -> wrong band and verdict
        results.append(NS(custom_id=case.id, result=NS(type="succeeded", message=json_reply(data, u=usage(2_000, 1_000)))))
    client = fake_client(results)
    sleeps = []

    out = run_llm(client, "claude-haiku-5-5", poll_seconds=5, sleep=sleeps.append)

    batches = client.messages.batches
    assert [r["custom_id"] for r in batches.created] == [c.id for c in CASES]
    params = batches.created[0]["params"]
    assert params["model"] == "claude-haiku-5-5"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert sleeps == [5, 5]                                # polled until "ended"

    claude = out["claude"]
    rows = {r["id"]: r["misses"] for r in claude["rows"]}
    assert rows["c09_alnahda_above_average"][0].startswith("error: batch result errored")
    assert any("band" in m for m in rows["c02_jlt_band5"])
    assert rows["c11_hard_wording"] == []                  # Claude caught the paraphrased flags
    assert claude["red_flag_counts"]["false_negative"] == 4   # only the errored case's flags are missing
    # 10 succeeded requests x (2,000 in, 1,000 out), batch price = 50%
    assert out["meter"].requests == 10
    assert out["meter"].cost_usd == pytest.approx(10 * (2_000 * 0.10 + 1_000 * 0.50) / 1e6 * 0.5)
    # Hybrid adds the regex flags on top, so it can only raise recall.
    assert out["hybrid"]["red_flag_recall"] >= claude["red_flag_recall"]


def test_llm_mode_can_collect_an_existing_batch_without_resubmitting():
    case = CASES[0]
    client = fake_client([NS(custom_id=case.id, result=NS(type="succeeded", message=reply("refusal")))])
    out = run_llm(client, "claude-haiku-5-5", batch_id="msgbatch_old", sleep=lambda s: None)
    assert client.messages.batches.created is None
    assert out["batch_id"] == "msgbatch_old"
    assert {r["id"]: r["misses"] for r in out["claude"]["rows"]}[case.id][0].startswith("error: Claude declined")


def test_today_is_fixed_so_results_are_reproducible():
    assert TODAY == date(2026, 11, 8)
