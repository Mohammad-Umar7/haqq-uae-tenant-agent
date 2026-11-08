# Haqq: Dubai Tenant Rights Agent

> **Not legal advice.** Haqq quotes the official English translation of Dubai's tenancy law (the Arabic text prevails) and can be wrong or out of date. Verify with RERA or the Rental Disputes Settlement Centre (RDSC) before you act on anything it says.

*Haqq* (حق) means "right" in Arabic. Working name of the project: "UAE Tenant Rights Agent". The law it knows is **Dubai's**; other emirates have different laws.

A tenant uploads their tenancy contract (PDF, including scanned or Arabic Ejari contracts, or pasted text) and chats with an AI agent that:

1. **Reads the contract once** into validated JSON: parties, property, annual rent, dates, security deposit, cheques, clauses.
2. **Checks a proposed rent increase** against the caps in Dubai Decree No. 43 of 2013, using the RERA Rental Index average the tenant enters.
3. **Checks notice deadlines**: 90 days to change terms (Law No. 26 of 2007 as amended, Art. 14) and 12 months via notary public or registered mail for eviction at expiry (Art. 25(2)).
4. **Scans clauses for red flags**, such as a non-refundable deposit, rent raised "at any time", an early-termination penalty above 2 months' rent, or a waiver of the right to go to the RDSC.
5. **Searches the official law text** and cites the article it relied on, with a link to the Dubai Legislation Portal. If the law doesn't cover a question, it says "I couldn't find this in the law".
6. **Drafts a polite, firm letter** to the landlord (or an RDSC complaint summary).

The **"What the agent did"** panel shows every step: each tool call with its input and output, the reasoning summary, and **what the analysis cost** ("This analysis cost about $0.0034 (31,342 tokens, 53% from cache)").

---

## Quick start

You need Python 3.11+.

**Windows (PowerShell)**

```powershell
cd uae-tenant-agent
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy .env.example .env        # optional: paste your Anthropic key into .env
.\.venv\Scripts\python.exe app.py
```

**macOS / Linux**

```bash
cd uae-tenant-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # optional: paste your Anthropic key into .env
python app.py
```

Open http://127.0.0.1:7860, click **Load sample contract**, then **Analyze my contract**.

Run the tests with `python -m pytest`, and the eval with `python -m evals.run_eval`.

> **Windows path-length gotcha.** If importing Gradio fails with `DLL load failed ... The filename or extension is too long`, the project folder is nested too deeply: some Pillow/NumPy files inside `.venv` end up over Windows' 260-character path limit. Move the project to a short path such as `C:\dev\uae-tenant-agent` and recreate the venv.

## Two modes

| | **LLM mode** (`ANTHROPIC_API_KEY` set) | **Demo mode** (no key) |
|---|---|---|
| Who decides the steps | Claude, in a tool-calling loop | A fixed script (`agent/demo.py`) |
| Reading the contract | One structured-extraction call (text or PDF) | Regex |
| Scanned / Arabic PDFs | Sent to Claude as a PDF `document` block | Not supported (paste the text) |
| Chat | Free-form questions | Law search only (BM25, no AI) |
| Cost | About $0.003 per analysis, $0 for repeats | $0 |

Demo mode lets anyone, including judges without a key, see the tools, the law search and the trace working.

### What the sample shows

`samples/sample_contract.txt` is a **fictional** Dubai Marina 1BR contract at AED 85,000/year (15 Jan 2026 to 14 Jan 2027). **Load sample contract** also fills the form with a demo scenario: RERA average AED 100,000, landlord asks AED 93,500, notice received by email on 2026-11-01. The analysis finds:

- **3 red flags**: non-refundable deposit (clause 7), rent increase "at any time" (clause 8), early-termination penalty of 4 months' rent (clause 9).
- **Rent increase**: the rent is 15% below average, so the cap is 5% (max AED 89,250); the proposed 10% exceeds it by AED 4,250.
- **Notice**: the last valid date was 16 Oct 2026, so the notice is 16 days late.
- **The law relied on**, quoted from the official text: Decree No. 43 of 2013 Art. 1 and 3, Law No. 26 of 2007 (as amended) Art. 13, 14 and 20.
- **A draft letter** to the landlord.

## Architecture

