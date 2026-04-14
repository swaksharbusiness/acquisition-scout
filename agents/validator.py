# agents/validator.py
# Validator Agent — fifth in the pipeline (LAST before Dashboard)
# Receives fully enriched leads from Loan Feasibility Agent
# Validates signal quality and assigns final triage priority
#
# Design philosophy — same as loan_feasibility.py:
#   Deterministic checks run in pure Python (no LLM)
#   LLM used only for genuine reasoning tasks
#
# What runs in Python (deterministic):
#   - Required field presence checks
#   - Data confidence threshold checks
#   - Price ceiling violation checks
#   - SBA preliminary screen result checks
#   - Verification URL presence checks
#   - Duplicate detection against memory
#   - Loop control (max 3 iterations)
#
# What runs in LLM (reasoning):
#   - Signal coherence check (do fields contradict each other?)
#   - Hands-off operability reasoning (is GM management realistic?)
#   - Overall triage commentary (what should buyer do first?)
#
# Retry loop:
#   If too few leads pass validation, returns rejection reasons
#   to main.py which re-runs the Researcher with those reasons
#   Maximum VALIDATOR_MAX_LOOPS total attempts (set in config.py)
#
# Pipeline position: 5 of 6
# Receives from: Loan Feasibility Agent
# Passes to: Dashboard Agent

import json
import os
import sys
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg

client = OpenAI(api_key=cfg.OPENAI_API_KEY)

# ─────────────────────────────────────────────
# Validation thresholds — all deterministic
# Change these in one place to affect all checks
# ─────────────────────────────────────────────
MIN_REQUIRED_FIELDS = 3        # minimum non-null fields for a lead to pass
MIN_LEADS_TO_PASS = 2          # minimum approved leads before dashboard runs
CONFIDENCE_WEIGHTS = {
    "high": 3,
    "medium": 2,
    "low": 1
}


def _check_required_fields(lead: dict) -> tuple:
    """
    Pure Python — no LLM.
    Checks that minimum required fields are non-null.
    Returns (passed: bool, present_fields: list, missing_fields: list)
    """

    snapshot = lead.get("business_snapshot", {})
    source = lead.get("source", {})
    loan = lead.get("loan_feasibility", {})

    # Fields we check for presence
    # Each is a (label, value) tuple
    checkable_fields = [
        ("business_type", snapshot.get("business_type")),
        ("industry", snapshot.get("industry")),
        ("location.city", snapshot.get("location", {}).get("city")),
        ("asking_price", snapshot.get("asking_price")),
        ("years_in_business", snapshot.get("years_in_business")),
        ("verify_url", source.get("verify_url")),
        ("triage_priority", lead.get("triage_priority")),
        ("preliminary_screen", loan.get("preliminary_screen")),
        ("broker_questions", loan.get("broker_questions")),
        ("manual_verification_checklist",
         lead.get("manual_verification_checklist")),
    ]

    present = [label for label, val in checkable_fields
               if val not in (None, [], "", cfg.NULL_STRING)]
    missing = [label for label, val in checkable_fields
               if val in (None, [], "", cfg.NULL_STRING)]

    passed = len(present) >= MIN_REQUIRED_FIELDS
    return passed, present, missing


def _check_price_ceiling(lead: dict, deal_math: dict) -> tuple:
    """
    Pure Python — no LLM.
    Checks whether asking price exceeds both SBA deal ceilings.
    Returns (failed: bool, reason: str or None)
    """

    asking_price = lead.get(
        "business_snapshot", {}
    ).get("asking_price")

    if asking_price is None:
        return False, None  # unknown price — not a hard fail

    standard_max = deal_math["standard_max_deal"]
    goodwill_max = deal_math["high_goodwill_max_deal"]

    if asking_price > standard_max and asking_price > goodwill_max:
        return True, (
            f"Asking price ${asking_price:,.0f} exceeds both standard max "
            f"(${standard_max:,.0f}) and high-goodwill max "
            f"(${goodwill_max:,.0f}) — outside buyer's SBA deal ceiling"
        )
    return False, None


