# url_rules.py
# Deterministic URL classification, listing ID extraction,
# canonical listing key computation, and Serper search client.
#
# No LLM involved anywhere in this file.
# Every function is pure Python — fully unit testable.
#
# Used by: researcher.py, scout.py, validator.py, main.py
#
# Functions:
#   is_specific_listing_url(url, platform)  -> bool
#   extract_listing_id(url, platform)       -> str | None
#   compute_canonical_key(...)              -> str
#   classify_url(url)                       -> dict
#   search(query, num_results)              -> list[dict]
#   normalize_url(url)                      -> str

import hashlib
import os
import re
import sys
import time
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv

import requests

load_dotenv()

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# ─────────────────────────────────────────────
# Platform URL patterns
# Each entry maps a platform name to:
#   - domain_patterns: list of domain substrings to match
#   - listing_path_regex: regex that matches a specific listing URL path
#   - listing_id_group: capture group name for listing ID
# ─────────────────────────────────────────────
_PLATFORM_RULES = {
    "bizbuysell": {
        "domain_patterns": ["bizbuysell.com"],
        "listing_path_regex": re.compile(
            r"/business-for-sale/[a-z0-9\-]+/(\d{4,})/?"
        ),
        "listing_id_group": 1,
        "category_path_patterns": [
            re.compile(r"/\w+/businesses-for-sale"),
            re.compile(r"/\w+/retiring-owner"),
            re.compile(r"/search"),
        ]
    },
    "bizquest": {
        "domain_patterns": ["bizquest.com"],
        "listing_path_regex": re.compile(
            r"/business-for-sale/[a-z0-9\-]+/(BW\d+|bw\d+)/?"
        ),
        "listing_id_group": 1,
        "category_path_patterns": [
            re.compile(r"/businesses-for-sale-in-"),
            re.compile(r"/search"),
        ]
    },
    "businessbroker": {
        "domain_patterns": ["businessbroker.net"],
        "listing_path_regex": re.compile(
            r"/listing/[a-z0-9\-]+-(\d{4,})/?"
        ),
        "listing_id_group": 1,
        "category_path_patterns": [
            re.compile(r"/businesses-for-sale"),
            re.compile(r"/search"),
        ]
    },
    "loopnet": {
        "domain_patterns": ["loopnet.com"],
        "listing_path_regex": re.compile(
            r"/biz/[a-z0-9\-]+/(\d{4,})/?"
        ),
        "listing_id_group": 1,
        "category_path_patterns": [
            re.compile(r"/biz/businesses-for-sale"),
            re.compile(r"/search"),
        ]
    },
    "sunbelt": {
        "domain_patterns": ["sunbeltnetwork.com", "sunbeltbusiness.com"],
        "listing_path_regex": re.compile(
            r"/listing/[a-z0-9\-\-]+/?"
        ),
        "listing_id_group": None,
        "category_path_patterns": [
            re.compile(r"/businesses-for-sale"),
            re.compile(r"/search"),
        ]
    },
    "murphy": {
        "domain_patterns": ["murphybusiness.com"],
        "listing_path_regex": re.compile(
            r"/listing/[a-z0-9\-]+/?"
        ),
        "listing_id_group": None,
        "category_path_patterns": [
            re.compile(r"/businesses-for-sale"),
        ]
    },
}

# Minimum path depth to consider a URL potentially specific
# e.g. bizbuysell.com/kansas/ has depth 1 — too shallow
# bizbuysell.com/business-for-sale/hvac-company/123456/ has depth 3
_MIN_PATH_DEPTH_FOR_SPECIFIC = 2


def normalize_url(url: str) -> str:
    """
    Normalizes a URL for consistent comparison and hashing.
    - Strips trailing slashes
    - Lowercases scheme and host
    - Removes common tracking parameters
    - Removes URL fragments (#)
    Returns normalized string.
    """
    if not url:
        return ""

    try:
        parsed = urlparse(url.strip())
        normalized = urlunparse((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            "",   # params
            "",   # query — strip tracking params
            ""    # fragment
        ))
        return normalized
    except Exception:
        return url.strip().lower().rstrip("/")


