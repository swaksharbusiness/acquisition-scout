# config.py
# Central configuration for the Acquisition Scout
# All agents import from here — do not hardcode values in individual agents

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# OpenAI
# ─────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = "gpt-4o"

# ─────────────────────────────────────────────
# Serper search API
# ─────────────────────────────────────────────
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
SERPER_ENDPOINT = "https://google.serper.dev/search"
MAX_SEARCH_RESULTS_PER_QUERY = 10
SEARCH_DELAY_SECONDS = 1.0     # pause between Serper calls to avoid rate limiting

# ─────────────────────────────────────────────
# Search defaults (overridden by config_state.json at runtime)
# ─────────────────────────────────────────────
DEFAULT_ZIP = "66214"
DEFAULT_LOCATION_LABEL = "Shawnee, Kansas (Kansas City metro)"
DEFAULT_RADIUS_MILES = 50
DEFAULT_LIQUID_CASH = 50_000

# ─────────────────────────────────────────────
# HONESTY POLICY
# Injected into every agent's system prompt.
# Non-negotiable — overrides all other instructions.
# ─────────────────────────────────────────────
HONESTY_POLICY = """
HONESTY POLICY — NON-NEGOTIABLE — OVERRIDES ALL OTHER INSTRUCTIONS:

1. NEVER invent, fabricate, estimate, or approximate any data point.
   This includes business names, addresses, asking prices, revenue figures,
   profit figures, URLs, owner ages, years in business, or seller signals.

2. If a piece of information is not explicitly visible on the actual page
   or source you are reading, you MUST use null for numbers and
   "NOT_FOUND" for strings. No exceptions.

3. NEVER paste a category page or browse page URL as if it were a specific
   listing URL. URL classification is handled deterministically by url_rules.py
   before you receive the data — do not override it.

4. NEVER round numbers to make them look cleaner. Report exactly what
   the source says. If the source says "$97,000" report 97000. If the
   source says "low $400s" report null and put "low $400s" in the raw
   string field.

5. NEVER infer seller motivation from business type alone. Only report
   motivation signals explicitly stated in the listing text.

6. If you find zero qualifying leads after exhausting all searches, return
   an empty array. This is acceptable and expected sometimes.
   An empty honest result is infinitely better than a populated dishonest one.

7. Every finding must have verification_required: true — always.

8. If you are uncertain whether a business is within the search radius,
   set within_radius to null — not true, not false.

VIOLATION CONSEQUENCES:
Invented data invalidates the entire pipeline output for that run.
The user has explicitly stated they would rather see zero leads
than one fabricated lead.
"""

# Placeholder values — use these when data is not found
NULL_STRING = "NOT_FOUND"
NULL_NUMBER = None  # maps to JSON null

# ─────────────────────────────────────────────
# SBA 7(a) Loan Rules — research-verified April 2025
# ─────────────────────────────────────────────
SBA_STANDARD_DOWN_PAYMENT_PCT = 0.10
SBA_HIGH_GOODWILL_DOWN_PAYMENT_PCT = 0.15
SBA_GOODWILL_THRESHOLD_PCT = 0.50
SBA_MIN_DSCR = 1.15
SBA_PREFERRED_DSCR = 1.25
SBA_MIN_CREDIT_SCORE = 680
SBA_MAX_LOAN_AMOUNT = 5_000_000
SBA_EXPRESS_LOAN_CAP = 500_000
SBA_MIN_YEARS_IN_BUSINESS = 2

SBA_INELIGIBLE_BUSINESS_TYPES = [
    "real estate investment",
    "passive investment",
    "lending institution",
    "insurance company",
    "pyramid scheme",
    "gambling",
    "speculative business",
    "non-profit",
    "religious organization",
    "government-owned entity",
]


def compute_max_deal_size(liquid_cash: float) -> dict:
    """
    Returns both standard and high-goodwill max deal sizes.
    Standard:     liquid_cash / 0.10  (10% down)
    High-goodwill: liquid_cash / 0.15 (15% down, when goodwill > 50%)
    """
    standard_max = liquid_cash / SBA_STANDARD_DOWN_PAYMENT_PCT
    high_goodwill_max = liquid_cash / SBA_HIGH_GOODWILL_DOWN_PAYMENT_PCT

    return {
        "liquid_cash": liquid_cash,
        "standard_max_deal": round(standard_max, 2),
        "high_goodwill_max_deal": round(high_goodwill_max, 2),
        "standard_down_payment_pct": SBA_STANDARD_DOWN_PAYMENT_PCT,
        "high_goodwill_down_payment_pct": SBA_HIGH_GOODWILL_DOWN_PAYMENT_PCT,
        "standard_down_payment_amount": liquid_cash,
        "high_goodwill_down_payment_amount": liquid_cash,
        "note": (
            f"With ${liquid_cash:,.0f} liquid: "
            f"standard max is ${standard_max:,.0f} (10% down), "
            f"high-goodwill max is ${high_goodwill_max:,.0f} (15% down)."
        )
    }