def _check_sba_screen(lead: dict) -> tuple:
    """
    Pure Python — no LLM.
    Checks the loan feasibility preliminary screen result.
    A 'fail' from deterministic screening is a hard rejection.
    Returns (hard_fail: bool, reason: str or None)
    """

    screen = lead.get(
        "loan_feasibility", {}
    ).get("preliminary_screen")

    if screen == "fail":
        screen_reason = lead.get(
            "loan_feasibility", {}
        ).get("preliminary_screen_reason", "SBA preliminary screen failed")
        return True, screen_reason

    return False, None


def _check_data_confidence(lead: dict) -> tuple:
    """
    Pure Python — no LLM.
    Checks data confidence level.
    'low' confidence alone is not a hard fail but is flagged.
    Returns (is_low_confidence: bool, confidence: str)
    """

    confidence = lead.get(
        "data_quality", {}
    ).get("data_confidence", "low")

    return confidence == "low", confidence


def _check_duplicate(lead: dict, seen_ids: list) -> tuple:
    """
    Pure Python — no LLM.
    Checks if this lead ID already exists in memory.
    Returns (is_duplicate: bool, lead_id: str)
    """

    lead_id = lead.get("lead_id", "")
    return lead_id in seen_ids, lead_id


def _check_verification_url(lead: dict) -> tuple:
    """
    Pure Python — no LLM.
    Checks whether a verify_url exists.
    No verify_url means the buyer has nowhere to go to check this lead.
    Returns (has_url: bool, url: str or None)
    """

    url = lead.get("source", {}).get("verify_url")
    has_url = url is not None and url != cfg.NULL_STRING and url != ""
    return has_url, url


def _run_deterministic_checks(
    leads: list,
    deal_math: dict,
    seen_ids: list
) -> list:
    """
    Pure Python — no LLM.
    Runs all deterministic checks on every lead.
    Returns a list of check result dicts, one per lead.
    Each result contains:
      - hard_fail: bool (lead must be rejected)
      - hard_fail_reasons: list of strings
      - warnings: list of non-fatal issues
      - passed_checks: list of checks that passed
      - field_check: detailed field presence result
      - is_duplicate: bool
      - data_confidence: string
      - has_verify_url: bool
    """

    results = []

    for lead in leads:
        lead_id = lead.get("lead_id", "unknown")
        hard_fail = False
        hard_fail_reasons = []
        warnings = []
        passed_checks = []

        # Check 1 — duplicate detection
        is_dup, _ = _check_duplicate(lead, seen_ids)
        if is_dup:
            hard_fail = True
            hard_fail_reasons.append(
                f"Lead ID '{lead_id}' already exists in memory — duplicate"
            )
        else:
            passed_checks.append("not_duplicate")

        # Check 2 — required fields
        fields_ok, present, missing = _check_required_fields(lead)
        if not fields_ok:
            hard_fail = True
            hard_fail_reasons.append(
                f"Only {len(present)}/{MIN_REQUIRED_FIELDS} required fields "
                f"present — insufficient signal. Missing: {', '.join(missing)}"
            )
        else:
            passed_checks.append(
                f"required_fields ({len(present)} present)"
            )

        # Check 3 — price ceiling
        price_fails, price_reason = _check_price_ceiling(lead, deal_math)
        if price_fails:
            hard_fail = True
            hard_fail_reasons.append(price_reason)
        else:
            passed_checks.append("price_within_ceiling_or_unknown")

        # Check 4 — SBA preliminary screen
        sba_fails, sba_reason = _check_sba_screen(lead)
        if sba_fails:
            hard_fail = True
            hard_fail_reasons.append(sba_reason)
        else:
            passed_checks.append(
                f"sba_screen_"
                f"{lead.get('loan_feasibility', {}).get('preliminary_screen', 'unknown')}"
            )

        # Check 5 — data confidence (warning only, not hard fail)
        is_low_conf, confidence = _check_data_confidence(lead)
        if is_low_conf:
            warnings.append(
                "Low data confidence — lead is based on search preview only, "
                "no specific listing URL confirmed"
            )
        else:
            passed_checks.append(f"data_confidence_{confidence}")

        # Check 6 — verification URL (warning only, not hard fail)
        has_url, url = _check_verification_url(lead)
        if not has_url:
            warnings.append(
                "No verification URL found — buyer has no direct link "
                "to view this listing"
            )
        else:
            passed_checks.append("verify_url_present")

        results.append({
            "lead_id": lead_id,
            "hard_fail": hard_fail,
            "hard_fail_reasons": hard_fail_reasons,
            "warnings": warnings,
            "passed_checks": passed_checks,
            "field_detail": {
                "present_fields": present if fields_ok else [],
                "missing_fields": missing
            },
            "is_duplicate": is_dup,
            "data_confidence": confidence,
            "has_verify_url": has_url
        })

    return results


