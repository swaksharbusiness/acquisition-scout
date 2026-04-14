# agents/loan_feasibility.py
# Loan Feasibility Agent — fourth in the pipeline
# Designed for minimum token usage — deterministic tasks run in pure Python
# Only genuine reasoning tasks are sent to the LLM
#
# Three functions:
#   _deal_math_check()      — pure Python arithmetic (no LLM)
#   _deterministic_screen() — pure Python logic (no LLM)
#   _ai_enrichment()        — LLM only for: goodwill assessment,
#                             eligibility nuance, broker questions,
#                             what a real determination needs
#
# Pipeline position: 4 of 6
# Receives from: Scout Agent
# Passes to: Validator Agent

import json
import os
import sys
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg

client = OpenAI(api_key=cfg.OPENAI_API_KEY)

# ─────────────────────────────────────────────
# Ineligible keyword list for fuzzy matching
# Broader than cfg.SBA_INELIGIBLE_BUSINESS_TYPES to catch
# natural language variants in listing signals
# ─────────────────────────────────────────────
_INELIGIBLE_KEYWORDS = [
    "gambling",
    "casino",
    "lottery",
    "lending",
    "hard money",
    "mortgage broker",
    "insurance compan",
    "real estate invest",
    "property invest",
    "passive invest",
    "non-profit",
    "nonprofit",
    "not-for-profit",
    "religious",
    "church",
    "pyramid",
    "speculative",
    "government-owned",
    "government owned",
    "municipally owned",
]


def _is_likely_ineligible(business_type: str) -> tuple:
    """
    Fuzzy keyword match against SBA ineligible business types.
    Returns (is_ineligible: bool, matched_keyword: str or None)
    Conservative — only flags clear matches.
    Ambiguous cases pass through to AI for nuanced assessment.
    """
    if not business_type:
        return False, None

    bt_lower = business_type.lower()
    for kw in _INELIGIBLE_KEYWORDS:
        if kw in bt_lower:
            return True, kw
    return False, None


def _deal_math_check(asking_price, deal_math: dict) -> dict:
    """
    Pure Python arithmetic — no LLM.
    Runs whenever asking price is known.
    Returns structured deal math with all calculations shown.
    """

    if asking_price is None:
        return {
            "asking_price": None,
            "standard_down_payment": None,
            "high_goodwill_down_payment": None,
            "loan_amount_standard": None,
            "loan_amount_high_goodwill": None,
            "buyer_can_cover_standard": None,
            "buyer_can_cover_high_goodwill": None,
            "within_standard_max_deal": None,
            "within_high_goodwill_max_deal": None,
            "deal_math_note": (
                "Asking price not visible in listing signal. "
                "Cannot calculate down payment. "
                "Ask broker for asking price before proceeding."
            )
        }

    liquid = deal_math["liquid_cash"]
    standard_max = deal_math["standard_max_deal"]
    goodwill_max = deal_math["high_goodwill_max_deal"]

    standard_down = round(
        asking_price * cfg.SBA_STANDARD_DOWN_PAYMENT_PCT, 2
    )
    goodwill_down = round(
        asking_price * cfg.SBA_HIGH_GOODWILL_DOWN_PAYMENT_PCT, 2
    )
    loan_standard = round(asking_price - standard_down, 2)
    loan_goodwill = round(asking_price - goodwill_down, 2)

    within_standard = asking_price <= standard_max
    within_goodwill = asking_price <= goodwill_max
    can_cover_standard = liquid >= standard_down
    can_cover_goodwill = liquid >= goodwill_down

    # Build plain English note
    if within_standard and can_cover_standard:
        note = (
            f"Asking price ${asking_price:,.0f} fits standard SBA deal math. "
            f"Down payment required: ${standard_down:,.0f} (10%). "
            f"Buyer has ${liquid:,.0f} liquid — can cover. "
            f"SBA loan amount: ${loan_standard:,.0f}."
        )
    elif not within_standard and within_goodwill and can_cover_goodwill:
        note = (
            f"Asking price ${asking_price:,.0f} exceeds standard max "
            f"(${standard_max:,.0f}) but fits high-goodwill scenario "
            f"(${goodwill_max:,.0f} max at 15% down). "
            f"Down payment required: ${goodwill_down:,.0f} (15%). "
            f"Buyer has ${liquid:,.0f} liquid — can cover under high-goodwill terms. "
            f"Confirm goodwill breakdown with broker."
        )
    elif not within_standard and not within_goodwill:
        note = (
            f"Asking price ${asking_price:,.0f} exceeds both standard max "
            f"(${standard_max:,.0f}) and high-goodwill max "
            f"(${goodwill_max:,.0f}). "
            f"Buyer cannot cover down payment with ${liquid:,.0f} liquid alone. "
            f"Seller financing could bridge the gap — ask broker."
        )
    else:
        note = (
            f"Asking price ${asking_price:,.0f} — buyer may not have "
            f"sufficient liquid cash (${liquid:,.0f}) to cover down payment. "
            f"Explore seller financing with broker."
        )

    return {
        "asking_price": asking_price,
        "standard_down_payment": standard_down,
        "high_goodwill_down_payment": goodwill_down,
        "loan_amount_standard": loan_standard,
        "loan_amount_high_goodwill": loan_goodwill,
        "buyer_can_cover_standard": can_cover_standard,
        "buyer_can_cover_high_goodwill": can_cover_goodwill,
        "within_standard_max_deal": within_standard,
        "within_high_goodwill_max_deal": within_goodwill,
        "deal_math_note": note
    }


