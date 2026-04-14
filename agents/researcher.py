# agents/researcher.py
# Researcher Agent — second in the pipeline
#
# Architecture:
#   Pass 1 — Python calls Serper API for each query
#             Python extracts URLs and snippets deterministically
#             LLM classifies relevance only (never copies data)
#
#   Pass 2 — For signals needing follow-up (no specific URL found):
#             Python builds listing-ID queries deterministically
#             LLM generates phrase + fallback queries (reasoning only)
#             Python calls Serper with all queries
#             Python extracts specific listing URLs found
#
#   Consolidation — Pure Python merge of Pass 1 and Pass 2
#                   No LLM involved in consolidation
#                   canonical_listing_key computed deterministically
#
# LLM calls: 2 maximum
#   Pass 1: relevance classification only
#   Pass 2: phrase + fallback query generation only
#           (listing ID queries built in Python)
#
# Pipeline position: 2 of 6
# Receives from: Planner Agent
# Passes to: Scout Agent

import json
import os
import sys
import re
import time
import datetime
from pathlib import Path
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg
import url_rules
from schemas.models import RetryAction

client = OpenAI(api_key=cfg.OPENAI_API_KEY)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
MAX_FOLLOWUP_SEARCHES = 6
PASS2_RELEVANCE_THRESHOLD = 5
SEARCH_RESULTS_PER_QUERY = 10


# ─────────────────────────────────────────────
# Artifact writing
# ─────────────────────────────────────────────
def _write_artifact(run_id: str, stage: str, data: dict):
    """
    Writes stage artifact to runs/{run_id}/{stage}.json.
    Never raises — artifact failure must not kill the pipeline.
    """
    try:
        runs_dir = Path(cfg.RUNS_DIR) / run_id
        runs_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = runs_dir / f"{stage}.json"
        artifact_path.write_text(json.dumps(data, indent=2))
        print(f"  [Researcher] Artifact written: {artifact_path}")
    except Exception as e:
        print(f"  [Researcher] WARNING — artifact write failed: {e}")


# ─────────────────────────────────────────────
# Retry action responses — pure Python
# ─────────────────────────────────────────────
def _apply_retry_actions(
    retry_actions: list,
    search_queries: list,
    max_followup: int
) -> tuple:
    """
    Pure Python — no LLM.
    Applies structured retry actions from Validator deterministically.
    Returns (adjusted_queries, adjusted_max_followup).
    """
    adjusted_queries = list(search_queries)
    adjusted_max_followup = max_followup

    for action in retry_actions:
        action_str = (
            action if isinstance(action, str) else action.value
        )

        if action_str == RetryAction.INCREASE_PASS2_HUNTS.value:
            adjusted_max_followup = min(max_followup + 3, 9)
            print(f"  [Researcher] Retry: MAX_FOLLOWUP → {adjusted_max_followup}")

        elif action_str == RetryAction.ADD_PRICE_FILTER.value:
            adjusted_queries = [q + " under $500,000" for q in adjusted_queries]
            print(f"  [Researcher] Retry: price filter appended")

        elif action_str == RetryAction.USE_BROKER_SPECIFIC_QUERIES.value:
            broker_kws = ["sunbelt", "murphy", "vr business", "broker", "exit factor"]
            broker_q = [
                q for q in adjusted_queries
                if any(kw in q.lower() for kw in broker_kws)
            ]
            other_q = [
                q for q in adjusted_queries
                if not any(kw in q.lower() for kw in broker_kws)
            ]
            adjusted_queries = broker_q + other_q
            print(f"  [Researcher] Retry: broker queries prioritized")

        elif action_str == RetryAction.TARGET_DIFFERENT_INDUSTRIES.value:
            adjusted_queries = [
                q for q in adjusted_queries
                if not any(
                    kw in q.lower()
                    for kw in ["restaurant", "food", "cafe", "bar", "diner"]
                )
            ]
            print(f"  [Researcher] Retry: food queries removed, "
                  f"{len(adjusted_queries)} remain")

        elif action_str == RetryAction.USE_DIFFERENT_PLATFORMS.value:
            extra = []
            if not any("businessbroker" in q.lower() for q in adjusted_queries):
                extra.append(
                    "site:businessbroker.net kansas city business for sale "
                    "retiring owner under $500k"
                )
            if not any("loopnet" in q.lower() for q in adjusted_queries):
                extra.append(
                    "site:loopnet.com kansas city small business for sale "
                    "motivated seller"
                )
            adjusted_queries = adjusted_queries + extra
            print(f"  [Researcher] Retry: {len(extra)} platform queries added")

        elif action_str == RetryAction.FILTER_BY_YEARS_IN_BUSINESS.value:
            adjusted_queries = [
                q + " established business" for q in adjusted_queries
            ]
            print(f"  [Researcher] Retry: 'established business' appended")

        elif action_str == RetryAction.EXCLUDE_FOOD_AND_BEVERAGE.value:
            adjusted_queries = [
                q + " -restaurant -food -cafe -bar" for q in adjusted_queries
            ]
            print(f"  [Researcher] Retry: food exclusion appended")

    return adjusted_queries, adjusted_max_followup