def _ai_reasoning(leads_for_reasoning: list) -> dict:
    """
    LLM call — only for tasks requiring genuine reasoning.
    Receives only leads that passed deterministic checks.
    Does NOT re-check anything already checked in Python.

    Two reasoning tasks:
      1. Signal coherence — do the fields tell a consistent story?
         e.g. "owner retiring" but business listed 2 weeks ago with no price
         reduction — signals may contradict each other
      2. Hands-off operability — is GM management realistic for this type?
         Based on business type knowledge, not on field checks

    Returns reasoning results indexed by lead_id.
    One LLM call for all leads combined.
    """

    if not leads_for_reasoning:
        return {"reasoning": []}

    system_prompt = f"""You are a senior acquisition analyst reviewing
business leads for a hands-off buyer.

You are doing exactly two reasoning tasks per lead.
All field checks, price checks, SBA screens, and duplicate detection
have already been done in Python. Do not repeat them.

TASK 1 — SIGNAL COHERENCE
Do the available signals tell a consistent, believable story?
Look for contradictions or red flags in the combination of signals.
Examples of incoherence:
  - "Owner retiring" but price is 3x industry multiples — contradicts motivation
  - "25 years in business" but no established web presence signal — worth flagging
  - "Seller financing available" but "price reduced" AND "motivated seller" —
    possibly distressed, not just motivated
  - Child care center with "no employees mentioned" — implausible, owner-dependent

Examples of coherence:
  - "Owner retiring after 20 years, seller financing available, price in range"
    — consistent motivated seller story
  - "Home services, 25 years, retiring, $97k asking" — consistent with
    a small established route business

TASK 2 — HANDS-OFF OPERABILITY REASONING
Based on business type knowledge — not on field values — how realistic
is it that this business can be run by a hired general manager?
Consider:
  - Does this business type typically have separable management?
  - Does it require owner relationships with customers or suppliers?
  - Does it require specialized licenses the owner holds personally?
  - Is there typically a natural GM role in this type of business?

{cfg.HONESTY_POLICY}

RULES:
- Do not re-check fields, prices, SBA screens, or duplicates
- Do not invent information not present in the lead signals
- If signals are too sparse to reason about coherence, say so honestly
- You output ONLY valid JSON. No markdown, no code fences."""

    # Condense leads to only what reasoning needs
    condensed = []
    for lead in leads_for_reasoning:
        snapshot = lead.get("business_snapshot", {})
        motivation = lead.get("seller_motivation", {})
        handsoff = lead.get("handsoff_assessment", {})
        condensed.append({
            "lead_id": lead.get("lead_id"),
            "business_type": snapshot.get("business_type"),
            "industry": snapshot.get("industry"),
            "years_in_business": snapshot.get("years_in_business"),
            "asking_price": snapshot.get("asking_price"),
            "triage_priority": lead.get("triage_priority"),
            "retirement_signal": motivation.get("retirement_signal"),
            "retirement_language": motivation.get("retirement_language"),
            "other_motivation": motivation.get("other_motivation"),
            "seller_financing_available": motivation.get(
                "seller_financing_available"
            ),
            "motivation_strength": motivation.get("motivation_strength"),
            "appears_gm_manageable": handsoff.get("appears_gm_manageable"),
            "handsoff_concerns": handsoff.get("concerns", []),
            "owner_hours_mentioned": handsoff.get("owner_hours_mentioned"),
            "sba_preliminary_screen": lead.get(
                "loan_feasibility", {}
            ).get("preliminary_screen"),
            "data_confidence": lead.get(
                "data_quality", {}
            ).get("data_confidence"),
            "preview_text_raw": lead.get("preview_text_raw")
        })

    user_prompt = f"""Reason about signal coherence and hands-off operability
for these leads. All deterministic checks already passed.

LEADS:
{json.dumps(condensed, indent=2)}

Return this EXACT JSON:

{{
  "reasoning": [
    {{
      "lead_id": "<same lead_id>",

      "signal_coherence": {{
        "assessment": "<'coherent' | 'incoherent' | 'sparse'>",
        "coherence_score": <1-10 integer — 10 is perfectly consistent signals>,
        "observations": [
          "<specific observation about signal consistency — based only on available data>"
        ],
        "red_flags": [
          "<specific contradiction or concern in the signal combination — or empty list>"
        ],
        "coherence_summary": "<one sentence honest assessment of whether signals tell a believable story>"
      }},

      "handsoff_reasoning": {{
        "assessment": "<'likely_manageable' | 'unlikely_manageable' | 'uncertain'>",
        "confidence": "<'high' | 'medium' | 'low'>",
        "reasoning": "<2-3 sentences based on business type knowledge>",
        "key_risk": "<the single biggest hands-off risk for this business type or null>",
        "key_advantage": "<the single biggest hands-off advantage for this business type or null>"
      }},

      "analyst_commentary": "<2-3 sentences of honest analyst commentary. What is interesting about this lead, what concerns you, what the buyer should prioritize finding out. Based only on available signals.>",

      "recommended_first_action": "<one specific action the buyer should take first with this lead — e.g. 'Log into BizBuySell at [verify_url] and request the financial package from the broker' or 'Contact broker to confirm location is within 50 miles of ZIP 66214'>"
    }}
  ]
}}"""

    response = client.chat.completions.create(
        model=cfg.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1,
        max_tokens=3000,
        response_format={"type": "json_object"}
    )

    raw = response.choices[0].message.content.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[Validator] AI reasoning JSON parse error: {e}")
        print(f"[Validator] Raw preview:\n{raw[:400]}")
        raise RuntimeError(
            f"Validator AI reasoning returned invalid JSON: {e}"
        )