def _deterministic_screen(lead: dict, deal_math: dict, dm_result: dict) -> dict:
    """
    Pure Python decision tree — no LLM.
    Handles all checks that are arithmetic or rule-based:
      - SBA ineligible list check (fuzzy keyword match)
      - Years in business minimum check
      - Price ceiling check
      - Preliminary screen pass/fail/unknown determination

    Returns a structured screen result and a list of
    reasons so the AI enrichment step has context.
    """

    snapshot = lead.get("business_snapshot", {})
    business_type = snapshot.get("business_type") or ""
    years = snapshot.get("years_in_business")
    asking_price = snapshot.get("asking_price")

    fail_reasons = []
    pass_signals = []
    unknown_reasons = []

    # ── Check 1: SBA ineligible list ──────────────────────
    is_ineligible, matched_kw = _is_likely_ineligible(business_type)
    if is_ineligible:
        fail_reasons.append(
            f"Business type '{business_type}' matches SBA ineligible "
            f"keyword '{matched_kw}'"
        )
        ineligible_result = "ineligible"
        ineligible_basis = (
            f"Keyword match: '{matched_kw}' found in business type. "
            f"SBA explicitly excludes this category."
        )
    elif not business_type:
        unknown_reasons.append("Business type not disclosed in listing signal")
        ineligible_result = "unknown"
        ineligible_basis = "Business type not available — cannot screen"
    else:
        ineligible_result = "likely_eligible"
        ineligible_basis = (
            f"No ineligible keywords found in '{business_type}'. "
            f"AI will assess nuance."
        )
        pass_signals.append(f"Business type '{business_type}' not flagged as ineligible")

    # ── Check 2: Years in business ─────────────────────────
    if years is not None:
        if years >= cfg.SBA_MIN_YEARS_IN_BUSINESS:
            years_ok = True
            years_basis = (
                f"{years} years in business confirmed — "
                f"meets SBA minimum of {cfg.SBA_MIN_YEARS_IN_BUSINESS} years"
            )
            pass_signals.append(years_basis)
        else:
            years_ok = False
            fail_reasons.append(
                f"Only {years} years in business — "
                f"SBA requires minimum {cfg.SBA_MIN_YEARS_IN_BUSINESS} years"
            )
            years_basis = (
                f"{years} years in business — "
                f"below SBA minimum of {cfg.SBA_MIN_YEARS_IN_BUSINESS} years"
            )
    else:
        years_ok = None
        years_basis = "Years in business not disclosed in listing signal"
        unknown_reasons.append("Years in business unknown")

    # ── Check 3: Price ceiling ─────────────────────────────
    price_within_any_budget = (
        dm_result.get("within_standard_max_deal") or
        dm_result.get("within_high_goodwill_max_deal")
    )
    price_known = asking_price is not None

    if price_known and price_within_any_budget is False:
        fail_reasons.append(
            f"Asking price ${asking_price:,.0f} exceeds buyer's maximum "
            f"deal size under both standard and high-goodwill SBA scenarios"
        )
    elif price_known and price_within_any_budget:
        pass_signals.append(
            f"Asking price ${asking_price:,.0f} within buyer's SBA deal ceiling"
        )
    else:
        unknown_reasons.append("Asking price unknown — cannot verify price ceiling")

    # ── Preliminary screen determination ───────────────────
    if fail_reasons:
        screen = "fail"
        screen_reason = " | ".join(fail_reasons)
    elif unknown_reasons and not pass_signals:
        screen = "unknown"
        screen_reason = (
            "Insufficient signal to screen: " + " | ".join(unknown_reasons)
        )
    elif unknown_reasons and pass_signals:
        # Has some positive signals but also some unknowns
        screen = "unknown"
        screen_reason = (
            f"Positive signals present ({'; '.join(pass_signals)}) but "
            f"key unknowns remain: {' | '.join(unknown_reasons)}"
        )
    else:
        screen = "pass"
        screen_reason = " | ".join(pass_signals) if pass_signals else (
            "No disqualifying factors found in available signals"
        )

    return {
        "ineligible_keyword_check": {
            "result": ineligible_result,
            "matched_keyword": matched_kw,
            "basis": ineligible_basis
        },
        "years_check": {
            "years_in_business": years,
            "years_ok": years_ok,
            "basis": years_basis
        },
        "price_check": {
            "asking_price": asking_price,
            "price_known": price_known,
            "within_any_budget": price_within_any_budget
        },
        "preliminary_screen": screen,
        "preliminary_screen_reason": screen_reason,
        "fail_reasons": fail_reasons,
        "pass_signals": pass_signals,
        "unknown_reasons": unknown_reasons
    }


