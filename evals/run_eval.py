"""Eval harness: score the pipeline on synthetic Dubai contracts.

Two modes:

  deterministic (default, free, no key)
      regex extraction -> clause_risk_scan -> rent calculator -> notice check,
      exactly the tools demo mode uses.

  llm (needs ANTHROPIC_API_KEY)
      Claude reads each contract with the app's own structured extraction
      (agent/extraction.py). All cases go in ONE Message Batches request,
      which costs 50% of normal prices and usually finishes within an hour.
      The math still runs in Python on Claude's extracted terms, so this
      measures what the LLM adds: reading the contract and spotting risky
      clauses that keyword rules miss.

  critic (needs ANTHROPIC_API_KEY)
      The Critic (the landlord's lawyer) reviews draft letters with one
      planted problem each (evals/critic_cases.json, evals/critic_eval.py).
      The deterministic run already scores the Critic's free automatic check.

Usage (from the project folder):
    python -m evals.run_eval                              # deterministic scorecard
    python -m evals.run_eval --write evals/RESULTS.md     # ...and save it
    python -m evals.run_eval --mode llm                   # submit a batch and wait
    python -m evals.run_eval --mode llm --batch-id msgbatch_...   # collect an earlier batch
    python -m evals.run_eval --mode critic --write evals/CRITIC_RESULTS.md

Metrics:
  verdict accuracy   rent within cap? notice on time? delivery method valid?
                     (every check with an expected value)
  band accuracy      the max increase % from Decree No. 43 of 2013
  red-flag precision of the flags raised, how many were expected
  red-flag recall    of the expected flags, how many were raised
  terms accuracy     annual rent and end date read from the contract
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

EVALS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = EVALS_DIR.parent
if str(PROJECT_DIR) not in sys.path:          # allow `python evals/run_eval.py` too
    sys.path.insert(0, str(PROJECT_DIR))

from agent.contract import LoadedContract  # noqa: E402
from agent.cost import UsageMeter  # noqa: E402
from agent.extraction import ExtractionError, build_extraction_params, parse_extraction  # noqa: E402
from agent.registry import ToolContext, execute_tool  # noqa: E402
from evals.critic_eval import render_critic_markdown, run_critic_deterministic, run_critic_llm  # noqa: E402

CASES_PATH = EVALS_DIR / "cases.json"
RESULTS_PATH = EVALS_DIR / "RESULTS.md"
VERDICT_FIELDS = ("within_cap", "notice_on_time", "delivery_ok")
TERM_FIELDS = ("annual_rent_aed", "end_date")


@dataclass(frozen=True)
class Case:
    id: str
    description: str
    contract_path: Path
    inputs: dict[str, Any]
    expected: dict[str, Any]

    @property
    def contract(self) -> LoadedContract:
        return LoadedContract.from_text(self.contract_path.read_text(encoding="utf-8"), self.contract_path.name)


@dataclass
class Prediction:
    annual_rent_aed: float | None = None
    end_date: str | None = None
    max_increase_percent: int | None = None
    within_cap: bool | None = None
    notice_on_time: bool | None = None
    delivery_ok: bool | None = None
    red_flags: set[str] = field(default_factory=set)
    error: str | None = None


def load_cases(path: Path = CASES_PATH) -> tuple[list[Case], date]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        Case(c["id"], c["description"], path.parent / c["contract"], c["inputs"], c["expected"])
        for c in data["cases"]
    ]
    return cases, date.fromisoformat(data["today"])


# ---- predictions --------------------------------------------------------------

def predict_from_terms(case: Case, terms: dict[str, Any], flags: set[str], today: date) -> Prediction:
    """Run the deterministic calculators on extracted terms (shared by both modes)."""
    ctx = ToolContext(contract_text=None, today=today)
    p = Prediction(annual_rent_aed=terms.get("annual_rent_aed"), end_date=terms.get("end_date"), red_flags=set(flags))
    rera, proposed = case.inputs.get("rera_average_rent"), case.inputs.get("proposed_new_rent")
    if p.annual_rent_aed and rera:
        args = {"current_annual_rent": p.annual_rent_aed, "rera_average_rent": rera}
        if proposed:
            args["proposed_new_rent"] = proposed
        out = execute_tool("rera_rent_increase_check", args, ctx).output
        p.max_increase_percent = out.get("max_increase_percent")
        p.within_cap = out.get("is_within_cap")
    if p.end_date and case.inputs.get("notice_type"):
        args = {"contract_end_date": p.end_date, "notice_type": case.inputs["notice_type"]}
        for key in ("notice_date", "delivery_method"):
            if case.inputs.get(key):
                args[key] = case.inputs[key]
        out = execute_tool("notice_deadline_check", args, ctx).output
        p.notice_on_time = out.get("notice_on_time")
        p.delivery_ok = out.get("delivery_method_ok")
    return p


def predict_deterministic(case: Case, today: date) -> Prediction:
    ctx = ToolContext(contract_text=case.contract.text, today=today)
    terms = execute_tool("extract_contract_terms", {}, ctx).output["terms"]
    flags = {f["flag_id"] for f in execute_tool("clause_risk_scan", {}, ctx).output["flags"]}
    return predict_from_terms(case, terms, flags, today)


# ---- scoring ---------------------------------------------------------------------

def _ratio(hit: int, total: int) -> float | None:
    return hit / total if total else None


def score(cases: list[Case], predictions: dict[str, Prediction]) -> dict[str, Any]:
    verdict_hits = verdict_total = band_hits = band_total = term_hits = term_total = 0
    tp = fp = fn = 0
    rows = []
    for case in cases:
        exp, pred = case.expected, predictions.get(case.id) or Prediction(error="no prediction")
        misses: list[str] = []
        for name in VERDICT_FIELDS:
            if exp.get(name) is None:
                continue
            verdict_total += 1
            if getattr(pred, name) == exp[name]:
                verdict_hits += 1
            else:
                misses.append(f"{name}: expected {exp[name]}, got {getattr(pred, name)}")
        if exp.get("max_increase_percent") is not None:
            band_total += 1
            if pred.max_increase_percent == exp["max_increase_percent"]:
                band_hits += 1
            else:
                misses.append(f"band: expected {exp['max_increase_percent']}%, got {pred.max_increase_percent}")
        for name in TERM_FIELDS:
            term_total += 1
            if getattr(pred, name) == exp[name]:
                term_hits += 1
            else:
                misses.append(f"{name}: expected {exp[name]}, got {getattr(pred, name)}")
        expected_flags = set(exp["red_flags"])
        tp += len(pred.red_flags & expected_flags)
        fp += len(pred.red_flags - expected_flags)
        fn += len(expected_flags - pred.red_flags)
        misses += [f"missed flag {f}" for f in sorted(expected_flags - pred.red_flags)]
        misses += [f"extra flag {f}" for f in sorted(pred.red_flags - expected_flags)]
        if pred.error:
            misses.insert(0, f"error: {pred.error}")
        rows.append({"id": case.id, "description": case.description, "misses": misses})
    return {
        "cases": len(cases),
        "verdict_accuracy": _ratio(verdict_hits, verdict_total), "verdict_counts": (verdict_hits, verdict_total),
        "band_accuracy": _ratio(band_hits, band_total), "band_counts": (band_hits, band_total),
        "terms_accuracy": _ratio(term_hits, term_total), "terms_counts": (term_hits, term_total),
        "red_flag_precision": _ratio(tp, tp + fp), "red_flag_recall": _ratio(tp, tp + fn),
        "red_flag_counts": {"true_positive": tp, "false_positive": fp, "false_negative": fn},
        "rows": rows,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_markdown(card: dict[str, Any], title: str, notes: list[str] | None = None) -> str:
    rf = card["red_flag_counts"]
    lines = [
        f"# {title}", "",
        f"{card['cases']} synthetic Dubai contracts (`evals/cases.json`). Regenerate with "
        "`python -m evals.run_eval --write evals/RESULTS.md`.", "",
        "| Metric | Score | Count |", "|---|---|---|",
        f"| Verdict accuracy (within cap / notice on time / delivery valid) | {_pct(card['verdict_accuracy'])} | {card['verdict_counts'][0]}/{card['verdict_counts'][1]} |",
        f"| Rent band accuracy (Decree No. 43 of 2013) | {_pct(card['band_accuracy'])} | {card['band_counts'][0]}/{card['band_counts'][1]} |",
        f"| Red-flag precision | {_pct(card['red_flag_precision'])} | {rf['true_positive']}/{rf['true_positive'] + rf['false_positive']} |",
        f"| Red-flag recall | {_pct(card['red_flag_recall'])} | {rf['true_positive']}/{rf['true_positive'] + rf['false_negative']} |",
        f"| Terms accuracy (annual rent, end date) | {_pct(card['terms_accuracy'])} | {card['terms_counts'][0]}/{card['terms_counts'][1]} |",
        "", "## Per case", "", "| Case | Result |", "|---|---|",
    ]
    for row in card["rows"]:
        result = "all checks passed" if not row["misses"] else "; ".join(row["misses"])
        lines.append(f"| `{row['id']}` | {result} |")
    if notes:
        lines += ["", "## Notes", "", *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"


DETERMINISTIC_NOTES = [
    "The calculators (rent band, notice dates, delivery method) are plain Python, so with correctly "
    "extracted terms their verdicts are exact.",
    "The red-flag scan is keyword rules. `c11_hard_wording` paraphrases three red flags (\"security amount "
    "will not be given back\", \"revise the rental amount whenever it sees fit\", \"forfeits a sum equal to six "
    "months of rent\") and the rules miss all three: that is the recall gap.",
    "`c10` says \"The Tenant is responsible for minor maintenance only; structural repairs ... remain the "
    "Landlord's responsibility\". The maintenance rule sees *tenant ... responsible ... structural* and "
    "flags it: that is the precision gap.",
    "The LLM mode (`--mode llm`, Message Batches API, 50% price) measures how much Claude's reading closes "
    "these gaps. It has not been run yet: it needs an API key.",
]


def run_deterministic(cases_path: Path = CASES_PATH) -> dict[str, Any]:
    cases, today = load_cases(cases_path)
    return score(cases, {c.id: predict_deterministic(c, today) for c in cases})


CRITIC_NOTES = [
    "The automatic check compares every amount, percentage, date, law and article in a letter with the case "
    "file. It catches a wrong number or date, but not `k3` (every number in it exists in the case file, just "
    "attached to the wrong claim), a threat, or a rule the law doesn't contain. That is the Claude Critic's "
    "job: `python -m evals.run_eval --mode critic` (results in `evals/CRITIC_RESULTS.md`).",
]


def render_results() -> str:
    """The whole committed RESULTS.md: the pipeline scorecard, then the Critic's automatic check."""
    critic = render_critic_markdown(run_critic_deterministic(), "Critic eval: automatic check only (code, free)")
    return (render_markdown(run_deterministic(), "Eval results: deterministic pipeline (no LLM)", DETERMINISTIC_NOTES)
            + "\n" + critic + "\n" + "\n".join(f"- {n}" for n in CRITIC_NOTES) + "\n")