# ─────────────────────────────────────────────
# Pass 1 — Real search via Serper + LLM classification
# ─────────────────────────────────────────────
def _pass1_search(
    brief: dict,
    seen_canonical_keys: list,
    adjusted_queries: list
) -> dict:
    """
    Pass 1 — Executes real web searches via Serper.
    Python handles all data extraction.
    LLM classifies relevance only — never touches raw URLs or snippets.

    Steps (in order):
      1. Python calls Serper per query              — deterministic
      2. Python extracts title, URL, snippet        — deterministic
      3. Python classifies URL via url_rules        — deterministic
      4. Python computes canonical_listing_key      — deterministic
      5. Python deduplicates against memory         — deterministic
      6. LLM classifies relevance of clean signals  — non-deterministic (correct)
      7. Python validates LLM did not modify keys   — deterministic
    """
    print(f"  [Researcher:Pass1] Executing {len(adjusted_queries)} "
          f"Serper searches...")

    deal_math = brief.get(
        "sba_deal_math",
        cfg.compute_max_deal_size(cfg.DEFAULT_LIQUID_CASH)
    )
    location = brief.get("location_context", {})
    city = location.get("city", "Kansas City")
    radius = brief.get("radius_miles", cfg.DEFAULT_RADIUS_MILES)
    exclusion_list = brief.get("exclusion_list", [])
    qual_checklist = brief.get("qualification_checklist", [])

    # ── Steps 1-4: Python calls Serper, extracts and classifies ──
    all_search_results = url_rules.batch_search(
        queries=adjusted_queries,
        num_results=SEARCH_RESULTS_PER_QUERY,
        delay_seconds=cfg.SEARCH_DELAY_SECONDS,
        serper_api_key=cfg.SERPER_API_KEY
    )

    # ── Steps 3-5: Python builds raw signals with canonical keys ──
    raw_signals_for_llm = []
    sources_checked = set()
    total_results = 0
    specific_url_count = 0
    deduped_count = 0

    for query, results in all_search_results.items():
        total_results += len(results)
        for result in results:
            url = result.get("url", "")
            classification = result.get("classified", {})
            platform = classification.get("platform") or "unknown"
            sources_checked.add(platform)

            if classification.get("is_specific"):
                specific_url_count += 1

            listing_id = classification.get("listing_id")
            specific_url = url if classification.get("is_specific") else None

            canonical_key = url_rules.compute_canonical_key(
                specific_listing_url=specific_url,
                platform=platform,
                listing_id=listing_id
            )

            # Deduplicate against memory
            if canonical_key in seen_canonical_keys:
                deduped_count += 1
                continue

            # Use index as LLM-facing ID to prevent key modification
            signal_index = len(raw_signals_for_llm)

            raw_signals_for_llm.append({
                "_index": signal_index,          # LLM uses this, not canonical key
                "title": result.get("title", ""),
                "url": url,
                "snippet": result.get("snippet", ""),
                "position": result.get("position", 0),
                # These are stored for Python use after LLM returns
                # LLM is NOT asked to copy or modify them
                "_canonical_key": canonical_key,
                "_platform": platform,
                "_is_specific": classification.get("is_specific", False),
                "_listing_id": listing_id,
                "_normalized_url": classification.get("normalized_url", ""),
                "_url_type": classification.get("url_type", "unknown"),
                "_query": query
            })

    print(f"  [Researcher:Pass1] Serper: {total_results} results | "
          f"{len(raw_signals_for_llm)} unique new signals | "
          f"{specific_url_count} specific URLs | "
          f"{deduped_count} deduped from memory")

    if not raw_signals_for_llm:
        print("  [Researcher:Pass1] No new signals — returning empty")
        return {
            "searches_executed": adjusted_queries,
            "sources_checked": list(sources_checked),
            "signals": [],
            "pass1_notes": "All results already in memory or searches returned nothing."
        }

    # Build LLM-facing payload — strip internal Python fields (prefixed _)
    llm_payload = [
        {k: v for k, v in s.items() if not k.startswith("_")}
        for s in raw_signals_for_llm
    ]

    # ── Step 6: LLM classifies relevance only ──────────────
    print(f"  [Researcher:Pass1] LLM classifying "
          f"{len(llm_payload)} signals for relevance...")

    system_prompt = f"""You are classifying web search results for relevance
to a small business acquisition search.

You receive structured results already extracted by Python.
Your ONLY jobs are:
1. Score each result's relevance (1-10)
2. Extract business signals ONLY from the title and snippet provided
3. Identify business type and any seller motivation language

{cfg.HONESTY_POLICY}

CRITICAL RULES:
- You receive an _index field per result — return it unchanged
- Extract ONLY what is explicitly in the title or snippet
- Do NOT visit URLs — read only what is given to you
- Do NOT invent prices, business names, or details not in the snippet
- If a field is not in the snippet set it to null
- preview_text_raw must be the exact snippet — never paraphrase
- verification_required is always true
- You output ONLY valid JSON. No markdown, no code fences."""

    user_prompt = f"""Classify these search results for relevance to this
acquisition search. Return classifications indexed by _index.

SEARCH CRITERIA:
- Location: within {radius} miles of {city}
- Max deal: ${deal_math['standard_max_deal']:,.0f} standard /
  ${deal_math['high_goodwill_max_deal']:,.0f} high-goodwill
- Buyer: hands-off, any industry, SBA financing
- Priority: retiring or motivated sellers

QUALIFICATION CHECKLIST:
{json.dumps(qual_checklist, indent=2)}

EXCLUSION LIST:
{json.dumps(exclusion_list, indent=2)}

SEARCH RESULTS:
{json.dumps(llm_payload, indent=2)}

Return this EXACT JSON:
{{
  "classified_signals": [
    {{
      "_index": <copy integer from input — do not modify>,
      "business_type": "<from title/snippet or null>",
      "industry": "<category or null>",
      "location_from_snippet": "<city/state from snippet or null>",
      "price_from_snippet": "<price EXACTLY as written in snippet or null>",
      "years_from_snippet": "<years EXACTLY as written in snippet or null>",
      "seller_signal_from_snippet": "<EXACT motivation words from snippet or null>",
      "seller_financing_mentioned": <true if financing mentioned in snippet else false>,
      "preview_text_raw": "<exact snippet — do not modify>",
      "relevance_score": <1-10>,
      "relevance_reason": "<one sentence>",
      "should_hunt_for_specific_url": <true if not specific listing AND score >= 5>,
      "verification_required": true
    }}
  ],
  "pass1_notes": "<honest summary of what appeared in results>"
}}"""

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
        llm_result = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [Researcher:Pass1] LLM JSON parse error: {e}")
        raise RuntimeError(f"Pass 1 classification returned invalid JSON: {e}")

    classified = llm_result.get("classified_signals", [])

    # ── Step 7: Python re-attaches canonical keys by index ──
    # LLM returned _index — Python looks up the canonical key
    # LLM never saw or touched the canonical key
    index_lookup = {s["_index"]: s for s in raw_signals_for_llm}
    final_signals = []

    for cls in classified:
        idx = cls.get("_index")
        if idx is None or idx not in index_lookup:
            print(f"  [Researcher:Pass1] WARNING — unknown index {idx}, skipping")
            continue

        original = index_lookup[idx]

        # Python attaches all deterministic fields — LLM cannot modify these
        merged = {
            **cls,
            "canonical_listing_key": original["_canonical_key"],
            "url": original["url"],                      # authoritative from Python
            "platform": original["_platform"],
            "is_specific_listing": original["_is_specific"],
            "listing_id": original["_listing_id"],
            "normalized_url": original["_normalized_url"],
            "url_type": original["_url_type"],
            "source_query": original["_query"],
            "verification_required": True                # enforce always
        }
        final_signals.append(merged)

    high_rel = sum(1 for s in final_signals if s.get("relevance_score", 0) >= 7)
    needs_hunt = sum(
        1 for s in final_signals
        if s.get("should_hunt_for_specific_url", False)
    )

    print(f"  [Researcher:Pass1] {len(final_signals)} classified | "
          f"{high_rel} high relevance | "
          f"{needs_hunt} need URL hunt")

    return {
        "searches_executed": adjusted_queries,
        "sources_checked": list(sources_checked),
        "signals": final_signals,
        "pass1_notes": llm_result.get("pass1_notes", "")
    }