def _ai_enrichment(condensed_leads: list) -> dict:
    """
    LLM call — only for tasks requiring genuine reasoning:
      1. Goodwill assessment (is goodwill likely > 50% of price?)
      2. Eligibility nuance (ambiguous business types not caught by keyword)
      3. Broker questions (business-type-specific knowledge)
      4. What a real determination needs (business-type-specific documents)

    Receives a condensed lead list — only fields the AI actually needs.
    Does NOT receive financial data, deal math, or screen results —
    those are handled deterministically and merged after this call.
    """

    system_prompt = f"""You are a senior SBA 7(a) loan analyst.

You are doing exactly four jobs for each business lead.
No more, no less.

JOB 1 — GOODWILL ASSESSMENT
Is goodwill likely to exceed 50% of the asking price for this business type?
This affects whether the down payment is 10% or 15%.
Base this on business type knowledge — not on financials.
Examples:
  High goodwill (>50%): insurance agencies, accounting firms, law practices,
  consulting firms, staffing agencies — value is in relationships and reputation
  Low goodwill (<50%): laundromats, car washes, manufacturing, storage units,
  restaurants with real estate — value is in tangible assets and equipment
  Mixed: child care centers, HVAC companies, pest control routes

JOB 2 — ELIGIBILITY NUANCE
Some business types are ambiguous and were not caught by keyword matching.
Assess whether the business type is SBA eligible if it is unclear.
Only weigh in if the deterministic check returned 'likely_eligible' or 'unknown'.
Do not reassess types already flagged as 'ineligible' by keyword match.

JOB 3 — BROKER QUESTIONS
Generate specific questions the buyer must ask the listing broker.
Questions must be phrased as if the buyer is speaking to the broker directly.
Every lead gets the 5 mandatory questions PLUS at least 3 business-type-specific ones.

JOB 4 — WHAT A REAL DETERMINATION NEEDS
List the specific financial documents and data points needed to make
a real SBA loan determination for this business type.
Be specific — not generic.

{cfg.HONESTY_POLICY}

RULES:
- You are NOT calculating deal math — that is already done
- You are NOT checking the ineligible list — that is already done
- You are NOT determining preliminary screen — that is already done
- You are NOT estimating revenue from asking price — never do this
- You output ONLY valid JSON. No markdown, no code fences."""

    mandatory_questions = [
        {
            "priority": "must_ask",
            "question": "Can you share the last two years of business tax returns "
                        "or a Seller's Discretionary Earnings (SDE) statement?",
            "why_it_matters": "Required to calculate DSCR for SBA underwriting — "
                              "without this no lender can assess loan viability.",
            "what_good_looks_like": "Seller has clean tax returns showing "
                                    "consistent and growing profit over 2+ years."
        },
        {
            "priority": "must_ask",
            "question": "What is the breakdown between tangible assets and goodwill "
                        "in the asking price?",
            "why_it_matters": "If goodwill exceeds 50% of the purchase price, "
                              "the SBA down payment requirement rises from 10% to 15%.",
            "what_good_looks_like": "Majority of value is in tangible assets, "
                                    "equipment, or inventory — not relationships."
        },
        {
            "priority": "must_ask",
            "question": "Is the seller open to carrying a portion of the purchase "
                        "price as a seller note or standby financing?",
            "why_it_matters": "Seller financing reduces the cash down payment "
                              "needed and can make deals work that otherwise wouldn't.",
            "what_good_looks_like": "Seller willing to carry 5-10% as a standby "
                                    "note, reducing buyer's cash requirement."
        },
        {
            "priority": "must_ask",
            "question": "How many years has this business been operating under "
                        "its current ownership and legal structure?",
            "why_it_matters": "SBA requires a minimum of 2 years in business — "
                              "lenders strongly prefer 5+ years of operating history.",
            "what_good_looks_like": "Business has operated under same ownership "
                                    "for 5 or more years with consistent financials."
        },
        {
            "priority": "must_ask",
            "question": "What is the current owner's weekly time commitment to "
                        "the business, and is there a management team or key "
                        "employees in place who would stay after the sale?",
            "why_it_matters": "The buyer needs a completely hands-off business "
                              "run by a hired general manager — owner-dependent "
                              "businesses are not suitable.",
            "what_good_looks_like": "Owner works fewer than 10 hours per week, "
                                    "strong GM or senior staff in place and willing "
                                    "to stay post-acquisition."
        }
    ]

    user_prompt = f"""Assess these business leads for goodwill, eligibility nuance,
broker questions, and what a real determination needs.

LEADS (condensed — only what you need for your four jobs):
{json.dumps(condensed_leads, indent=2)}

MANDATORY BROKER QUESTIONS (include these verbatim for every lead,
then add at least 3 more that are specific to the business type):
{json.dumps(mandatory_questions, indent=2)}

Return this EXACT JSON:

{{
  "enrichments": [
    {{
      "lead_id": "<same lead_id>",

      "goodwill_assessment": {{
        "goodwill_likely_high": <true or false or null if truly uncertain>,
        "confidence": "<'high' | 'medium' | 'low'>",
        "basis": "<specific reasoning based on business type — not a generic statement>",
        "down_payment_implication": "<one sentence: 10% down or 15% down and why>"
      }},

      "eligibility_nuance": {{
        "assessment": "<'eligible' | 'ineligible' | 'ambiguous' | 'skip'>",
        "note": "<only populate if assessment adds something beyond keyword check — otherwise null>"
      }},

      "what_a_real_determination_needs": [
        "<specific document or data point — not generic>",
        "<tailor to business type>",
        "<minimum 4 items>"
      ],

      "broker_questions": [
        {{
          "priority": "<'must_ask' | 'should_ask' | 'nice_to_ask'>",
          "question": "<exact question phrased as buyer speaking to broker>",
          "why_it_matters": "<one sentence>",
          "what_good_looks_like": "<what answer indicates strong SBA candidate>"
        }}
      ]
    }}
  ]
}}

REMINDER — your four jobs only:
1. Goodwill assessment
2. Eligibility nuance (only if ambiguous)
3. Broker questions (5 mandatory + 3 business-type-specific minimum)
4. What a real determination needs"""

    response = client.chat.completions.create(
        model=cfg.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1,
        max_tokens=4000,
        response_format={"type": "json_object"}
    )

    raw = response.choices[0].message.content.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[LoanFeasibility] AI enrichment JSON parse error: {e}")
        print(f"[LoanFeasibility] Raw preview:\n{raw[:400]}")
        raise RuntimeError(
            f"Loan Feasibility AI enrichment returned invalid JSON: {e}"
        )


