# Critic eval: does the landlord's lawyer catch planted mistakes? (`claude-haiku-5-5`)

Draft letters for the sample case with one planted problem each (`evals/critic_cases.json`). Regenerate with `python -m evals.run_eval --mode critic --write evals/CRITIC_RESULTS.md`. The whole critic eval cost about $0.012 (48,512 tokens, 0% from cache).

## The Critic as shipped: automatic check + Claude

Planted problems caught: **5/5**. False alarms on the clean letter: **0**.

| Letter | Planted problem | Result |
|---|---|---|
| `k0_clean` | The correct letter (false-alarm check). | no false alarm |
| `k1_wrong_cap_amount` | Maximum rent AED 91,250 instead of AED 89,250. | caught |
| `k2_wrong_notice_date` | Notice received on 1 October 2026 instead of 1 November 2026. | caught |
| `k3_wrong_band` | Says the cap is 10% (AED 93,500) when the 15%-below band allows 5%. | caught |
| `k4_threatening_tone` | Adds a threat the landlord's lawyer could use against the tenant. | caught |
| `k5_unsupported_claim` | Claims a rule the law does not contain. | caught |

## Claude's review alone (no automatic check)

Planted problems caught: **5/5**. False alarms on the clean letter: **0**.

| Letter | Planted problem | Result |
|---|---|---|
| `k0_clean` | The correct letter (false-alarm check). | no false alarm |
| `k1_wrong_cap_amount` | Maximum rent AED 91,250 instead of AED 89,250. | caught |
| `k2_wrong_notice_date` | Notice received on 1 October 2026 instead of 1 November 2026. | caught |
| `k3_wrong_band` | Says the cap is 10% (AED 93,500) when the 15%-below band allows 5%. | caught |
| `k4_threatening_tone` | Adds a threat the landlord's lawyer could use against the tenant. | caught |
| `k5_unsupported_claim` | Claims a rule the law does not contain. | caught |

## What the Critic said (Claude only)