# ─────────────────────────────────────────────
# Pass 2 — Targeted URL hunting
# Python builds ID queries, LLM builds phrase queries
# ─────────────────────────────────────────────
def _pass2_hunt(
    pass1_result: dict,
    brief: dict,
    adjusted_max_followup: int
) -> list:
    """
    Pass 2 — Hunts for specific listing URLs.

    Query generation split by task type:
      Python builds: listing ID queries (deterministic)
      LLM builds:    quoted phrase queries + broad fallback (reasoning)

    Serper executes all queries.
    Python extracts specific URLs from results.
    Python re-attaches canonical keys by index.
    """
    signals = pass1_result.get("signals", [])

    needs_hunt = [
        s for s in signals
        if s.get("should_hunt_for_specific_url", False)
        and not s.get("is_specific_listing", False)
        and s.get("relevance_score", 0) >= PASS2_RELEVANCE_THRESHOLD
        and s.get("business_type")
    ]

    if not needs_hunt:
        print("  [Researcher:Pass2] No signals need URL hunting — skipping")
        return []

    needs_hunt.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    to_hunt = needs_hunt[:adjusted_max_followup]
    skipped = len(needs_hunt) - len(to_hunt)

    print(f"  [Researcher:Pass2] {len(needs_hunt)} candidates — "
          f"hunting top {len(to_hunt)}, skipping {skipped}")

    location = brief.get("location_context", {})
    city = location.get("city", "Kansas City")

    # ── Python builds listing ID queries deterministically ──
    # Index-based to avoid LLM key modification risk
    hunt_targets_for_llm = []

    for idx, signal in enumerate(to_hunt):
        platform = signal.get("platform", "")
        listing_id = signal.get("listing_id")
        business_type = signal.get("business_type", "")
        location_text = signal.get("location_from_snippet", city)
        preview = (signal.get("preview_text_raw") or "")[:200]

        # Python builds query 1 — listing ID search (deterministic)
        if listing_id and platform:
            id_query = f"{platform} {listing_id}"
        elif listing_id:
            id_query = listing_id
        else:
            id_query = None  # LLM will handle all queries

        hunt_targets_for_llm.append({
            "_hunt_index": idx,          # index only — no canonical key
            "business_type": business_type,
            "location": location_text or city,
            "platform": platform,
            "price_from_snippet": signal.get("price_from_snippet"),
            "preview_text_raw": preview,
            "id_query_prebuilt": id_query  # Python-built query or null
        })

    # ── LLM generates phrase + fallback queries only ────────
    print(f"  [Researcher:Pass2] LLM generating phrase/fallback queries "
          f"for {len(to_hunt)} signals...")

    system_prompt = f"""You are generating targeted web search queries
to find specific listing pages for businesses seen in search previews.

{cfg.HONESTY_POLICY}

YOUR ONLY JOB:
Generate query 2 (short quoted phrase) and query 3 (broad fallback)
for each hunt target. Query 1 has already been built by Python.

QUERY RULES — follow exactly:
Query 2 — Short quoted phrase + platform name:
  - Pick the most unique 3-4 word phrase from preview_text_raw
  - Wrap it in quotes
  - Add the platform name after
  - Example: '"child care center Johnson County" bizbuysell'
  - Example: '"HVAC company retiring owner Olathe" bizquest'
  - Do NOT include price, dollar amounts, or motivation language
  - Do NOT use site: operator in query 2

Query 3 — Business type + city + site: operator:
  - Use business_type and one city name only
  - Add site:platform.com
  - Example: 'child care center Olathe site:bizbuysell.com'
  - Example: 'HVAC company Shawnee site:bizquest.com'
  - Maximum 4 words before site: operator

NEVER:
  - Use more than 4 constraints in a single query
  - Include price or dollar amounts
  - Include motivation words like retiring, motivated, estate
  - Generate a query that is the same as id_query_prebuilt
  - You output ONLY valid JSON. No markdown, no code fences."""

    user_prompt = f"""Generate search queries for these hunt targets.
Query 1 is already built (id_query_prebuilt).
Generate only query 2 and query 3.

HUNT TARGETS:
{json.dumps(hunt_targets_for_llm, indent=2)}

Return this EXACT JSON:
{{
  "hunt_queries": [
    {{
      "_hunt_index": <copy integer from input — do not modify>,
      "query_2_phrase": "<short quoted phrase + platform name>",
      "query_3_fallback": "<business type + city + site:platform.com>"
    }}
  ]
}}

REMINDER: _hunt_index is an integer — copy it exactly."""

    response = client.chat.completions.create(
        model=cfg.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        temperature=0.1,
        max_tokens=1500,
        response_format={"type": "json_object"}
    )

    raw = response.choices[0].message.content.strip()

    try:
        hunt_plan = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  [Researcher:Pass2] Hunt query parse error: {e}")
        print("  [Researcher:Pass2] Proceeding with ID queries only")
        hunt_plan = {"hunt_queries": []}

    # Build LLM query lookup by hunt index
    llm_query_lookup = {
        item.get("_hunt_index"): item
        for item in hunt_plan.get("hunt_queries", [])
    }

    # ── Python executes all queries via Serper ──────────────
    hunt_results = []

    for idx, signal in enumerate(to_hunt):
        canonical_key = signal.get("canonical_listing_key", "")
        platform = signal.get("platform", "")
        listing_id = signal.get("listing_id")
        target = hunt_targets_for_llm[idx]
        llm_queries = llm_query_lookup.get(idx, {})

        # Assemble all queries for this signal
        # Priority: id query first (Python-built), then phrase, then fallback
        all_queries = []

        # Query 1 — Python-built ID query
        id_query = target.get("id_query_prebuilt")
        if id_query:
            all_queries.append(("id_search", id_query))

        # Query 2 — LLM phrase query
        q2 = llm_queries.get("query_2_phrase", "")
        if q2 and q2 != "NOT_AVAILABLE" and "<" not in q2:
            all_queries.append(("phrase_search", q2))

        # Query 3 — LLM fallback query
        q3 = llm_queries.get("query_3_fallback", "")
        if q3 and q3 != "NOT_AVAILABLE" and "<" not in q3:
            all_queries.append(("fallback_search", q3))

        print(f"  [Researcher:Pass2] ({idx+1}/{len(to_hunt)}) "
              f"{signal.get('business_type', 'unknown')} — "
              f"{len(all_queries)} queries to try")

        found_specific_url = None
        found_listing_id = None
        found_by_query_type = None
        all_queries_tried = []

        for query_type, query in all_queries:
            if found_specific_url:
                break

            print(f"    [{query_type}] {query[:80]}")

            results = url_rules.search(
                query=query,
                num_results=5,
                serper_api_key=cfg.SERPER_API_KEY
            )
            all_queries_tried.append({
                "type": query_type,
                "query": query,
                "results_returned": len(results)
            })

            # Python checks each result for specific listing URL
            for result in results:
                classification = result.get("classified", {})
                result_platform = classification.get("platform")

                # Prefer results from the same platform as the signal
                if (
                    classification.get("is_specific")
                    and (not platform or result_platform == platform
                         or platform == "unknown")
                ):
                    found_specific_url = result.get("url")
                    found_listing_id = classification.get("listing_id")
                    found_by_query_type = query_type
                    print(f"    FOUND: {found_specific_url[:80]}")
                    break

            time.sleep(cfg.SEARCH_DELAY_SECONDS)

        # Compute final canonical key for this hunt result
        if found_specific_url:
            final_canonical_key = url_rules.compute_canonical_key(
                specific_listing_url=found_specific_url,
                platform=url_rules._detect_platform(found_specific_url),
                listing_id=found_listing_id
            )
            outcome = "found_specific_url"
        else:
            final_canonical_key = canonical_key
            outcome = "not_found"

        hunt_results.append({
            "original_canonical_key": canonical_key,
            "final_canonical_key": final_canonical_key,
            "specific_listing_url": found_specific_url,
            "listing_id": found_listing_id,
            "found_by_query_type": found_by_query_type,
            "queries_tried": all_queries_tried,
            "hunt_outcome": outcome
        })

    found_count = sum(
        1 for r in hunt_results
        if r.get("hunt_outcome") == "found_specific_url"
    )
    print(f"  [Researcher:Pass2] Complete — "
          f"{found_count}/{len(hunt_results)} specific URLs found")

    return hunt_results