def _assign_final_priority(
    lead: dict,
    det_result: dict,
    reasoning: dict
) -> str:
    """
    Pure Python — no LLM.
    Assigns final triage priority based on deterministic
    check results and AI reasoning scores.

    Rules (applied in order, first match wins):
      FAIL  → already rejected, not called for these
      HIGH  → scout priority HIGH + coherence_score >= 7
               + handsoff assessment likely_manageable
               + has verify_url + not low confidence
      MEDIUM → scout priority HIGH or MEDIUM
               + coherence_score >= 5
               + no incoherent signal assessment
      LOW   → everything else that passed deterministic checks
    """

    scout_priority = lead.get("triage_priority", "LOW")
    has_url = det_result.get("has_verify_url", False)
    confidence = det_result.get("data_confidence", "low")
    warnings = det_result.get("warnings", [])

    coherence_score = reasoning.get(
        "signal_coherence", {}
    ).get("coherence_score", 5)
    coherence_assessment = reasoning.get(
        "signal_coherence", {}
    ).get("assessment", "sparse")
    handsoff = reasoning.get(
        "handsoff_reasoning", {}
    ).get("assessment", "uncertain")

    # HIGH — strict criteria
    if (
        scout_priority == "HIGH"
        and coherence_score >= 7
        and coherence_assessment != "incoherent"
        and handsoff == "likely_manageable"
        and has_url
        and confidence != "low"
    ):
        return "HIGH"

    # MEDIUM — relaxed criteria
    if (
        scout_priority in ("HIGH", "MEDIUM")
        and coherence_score >= 5
        and coherence_assessment != "incoherent"
    ):
        return "MEDIUM"

    # LOW — anything else that passed hard checks
    return "LOW"


