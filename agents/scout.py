# agents/scout.py
# Scout Agent — third in the pipeline
# Receives raw findings from the Researcher Agent
# Produces structured triage records for each lead
# Generates a manual verification checklist per lead
# Does NOT score loan feasibility — that is Loan Feasibility Agent's job
# Does NOT approve or reject — that is Validator Agent's job
# Does NOT invent data — only structures what Researcher explicitly found

import json
import os
import sys
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg

client = OpenAI(api_key=cfg.OPENAI_API_KEY)


def run(researcher_output: dict, brief: dict) -> dict:
    """
    Takes the Researcher's consolidated findings and produces
    structured triage records ready for the Loan Feasibility Agent.

    Pipeline position: 3 of 6
    Receives from: Researcher Agent
    Passes to: Loan Feasibility Agent

    For each finding the Scout produces:
      - A clean structured lead record
      - A triage assessment (is this worth pursuing?)
      - A manual verification checklist (what to check and where)
      - A hands-off operability assessment
      - A retirement/motivation signal summary
      - Data gaps clearly listed (what is unknown)

    Never fills data gaps by inference.
    Never scores loan feasibility — that belongs to Loan Feasibility Agent.
    Never approves or rejects — that belongs to Validator Agent.
    """

    print(f"[Scout] Starting — "
          f"processing {len(researcher_output.get('raw_findings', []))} findings")

    raw_findings = researcher_output.get("raw_findings", [])

    if not raw_findings:
        print("[Scout] No findings to process — returning empty leads")
        return {
            "leads": [],
            "scout_notes": (
                "Researcher returned zero findings. "
                "No leads to structure. "
                "This is an honest empty result."
            ),
            "total_leads": 0,
            "triage_summary": {
                "high_priority": 0,
                "medium_priority": 0,
                "low_priority": 0
            }
        }

    deal_math = brief.get(
        "sba_deal_math",
        cfg.compute_max_deal_size(cfg.DEFAULT_LIQUID_CASH)
    )
    radius = brief.get("radius_miles", cfg.DEFAULT_RADIUS_MILES)
    zip_code = brief.get("zip_code", cfg.DEFAULT_ZIP)
    location = brief.get("location_context", {})
    city = location.get("city", "Shawnee")

    system_prompt = f"""You are a senior acquisition analyst producing structured
triage records for a business buyer evaluating small business acquisitions.

The buyer profile:
{cfg.BUYER_PROFILE}

{cfg.HONESTY_POLICY}

YOUR SPECIFIC RULES FOR THIS TASK:

1. TRIAGE ONLY — you are producing a lead triage record, not a due diligence report.
   Every record must carry a prominent disclaimer that it is based on search signals
   only and requires manual verification before any action is taken.

2. VERIFICATION CHECKLISTS — for each lead you must produce a specific, actionable
   manual verification checklist. Each item must name:
   - The exact question to answer
   - The exact place to find the answer (specific URL, who to call, what to ask)
   This checklist is what the buyer will use when they manually review the lead.

3. DATA GAPS — you must explicitly list every important field that is null or unknown.
   Do not gloss over missing data. Missing revenue, missing profit, missing years in
   business — these must be called out clearly as gaps the buyer needs to fill.

4. HANDS-OFF ASSESSMENT — assess whether this business can realistically be run
   by a hired general manager without the buyer present day-to-day. Base this only
   on what the listing signal actually says — not on general industry assumptions.
   If the signal does not contain enough information, say so explicitly.

5. TRIAGE PRIORITY — assign one of three priorities:
   HIGH   = strong motivated seller signal + price within budget + location confirmed
             + no obvious SBA disqualifiers + appears GM-manageable
   MEDIUM = some positive signals but one or more key unknowns (price, location,
             years in business, GM-operability not confirmed)
   LOW    = weak signals, major unknowns, or one red flag present
             (not a rejection — still worth a quick look, just lower urgency)

6. NEVER assign HIGH priority to a lead with no specific listing URL.
   A lead with only a category page URL is at most MEDIUM priority.

7. NEVER infer financial data. If revenue and profit are null in the researcher
   output, they stay null in your output. Do not estimate them from the asking price
   or industry norms.

8. You output ONLY valid JSON. No markdown, no code fences, no explanation."""

    user_prompt = f"""Structure these raw research findings into triage leads.

CONTEXT:
- Search area: {radius} miles from ZIP {zip_code} (centered on {city})
- Standard max deal: ${deal_math['standard_max_deal']:,.0f} (10% down)
- High-goodwill max deal: ${deal_math['high_goodwill_max_deal']:,.0f} (15% down)
- Buyer liquid cash: ${deal_math['liquid_cash']:,.0f}
- Today this tool is a LEAD TRIAGE TOOL — not a due diligence system

RAW FINDINGS FROM RESEARCHER:
{json.dumps(raw_findings, indent=2)}

For each finding produce a structured triage lead. Return this EXACT JSON:

{{
  "leads": [
    {{
      "lead_id": "<same as finding_id from researcher>",
      "triage_priority": "<'HIGH' | 'MEDIUM' | 'LOW'>",
      "triage_priority_reason": "<one specific sentence explaining exactly why this priority was assigned>",
      "verification_required": true,
      "verification_disclaimer": "This lead is based on search preview signals only. No financial data has been verified. Do not take any action on this lead without first manually verifying it at the source.",

      "business_snapshot": {{
        "business_type": "<from researcher or null>",
        "industry": "<from researcher or null>",
        "business_name": "<from researcher or null — null if not found>",
        "location": {{
          "city": "<from researcher or null>",
          "state": "<from researcher or null>",
          "within_radius_confirmed": <true or false or null — null if not confirmed>,
          "distance_estimate": "<from researcher or null>"
        }},
        "years_in_business": <from researcher or null>,
        "asking_price": <from researcher or null>,
        "asking_price_raw": "<exact string from researcher or null>"
      }},

      "seller_motivation": {{
        "retirement_signal": <true or false — only if explicitly in researcher data>,
        "retirement_language": "<exact words from listing or null>",
        "other_motivation": "<exact motivation language from listing or null>",
        "seller_financing_available": <true or false or null>,
        "motivation_strength": "<'strong' | 'moderate' | 'weak' | 'unknown'>",
        "motivation_strength_reason": "<one sentence based only on what signals were found>"
      }},

      "handsoff_assessment": {{
        "appears_gm_manageable": <true or false or null>,
        "assessment_basis": "<what specific information this assessment is based on — if null say 'insufficient information in listing signal'>",
        "concerns": ["<specific concern about hands-off operability if any — based on evidence only>"],
        "owner_hours_mentioned": "<exact text if owner hours were mentioned or null>"
      }},

      "data_quality": {{
        "data_confidence": "<from researcher: 'high' | 'medium' | 'low'>",
        "data_source": "<from researcher>",
        "specific_listing_url_found": <true or false>,
        "known_fields": ["<list of fields that have real non-null values>"],
        "missing_fields": [
          "<list every important field that is null — be thorough>",
          "Examples: annual_revenue, annual_profit_sde, years_in_business, owner_hours, number_of_employees"
        ],
        "missing_fields_impact": "<one sentence on how the missing data affects triage reliability>"
      }},

      "sba_signal": {{
        "appears_sba_eligible": <true or false or null>,
        "eligibility_basis": "<what this is based on — be specific about what is known vs assumed>",
        "price_fits_standard_sba": <true or false or null>,
        "price_fits_goodwill_sba": <true or false or null>,
        "years_in_business_meets_minimum": <true or false or null>,
        "sba_red_flags": ["<specific SBA concern if evidence exists — not general assumptions>"]
      }},

      "source": {{
        "platform": "<from researcher>",
        "specific_listing_url": "<from researcher or null>",
        "category_page_url": "<from researcher or null>",
        "verify_url": "<best URL for manual verification — from researcher>",
        "listing_id": "<from researcher or null>",
        "listing_type": "<from researcher>"
      }},

      "manual_verification_checklist": [
        {{
          "priority": "<'must_check' | 'should_check' | 'nice_to_check'>",
          "question": "<specific question the buyer needs to answer>",
          "where_to_find": "<exact instruction: URL to visit, who to call, what to ask>",
          "why_it_matters": "<one sentence on why this matters for the acquisition decision>"
        }}
      ],

      "preview_text_raw": "<exact preview text from researcher — do not modify>",
      "scout_notes": "<honest analyst commentary on this lead — what looks interesting, what concerns you, what is unknown. Based only on available signals.>"
    }}
  ],

  "scout_notes": "<overall commentary on the batch — signal quality, patterns, what was strong, what was weak>",

  "total_leads": <number>,

  "triage_summary": {{
    "high_priority": <count>,
    "medium_priority": <count>,
    "low_priority": <count>
  }}
}}

VERIFICATION CHECKLIST GUIDANCE:
Every lead must have at minimum these checklist items:
1. MUST CHECK — Confirm asking price and whether financials are available
   (Where: log into the platform at verify_url, check the financials tab)
2. MUST CHECK — Confirm exact location and distance from {zip_code}
   (Where: listing page or ask broker directly)
3. MUST CHECK — Confirm years in business and SBA eligibility
   (Where: listing page financials, ask broker for business tax returns)
4. MUST CHECK — Confirm whether seller financing is available and on what terms
   (Where: contact the listing broker directly)
5. SHOULD CHECK — Assess whether a general manager could run this day-to-day
   (Where: ask broker about current owner involvement and staff structure)
6. SHOULD CHECK — Request a copy of the last 2 years of business tax returns
   (Where: ask broker — this is standard for any serious acquisition inquiry)

Add additional checklist items specific to the business type and signals found.
For example a child care center needs: licensing requirements, staff certifications,
regulatory compliance checks, lease terms on the facility.

TRIAGE PRIORITY RULES — apply strictly:
HIGH:   specific_listing_url found AND price within budget AND location confirmed
        AND retirement/motivation signal present AND no SBA red flags
MEDIUM: missing ONE of the above criteria
LOW:    missing TWO OR MORE criteria, OR any red flag present

Do not upgrade a lead's priority because it seems interesting.
Apply the rules as written."""

    print("[Scout] Calling OpenAI gpt-4o to structure triage leads...")

    response = client.chat.completions.create(
        model=cfg.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1,
        max_tokens=6000,
        response_format={"type": "json_object"}
    )

    raw = response.choices[0].message.content.strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[Scout] JSON parse error: {e}")
        print(f"[Scout] Raw preview:\n{raw[:400]}")
        raise RuntimeError(f"Scout agent returned invalid JSON: {e}")

    leads = result.get("leads", [])

    # Enforce verification_required=true on every lead
    # and validate triage priority rules
    corrections = 0
    for lead in leads:
        if lead.get("verification_required") is not True:
            lead["verification_required"] = True
            corrections += 1

        # Enforce: no specific URL = cannot be HIGH priority
        has_specific_url = lead.get(
            "data_quality", {}
        ).get("specific_listing_url_found", False)

        if not has_specific_url and lead.get("triage_priority") == "HIGH":
            lead["triage_priority"] = "MEDIUM"
            lead["triage_priority_reason"] = (
                lead.get("triage_priority_reason", "") +
                " [Downgraded from HIGH: no specific listing URL found]"
            )
            corrections += 1

    if corrections > 0:
        print(f"[Scout] Applied {corrections} priority/verification corrections")

    # Recount triage summary after corrections
    high = sum(1 for l in leads if l.get("triage_priority") == "HIGH")
    med = sum(1 for l in leads if l.get("triage_priority") == "MEDIUM")
    low = sum(1 for l in leads if l.get("triage_priority") == "LOW")

    result["triage_summary"] = {
        "high_priority": high,
        "medium_priority": med,
        "low_priority": low
    }
    result["total_leads"] = len(leads)

    print(f"[Scout] Complete — "
          f"{len(leads)} leads structured | "
          f"HIGH={high}, MEDIUM={med}, LOW={low} | "
          f"all marked verification_required=true")

    return result


if __name__ == "__main__":
    # Quick local test — runs planner, researcher, then scout
    # python agents/scout.py
    import sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from agents.planner import run as planner_run
    from agents.researcher import run as researcher_run

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
    result = run(
        researcher_output=researcher_output,
        brief=brief
    )

    print("\n── Scout output ──")
    print(json.dumps(result, indent=2))