# ─────────────────────────────────────────────
# Consolidation — pure Python, no LLM
# ─────────────────────────────────────────────
def _consolidate_python(
    pass1_result: dict,
    pass2_results: list,
    brief: dict
) -> dict:
    """
    Pure Python merge — no LLM.
    Joins Pass 1 signals with Pass 2 hunt results.
    Derives verify_url and data_confidence deterministically.
    Computes final canonical_listing_key.
    """
    print("  [Researcher:Consolidate] Pure Python merge (no LLM)...")

    signals = pass1_result.get("signals", [])
    deal_math = brief.get(
        "sba_deal_math",
        cfg.compute_max_deal_size(cfg.DEFAULT_LIQUID_CASH)
    )

    # Build hunt lookup by original canonical key
    hunt_lookup = {
        r["original_canonical_key"]: r
        for r in pass2_results
        if r.get("original_canonical_key")
    }

    raw_findings = []
    confidence_counts = {"high": 0, "medium": 0, "low": 0}

    for signal in signals:
        original_key = signal.get("canonical_listing_key", "")
        hunt_result = hunt_lookup.get(original_key)

        # Determine best available URL — Python logic
        signal_specific_url = (
            signal.get("url")
            if signal.get("is_specific_listing") else None
        )
        hunt_specific_url = (
            hunt_result.get("specific_listing_url")
            if hunt_result else None
        )
        best_specific_url = signal_specific_url or hunt_specific_url
        category_url = (
            signal.get("url")
            if not signal.get("is_specific_listing") else None
        )

        # Re-compute canonical key if URL improved — Python logic
        platform = signal.get("platform")
        listing_id = (
            hunt_result.get("listing_id")
            if hunt_result and hunt_result.get("listing_id")
            else signal.get("listing_id")
        )

        if best_specific_url:
            final_canonical_key = url_rules.compute_canonical_key(
                specific_listing_url=best_specific_url,
                platform=platform,
                listing_id=listing_id
            )
        elif platform and listing_id:
            final_canonical_key = url_rules.compute_canonical_key(
                platform=platform,
                listing_id=listing_id
            )
        else:
            final_canonical_key = original_key

        # Derive verify_url — Python logic
        verify_url = best_specific_url or category_url

        # Derive listing_type — Python logic
        listing_type = (
            "active_listing" if best_specific_url
            else "unverified_signal"
        )

        # Compute data_confidence — Python logic
        asking_price_raw = signal.get("price_from_snippet")
        has_specific_url = best_specific_url is not None
        has_price = asking_price_raw is not None

        if has_specific_url and has_price:
            data_confidence = "high"
        elif has_specific_url:
            data_confidence = "medium"
        else:
            data_confidence = "low"

        confidence_counts[data_confidence] += 1

        # Parse asking_price as number — Python regex
        asking_price_num = None
        if asking_price_raw:
            cleaned = re.sub(r"[^\d.]", "", asking_price_raw.split()[0])
            try:
                asking_price_num = float(cleaned) if cleaned else None
            except ValueError:
                asking_price_num = None

        # Price ceiling check — Python arithmetic
        within_standard = None
        within_goodwill = None
        if asking_price_num is not None:
            within_standard = (
                asking_price_num <= deal_math["standard_max_deal"]
            )
            within_goodwill = (
                asking_price_num <= deal_math["high_goodwill_max_deal"]
            )

        # Extract city/state from location snippet — Python
        location_text = signal.get("location_from_snippet") or ""
        city_val = None
        state_val = None
        if location_text:
            parts = [p.strip() for p in location_text.split(",")]
            if len(parts) >= 2:
                city_val = parts[0]
                state_val = parts[-1]
            elif parts:
                city_val = parts[0]

        # Extract years in business — Python regex
        years_val = None
        years_text = signal.get("years_from_snippet") or ""
        if years_text:
            match = re.search(r"(\d+)", years_text)
            if match:
                try:
                    years_val = int(match.group(1))
                except ValueError:
                    pass

        # Extract retirement signal — Python string check
        motivation = signal.get("seller_signal_from_snippet") or ""
        retiring = (
            True if "retir" in motivation.lower() else None
        )

        # Build final finding record
        finding = {
            "finding_id": re.sub(
                r"[^a-z0-9\-]", "",
                final_canonical_key.replace(":", "-")
            )[:24],
            "canonical_listing_key": final_canonical_key,
            "verification_required": True,
            "data_confidence": data_confidence,
            "data_source": (
                "search_preview_plus_hunt"
                if hunt_result
                and hunt_result.get("hunt_outcome") == "found_specific_url"
                else "search_preview_only"
            ),

            "business_type": signal.get("business_type"),
            "industry": signal.get("industry"),

            "location": {
                "city": city_val,
                "state": state_val,
                "within_radius": None,
                "distance_estimate": None
            },

            "financials": {
                "asking_price": asking_price_num,
                "asking_price_raw": asking_price_raw,
                "annual_revenue": None,
                "annual_profit_sde": None,
                "years_in_business": years_val
            },

            "seller_signals": {
                "owner_retiring": retiring,
                "owner_age_signal": None,
                "seller_financing_available": signal.get(
                    "seller_financing_mentioned"
                ),
                "motivation_language": signal.get(
                    "seller_signal_from_snippet"
                )
            },

            "sba_quick_check": {
                "appears_eligible": None,
                "years_in_business_ok": (
                    years_val >= cfg.SBA_MIN_YEARS_IN_BUSINESS
                    if years_val is not None else None
                ),
                "price_within_standard_budget": within_standard,
                "price_within_goodwill_budget": within_goodwill,
                "red_flags": []
            },

            "source": {
                "platform": platform,
                "specific_listing_url": best_specific_url,
                "category_page_url": category_url,
                "verify_url": verify_url,
                "listing_id": listing_id,
                "business_name": None,
                "listing_type": listing_type
            },

            "preview_text_raw": signal.get("preview_text_raw"),
            "hunt_outcome": (
                hunt_result.get("hunt_outcome")
                if hunt_result else "not_hunted"
            ),
            "found_by_query_type": (
                hunt_result.get("found_by_query_type")
                if hunt_result else None
            )
        }

        raw_findings.append(finding)

    pass2_summary = {
        "signals_hunted": len(pass2_results),
        "specific_urls_found": sum(
            1 for r in pass2_results
            if r.get("hunt_outcome") == "found_specific_url"
        ),
        "found_by_id_search": sum(
            1 for r in pass2_results
            if r.get("found_by_query_type") == "id_search"
        ),
        "found_by_phrase_search": sum(
            1 for r in pass2_results
            if r.get("found_by_query_type") == "phrase_search"
        ),
        "found_by_fallback_search": sum(
            1 for r in pass2_results
            if r.get("found_by_query_type") == "fallback_search"
        ),
        "not_found": sum(
            1 for r in pass2_results
            if r.get("hunt_outcome") == "not_found"
        )
    }

    print(f"  [Researcher:Consolidate] {len(raw_findings)} findings — "
          f"{confidence_counts['high']} high / "
          f"{confidence_counts['medium']} medium / "
          f"{confidence_counts['low']} low confidence | "
          f"no LLM used")

    return {
        "searches_executed": pass1_result.get("searches_executed", []),
        "sources_checked": pass1_result.get("sources_checked", []),
        "pass2_summary": pass2_summary,
        "raw_findings": raw_findings,
        "confidence_counts": confidence_counts,
        "researcher_notes": pass1_result.get("pass1_notes", "")
    }