def _detect_platform(url: str) -> str | None:
    """
    Detects which platform a URL belongs to.
    Returns platform key string or None if unrecognized.
    """
    if not url:
        return None

    url_lower = url.lower()
    for platform, rules in _PLATFORM_RULES.items():
        for domain in rules["domain_patterns"]:
            if domain in url_lower:
                return platform
    return None


def is_specific_listing_url(url: str, platform: str = None) -> bool:
    """
    Determines whether a URL points to a specific business listing
    (not a category, search results, or browse page).

    Uses platform-specific regex patterns where available.
    Falls back to heuristic path depth check for unknown platforms.

    Returns True if URL appears to be a specific listing.
    Returns False if URL appears to be a category/browse page.
    Returns False if URL is None or empty.

    Examples:
      True:  bizbuysell.com/business-for-sale/hvac-company-shawnee/987654/
      False: bizbuysell.com/kansas/businesses-for-sale/
      True:  bizquest.com/business-for-sale/child-care/BW123456/
      False: bizquest.com/businesses-for-sale-in-kansas-city-mo/
    """
    if not url:
        return False

    detected_platform = platform or _detect_platform(url)

    if detected_platform and detected_platform in _PLATFORM_RULES:
        rules = _PLATFORM_RULES[detected_platform]

        # Check if it matches a known category path — fast rejection
        parsed_path = urlparse(url).path.lower()
        for cat_pattern in rules.get("category_path_patterns", []):
            if cat_pattern.search(parsed_path):
                return False

        # Check if it matches the listing pattern
        listing_regex = rules.get("listing_path_regex")
        if listing_regex and listing_regex.search(parsed_path):
            return True

        # Matched platform domain but no listing pattern found
        return False

    # Unknown platform — use heuristic path depth
    try:
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        return len(path_parts) >= _MIN_PATH_DEPTH_FOR_SPECIFIC
    except Exception:
        return False


def extract_listing_id(url: str, platform: str = None) -> str | None:
    """
    Extracts the listing ID from a specific listing URL.
    Returns the ID string if found, None otherwise.

    Examples:
      bizbuysell.com/.../987654/  → "987654"
      bizquest.com/.../BW123456/  → "BW123456"
      unknown platform URL        → None
    """
    if not url:
        return None

    detected_platform = platform or _detect_platform(url)

    if not detected_platform or detected_platform not in _PLATFORM_RULES:
        return None

    rules = _PLATFORM_RULES[detected_platform]
    listing_regex = rules.get("listing_path_regex")
    group = rules.get("listing_id_group")

    if not listing_regex or group is None:
        return None

    parsed_path = urlparse(url).path
    match = listing_regex.search(parsed_path)
    if match:
        try:
            return match.group(group)
        except IndexError:
            return None

    return None


def classify_url(url: str) -> dict:
    """
    Full URL classification — combines platform detection,
    specific vs category determination, and listing ID extraction.

    Returns a dict with:
      platform         str | None
      is_specific      bool
      listing_id       str | None
      normalized_url   str
      url_type         'specific_listing' | 'category_page' | 'unknown' | 'empty'
    """
    if not url:
        return {
            "platform": None,
            "is_specific": False,
            "listing_id": None,
            "normalized_url": "",
            "url_type": "empty"
        }

    platform = _detect_platform(url)
    specific = is_specific_listing_url(url, platform)
    listing_id = extract_listing_id(url, platform) if specific else None
    normalized = normalize_url(url)

    if specific:
        url_type = "specific_listing"
    elif platform:
        url_type = "category_page"
    else:
        url_type = "unknown"

    return {
        "platform": platform,
        "is_specific": specific,
        "listing_id": listing_id,
        "normalized_url": normalized,
        "url_type": url_type
    }