```mermaid
flowchart LR
    U([Tenant]) -->|upload, form, chat| UI[Gradio UI<br/>app.py]
    UI --> RT[One turn<br/>agent/runner.py]
    RT -->|no key| D[Demo pipeline<br/>agent/demo.py]
    RT <-->|same request seen?| RC[(Local result cache<br/>.haqq_cache/)]
    RT -->|first turn only| X[Read contract once<br/>agent/extraction.py<br/>structured outputs]
    X <-->|text or PDF document block| C[(Claude API<br/>Haiku 5.5)]
    RT --> L[Agent loop<br/>agent/loop.py<br/>cost meter + spend guard]
    L <-->|cached tools + system,<br/>tool_use / tool_result| C
    L -->|run requested tool| R[Tool registry<br/>agent/registry.py]
    D -->|fixed order| R
    R --> T1[extract_contract_terms]
    R --> T2[clause_risk_scan]
    R --> T3[rera_rent_increase_check]
    R --> T4[notice_deadline_check]
    R --> T5[search_tenancy_law]
    R --> T6[draft_letter]
    T5 --> BM[BM25 index<br/>agent/law_search.py]
    BM --> LAW[(Official law text<br/>law_articles.json)]
    T2 & T3 & T4 & T5 & T6 --> KB[(Plain-English notes<br/>tenant_rules.json)]
```