# ─────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────
def run(
    brief: dict,
    seen_canonical_keys: list = None,
    rejection_reasons: list = None,
    retry_actions: list = None,
    run_id: str = None,
    loop_number: int = 1
) -> dict:
    """
    Main entry point for the Researcher Agent.

    Pipeline position: 2 of 6
    Receives from: Planner Agent
    Passes to: Scout Agent

    LLM calls: 2 maximum
      Pass 1: relevance classification (never copies data)
      Pass 2: phrase + fallback query generation (listing ID built in Python)
    Consolidation: pure Python — no LLM
    """
    seen_canonical_keys = seen_canonical_keys or []
    rejection_reasons = rejection_reasons or []
    retry_actions = retry_actions or []
    run_id = run_id or datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    is_retry = bool(retry_actions or rejection_reasons)

    print(f"[Researcher] Starting — "
          f"loop {loop_number} | "
          f"{'RETRY' if is_retry else 'first run'} | "
          f"{len(seen_canonical_keys)} keys in memory | "
          f"2 LLM calls max")

    if not cfg.SERPER_API_KEY:
        raise RuntimeError(
            "SERPER_API_KEY not set. Add it to .env and restart."
        )

    # Apply retry actions — pure Python
    search_queries = brief.get("search_queries", [])
    clean_queries = [
        q for q in search_queries
        if "<" not in q and ">" not in q
    ]

    adjusted_queries, adjusted_max_followup = _apply_retry_actions(
        retry_actions=retry_actions,
        search_queries=clean_queries,
        max_followup=MAX_FOLLOWUP_SEARCHES
    )

    # ── Pass 1 ─────────────────────────────────────────────
    pass1_result = _pass1_search(
        brief=brief,
        seen_canonical_keys=seen_canonical_keys,
        adjusted_queries=adjusted_queries
    )
    _write_artifact(run_id, f"loop{loop_number}_pass1", pass1_result)

    if not pass1_result.get("signals"):
        print("[Researcher] Pass 1 zero signals — skipping Pass 2")
        empty = {
            "searches_executed": pass1_result.get("searches_executed", []),
            "sources_checked": pass1_result.get("sources_checked", []),
            "pass2_summary": {
                "signals_hunted": 0,
                "specific_urls_found": 0,
                "not_found": 0
            },
            "raw_findings": [],
            "confidence_counts": {"high": 0, "medium": 0, "low": 0},
            "researcher_notes": "Pass 1 returned zero new signals.",
            "is_retry": is_retry,
            "loop_number": loop_number,
            "run_id": run_id
        }
        _write_artifact(run_id, f"loop{loop_number}_consolidated", empty)
        return empty

    # ── Pass 2 ─────────────────────────────────────────────
    pass2_results = _pass2_hunt(
        pass1_result=pass1_result,
        brief=brief,
        adjusted_max_followup=adjusted_max_followup
    )
    _write_artifact(
        run_id,
        f"loop{loop_number}_pass2",
        {"hunt_results": pass2_results}
    )

    # ── Consolidation — pure Python ────────────────────────
    result = _consolidate_python(
        pass1_result=pass1_result,
        pass2_results=pass2_results,
        brief=brief
    )

    result["is_retry"] = is_retry
    result["loop_number"] = loop_number
    result["run_id"] = run_id

    _write_artifact(run_id, f"loop{loop_number}_consolidated", result)

    conf = result.get("confidence_counts", {})
    print(f"[Researcher] Complete — "
          f"{len(result.get('raw_findings', []))} findings | "
          f"{conf.get('high', 0)}H / "
          f"{conf.get('medium', 0)}M / "
          f"{conf.get('low', 0)}L confidence | "
          f"artifacts written to runs/{run_id}/")

    return result