def compute_canonical_key(
    specific_listing_url: str = None,
    platform: str = None,
    listing_id: str = None,
    business_name: str = None,
    city: str = None,
    asking_price_raw: str = None
) -> str:
    """
    Computes a stable, deterministic canonical key for a lead.
    Used for deduplication across runs — stored in memory instead of lead_id.

    Priority order (first available wins):
      1. normalized specific_listing_url     (most stable)
      2. platform + ':' + listing_id         (second most stable)
      3. hash(platform+name+city+price_raw)  (fuzzy fallback)

    Returns a string canonical key — never None, never empty.

    This key is the single source of truth for deduplication.
    The same business should always produce the same key
    regardless of what slug or lead_id was assigned upstream.
    """

    # Tier 1 — normalized specific listing URL
    if specific_listing_url:
        classification = classify_url(specific_listing_url)
        if classification["is_specific"]:
            return f"url:{classification['normalized_url']}"

    # Tier 2 — platform + listing ID
    if platform and listing_id:
        return f"id:{platform.lower()}:{listing_id}"

    # Tier 3 — fuzzy hash fallback
    # Normalize each component to reduce variation
    parts = [
        (platform or "").lower().strip(),
        (business_name or "").lower().strip(),
        (city or "").lower().strip(),
        (asking_price_raw or "").lower().replace("$", "").replace(",", "").strip()
    ]
    raw_string = "|".join(parts)
    hash_value = hashlib.md5(raw_string.encode()).hexdigest()[:12]
    return f"hash:{hash_value}"


def search(
    query: str,
    num_results: int = 10,
    serper_api_key: str = None
) -> list:
    """
    Executes a web search via Serper.dev and returns structured results.
    Returns raw search results as a list of dicts — no LLM involved.

    Each result dict contains:
      title    str
      url      str
      snippet  str
      position int
      classified dict  (from classify_url — added deterministically)

    Returns empty list if search fails — never raises.
    Caller should check len(results) == 0 and handle accordingly.
    """
    key = serper_api_key or os.getenv("SERPER_API_KEY")

    if not key:
        print(f"  [search] ERROR — SERPER_API_KEY not set")
        return []

    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": key,
                "Content-Type": "application/json"
            },
            json={
                "q": query,
                "num": num_results,
                "gl": "us",
                "hl": "en"
            },
            timeout=15
        )

        if response.status_code != 200:
            print(f"  [search] HTTP {response.status_code} for query: {query[:60]}")
            return []

        data = response.json()
        raw_results = data.get("organic", [])

        # Enrich each result deterministically
        structured = []
        for item in raw_results:
            url = item.get("link", "")
            structured.append({
                "title": item.get("title", ""),
                "url": url,
                "snippet": item.get("snippet", ""),
                "position": item.get("position", 0),
                "classified": classify_url(url)
            })

        return structured

    except requests.exceptions.Timeout:
        print(f"  [search] TIMEOUT for query: {query[:60]}")
        return []

    except requests.exceptions.RequestException as e:
        print(f"  [search] Request error: {e}")
        return []

    except Exception as e:
        print(f"  [search] Unexpected error: {e}")
        return []


def batch_search(
    queries: list,
    num_results: int = 10,
    delay_seconds: float = 1.0,
    serper_api_key: str = None
) -> dict:
    """
    Executes multiple searches sequentially with a delay between each.
    Returns a dict keyed by query string.

    Handles failures per-query — a failed query returns an empty list
    without stopping the rest of the batch.
    """
    results = {}
    total = len(queries)

    for i, query in enumerate(queries, 1):
        print(f"  [search] ({i}/{total}) {query[:70]}...")
        results[query] = search(query, num_results, serper_api_key)

        hits = len(results[query])
        specific = sum(
            1 for r in results[query]
            if r.get("classified", {}).get("is_specific")
        )
        print(f"  [search] ({i}/{total}) {hits} results, "
              f"{specific} specific listing URLs")

        # Respect rate limiting — pause between searches
        if i < total:
            time.sleep(delay_seconds)

    return results