# ---- LLM mode: Message Batches API ----------------------------------------------

def build_batch_requests(cases: list[Case], model: str) -> list[dict[str, Any]]:
    """One request per case, keyed by custom_id (results come back in any order)."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    return [
        Request(custom_id=case.id, params=MessageCreateParamsNonStreaming(**build_extraction_params(case.contract, model)))
        for case in cases
    ]


def submit_batch(client: Any, cases: list[Case], model: str) -> str:
    batch = client.messages.batches.create(requests=build_batch_requests(cases, model))
    return batch.id


def wait_for_batch(client: Any, batch_id: str, poll_seconds: float = 30, timeout_seconds: float = 24 * 3600,
                   sleep: Callable[[float], None] = time.sleep) -> Any:
    waited = 0.0
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        if batch.processing_status == "ended":
            return batch
        if waited >= timeout_seconds:
            raise TimeoutError(f"Batch {batch_id} still {batch.processing_status} after {waited:.0f}s")
        print(f"  batch {batch_id}: {batch.processing_status}...", file=sys.stderr)
        sleep(poll_seconds)
        waited += poll_seconds


def collect_batch(client: Any, batch_id: str, cases: list[Case], model: str, today: date
                  ) -> tuple[dict[str, Prediction], dict[str, Prediction], UsageMeter]:
    """Returns (Claude-only predictions, hybrid regex+Claude predictions, cost meter)."""
    by_id = {c.id: c for c in cases}
    claude: dict[str, Prediction] = {}
    hybrid: dict[str, Prediction] = {}
    meter = UsageMeter(model, batch=True)
    for item in client.messages.batches.results(batch_id):
        case = by_id.get(item.custom_id)
        if case is None:
            continue
        if item.result.type != "succeeded":
            claude[case.id] = hybrid[case.id] = Prediction(error=f"batch result {item.result.type}")
            continue
        message = item.result.message
        meter.add(getattr(message, "usage", None))
        try:
            extraction = parse_extraction(message)
        except ExtractionError as exc:
            claude[case.id] = hybrid[case.id] = Prediction(error=str(exc))
            continue
        flags = {f.flag_id for f in extraction.red_flags if f.flag_id != "other"}
        if not extraction.mentions_ejari:
            flags.add("ejari_not_mentioned")
        claude[case.id] = predict_from_terms(case, extraction.terms(), flags, today)
        regex_flags = predict_deterministic(case, today).red_flags
        hybrid[case.id] = predict_from_terms(case, extraction.terms(), flags | regex_flags, today)
    return claude, hybrid, meter


def run_llm(client: Any, model: str, batch_id: str | None = None, cases_path: Path = CASES_PATH,
            poll_seconds: float = 30, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    cases, today = load_cases(cases_path)
    if batch_id is None:
        batch_id = submit_batch(client, cases, model)
        print(f"Submitted batch {batch_id} ({len(cases)} requests).", file=sys.stderr)
    wait_for_batch(client, batch_id, poll_seconds=poll_seconds, sleep=sleep)
    claude, hybrid, meter = collect_batch(client, batch_id, cases, model, today)
    return {"batch_id": batch_id, "claude": score(cases, claude), "hybrid": score(cases, hybrid), "meter": meter}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Haqq on synthetic Dubai contracts.")
    parser.add_argument("--mode", choices=["deterministic", "llm", "critic"], default="deterministic")
    parser.add_argument("--write", type=Path, help="also write the scorecard (markdown) to this file")
    parser.add_argument("--batch-id", help="llm mode: collect an existing batch instead of submitting")
    parser.add_argument("--poll-seconds", type=float, default=30)
    args = parser.parse_args(argv)

    if args.mode == "deterministic":
        text = render_results()
    elif args.mode == "critic":
        import anthropic

        from agent.config import load_settings

        settings = load_settings()
        if not settings.llm_enabled:
            print("Critic mode needs ANTHROPIC_API_KEY (in .env or the environment).", file=sys.stderr)
            return 2
        result = run_critic_llm(anthropic.Anthropic(api_key=settings.api_key), settings)
        cost = result["meter"].summary().replace("This analysis", "The whole critic eval")
        text = (f"# Critic eval: does the landlord's lawyer catch planted mistakes? (`{settings.model}`)\n\n"
                "Draft letters for the sample case with one planted problem each (`evals/critic_cases.json`). "
                f"Regenerate with `python -m evals.run_eval --mode critic --write evals/CRITIC_RESULTS.md`. {cost}\n\n"
                + render_critic_markdown(result["team"], "The Critic as shipped: automatic check + Claude")
                + "\n" + render_critic_markdown(result["claude_only"], "Claude's review alone (no automatic check)")
                + "\n## What the Critic said (Claude only)\n\n```json\n"
                + json.dumps(result["objections"], ensure_ascii=False, indent=1) + "\n```\n")
    else:
        import anthropic

        from agent.config import load_settings

        settings = load_settings()
        if not settings.llm_enabled:
            print("LLM mode needs ANTHROPIC_API_KEY (in .env or the environment).", file=sys.stderr)
            return 2
        client = anthropic.Anthropic(api_key=settings.api_key)
        result = run_llm(client, settings.model, args.batch_id, poll_seconds=args.poll_seconds)
        meter = result["meter"]
        cost_note = (f"Batch `{result['batch_id']}` on `{settings.model}`. "
                     f"{meter.summary().replace('This analysis', 'The whole eval')} (Batches API: 50% off.)")
        text = (render_markdown(result["claude"], f"Eval results: Claude extraction ({settings.model})", [cost_note])
                + "\n" + render_markdown(result["hybrid"], "Eval results: hybrid (regex flags + Claude flags)"))
    print(text)
    if args.write:
        args.write.write_text(text, encoding="utf-8")
        print(f"Wrote {args.write}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
