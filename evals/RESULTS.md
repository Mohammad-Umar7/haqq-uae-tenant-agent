# Eval results: deterministic pipeline (no LLM)

11 synthetic Dubai contracts (`evals/cases.json`). Regenerate with `python -m evals.run_eval --write evals/RESULTS.md`.

| Metric | Score | Count |
|---|---|---|
| Verdict accuracy (within cap / notice on time / delivery valid) | 100.0% | 21/21 |
| Rent band accuracy (Decree No. 43 of 2013) | 100.0% | 10/10 |
| Red-flag precision | 92.3% | 12/13 |
| Red-flag recall | 80.0% | 12/15 |
| Terms accuracy (annual rent, end date) | 100.0% | 22/22 |

## Per case

| Case | Result |
|---|---|
| `c01_marina_band0` | all checks passed |
| `c02_jlt_band5` | all checks passed |
| `c03_deira_band10` | all checks passed |
| `c04_silicon_band15` | all checks passed |
| `c05_ranches_band20` | all checks passed |
| `c06_jvc_boundary` | all checks passed |
| `c07_bizbay_eviction_email` | all checks passed |
| `c08_downtown_eviction_notary` | all checks passed |
| `c09_alnahda_above_average` | all checks passed |
| `c10_sportscity_fair_alt_layout` | extra flag tenant_bears_all_maintenance |
| `c11_hard_wording` | missed flag early_termination_penalty_high; missed flag non_refundable_deposit; missed flag rent_increase_any_time |

## Notes

- The calculators (rent band, notice dates, delivery method) are plain Python, so with correctly extracted terms their verdicts are exact.
- The red-flag scan is keyword rules. `c11_hard_wording` paraphrases three red flags ("security amount will not be given back", "revise the rental amount whenever it sees fit", "forfeits a sum equal to six months of rent") and the rules miss all three: that is the recall gap.
- `c10` says "The Tenant is responsible for minor maintenance only; structural repairs ... remain the Landlord's responsibility". The maintenance rule sees *tenant ... responsible ... structural* and flags it: that is the precision gap.
- The LLM mode (`--mode llm`, Message Batches API, 50% price) measures how much Claude's reading closes these gaps. It has not been run yet: it needs an API key.