if __name__ == "__main__":
    # ── Test runner ──────────────────────────────────────
    # python url_rules.py
    # Tests every function with known inputs and expected outputs.
    # No API key needed for URL classification tests.
    # SERPER_API_KEY needed only for the search test.

    print("=" * 60)
    print("url_rules.py — unit tests")
    print("=" * 60)

    failures = []

    def assert_eq(label, actual, expected):
        status = "PASS" if actual == expected else "FAIL"
        if status == "FAIL":
            failures.append(f"{label}: expected {expected!r}, got {actual!r}")
        print(f"  [{status}] {label}")

    def assert_true(label, value):
        assert_eq(label, bool(value), True)

    def assert_false(label, value):
        assert_eq(label, bool(value), False)

    # ── normalize_url ─────────────────────────────────────
    print("\n── normalize_url ──")
    assert_eq(
        "strips trailing slash",
        normalize_url("https://www.bizbuysell.com/business-for-sale/hvac/123/"),
        "https://www.bizbuysell.com/business-for-sale/hvac/123"
    )
    assert_eq(
        "lowercases host",
        normalize_url("https://WWW.BIZBUYSELL.COM/business-for-sale/hvac/123/"),
        "https://www.bizbuysell.com/business-for-sale/hvac/123"
    )
    assert_eq(
        "strips query params",
        normalize_url("https://www.bizbuysell.com/business-for-sale/hvac/123/?utm_source=google"),
        "https://www.bizbuysell.com/business-for-sale/hvac/123"
    )
    assert_eq(
        "handles empty string",
        normalize_url(""),
        ""
    )

    # ── _detect_platform ─────────────────────────────────
    print("\n── _detect_platform ──")
    assert_eq(
        "detects bizbuysell",
        _detect_platform("https://www.bizbuysell.com/business-for-sale/hvac/123/"),
        "bizbuysell"
    )
    assert_eq(
        "detects bizquest",
        _detect_platform("https://www.bizquest.com/business-for-sale/childcare/BW123456/"),
        "bizquest"
    )
    assert_eq(
        "returns None for unknown",
        _detect_platform("https://www.craigslist.org/biz/"),
        None
    )
    assert_eq(
        "handles None",
        _detect_platform(None),
        None
    )

    # ── is_specific_listing_url ───────────────────────────
    print("\n── is_specific_listing_url ──")

    # BizBuySell
    assert_true(
        "BizBuySell specific listing",
        is_specific_listing_url(
            "https://www.bizbuysell.com/business-for-sale/"
            "hvac-company-shawnee-ks/987654/"
        )
    )
    assert_false(
        "BizBuySell category page",
        is_specific_listing_url(
            "https://www.bizbuysell.com/kansas/businesses-for-sale/"
        )
    )
    assert_false(
        "BizBuySell retiring owner category",
        is_specific_listing_url(
            "https://www.bizbuysell.com/kansas/retiring-owner-businesses-for-sale/"
        )
    )

    # BizQuest
    assert_true(
        "BizQuest specific listing",
        is_specific_listing_url(
            "https://www.bizquest.com/business-for-sale/"
            "child-care-center-johnson-county/BW2434139/"
        )
    )
    assert_false(
        "BizQuest city browse page",
        is_specific_listing_url(
            "https://www.bizquest.com/businesses-for-sale-in-kansas-city-mo/"
        )
    )

    # BusinessBroker
    assert_true(
        "BusinessBroker specific listing",
        is_specific_listing_url(
            "https://www.businessbroker.net/listing/hvac-company-12345/"
        )
    )

    # Edge cases
    assert_false(
        "None URL",
        is_specific_listing_url(None)
    )
    assert_false(
        "empty string",
        is_specific_listing_url("")
    )

    # ── extract_listing_id ───────────────────────────────
    print("\n── extract_listing_id ──")
    assert_eq(
        "BizBuySell listing ID",
        extract_listing_id(
            "https://www.bizbuysell.com/business-for-sale/hvac/987654/"
        ),
        "987654"
    )
    assert_eq(
        "BizQuest listing ID",
        extract_listing_id(
            "https://www.bizquest.com/business-for-sale/childcare/BW2434139/"
        ),
        "BW2434139"
    )
    assert_eq(
        "category URL returns None",
        extract_listing_id(
            "https://www.bizbuysell.com/kansas/businesses-for-sale/"
        ),
        None
    )
    assert_eq(
        "None URL returns None",
        extract_listing_id(None),
        None
    )

    # ── classify_url ────────────────────────────────────
    print("\n── classify_url ──")
    result = classify_url(
        "https://www.bizbuysell.com/business-for-sale/hvac/987654/"
    )
    assert_eq("classify: platform", result["platform"], "bizbuysell")
    assert_eq("classify: is_specific", result["is_specific"], True)
    assert_eq("classify: listing_id", result["listing_id"], "987654")
    assert_eq("classify: url_type", result["url_type"], "specific_listing")

    result2 = classify_url(
        "https://www.bizbuysell.com/kansas/businesses-for-sale/"
    )
    assert_eq("classify category: is_specific", result2["is_specific"], False)
    assert_eq("classify category: url_type", result2["url_type"], "category_page")

    result3 = classify_url("")
    assert_eq("classify empty: url_type", result3["url_type"], "empty")

    # ── compute_canonical_key ────────────────────────────
    print("\n── compute_canonical_key ──")

    # Tier 1 — specific URL
    key1a = compute_canonical_key(
        specific_listing_url="https://www.bizbuysell.com/business-for-sale/hvac/987654/"
    )
    key1b = compute_canonical_key(
        specific_listing_url="https://www.bizbuysell.com/business-for-sale/hvac/987654"
    )
    assert_true("tier 1 key starts with url:", key1a.startswith("url:"))
    assert_eq("tier 1 key stable across trailing slash", key1a, key1b)

    # Tier 2 — platform + listing ID
    key2 = compute_canonical_key(
        specific_listing_url=None,
        platform="bizbuysell",
        listing_id="987654"
    )
    assert_true("tier 2 key starts with id:", key2.startswith("id:"))
    assert_eq(
        "tier 2 key format",
        key2,
        "id:bizbuysell:987654"
    )

    # Tier 3 — hash fallback
    key3a = compute_canonical_key(
        specific_listing_url=None,
        platform="unknown",
        listing_id=None,
        business_name="KC HVAC Company",
        city="Shawnee",
        asking_price_raw="$97,000"
    )
    key3b = compute_canonical_key(
        specific_listing_url=None,
        platform="unknown",
        listing_id=None,
        business_name="kc hvac company",  # different case
        city="shawnee",
        asking_price_raw="97000"           # different format
    )
    assert_true("tier 3 key starts with hash:", key3a.startswith("hash:"))
    assert_eq("tier 3 key stable across case/format variants", key3a, key3b)

    # Tier 1 beats Tier 2 when both available
    key_tier1_wins = compute_canonical_key(
        specific_listing_url="https://www.bizbuysell.com/business-for-sale/hvac/987654/",
        platform="bizbuysell",
        listing_id="987654"
    )
    assert_true("tier 1 wins when URL available", key_tier1_wins.startswith("url:"))

    # ── Search test (requires SERPER_API_KEY) ────────────
    print("\n── search (requires SERPER_API_KEY) ──")
    serper_key = os.getenv("SERPER_API_KEY")
    if not serper_key:
        print("  [SKIP] SERPER_API_KEY not set — skipping live search test")
        print("  Set SERPER_API_KEY in your .env and re-run to test search")
    else:
        print("  Running live search test...")
        results = search(
            "bizbuysell.com kansas city business for sale retiring owner",
            num_results=5,
            serper_api_key=serper_key
        )
        if results:
            print(f"  [PASS] search returned {len(results)} results")
            print(f"  First result: {results[0]['title'][:60]}")
            print(f"  First URL: {results[0]['url'][:80]}")
            print(f"  First URL classified as: "
                  f"{results[0]['classified']['url_type']}")
        else:
            print("  [FAIL] search returned empty results — check API key")
            failures.append("live search returned empty results")

    # ── Summary ──────────────────────────────────────────
    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED — {len(failures)} test(s) failed:")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)