def run(
    loan_feasibility_output: dict,
    brief: dict,
    seen_ids: list = None,
    loop_number: int = 1
) -> dict:
    """
    Main entry point for the Validator Agent.

    Execution order:
      Step 1 — _run_deterministic_checks() — pure Python, all leads
      Step 2 — _ai_reasoning()             — single LLM call,
                                             passing leads only
      Step 3 — _assign_final_priority()    — pure Python, per lead
      Step 4 — Build approved/rejected split

    Pipeline position: 5 of 6
    Receives from: Loan Feasibility Agent
    Passes to: Dashboard Agent (approved leads)
               main.py (rejected leads with reasons for retry)

    Parameters:
      loan_feasibility_output — full output from loan_feasibility.run()
      brief                   — planner brief (for deal math)
      seen_ids                — IDs already in memory (duplicate check)
      loop_number             — current loop iteration (1, 2, or 3)
    """

    seen_ids = seen_ids or []
    leads = loan_feasibility_output.get("leads_with_loan_assessment", [])

    print(f"[Validator] Starting — "
          f"loop {loop_number}/{cfg.VALIDATOR_MAX_LOOPS} | "
          f"{len(leads)} leads to validate | "
          f"1 LLM call total")

    if not leads:
        print("[Validator] No leads to validate — returning empty output")
        return {
            "approved_leads": [],
            "rejected_leads": [],
            "rejection_reasons": ["No leads were produced by the pipeline"],
            "validation_summary": {
                "total": 0,
                "approved": 0,
                "rejected": 0,
                "loop_number": loop_number,
                "needs_retry": True
            },
            "validator_notes": "No leads received — retry recommended."
        }

    deal_math = brief.get(
        "sba_deal_math",
        cfg.compute_max_deal_size(cfg.DEFAULT_LIQUID_CASH)
    )

    # ── Step 1: Deterministic checks — pure Python ─────────
    print("[Validator] Step 1 — Deterministic checks (Python)...")

    det_results = _run_deterministic_checks(leads, deal_math, seen_ids)

    # Split into passing and failing
    det_lookup = {r["lead_id"]: r for r in det_results}
    passing_leads = [
        l for l in leads
        if not det_lookup.get(l.get("lead_id", ""), {}).get("hard_fail", True)
    ]
    failing_leads = [
        l for l in leads
        if det_lookup.get(l.get("lead_id", ""), {}).get("hard_fail", True)
    ]

    hard_fail_count = len(failing_leads)
    passing_count = len(passing_leads)

    print(f"[Validator] Deterministic checks — "
          f"{passing_count} passing, {hard_fail_count} hard fail")

    # ── Step 2: AI reasoning — single LLM call ─────────────
    # Only for leads that passed deterministic checks
    reasoning_lookup = {}

    if passing_leads:
        print("[Validator] Step 2 — AI reasoning (1 LLM call)...")
        ai_result = _ai_reasoning(passing_leads)
        reasoning_lookup = {
            r.get("lead_id"): r
            for r in ai_result.get("reasoning", [])
        }
    else:
        print("[Validator] Step 2 — Skipping AI reasoning "
              "(no leads passed deterministic checks)")

    # ── Step 3: Assign final priority — pure Python ─────────
    print("[Validator] Step 3 — Assigning final triage priorities (Python)...")

    approved_leads = []
    rejected_leads = []
    rejection_reasons = []

    # Process hard-failing leads first
    for lead in failing_leads:
        lead_id = lead.get("lead_id", "unknown")
        det = det_lookup.get(lead_id, {})
        rejected_leads.append({
            **lead,
            "validation": {
                "result": "rejected",
                "hard_fail": True,
                "hard_fail_reasons": det.get("hard_fail_reasons", []),
                "warnings": det.get("warnings", []),
                "passed_checks": det.get("passed_checks", []),
                "loop_number": loop_number
            }
        })
        for reason in det.get("hard_fail_reasons", []):
            rejection_reasons.append(
                f"Lead '{lead_id}' rejected: {reason}"
            )

    # Process passing leads
    for lead in passing_leads:
        lead_id = lead.get("lead_id", "unknown")
        det = det_lookup.get(lead_id, {})
        reasoning = reasoning_lookup.get(lead_id, {})

        final_priority = _assign_final_priority(lead, det, reasoning)

        coherence = reasoning.get("signal_coherence", {})
        handsoff_r = reasoning.get("handsoff_reasoning", {})

        approved_lead = {
            **lead,
            "triage_priority": final_priority,
            "validation": {
                "result": "approved",
                "hard_fail": False,
                "loop_number": loop_number,
                "passed_checks": det.get("passed_checks", []),
                "warnings": det.get("warnings", []),
                "data_confidence": det.get("data_confidence"),
                "has_verify_url": det.get("has_verify_url"),
                "signal_coherence": {
                    "assessment": coherence.get("assessment", "sparse"),
                    "coherence_score": coherence.get("coherence_score", 5),
                    "observations": coherence.get("observations", []),
                    "red_flags": coherence.get("red_flags", []),
                    "summary": coherence.get("coherence_summary", "")
                },
                "handsoff_reasoning": {
                    "assessment": handsoff_r.get("assessment", "uncertain"),
                    "confidence": handsoff_r.get("confidence", "low"),
                    "reasoning": handsoff_r.get("reasoning", ""),
                    "key_risk": handsoff_r.get("key_risk"),
                    "key_advantage": handsoff_r.get("key_advantage")
                },
                "analyst_commentary": reasoning.get(
                    "analyst_commentary", ""
                ),
                "recommended_first_action": reasoning.get(
                    "recommended_first_action", ""
                )
            }
        }
        approved_leads.append(approved_lead)

    # ── Step 4: Retry decision — pure Python ───────────────
    approved_count = len(approved_leads)
    needs_retry = (
        approved_count < MIN_LEADS_TO_PASS
        and loop_number < cfg.VALIDATOR_MAX_LOOPS
    )

    if needs_retry:
        print(f"[Validator] Only {approved_count} leads approved "
              f"(minimum {MIN_LEADS_TO_PASS}) — "
              f"retry recommended (loop {loop_number}/{cfg.VALIDATOR_MAX_LOOPS})")
    elif approved_count < MIN_LEADS_TO_PASS:
        print(f"[Validator] Only {approved_count} leads approved "
              f"but max loops reached — proceeding with what we have")
    else:
        print(f"[Validator] {approved_count} leads approved — sufficient")

    # Priority breakdown
    high = sum(1 for l in approved_leads if l.get("triage_priority") == "HIGH")
    med = sum(1 for l in approved_leads if l.get("triage_priority") == "MEDIUM")
    low = sum(1 for l in approved_leads if l.get("triage_priority") == "LOW")

    result = {
        "approved_leads": approved_leads,
        "rejected_leads": rejected_leads,
        "rejection_reasons": rejection_reasons,
        "validation_summary": {
            "total": len(leads),
            "approved": approved_count,
            "rejected": len(rejected_leads),
            "high_priority": high,
            "medium_priority": med,
            "low_priority": low,
            "loop_number": loop_number,
            "needs_retry": needs_retry,
            "min_leads_threshold": MIN_LEADS_TO_PASS
        },
        "validator_notes": (
            f"Loop {loop_number}/{cfg.VALIDATOR_MAX_LOOPS}. "
            f"{approved_count} approved ({high} HIGH, {med} MEDIUM, {low} LOW), "
            f"{len(rejected_leads)} rejected. "
            f"{'Retry recommended — insufficient approved leads.' if needs_retry else 'Sufficient leads approved.'}"
        )
    }

    print(f"[Validator] Complete — "
          f"1 LLM call used | "
          f"{approved_count} approved ({high}H/{med}M/{low}L) | "
          f"{len(rejected_leads)} rejected | "
          f"needs_retry={needs_retry}")

    return result


if __name__ == "__main__":
    # Quick local test — runs full chain to this point
    # python agents/validator.py
    import sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from agents.planner import run as planner_run
    from agents.researcher import run as researcher_run
    from agents.scout import run as scout_run
    from agents.loan_feasibility import run as loan_run

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
    loan_output = loan_run(
        scout_output=scout_output,
        brief=brief
    )

    print("\n=== Running Validator ===")
    result = run(
        loan_feasibility_output=loan_output,
        brief=brief,
        seen_ids=[],
        loop_number=1
    )

    print("\n── Validator output ──")
    print(json.dumps(result, indent=2))