def run(scout_output: dict, brief: dict) -> dict:
    """
    Main entry point for the Loan Feasibility Agent.

    Execution order:
      Step 1 — _deal_math_check() per lead    — pure Python
      Step 2 — _deterministic_screen() per lead — pure Python
      Step 3 — _ai_enrichment() for all leads  — single LLM call
      Step 4 — merge all results per lead

    One LLM call total regardless of number of leads.
    All deterministic logic runs in Python before the LLM is involved.
    """

    leads = scout_output.get("leads", [])

    print(f"[LoanFeasibility] Starting — "
          f"{len(leads)} leads | "
          f"1 LLM call total (all deterministic logic runs in Python)")

    if not leads:
        print("[LoanFeasibility] No leads to assess — returning empty output")
        return {
            "leads_with_loan_assessment": [],
            "loan_summary": {"pass": 0, "fail": 0, "unknown": 0},
            "loan_feasibility_notes": "Scout returned zero leads."
        }

    deal_math = brief.get(
        "sba_deal_math",
        cfg.compute_max_deal_size(cfg.DEFAULT_LIQUID_CASH)
    )

    # ── Step 1: Deal math — pure Python ───────────────────
    print("[LoanFeasibility] Step 1 — Deal math checks (Python)...")
    dm_results = {}
    for lead in leads:
        lead_id = lead.get("lead_id", "unknown")
        asking_price = lead.get("business_snapshot", {}).get("asking_price")
        dm_results[lead_id] = _deal_math_check(asking_price, deal_math)

    # ── Step 2: Deterministic screen — pure Python ─────────
    print("[LoanFeasibility] Step 2 — Deterministic eligibility screen (Python)...")
    screen_results = {}
    for lead in leads:
        lead_id = lead.get("lead_id", "unknown")
        screen_results[lead_id] = _deterministic_screen(
            lead, deal_math, dm_results[lead_id]
        )

    # Log screen summary before LLM call
    fails = sum(1 for s in screen_results.values() if s["preliminary_screen"] == "fail")
    passes = sum(1 for s in screen_results.values() if s["preliminary_screen"] == "pass")
    unknowns = sum(1 for s in screen_results.values() if s["preliminary_screen"] == "unknown")
    print(f"[LoanFeasibility] Deterministic screen — "
          f"{passes} pass, {fails} fail, {unknowns} unknown")

    # ── Step 3: AI enrichment — single LLM call ───────────
    print("[LoanFeasibility] Step 3 — AI enrichment (1 LLM call)...")

    # Send only what AI actually needs — business type, industry,
    # years, seller signals, and the deterministic screen result
    # so it knows what NOT to re-assess
    condensed_leads = []
    for lead in leads:
        lead_id = lead.get("lead_id", "unknown")
        snapshot = lead.get("business_snapshot", {})
        screen = screen_results.get(lead_id, {})
        condensed_leads.append({
            "lead_id": lead_id,
            "business_type": snapshot.get("business_type"),
            "industry": snapshot.get("industry"),
            "business_name": snapshot.get("business_name"),
            "years_in_business": snapshot.get("years_in_business"),
            "seller_financing_available": lead.get(
                "seller_motivation", {}
            ).get("seller_financing_available"),
            "handsoff_concerns": lead.get(
                "handsoff_assessment", {}
            ).get("concerns", []),
            "deterministic_screen_result": screen.get("preliminary_screen"),
            "ineligible_keyword_check": screen.get(
                "ineligible_keyword_check", {}
            ).get("result"),
            "note_to_ai": (
                "Deal math and ineligible keyword check already done in Python. "
                "Only assess goodwill, eligibility nuance if ambiguous, "
                "broker questions, and what a real determination needs."
            )
        })

    ai_result = _ai_enrichment(condensed_leads)

    # Build lookup by lead_id
    enrichment_lookup = {
        e.get("lead_id"): e
        for e in ai_result.get("enrichments", [])
    }

    # ── Step 4: Merge all results ──────────────────────────
    print("[LoanFeasibility] Step 4 — Merging results...")

    enriched_leads = []
    final_pass = 0
    final_fail = 0
    final_unknown = 0

    for lead in leads:
        lead_id = lead.get("lead_id", "unknown")
        dm = dm_results.get(lead_id, {})
        screen = screen_results.get(lead_id, {})
        ai = enrichment_lookup.get(lead_id, {})

        # Final preliminary screen — deterministic result is authoritative
        # AI eligibility nuance can upgrade 'likely_eligible' to 'ineligible'
        # but cannot override a deterministic 'fail'
        final_screen = screen.get("preliminary_screen", "unknown")
        final_screen_reason = screen.get("preliminary_screen_reason", "")

        ai_eligibility = ai.get("eligibility_nuance", {}).get("assessment")
        if (
            ai_eligibility == "ineligible"
            and final_screen != "fail"
        ):
            final_screen = "fail"
            final_screen_reason = (
                f"AI eligibility nuance flagged ineligibility: "
                f"{ai.get('eligibility_nuance', {}).get('note', 'see eligibility nuance')}"
            )

        if final_screen == "pass":
            final_pass += 1
        elif final_screen == "fail":
            final_fail += 1
        else:
            final_unknown += 1

        enriched_lead = {
            **lead,
            "loan_feasibility": {
                "preliminary_screen": final_screen,
                "preliminary_screen_reason": final_screen_reason,

                "real_determination_disclaimer": (
                    "This is a preliminary screen only — not a loan determination. "
                    "A real SBA loan decision requires revenue, profit, and asset "
                    "data from the broker. Use the broker questions below to "
                    "obtain that data before drawing any conclusions about "
                    "loan viability."
                ),

                "deal_math": dm,

                "eligibility_screen": {
                    "keyword_check": screen.get("ineligible_keyword_check", {}),
                    "years_check": screen.get("years_check", {}),
                    "price_check": screen.get("price_check", {}),
                    "ai_nuance": ai.get("eligibility_nuance", {})
                },

                "goodwill_assessment": ai.get("goodwill_assessment", {
                    "goodwill_likely_high": None,
                    "confidence": "low",
                    "basis": "AI enrichment unavailable for this lead",
                    "down_payment_implication": "Cannot determine without assessment"
                }),

                "what_a_real_determination_needs": ai.get(
                    "what_a_real_determination_needs", [
                        "Last 2 years of business federal tax returns",
                        "Seller Discretionary Earnings (SDE) statement",
                        "Complete asset list with appraised values",
                        "Current lease agreement and remaining term",
                        "List of all business liabilities and existing debt"
                    ]
                ),

                "broker_questions": ai.get("broker_questions", [])
            }
        }

        enriched_leads.append(enriched_lead)

    # Validate broker questions present on every lead
    missing_questions = sum(
        1 for l in enriched_leads
        if not l.get("loan_feasibility", {}).get("broker_questions")
    )
    if missing_questions > 0:
        print(f"[LoanFeasibility] WARNING — {missing_questions} leads "
              f"missing broker questions")

    result = {
        "leads_with_loan_assessment": enriched_leads,
        "loan_summary": {
            "pass": final_pass,
            "fail": final_fail,
            "unknown": final_unknown
        },
        "loan_feasibility_notes": (
            f"{final_pass} leads passed preliminary screen, "
            f"{final_fail} failed, "
            f"{final_unknown} unknown. "
            f"Preliminary screen checks: SBA ineligible keyword match, "
            f"years in business minimum, and price ceiling — all in Python. "
            f"'Pass' means no disqualifying factors found in available signals. "
            f"It does NOT mean the loan is approved. "
            f"Revenue, profit, and DSCR must be confirmed with the broker "
            f"using the questions provided before any real loan determination."
        )
    }

    print(f"[LoanFeasibility] Complete — "
          f"{len(enriched_leads)} leads enriched | "
          f"1 LLM call used | "
          f"screen: {final_pass} pass, {final_fail} fail, {final_unknown} unknown")

    return result


if __name__ == "__main__":
    # Quick local test — runs full chain to this point
    # python agents/loan_feasibility.py
    import sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from agents.planner import run as planner_run
    from agents.researcher import run as researcher_run
    from agents.scout import run as scout_run

    print("=" * 60)
    print("PIPELINE: Planner → Researcher → Scout → "
          "Loan Feasibility → Validator → Dashboard")
    print("=" * 60)

    print("\n=== Running Planner ===")
    brief = planner_run(
        zip_code=cfg.DEFAULT_ZIP,
        liquid_cash=cfg.DEFAULT_LIQUID_CASH,
        radius_miles=cfg.DEFAULT_RADIUS_MILES
    )

    print("\n=== Running Researcher ===")
    researcher_output = researcher_run(
        brief=brief,
        seen_ids=[],
        rejection_reasons=[]
    )

    print("\n=== Running Scout ===")
    scout_output = scout_run(
        researcher_output=researcher_output,
        brief=brief
    )

    print("\n=== Running Loan Feasibility ===")
    result = run(
        scout_output=scout_output,
        brief=brief
    )

    print("\n── Loan Feasibility output ──")
    print(json.dumps(result, indent=2))