```json
{
 "k0_clean": [
  {
   "kind": "missing_point",
   "severity": "high",
   "quote": "I received your notice on 1 November 2026, 74 days before the contract expires; the rules require at least 90 days' written notice.",
   "problem": "The strongest point is that the notice was late: the last valid date was 16 October 2026, so the notice was 16 days late. The letter never states the deadline or says a late notice generally cannot be imposed at this renewal, so the landlord can treat the timing as a minor issue.",
   "fix": "State that the last valid notice date was 16 October 2026, that the notice dated 1 November 2026 was 16 days late under Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14, and that the proposed increase therefore cannot be imposed at this renewal.",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "high",
   "quote": "Dubai Decree No. 43 of 2013 (rent increase caps): The maximum rent increase depends on how far the current rent is below the RERA Rental Index average",
   "problem": "The cap is cited by a descriptive label rather than the article that contains it. The label is not a precise citation, and the letter does not cite Art. 3 for how the average is set, so the landlord can dispute the rule's source.",
   "fix": "Cite Dubai Decree No. 43 of 2013, Art. 1 for the cap bands and the 5% limit, and Dubai Decree No. 43 of 2013, Art. 3 for the RERA Rental Index as the source of the AED 100,000 average.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "high",
   "quote": "Dubai updated its rental index in 2025, so older figures may be out of date.",
   "problem": "This sentence invites the landlord to question the AED 100,000 average, which is the basis of the 5% cap. The letter also does not say which index edition or date the AED 100,000 figure comes from.",
   "fix": "Delete this sentence. Add the source and date of the AED 100,000 figure from the RERA Rental Index, using a [placeholder] if the tenant has not confirmed it.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "medium",
   "quote": "I received your notice on 1 November 2026",
   "problem": "The case file records the notice date as 1 November 2026 and the delivery method as email. It does not record when the tenant received the notice, so the letter states a receipt fact the file does not support.",
   "fix": "Replace the sentence with 'Your notice is dated 1 November 2026 and was sent by email.'",
   "source": "Critic"
  },
  {
   "kind": "tone",
   "severity": "medium",
   "quote": "I ask that the rent for the renewal stays within the legal limit set out above.",
   "problem": "This request is vague. The landlord can answer by offering a figure within the cap, such as AED 89,250, and ignore the late-notice point.",
   "fix": "State the figure: 'I ask that the rent remains at AED 85,000 for the renewal, because the notice was late. If you contend that a valid increase applies, it may not exceed 5%, which is AED 89,250 per year.'",
   "source": "Critic"
  }
 ],
 "k1_wrong_cap_amount": [
  {
   "kind": "wrong_number",
   "severity": "high",
   "quote": "so my rent is 15.0% below average and the maximum increase is 5% (AED 91,250 per year).",
   "problem": "The maximum allowed new rent is AED 89,250 (85,000 plus 5% = 4,250). AED 91,250 is wrong and would let the landlord argue the tenant accepts a higher ceiling.",
   "fix": "Replace with: \"so my rent is 15.0% below average, the maximum increase is 5% (AED 4,250 per year), and the highest rent permitted is AED 89,250 per year.\"",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "high",
   "quote": "I received your notice on 1 November 2026, 74 days before the contract expires; the rules require at least 90 days' written notice.",
   "problem": "The letter mentions lateness but does not state the consequence or the last valid date (16 October 2026). It is buried and the landlord can say the notice is merely short without conceding the increase fails.",
   "fix": "State clearly: \"The last valid notice date was 16 October 2026. Your notice of 1 November 2026 was 16 days late, so under Article 14 the rent change cannot be imposed at this renewal.\"",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "medium",
   "quote": "Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14: Unless the parties agreed otherwise, a party who wants to change any term of the tenancy contract at renewal, including the rent, must notify the other party at least 90 days before the contract expires.",
   "problem": "Cited by a paraphrase summary rather than the article's actual text, and the letter does not tie Art. 14 to the late notice in one sentence. The cap citation is also cited to a summary rather than Decree Art. 1.",
   "fix": "Cite the exact provisions: \"Dubai Decree No. 43 of 2013, Art. 1\" for the 5% cap, \"Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14\" for the 90-day notice, and \"Dubai Decree No. 43 of 2013, Art. 3\" for the RERA Rental Index basis.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "medium",
   "quote": "Dubai updated its rental index in 2025, so older figures may be out of date.",
   "problem": "This hedge undercuts the tenant's own RERA figure of AED 100,000 and gives the landlord an opening to dispute the benchmark, while the letter never says which index date was used.",
   "fix": "Remove the sentence and instead state: \"The average rent for similar units is AED 100,000 under the RERA Rental Index.\" If available, add the index edition or date from the tenant's source.",
   "source": "Critic"
  },
  {
   "kind": "tone",
   "severity": "low",
   "quote": "I ask that the rent for the renewal stays within the legal limit set out above.",
   "problem": "Vague: it does not state what the tenant wants the landlord to do, and the landlord can reply that the tenant has not proposed a figure.",
   "fix": "Replace with: \"I ask that the renewal rent not exceed AED 89,250 per year, or that the increase be withdrawn because the notice was late.\"",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "low",
   "quote": "Please reply in writing within 14 days. If we cannot agree, I may refer the matter to the Rental Disputes Settlement Centre (RDSC).",
   "problem": "The 14-day deadline and RDSC reference are not tied to any legal basis and may read as a bluff. The letter does not mention Art. 13's allowance for the tribunal to determine a fair rent.",
   "fix": "Keep the reply deadline but state: \"If we cannot agree, I may refer the matter to the Rental Disputes Settlement Centre under Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 13.\"",
   "source": "Critic"
  }
 ],
 "k2_wrong_notice_date": [
  {
   "kind": "wrong_date",
   "severity": "high",
   "quote": "I received your notice on 1 October 2026, 74 days before the contract expires",
   "problem": "The case file gives the notice date as 1 November 2026 (Sunday 01 Nov 2026), not 1 October. The letter's date is also inconsistent with its own '74 days' figure: 1 October to 14 January 2027 is about 105 days. The landlord can use this error to dispute the whole timeline.",
   "fix": "Replace with: 'I received your notice dated 1 November 2026, which is 74 days before the contract expires on 14 January 2027, and the rules require at least 90 days' written notice.'",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "high",
   "quote": "I ask that the rent for the renewal stays within the legal limit set out above.",
   "problem": "The strongest point is that the notice was late. The last valid notice date was 16 October 2026, so the increase generally cannot be imposed at this renewal. The letter never states this consequence. It also never states the concrete figures: the proposed AED 93,500 exceeds the 5% cap, which allows at most AED 89,250, and the excess is AED 4,250. The request is vague, so the landlord can ignore it.",
   "fix": "State that the notice was 16 days late, so the proposed change cannot be imposed at this renewal. Add that, in the alternative, the increase may not exceed AED 89,250 (5%), which is AED 4,250 below the proposed AED 93,500. Ask the landlord to confirm in writing that the rent stays at AED 85,000 or, at most, AED 89,250.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "high",
   "quote": "According to the RERA Rental Index the average rent for similar units is AED 100,000",
   "problem": "The AED 100,000 figure is the tenant's own input on the form. The case file does not verify it against the RERA Rental Index. The letter attributes it to the index, which the landlord can challenge. The letter's own rule summary also says the 2025 index update means older figures may be out of date, which invites the landlord to dispute the 15% band.",
   "fix": "Attribute the figure to the tenant's own check, for example 'Based on the RERA Rental Index figure I have found, the average for similar units is AED 100,000'. Attach or name the specific index entry and its date. Delete the sentence about older figures, or say the tenant will confirm the current 2025 index value before any hearing.",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "medium",
   "quote": "Dubai Decree No. 43 of 2013 (rent increase caps): The maximum rent increase depends on how far the current rent is below the RERA Rental Index average for similar units",
   "problem": "The cap is set by Art. 1 of Decree No. 43 of 2013, but the letter cites only the generic label and paraphrases a summary rather than the article. The paraphrase also includes bands the letter does not need, which weakens the point. The Art. 3 link to the RERA index is also not cited for the average-rent claim.",
   "fix": "Cite 'Dubai Decree No. 43 of 2013, Art. 1' for the caps, quoting only the 11%-20% band that applies (maximum 5%). Cite 'Dubai Decree No. 43 of 2013, Art. 3' for the RERA Rental Index as the source of the average.",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "medium",
   "quote": "Unless the parties agreed otherwise, a party who wants to change any term of the tenancy contract at renewal, including the rent, must notify the other party at least 90 days before the contract expires.",
   "problem": "The Art. 14 rule starts with 'Unless the parties agreed otherwise'. Clause 8 of the contract lets the landlord raise rent at any time on 30 days' notice. The landlord can argue that clause displaces the 90-day rule. The letter does not address clause 8 at all.",
   "fix": "Add a sentence stating that clause 8 cannot override the statutory cap in Decree No. 43 of 2013, Art. 1, and that the 90-day notice requirement of Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14 applies to this renewal. Do not claim a legal rule beyond what these articles say.",
   "source": "Critic"
  },
  {
   "kind": "tone",
   "severity": "low",
   "quote": "I would prefer to settle this amicably and I am happy to discuss it. Please reply in writing within 14 days. If we cannot agree, I may refer the matter to the Rental Disputes Settlement Centre (RDSC).",
   "problem": "The 14-day reply deadline has no stated consequence, and the RDSC referral is unclear. It is not in the case file, so the letter should not describe its procedure. The landlord can ignore a vague reply window.",
   "fix": "Keep the amicable tone. Give a specific reply date, for example 14 days from the letter date. Say 'If we cannot agree on the renewal rent, I may seek a determination through the appropriate Dubai dispute body' unless the RDSC is confirmed in the case file.",
   "source": "Critic"
  }
 ],
 "k3_wrong_band": [
  {
   "kind": "wrong_number",
   "severity": "high",
   "quote": "so my rent is 15.0% below average and the maximum increase is 10% (AED 93,500 per year).",
   "problem": "The case file puts the rent 15.0% below average in the 11-20% band, so the maximum increase is 5%, or AED 4,250, giving a maximum new rent of AED 89,250. The letter states 10% and AED 93,500, which is the landlord's proposed rent, not a legal maximum. This wrongly concedes the landlord's figure.",
   "fix": "Replace with: 'so my rent is 15.0% below average, which falls in the 11-20% band. The maximum increase is 5%, which is AED 4,250, giving a maximum new annual rent of AED 89,250.'",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "high",
   "quote": "I received your notice on 1 November 2026, 74 days before the contract expires",
   "problem": "The strongest point is buried. The notice was late: the last valid date was 16 October 2026, so it is 16 days late. The letter never says that a late notice generally means the change cannot be imposed at this renewal, so the landlord can read the date as a minor detail.",
   "fix": "State the conclusion directly: 'The last valid notice date was 16 October 2026. Your notice, dated 1 November 2026, was 16 days late, so the proposed increase cannot be imposed at this renewal.' Put this point before the cap argument.",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "medium",
   "quote": "the rules require at least 90 days' written notice.",
   "problem": "The 90-day notice requirement is stated with no citation. A landlord can dismiss an uncited rule.",
   "fix": "Cite it: 'Under Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14, unless the parties agreed otherwise, a party wishing to amend terms at renewal must notify the other party at least 90 days before the contract expires.'",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "medium",
   "quote": "Dubai Decree No. 43 of 2013 (rent increase caps)",
   "problem": "The cap rule is cited by a summary label, not by the article. The bands in the letter are paraphrased from the summary, not the article text, so the landlord can challenge whether the letter quotes the law accurately.",
   "fix": "Cite the article directly: 'Dubai Decree No. 43 of 2013, Art. 1', and for the index reference cite 'Dubai Decree No. 43 of 2013, Art. 3'. Keep the bands only if they are attributed to Art. 1.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "medium",
   "quote": "Dubai updated its rental index in 2025, so older figures may be out of date.",
   "problem": "This sentence undermines the tenant's own AED 100,000 average. It invites the landlord to dispute the figure and gives no source for the average. The letter does not show the figure comes from the current RERA index.",
   "fix": "Remove the 2025 sentence. Instead state the source: 'The average rent for similar units is AED 100,000 according to the RERA Rent Index (Dubai Decree No. 43 of 2013, Art. 3). If you hold a different figure from the current index, please share it.'",
   "source": "Critic"
  },
  {
   "kind": "tone",
   "severity": "low",
   "quote": "I ask that the rent for the renewal stays within the legal limit set out above.",
   "problem": "The request is vague. It does not name the amount, so the landlord can reply that they will 'consider it' without committing to a figure.",
   "fix": "Make the request specific: 'I ask that the renewal rent does not exceed AED 89,250 per year, or that the increase be withdrawn because the notice was late.'",
   "source": "Critic"
  }
 ],
 "k4_threatening_tone": [
  {
   "kind": "tone",
   "severity": "high",
   "quote": "If you do not withdraw this increase within 3 days I will report you to the police and post about you on social media.",
   "problem": "This threat is emotional and not grounded in any law in the case file. The landlord's lawyer can use it to paint the tenant as the aggressor and to ignore the valid rent-cap and notice points. Nothing in the file supports a 3-day deadline or a police report for this dispute.",
   "fix": "Delete the sentence. Replace it with a neutral statement, such as: Please confirm in writing that the proposed increase is withdrawn, or that the renewal rent will not exceed AED 89,250 per year.",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "high",
   "quote": "I received your notice on 1 November 2026, 74 days before the contract expires; the rules require at least 90 days' written notice.",
   "problem": "The late notice is the tenant's strongest point, but the letter only lists it as a fact and never states its consequence. The case file says a change notified this late generally cannot be imposed at this renewal, and the last valid notice date was 16 October 2026. Without that conclusion, the landlord can treat the notice point as a minor side issue.",
   "fix": "State the consequence directly: the notice was given on 1 November 2026, which is 16 days after the last valid date of 16 October 2026, so under Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14 the increase cannot be applied at this renewal.",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "medium",
   "quote": "the rules require at least 90 days' written notice.",
   "problem": "This legal point has no citation. The text of Art. 14 in the case file says 'no less than ninety (90) days' and does not mention writing. The word 'written' goes beyond the cited article, which the landlord's lawyer can challenge.",
   "fix": "Cite the article and match its wording: Under Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14, a party wishing to amend a term must notify the other party no less than ninety (90) days before the lease expires. Remove 'written' unless you also cite a source for it.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "medium",
   "quote": "Dubai updated its rental index in 2025, so older figures may be out of date.",
   "problem": "This sentence undermines the letter's main figure. The letter relies on an average of AED 100,000 for the 15.0% calculation, and this sentence tells the landlord that the figure may be outdated. The case file does not confirm the current index value for this unit, so the 5% cap rests on an unverified number.",
   "fix": "Before sending, check the current RERA Rental Index figure for comparable 1BR units in Marina Crest Tower or Dubai Marina. If the figure is confirmed, cite it as the source and delete this sentence. If it cannot be confirmed, say the calculation is based on the RERA average of AED 100,000 and ask the landlord to provide the index figure he relies on.",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "medium",
   "quote": "I ask that the rent for the renewal stays within the legal limit set out above.",
   "problem": "The demand is vague. It does not say what rent the tenant accepts, so the landlord can reply that the tenant is 'within the limit' by offering a figure above AED 89,250, or can simply ignore the request. The case file gives a specific cap of AED 89,250 per year.",
   "fix": "Replace the sentence with a specific request: I ask that you withdraw the proposed rent of AED 93,500 and confirm in writing that the renewal rent will not exceed AED 89,250 per year, which is the 5% maximum allowed under Dubai Decree No. 43 of 2013, Art. 1.",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "low",
   "quote": "the maximum increase is 5% (AED 89,250 per year).",
   "problem": "The cap is stated in the facts section with no citation. The rules section cites the case-file summary label 'Dubai Decree No. 43 of 2013 (rent increase caps)' rather than the article itself, so the legal basis for the 5% figure is weaker than it needs to be.",
   "fix": "Add the article to the facts sentence: the maximum increase is 5% (AED 89,250 per year) under Dubai Decree No. 43 of 2013, Art. 1, and in the rules section cite Dubai Decree No. 43 of 2013, Art. 1 instead of the summary label.",
   "source": "Critic"
  }
 ],
 "k5_unsupported_claim": [
  {
   "kind": "unsupported_claim",
   "severity": "high",
   "quote": "The law forbids any rent increase for a tenant who has lived in the property for more than one year.",
   "problem": "No article in allowed_citations or the case file says this. The landlord's lawyer can dismiss it at once, and it weakens the letter's credibility on the points that are supported.",
   "fix": "Delete this bullet. Rely only on the late-notice point under Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14 and the cap under Dubai Decree No. 43 of 2013, Art. 1.",
   "source": "Critic"
  },
  {
   "kind": "missing_point",
   "severity": "high",
   "quote": "I received your notice on 1 November 2026, 74 days before the contract expires; the rules require at least 90 days' written notice.",
   "problem": "The strongest point, that the notice is 16 days late and so the increase generally cannot apply at this renewal, is buried in a fact list and never stated as a conclusion. The last valid notice date (16 October 2026) is not given.",
   "fix": "State it directly: the contract expires on 14 January 2027, so notice had to be given by 16 October 2026 under Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14. Your notice dated 1 November 2026 was 16 days late, so the proposed increase cannot be imposed at this renewal.",
   "source": "Critic"
  },
  {
   "kind": "weak_citation",
   "severity": "high",
   "quote": "Dubai Decree No. 43 of 2013 (rent increase caps): The maximum rent increase depends on how far the current rent is below the RERA Rental Index average for similar units: up to 10% below = no increase; 11-20% below = max 5%; 21-30% below = max 10%; 31-40% below = max 15%; more than 40% below = max 20%.",
   "problem": "The letter cites an internal summary, not the article. The 5% cap should be tied to Art. 1 and its band, and the Art. 3 index reference is missing. The Law No. 26 citation is also cited only by a summary.",
   "fix": "Cite Dubai Decree No. 43 of 2013, Art. 1 for the 11-20% band and 5% cap, and Dubai Decree No. 43 of 2013, Art. 3 for the RERA Rental Index. Quote or paraphrase the article text, not the summary.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "medium",
   "quote": "According to the RERA Rental Index the average rent for similar units is AED 100,000",
   "problem": "The letter states the index figure as fact, but the landlord may ask for the source. The case file gives only the figure the tenant entered, with no index date or extract.",
   "fix": "Name the index edition and date used, or attach a copy of the RERA Rental Index extract for the building or area, and say that you will provide it on request.",
   "source": "Critic"
  },
  {
   "kind": "unsupported_claim",
   "severity": "medium",
   "quote": "Dubai updated its rental index in 2025, so older figures may be out of date.",
   "problem": "This sentence in the rules section invites the landlord to dispute the AED 100,000 average and weakens the 15% and 5% calculation that depends on it. It is a caveat, not a point for the tenant.",
   "fix": "Delete this sentence. If the index date is uncertain, confirm the current RERA figure before sending and state the date used in the letter.",
   "source": "Critic"
  }
 ]
}
```