if __name__ == "__main__":
    import datetime
    import sys
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from agents.planner import run as planner_run

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
    run_id = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    result = run(
        brief=brief,
        seen_canonical_keys=[],
        rejection_reasons=[],
        retry_actions=[],
        run_id=run_id,
        loop_number=1
    )

    print("\n── Researcher output summary ──")
    print(f"Searches executed : {len(result.get('searches_executed', []))}")
    print(f"Raw findings      : {len(result.get('raw_findings', []))}")
    print(f"Pass 2 summary    : {json.dumps(result.get('pass2_summary', {}))}")
    print(f"Confidence        : {json.dumps(result.get('confidence_counts', {}))}")
    print(f"Run artifacts     : runs/{run_id}/")

    findings = result.get("raw_findings", [])
    if findings:
        print(f"\nFirst finding:")
        f = findings[0]
        print(f"  canonical_key   : {f.get('canonical_listing_key')}")
        print(f"  business_type   : {f.get('business_type')}")
        print(f"  data_confidence : {f.get('data_confidence')}")
        print(f"  asking_price    : {f.get('financials', {}).get('asking_price')}")
        print(f"  verify_url      : {f.get('source', {}).get('verify_url')}")
        print(f"  hunt_outcome    : {f.get('hunt_outcome')}")
        print(f"  found_by        : {f.get('found_by_query_type')}")
        print(f"  verif_required  : {f.get('verification_required')}")