- **Claude decides, Python executes.** Claude only *requests* tool calls; `agent/registry.py` validates the arguments and runs plain Python functions. No tool calls an LLM.
- **Read once.** The contract (text, or the PDF itself when it's scanned or Arabic) goes to Claude once, in a call constrained by a JSON schema. The agent loop then works from that compact JSON, cross-checked against the regex reader.
- **Agentic RAG with no extra API cost.** `search_tenancy_law` runs BM25 keyword search locally over the official article text. The agent decides when to search and must cite what it found.
- **The math is Python.** Rent bands, notice dates and amounts are computed by unit-tested code, never by the model.
- **Contract text is data.** It reaches Claude only inside the extraction request and as tool results, never as instructions. Text addressed to "the AI" is reported, not followed.

### Project layout

```
uae-tenant-agent/
├── app.py                     Gradio UI and event wiring
├── agent/
│   ├── config.py              env vars: key, model, effort, limits, cache
│   ├── runner.py              one turn: limits -> result cache -> read once -> loop
│   ├── extraction.py          read-the-contract-once (JSON schema + Pydantic)
│   ├── loop.py                the Claude tool-use loop (cost meter, spend guard)
│   ├── cost.py                usage -> USD (price table, cache multipliers)
│   ├── result_cache.py        free repeats (hash -> JSON file)
│   ├── law_search.py          BM25 over the official law text
│   ├── demo.py                no-key fixed pipeline + report
│   ├── registry.py            tool schemas for Claude + validated dispatcher
│   ├── prompts.py             frozen system prompt and per-turn user message
│   ├── tools/                 one file per tool (pure Python)
│   ├── knowledge.py           plain-English rule notes
│   ├── knowledge_base/
│   │   ├── law_articles.json  official law text, one record per article
│   │   └── tenant_rules.json  plain-English notes linked to articles
│   ├── contract.py            PDF/TXT loading, upload limits, clause splitting
│   ├── dates.py, inputs.py, trace.py
├── evals/                     synthetic contracts, expected values, scorer, RESULTS.md
├── scripts/
│   ├── build_law_corpus.py    official PDFs -> law_articles.json
│   └── estimate_cost.py       cost model for one analysis
├── tests/                     pytest suite (fakes for Claude; no network, no key)
├── samples/sample_contract.txt
├── requirements.txt / requirements-dev.txt
├── .env.example, .gitignore
├── README.md, LEARNING.md
```

## The law corpus

`agent/knowledge_base/law_articles.json` holds the **official English text**, split into one record per article (law, article number, heading, text, citation, source URL). It was built by `scripts/build_law_corpus.py` from these PDFs on the Dubai Legislation Portal (Supreme Legislation Committee):

| Law | Source |
|---|---|
| Law No. (26) of 2007 Regulating the Relationship between Landlords and Tenants in the Emirate of Dubai | [dlp.dubai.gov.ae](https://dlp.dubai.gov.ae/Legislation%20Reference/2007/Law%20No.%20(26)%20of%202007.pdf) |
| Law No. (33) of 2008 Amending Law No. (26) of 2007 | [dlp.dubai.gov.ae](https://dlp.dubai.gov.ae/Legislation%20Reference/2009/Law%20No.%20(33)%20of%202008%20Amending%20Law%20No.%20(26)%20of%202007.pdf) |
| Decree No. (43) of 2013 Determining Rent Increase for Real Property in the Emirate of Dubai | [dlp.dubai.gov.ae](https://dlp.dubai.gov.ae/Legislation%20Reference/2013/Decree%20No.%20(43)%20of%202013%20Determining%20Rent%20Increase%20for%20Real%20Property.pdf) |

- The text is copied as printed. The only edits repair PDF-extraction artifacts (for example "specifi c" becomes "specific"); the list is stored in the JSON under `text_cleanup`. Official typos such as "will must surrender" (Art. 21) are kept.
- Law No. 33 of 2008 **replaced** 11 articles of the 2007 law (2, 3, 4, 9, 13, 14, 15, 25, 26, 29, 36). Both versions are stored, but the 2007 wording is marked `in_force: false` and is **never searchable**. This matters: the 2007 Art. 25 gave 90 days' eviction notice, while the current text requires 12 months.
- Each PDF's SHA-256 is recorded. To rebuild: `python scripts/build_law_corpus.py --download downloads/law_pdfs`.
- `tenant_rules.json` keeps seven short plain-English notes (rule ids used by the calculators and letters), each linked to the official articles it explains.

## Market rent data: investigated, not included

We wanted a `market_rent_lookup(area, bedrooms)` tool built from real Ejari rent contracts (median annual rent by area and bedroom count, labelled as an estimate next to the official RERA calculator). We only wanted data anyone can download without an account, and we did not want to make numbers up. On 8 November 2026 we found:

| Source | What happened |
|---|---|
| Dubai Pulse dataset `dld_rent_contracts-open` (`https://www.dubaipulse.gov.ae/data/dld-registration/dld_rent_contracts-open`) | Redirects to the Data Dubai home page (`https://data.dubai/en/`). The dataset page no longer exists there. The old `https://www.dubaipulse.gov.ae/dataset/...` download paths reset the connection. |
| Data Dubai (`https://data.dubai/en/data-and-statistics`) | No rent-contract or Ejari dataset listed. Its "Request Data" route is a form, and the data exchange is "restricted to government entities and authorized users only". |
| DLD open data, Rents tab (`https://dubailand.gov.ae/en/open-data/real-estate-data/`) | The data comes from `https://gateway.dubailand.gov.ae/open-data/...`, but the page has Google reCAPTCHA switched on (`captchaEnabled: true`) and sends each query through a CAPTCHA proxy. A person can search and export it in a browser; a script would have to get around the CAPTCHA, which we won't do. |
| `https://data.dubailand.gov.ae/` (named by third-party sites) | Did not connect. |

So Haqq has no market data, and the RERA average still comes from the tenant, who gets it from the official DLD/RERA Rental Index calculator (the source of truth). If DLD publishes a bulk download again, the plan above still stands: aggregate recent contracts into a small CSV with the source URL, download date and row counts, and label every figure "DLD Ejari median, an estimate; the official RERA calculator is the source of truth".

## Cost & efficiency

Default model: **Claude Haiku 5.5** ($0.10 per million input tokens, $0.50 per million output tokens, for prompts up to 100K tokens). Estimated cost of one full "Analyze my contract" run on the sample, from `python scripts/estimate_cost.py`:

| Scenario | Cost per analysis |
|---|---|
| Prompt caching on, first run (as shipped) | **about $0.0034** (31K tokens, 53% of the prompt from cache) |
| Tools + system prompt already cached by an earlier user | about $0.0031 |
| Prompt caching off (for comparison) | about $0.0045 |
| The same request again (local result cache) | **$0.00** |

That is roughly 290 analyses per US dollar. How the estimate is made: the script builds the real payloads for the sample (tool schemas, system prompt, extraction request, and the actual tool outputs), converts characters to tokens at about 3 characters per token, assumes typical output lengths per round (thinking included), and prices a 4-round run with the same code as the in-app meter. It is an estimate: with a key, `--count-tokens` uses the free token-counting endpoint for exact prompt sizes, and the trace panel shows the real cost of every run.

What keeps it cheap:

| Lever | Where | Effect |
|---|---|---|
| Low-cost model | `CLAUDE_MODEL=claude-haiku-5-5` | Opus 5.5 would cost 40x more per token |
| Prompt caching | `agent/loop.py`, `agent/prompts.py` | tools + system and earlier rounds are re-read at 10% of the price |
| Read the contract once | `agent/extraction.py` | the contract (or PDF pages) is sent once, not in every round |
| Local search | `agent/law_search.py` | BM25 on your machine: no embeddings API |
| Math in Python | `agent/tools/` | no tokens spent on arithmetic, and no arithmetic mistakes |
| Free repeats | `agent/result_cache.py` | identical requests make no API call |
| Batch evals | `evals/run_eval.py --mode llm` | Message Batches API at 50% of the price |

**Spend guards for a public deploy**: a maximum cost per analysis (`HAQQ_MAX_COST_PER_RUN_USD`, default $0.05, about 15x a normal run; the loop stops gracefully and shows what it had), a per-session question limit (`HAQQ_MAX_REQUESTS_PER_SESSION`, default 20), an upload size limit (`HAQQ_MAX_UPLOAD_MB`, default 5 MB), a page limit (`HAQQ_MAX_PDF_PAGES`, default 10), and the existing tool-round limit (`AGENT_MAX_TOOL_ROUNDS`, default 10). Every run's usage and cost is also written to the log (`haqq.cost`).

## Evals

`evals/` holds 11 synthetic Dubai contracts (fictional names). They cover every rent band, the exact 10%-below boundary, a rent above average, late and on-time notices, eviction by email vs through a notary, and contracts with and without each red flag. One contract states its red flags in unusual wording, and one has a fair clause written to look risky. Expected values were worked out by hand.

`python -m evals.run_eval` scores the deterministic pipeline ([`evals/RESULTS.md`](evals/RESULTS.md)):

| Metric | Deterministic pipeline |
|---|---|
| Verdict accuracy (within cap / notice on time / delivery valid) | 100% (21/21) |
| Rent band accuracy | 100% (10/10) |
| Red-flag precision | 92.3% (12/13) |
| Red-flag recall | 80.0% (12/15) |

The misses are the keyword scan's known limits: it can't see red flags in paraphrased wording, and it flags one fair maintenance clause. `python -m evals.run_eval --mode llm` sends the same cases through Claude's read-once extraction as one Message Batches request (50% cheaper) and scores Claude alone and Claude plus regex. It needs an API key and has not been run yet.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Enables LLM mode. Without it the app runs in demo mode. |
| `CLAUDE_MODEL` | `claude-haiku-5-5` | Low-cost default. `claude-sonnet-5-5` or `claude-opus-5-5` reason better and cost more. |
| `CLAUDE_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max`: how much the model thinks. |
| `AGENT_MAX_TOOL_ROUNDS` | `10` | Safety limit on tool-call rounds per question. |
| `HAQQ_MAX_COST_PER_RUN_USD` | `0.05` | Stop an analysis once it has cost this much. |
| `HAQQ_MAX_REQUESTS_PER_SESSION` | `20` | LLM questions allowed per browser session (cache hits are free). |
| `HAQQ_MAX_UPLOAD_MB` / `HAQQ_MAX_PDF_PAGES` | `5` / `10` | Upload limits. |
| `HAQQ_CACHE_ENABLED` / `HAQQ_CACHE_DIR` | `true` / `.haqq_cache` | Free repeats. The folder is git-ignored. |

The loop uses adaptive thinking with summarised reasoning (shown in the trace) and prompt caching. It is written for current Claude models (Haiku 5.5, Sonnet 5.5, Opus 5.5); older models such as Haiku 4.5 don't accept these request settings.

## Deploying to Hugging Face Spaces (later)

Not deployed yet. When you're ready:

1. Create a Gradio Space, then push this folder (without `.env` and `.haqq_cache/`).
2. Put this block at the very top of `README.md` (Spaces reads it as config):
   ```yaml
   ---
   title: Haqq - Dubai Tenant Rights Agent
   sdk: gradio
   sdk_version: 6.31.0
   python_version: "3.11"
   app_file: app.py
   ---
   ```
3. In the Space settings, add `ANTHROPIC_API_KEY` as a **secret**. Without it the Space runs in demo mode. Review the spend guards above before making the Space public.

## Limitations

- Dubai only. The law corpus covers Law No. 26 of 2007 (as amended by Law No. 33 of 2008) and Decree No. 43 of 2013, not the RDSC's own decree, RERA circulars or court practice. For anything else the agent says it couldn't find it in the law.
- The English text is an official translation; the Arabic text prevails.
- Keyword search finds articles that share words with the question; a question phrased very differently from the law can miss. The agent can search again with other words.
- Demo mode reads only PDFs with a text layer. LLM mode reads scanned and Arabic PDFs, but those code paths are tested only with a fake Claude so far.
- The red-flag scan is keyword-based (see Evals). In LLM mode Claude also reads every clause.
- Not a substitute for RERA, the RDSC or a lawyer.

---

**Disclaimer:** Not legal advice. This project is a student prototype built for the Decoding Data Science "Building Agentic AI Application" challenge (Dubai, November 2026). All names in the sample and eval contracts are fictional.