# ─────────────────────────────────────────────
# Buyer profile
# ─────────────────────────────────────────────
BUYER_PROFILE = """
The buyer is a software engineer working a full-time remote job.
They need a completely hands-off business that can be run by a hired general manager.
They have no industry preference — the best deal wins.
They are tech-savvy and can modernize operations, add software tooling, and improve
digital presence in any business they acquire — this is a value-add advantage.
They are pursuing an SBA 7(a) loan for financing.
They are open to seller financing as part of the deal structure.
Extra attention should be given to businesses where the owner is near retirement age
(55+) or explicitly retiring — but this is a bonus signal, not a hard requirement.
Any legitimate, SBA-eligible business that can be managed hands-off qualifies.
"""

# ─────────────────────────────────────────────
# Agent pipeline settings
# ─────────────────────────────────────────────
VALIDATOR_MAX_LOOPS = 3
VALIDATOR_MIN_SCORE = 7
TARGET_LEAD_COUNT = 6

# ─────────────────────────────────────────────
# Retry actions — finite enum for structured retry guidance
# Validator selects from this set — Researcher responds deterministically
# Must stay in sync with schemas.models.RetryAction enum
# ─────────────────────────────────────────────
RETRY_ACTIONS = {
    "increase_pass2_hunts": {
        "description": "Increase MAX_FOLLOWUP_SEARCHES from 6 to 9",
        "researcher_response": "Set MAX_FOLLOWUP_SEARCHES = 9 for this retry"
    },
    "add_price_filter_to_queries": {
        "description": "Append price ceiling to all search queries",
        "researcher_response": "Append 'under $500,000' to every search query"
    },
    "use_broker_specific_queries": {
        "description": "Prioritize named-broker queries over generic ones",
        "researcher_response": "Run broker-name queries first, generic last"
    },
    "broaden_search_radius": {
        "description": "Expand search to adjacent cities beyond current radius",
        "researcher_response": "Add cities 50-75 miles from ZIP to search area"
    },
    "target_different_industries": {
        "description": "Avoid industries that produced rejections",
        "researcher_response": "Exclude food & beverage, focus on services"
    },
    "use_different_platforms": {
        "description": "Try platforms not used in previous pass",
        "researcher_response": "Prioritize BusinessBroker.net and LoopNet"
    },
    "filter_by_years_in_business": {
        "description": "Add years-in-business filter to queries",
        "researcher_response": "Append 'established business' or '10+ years' to queries"
    },
    "exclude_food_and_beverage": {
        "description": "Exclude restaurants and food businesses",
        "researcher_response": "Add '-restaurant -food -cafe' to search queries"
    }
}

# ─────────────────────────────────────────────
# Validator thresholds
# ─────────────────────────────────────────────

# Tiered required fields — replaces flat MIN_REQUIRED_FIELDS count
# Must-have: if ANY of these missing → hard fail
VALIDATOR_MUST_HAVE_FIELDS = [
    "business_snapshot.business_type",
    "business_snapshot.location.city",
]

# Must have at least ONE of these → hard fail if both missing
VALIDATOR_MUST_HAVE_ONE_OF = [
    "source.verify_url",
    "source.specific_listing_url",
]

# Strongly preferred — missing ones generate warnings
VALIDATOR_STRONGLY_PREFERRED_FIELDS = [
    "business_snapshot.asking_price",
    "business_snapshot.asking_price_raw",
    "business_snapshot.years_in_business",
    "seller_motivation.motivation_language",
]

# Minimum approved leads before dashboard runs without retry
MIN_LEADS_TO_PASS = 2

# Data confidence weights for priority assignment
CONFIDENCE_WEIGHTS = {
    "high": 3,
    "medium": 2,
    "low": 1
}

# ─────────────────────────────────────────────
# File paths
# ─────────────────────────────────────────────
MEMORY_FILE = "ideas_memory.json"
CONFIG_STATE_FILE = "config_state.json"
DASHBOARD_FILE = "docs/index.html"
RUNS_DIR = "runs"  # run artifacts written here — runs/{run_id}/{stage}.json

# ─────────────────────────────────────────────
# Broker and listing sources
# ─────────────────────────────────────────────
LISTING_SOURCES = [
    "BizBuySell.com",
    "BizQuest.com",
    "BusinessBroker.net",
    "LoopNet.com",
    "Sunbelt Business Brokers Kansas City",
    "Murphy Business Brokers Kansas City",
    "VR Business Brokers Kansas City",
    "Kansas City Business Journal listings",
    "Exit Factor Kansas City",
]

# ─────────────────────────────────────────────
# Motivated seller signals
# ─────────────────────────────────────────────
MOTIVATED_SELLER_SIGNALS = [
    "owner retiring",
    "owner relocating",
    "health reasons",
    "estate sale",
    "family circumstances",
    "priced to sell",
    "motivated seller",
    "owner will finance",
    "seller financing available",
    "been listed 6+ months",
    "price reduced",
    "owner near retirement age (55+)",
    "business listed by family member",
    "no children to pass business to",
]

# ─────────────────────────────────────────────
# Data quality
# ─────────────────────────────────────────────
DATA_QUALITY = {
    "high_confidence_required_fields": [
        "financials.asking_price",
        "source.specific_listing_url",
        "location.city",
        "business_type",
    ],
    "medium_confidence_required_fields": [
        "source.verify_url",
        "location.city",
        "business_type",
    ],
    "minimum_viable_fields": [
        "business_type",
        "location.city",
    ],
}