# agents/planner.py
# Planner Agent — first in the pipeline
# Reads the runtime config (zip, cash, radius) and produces a structured
# search brief as a JSON object. Does NOT search the web. Only plans.
# Every other agent downstream receives this brief as its starting context.

import json
import os
import sys
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg

client = OpenAI(api_key=cfg.OPENAI_API_KEY)


def run(zip_code: str, liquid_cash: float, radius_miles: int) -> dict:
    """
    Takes the three runtime parameters and produces a fully structured
    search brief for the Researcher Agent.

    Returns a dict with:
      - location_context
      - buyer_summary
      - sba_deal_math
      - search_queries
      - broker_targets
      - motivated_seller_signals
      - qualification_checklist
      - exclusion_list
      - target_industries
      - research_instructions
    """

    print(f"[Planner] Starting — zip={zip_code}, "
          f"cash=${liquid_cash:,.0f}, radius={radius_miles}mi")

    deal_math = cfg.compute_max_deal_size(liquid_cash)

    system_prompt = f"""You are a senior acquisition advisor specializing in
small business acquisitions financed through SBA 7(a) loans.

Your job is to produce a precise, structured search brief that will guide
a team of AI research agents to find the best acquisition opportunities.

{cfg.HONESTY_POLICY}

PLANNING RULES:
- You are only planning — you are not searching the web yet.
- Every search query you write must be a real string someone would type 
  into Google. No placeholders, no angle brackets, no template variables.
- Every broker or platform you list must be a real business that actually
  serves the Kansas City metro area. Do not invent broker names.
- Every target industry must be genuinely viable within a {radius_miles}-mile
  radius of ZIP {zip_code}. Do not list industries that don't exist in 
  suburban Kansas.
- If you are not confident what city ZIP {zip_code} belongs to, state
  "NOT_FOUND" in the city field rather than guessing.
- You output ONLY valid JSON. No markdown, no code fences, no explanation.
  No text before or after the JSON object."""

    user_prompt = f"""Create a detailed acquisition search brief for this profile:

LOCATION: ZIP code {zip_code}
Identify the exact city, county, and metro area this ZIP belongs to.
List the real cities within {radius_miles} miles — only cities that actually
exist in that geography. Do not invent or approximate city names.

BUYER PROFILE:
{cfg.BUYER_PROFILE}

SBA DEAL MATH (use exactly these numbers — do not recalculate):
- Liquid cash available: ${liquid_cash:,.0f}
- Standard max deal size (10% down): ${deal_math['standard_max_deal']:,.0f}
- High-goodwill max deal size (15% down): ${deal_math['high_goodwill_max_deal']:,.0f}
- Goodwill rule triggers when goodwill exceeds 50% of purchase price
- SBA minimum DSCR: {cfg.SBA_MIN_DSCR}x (lenders prefer {cfg.SBA_PREFERRED_DSCR}x)
- Minimum years in business: {cfg.SBA_MIN_YEARS_IN_BUSINESS}
- Minimum credit score: {cfg.SBA_MIN_CREDIT_SCORE}

SEARCH RADIUS: {radius_miles} miles from ZIP {zip_code}

REAL LISTING PLATFORMS TO TARGET:
{json.dumps(cfg.LISTING_SOURCES, indent=2)}

MOTIVATED SELLER SIGNALS TO PRIORITIZE:
{json.dumps(cfg.MOTIVATED_SELLER_SIGNALS, indent=2)}

SBA INELIGIBLE TYPES TO EXCLUDE:
{json.dumps(cfg.SBA_INELIGIBLE_BUSINESS_TYPES, indent=2)}

Produce a JSON object with EXACTLY these fields and no others:

{{
  "location_context": {{
    "zip_code": "{zip_code}",
    "city": "<exact city — NOT_FOUND if uncertain>",
    "county": "<exact county — NOT_FOUND if uncertain>",
    "metro_area": "<metro area name — NOT_FOUND if uncertain>",
    "surrounding_cities": [
      "<only real cities within {radius_miles} miles — verified geography>"
    ],
    "search_area_description": "<one factual sentence describing the search area>"
  }},

  "buyer_summary": {{
    "occupation": "Software engineer, full-time remote",
    "management_style": "Hands-off — will hire a general manager",
    "tech_advantage": "Can modernize operations, add software tooling, improve digital presence",
    "industry_preference": "None — best deal wins",
    "retirement_seller_priority": "High — extra attention to owners 55+ or explicitly retiring, but not required",
    "financing": "SBA 7(a) loan + seller financing welcome",
    "liquid_cash": {liquid_cash},
    "standard_max_deal": {deal_math['standard_max_deal']},
    "high_goodwill_max_deal": {deal_math['high_goodwill_max_deal']}
  }},

  "search_queries": [
    "<query 1 — real Google search string, site-specific to bizbuysell.com>",
    "<query 2 — real Google search string, site-specific to bizquest.com>",
    "<query 3 — retiring owner business for sale in specific KC suburb>",
    "<query 4 — specific industry + location + for sale>",
    "<query 5 — motivated seller business Kansas City metro under $500k>",
    "<query 6 — seller financing business for sale Johnson County KS>",
    "<query 7 — specific broker name + Kansas City + business for sale>",
    "<query 8 — estate sale business Kansas City OR Overland Park OR Shawnee>",
    "<query 9 — owner retiring business for sale Olathe OR Lenexa OR Leawood>",
    "<query 10 — specific recession-resistant industry + Kansas City + acquisition>"
  ],

  "broker_targets": [
    {{
      "name": "<real broker name that serves KC metro>",
      "url": "<real verified URL>",
      "search_instruction": "<specific instruction: what to search for on this platform, including price range and location filters to apply>"
    }}
  ],

  "qualification_checklist": [
    "Business must be physically located within {radius_miles} miles of ZIP {zip_code}",
    "Asking price must be at or below ${deal_math['standard_max_deal']:,.0f} (standard SBA case) or ${deal_math['high_goodwill_max_deal']:,.0f} (high-goodwill case)",
    "Business must have been operating for at least {cfg.SBA_MIN_YEARS_IN_BUSINESS} years to qualify for SBA financing",
    "Business must be SBA 7(a) eligible — not on the ineligible list",
    "Business must be operable by a hired general manager without owner present",
    "Business must show signs of stable or growing revenue — not in distress"
  ],

  "exclusion_list": [
    "<specific exclusion 1 — SBA ineligible type>",
    "<specific exclusion 2 — owner-dependent businesses like solo practitioners>",
    "<specific exclusion 3 — businesses requiring specialized licenses buyer cannot obtain>",
    "<specific exclusion 4 — businesses priced above the SBA deal ceiling>",
    "<specific exclusion 5 — businesses outside the {radius_miles}-mile radius>",
    "<specific exclusion 6 — startups or businesses under 2 years old>",
    "<specific exclusion 7 — businesses in active financial distress or bankruptcy>"
  ],

  "motivated_seller_signals": {json.dumps(cfg.MOTIVATED_SELLER_SIGNALS)},

  "target_industries": [
    {{
      "industry": "<real industry that exists and is common in KC suburbs>",
      "why_fits": "<specific one-sentence reason this works for a hands-off tech buyer>",
      "typical_price_range": "<realistic price range for this industry at small business scale>",
      "goodwill_heavy": <true or false>,
      "gm_manageable": <true or false>,
      "common_in_area": <true or false>
    }}
  ],

  "research_instructions": {{
    "total_leads_to_find": {cfg.TARGET_LEAD_COUNT},
    "early_leads_count": 3,
    "serious_listings_count": 3,
    "early_lead_definition": "Promising signal found during research — business not confirmed for sale but shows acquisition potential based on owner age, business maturity, or industry fit",
    "serious_listing_definition": "Business actively listed for sale on a real platform with a specific listing URL, asking price visible, and at least partial seller details available",
    "priority_order": "Motivated seller signals first, then price fit, then industry, then location within radius",
    "data_integrity": "Only report what is explicitly visible on the actual source page. Null is always correct when data is missing. Never estimate or infer financial figures.",
    "avoid_repeating": "The researcher will receive a list of already-seen business IDs — skip all of them without exception"
  }}
}}

FINAL REMINDER:
- Search queries must be real strings — no angle brackets or placeholders in the final output
- Broker URLs must be real URLs — verify they serve the Kansas City area
- Surrounding cities must be real cities geographically within {radius_miles} miles of {zip_code}
- Target industries must be genuinely present in suburban Kansas City
- If you are uncertain about any fact, use NOT_FOUND rather than guessing"""

    print("[Planner] Calling OpenAI gpt-4o...")

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
        brief = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[Planner] JSON parse error: {e}")
        print(f"[Planner] Raw response preview:\n{raw[:500]}")
        raise RuntimeError(f"Planner agent returned invalid JSON: {e}")

    # Validate that city was actually identified
    city = brief.get("location_context", {}).get("city", cfg.NULL_STRING)
    if city == cfg.NULL_STRING:
        print(f"[Planner] WARNING — could not identify city for ZIP {zip_code}")

    # Validate search queries contain no template placeholders
    queries = brief.get("search_queries", [])
    bad_queries = [q for q in queries if "<" in q or ">" in q]
    if bad_queries:
        print(f"[Planner] WARNING — {len(bad_queries)} queries contain placeholders "
              f"and will be filtered out by the researcher")
        brief["search_queries"] = [q for q in queries if "<" not in q and ">" not in q]

    # Inject computed deal math
    brief["sba_deal_math"] = deal_math
    brief["radius_miles"] = radius_miles
    brief["zip_code"] = zip_code

    city = brief.get("location_context", {}).get("city", "unknown")
    industry_count = len(brief.get("target_industries", []))
    query_count = len(brief.get("search_queries", []))

    print(f"[Planner] Complete — "
          f"city={city}, "
          f"industries={industry_count}, "
          f"queries={query_count}, "
          f"max_deal=${deal_math['standard_max_deal']:,.0f}")

    return brief


if __name__ == "__main__":
    # Quick local test
    # python agents/planner.py
    result = run(
        zip_code=cfg.DEFAULT_ZIP,
        liquid_cash=cfg.DEFAULT_LIQUID_CASH,
        radius_miles=cfg.DEFAULT_RADIUS_MILES
    )
    print("\n── Planner output ──")
    print(json.dumps(result, indent=2))