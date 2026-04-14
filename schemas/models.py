# schemas/models.py
# Pydantic models for every stage boundary in the pipeline.
# Each agent's output is validated against these models
# before being passed to the next agent.
#
# Benefits:
#   - Schema drift caught immediately at stage boundaries
#   - Every agent independently testable against fixtures
#   - Clear contract between agents — no silent field changes
#   - Enables golden file tests
#
# Install: pip install pydantic
# Usage:
#   from schemas.models import ResearchFindings
#   findings = ResearchFindings.model_validate(raw_dict)

from __future__ import annotations

from enum import Enum
from typing import Any
import hashlib
from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────
# Enums — finite value sets enforced at schema level
# ─────────────────────────────────────────────

class DataConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class UrlType(str, Enum):
    SPECIFIC_LISTING = "specific_listing"
    CATEGORY_PAGE = "category_page"
    UNKNOWN = "unknown"
    EMPTY = "empty"


class ListingType(str, Enum):
    ACTIVE_LISTING = "active_listing"
    UNVERIFIED_SIGNAL = "unverified_signal"


class TriagePriority(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class MotivationStrength(str, Enum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    UNKNOWN = "unknown"


class HandsoffAssessment(str, Enum):
    LIKELY_MANAGEABLE = "likely_manageable"
    UNLIKELY_MANAGEABLE = "unlikely_manageable"
    UNCERTAIN = "uncertain"


class LoanScreen(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class ValidationResult(str, Enum):
    ACCEPTED_FOR_DASHBOARD = "accepted_for_dashboard"
    REJECTED = "rejected"


class CoherenceAssessment(str, Enum):
    COHERENT = "coherent"
    INCOHERENT = "incoherent"
    SPARSE = "sparse"


class RetryAction(str, Enum):
    """
    Finite set of actions the Researcher can respond to deterministically.
    Validator selects from this enum — never free-form prose.
    """
    INCREASE_PASS2_HUNTS = "increase_pass2_hunts"
    ADD_PRICE_FILTER = "add_price_filter_to_queries"
    USE_BROKER_SPECIFIC_QUERIES = "use_broker_specific_queries"
    BROADEN_SEARCH_RADIUS = "broaden_search_radius"
    TARGET_DIFFERENT_INDUSTRIES = "target_different_industries"
    USE_DIFFERENT_PLATFORMS = "use_different_platforms"
    FILTER_BY_YEARS_IN_BUSINESS = "filter_by_years_in_business"
    EXCLUDE_FOOD_AND_BEVERAGE = "exclude_food_and_beverage"


# ─────────────────────────────────────────────
# Stage 1 — Planner output
# ─────────────────────────────────────────────

class LocationContext(BaseModel):
    zip_code: str
    city: str | None = None
    county: str | None = None
    metro_area: str | None = None
    surrounding_cities: list[str] = Field(default_factory=list)
    search_area_description: str | None = None


class SBADealMath(BaseModel):
    liquid_cash: float
    standard_max_deal: float
    high_goodwill_max_deal: float
    standard_down_payment_pct: float
    high_goodwill_down_payment_pct: float
    standard_down_payment_amount: float
    high_goodwill_down_payment_amount: float
    note: str

    @field_validator("standard_max_deal")
    @classmethod
    def validate_standard_max(cls, v, info):
        # standard max should equal liquid / 0.10
        liquid = info.data.get("liquid_cash", 0)
        if liquid > 0:
            expected = round(liquid / 0.10, 2)
            if abs(v - expected) > 1.0:
                raise ValueError(
                    f"standard_max_deal {v} does not match "
                    f"liquid_cash {liquid} / 0.10 = {expected}"
                )
        return v


class BrokerTarget(BaseModel):
    name: str
    url: str | None = None
    search_instruction: str | None = None


class TargetIndustry(BaseModel):
    industry: str
    why_fits: str | None = None
    typical_price_range: str | None = None
    goodwill_heavy: bool | None = None
    gm_manageable: bool | None = None
    common_in_area: bool | None = None


class ResearchInstructions(BaseModel):
    total_leads_to_find: int
    early_leads_count: int
    serious_listings_count: int
    early_lead_definition: str | None = None
    serious_listing_definition: str | None = None
    priority_order: str | None = None
    data_integrity: str | None = None
    avoid_repeating: str | None = None


class PlannerBrief(BaseModel):
    """Output of the Planner Agent — input to the Researcher Agent."""
    location_context: LocationContext
    buyer_summary: dict[str, Any] = Field(default_factory=dict)
    sba_deal_math: SBADealMath
    search_queries: list[str] = Field(default_factory=list)
    broker_targets: list[BrokerTarget] = Field(default_factory=list)
    qualification_checklist: list[str] = Field(default_factory=list)
    exclusion_list: list[str] = Field(default_factory=list)
    motivated_seller_signals: list[str] = Field(default_factory=list)
    target_industries: list[TargetIndustry] = Field(default_factory=list)
    research_instructions: ResearchInstructions | None = None
    radius_miles: int
    zip_code: str

    @field_validator("search_queries")
    @classmethod
    def no_template_placeholders(cls, v):
        bad = [q for q in v if "<" in q or ">" in q]
        if bad:
            raise ValueError(
                f"search_queries contains template placeholders: {bad}"
            )
        return v


# ─────────────────────────────────────────────
# Stage 2 — Researcher output
# ─────────────────────────────────────────────

class URLClassification(BaseModel):
    platform: str | None = None
    is_specific: bool
    listing_id: str | None = None
    normalized_url: str
    url_type: UrlType


class SearchResult(BaseModel):
    """A single result from Serper search."""
    title: str
    url: str
    snippet: str
    position: int
    classified: URLClassification


class RawSignal(BaseModel):
    """
    A single business signal extracted from search results.
    All fields explicitly nullable — null means not found, not zero.
    """
    signal_id: str
    canonical_listing_key: str  # computed deterministically by url_rules.py
    business_type: str | None = None
    industry: str | None = None
    location_preview: str | None = None
    price_preview: str | None = None
    seller_signal_preview: str | None = None
    years_in_business_preview: str | None = None
    category_page_url: str | None = None
    specific_listing_url: str | None = None
    platform: str | None = None
    preview_text_raw: str | None = None
    relevance_score: int = Field(ge=1, le=10)
    relevance_reason: str | None = None
    verification_required: bool = True

    @field_validator("verification_required")
    @classmethod
    def must_be_true(cls, v):
        if v is not True:
            raise ValueError("verification_required must always be True")
        return v


class Pass2HuntResult(BaseModel):
    """Result of a targeted follow-up search for a specific business."""
    signal_id: str
    search_queries_tried: list[str] = Field(default_factory=list)
    specific_listing_url: str | None = None
    listing_id: str | None = None
    business_name: str | None = None
    additional_details_found: str | None = None
    hunt_outcome: str  # found_specific_url | found_name_only | not_found | error
    hunt_notes: str | None = None


class ResearchFindings(BaseModel):
    """Output of the Researcher Agent — input to the Scout Agent."""
    searches_executed: list[str] = Field(default_factory=list)
    sources_checked: list[str] = Field(default_factory=list)
    pass2_summary: dict[str, Any] = Field(default_factory=dict)
    raw_findings: list[dict[str, Any]] = Field(default_factory=list)
    researcher_notes: str | None = None
    is_retry: bool = False
    rejection_reasons_addressed: list[str] = Field(default_factory=list)

    @field_validator("raw_findings")
    @classmethod
    def all_findings_have_canonical_key(cls, v):
        missing = [
            i for i, f in enumerate(v)
            if not f.get("canonical_listing_key")
        ]
        if missing:
            raise ValueError(
                f"raw_findings at indices {missing} "
                f"missing canonical_listing_key"
            )
        return v


# ─────────────────────────────────────────────
# Stage 3 — Scout output
# ─────────────────────────────────────────────

class VerificationChecklistItem(BaseModel):
    priority: str  # must_check | should_check | nice_to_check
    question: str
    where_to_find: str
    why_it_matters: str


class ScoutLead(BaseModel):
    """A single structured triage lead from the Scout Agent."""
    lead_id: str
    canonical_listing_key: str
    triage_priority: TriagePriority
    triage_priority_reason: str
    verification_required: bool = True
    verification_disclaimer: str

    business_snapshot: dict[str, Any] = Field(default_factory=dict)
    seller_motivation: dict[str, Any] = Field(default_factory=dict)
    handsoff_assessment: dict[str, Any] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)
    sba_signal: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)
    manual_verification_checklist: list[VerificationChecklistItem] = Field(
        default_factory=list
    )
    preview_text_raw: str | None = None
    scout_notes: str | None = None

    @field_validator("verification_required")
    @classmethod
    def must_be_true(cls, v):
        if v is not True:
            raise ValueError("verification_required must always be True")
        return v

    @field_validator("manual_verification_checklist")
    @classmethod
    def minimum_checklist_items(cls, v):
        if len(v) < 4:
            raise ValueError(
                f"manual_verification_checklist must have at least 4 items, "
                f"got {len(v)}"
            )
        return v


class ScoutOutput(BaseModel):
    """Output of the Scout Agent — input to the Loan Feasibility Agent."""
    leads: list[ScoutLead] = Field(default_factory=list)
    scout_notes: str | None = None
    total_leads: int
    triage_summary: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def total_matches_leads(self):
        if self.total_leads != len(self.leads):
            raise ValueError(
                f"total_leads {self.total_leads} does not match "
                f"len(leads) {len(self.leads)}"
            )
        return self


# ─────────────────────────────────────────────
# Stage 4 — Loan Feasibility output
# ─────────────────────────────────────────────

class DealMathResult(BaseModel):
    asking_price: float | None = None
    standard_down_payment: float | None = None
    high_goodwill_down_payment: float | None = None
    loan_amount_standard: float | None = None
    loan_amount_high_goodwill: float | None = None
    buyer_can_cover_standard: bool | None = None
    buyer_can_cover_high_goodwill: bool | None = None
    within_standard_max_deal: bool | None = None
    within_high_goodwill_max_deal: bool | None = None
    deal_math_note: str


class BrokerQuestion(BaseModel):
    priority: str  # must_ask | should_ask | nice_to_ask
    question: str
    why_it_matters: str
    what_good_looks_like: str


class LoanFeasibilityAssessment(BaseModel):
    preliminary_screen: LoanScreen
    preliminary_screen_reason: str
    real_determination_disclaimer: str
    deal_math: DealMathResult
    eligibility_screen: dict[str, Any] = Field(default_factory=dict)
    goodwill_assessment: dict[str, Any] = Field(default_factory=dict)
    what_a_real_determination_needs: list[str] = Field(default_factory=list)
    broker_questions: list[BrokerQuestion] = Field(default_factory=list)

    @field_validator("broker_questions")
    @classmethod
    def minimum_broker_questions(cls, v):
        if len(v) < 5:
            raise ValueError(
                f"broker_questions must have at least 5 items "
                f"(5 mandatory), got {len(v)}"
            )
        return v

    @field_validator("real_determination_disclaimer")
    @classmethod
    def disclaimer_must_exist(cls, v):
        if not v or len(v) < 20:
            raise ValueError("real_determination_disclaimer must be populated")
        return v


class LoanFeasibilityOutput(BaseModel):
    """
    Output of Loan Feasibility Agent — input to Validator Agent.
    Each lead carries its full Scout data plus loan_feasibility block.
    """
    leads_with_loan_assessment: list[dict[str, Any]] = Field(
        default_factory=list
    )
    loan_summary: dict[str, int] = Field(default_factory=dict)
    loan_feasibility_notes: str | None = None

    @field_validator("leads_with_loan_assessment")
    @classmethod
    def all_leads_have_assessment(cls, v):
        missing = [
            i for i, lead in enumerate(v)
            if "loan_feasibility" not in lead
        ]
        if missing:
            raise ValueError(
                f"leads at indices {missing} missing loan_feasibility block"
            )
        return v


# ─────────────────────────────────────────────
# Stage 5 — Validator output
# ─────────────────────────────────────────────

class ConsistencyViolation(str, Enum):
    RETIREMENT_SIGNAL_WITHOUT_EVIDENCE = "retirement_signal_without_evidence"
    ASKING_PRICE_WITHOUT_RAW_STRING = "asking_price_without_raw_string"
    SPECIFIC_URL_CLAIMED_BUT_VERIFY_URL_NULL = "specific_url_claimed_but_verify_url_null"
    SELLER_FINANCING_WITHOUT_PREVIEW_EVIDENCE = "seller_financing_claimed_without_preview_evidence"
    HIGH_PRIORITY_WITHOUT_SPECIFIC_URL = "high_priority_without_specific_url"


class StructuredRetryGuidance(BaseModel):
    """
    Machine-consumable retry guidance from the Validator.
    Failure modes are counts. Actions are a finite enum set.
    The Researcher responds deterministically to each action.
    Never free-form prose — that slides back into LLM interpretation.
    """
    failure_mode_counts: dict[str, int] = Field(default_factory=dict)
    suggested_actions: list[RetryAction] = Field(default_factory=list)
    human_summary: str


class ValidationBlock(BaseModel):
    result: ValidationResult
    hard_fail: bool
    hard_fail_reasons: list[str] = Field(default_factory=list)
    consistency_violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    passed_checks: list[str] = Field(default_factory=list)
    loop_number: int
    data_confidence: DataConfidence | None = None
    has_verify_url: bool = False
    signal_coherence: dict[str, Any] | None = None
    handsoff_reasoning: dict[str, Any] | None = None
    analyst_commentary: str | None = None
    recommended_first_action: str | None = None


class ValidatorOutput(BaseModel):
    """
    Output of the Validator Agent — input to the Dashboard Agent.
    Final stage before dashboard generation.
    """
    accepted_leads: list[dict[str, Any]] = Field(default_factory=list)
    rejected_leads: list[dict[str, Any]] = Field(default_factory=list)
    retry_guidance: StructuredRetryGuidance | None = None
    validation_summary: dict[str, Any] = Field(default_factory=dict)
    validator_notes: str | None = None

    @model_validator(mode="after")
    def needs_retry_requires_guidance(self):
        summary = self.validation_summary
        needs_retry = summary.get("needs_retry", False)
        if needs_retry and self.retry_guidance is None:
            raise ValueError(
                "needs_retry is True but retry_guidance is None — "
                "Validator must provide structured retry guidance when retrying"
            )
        return self

    @field_validator("accepted_leads")
    @classmethod
    def accepted_leads_have_correct_result(cls, v):
        wrong = [
            i for i, lead in enumerate(v)
            if lead.get("validation", {}).get("result") != "accepted_for_dashboard"
        ]
        if wrong:
            raise ValueError(
                f"accepted_leads at indices {wrong} have wrong "
                f"validation.result — must be 'accepted_for_dashboard'"
            )
        return v


# ─────────────────────────────────────────────
# Run artifact schema
# Written by orchestrator after each stage
# ─────────────────────────────────────────────

class RunArtifact(BaseModel):
    """
    Written to runs/{run_id}/{stage}.json after each stage completes.
    Enables debugging, reproducibility, and golden file tests.
    """
    run_id: str
    stage: str
    timestamp_utc: str
    zip_code: str
    liquid_cash: float
    radius_miles: int
    loop_number: int
    stage_succeeded: bool
    error_detail: str | None = None
    output_summary: dict[str, Any] = Field(default_factory=dict)


if __name__ == "__main__":
    # ── Schema validation tests ───────────────────────────
    # python schemas/models.py
    import sys

    print("=" * 60)
    print("schemas/models.py — validation tests")
    print("=" * 60)

    failures = []

    def assert_raises(label, fn):
        try:
            fn()
            failures.append(f"{label}: expected ValidationError, got nothing")
            print(f"  [FAIL] {label}")
        except Exception:
            print(f"  [PASS] {label}")

    def assert_ok(label, fn):
        try:
            fn()
            print(f"  [PASS] {label}")
        except Exception as e:
            failures.append(f"{label}: unexpected error: {e}")
            print(f"  [FAIL] {label}: {e}")

    # SBADealMath validation
    print("\n── SBADealMath ──")
    assert_ok(
        "valid deal math",
        lambda: SBADealMath(
            liquid_cash=50000,
            standard_max_deal=500000,
            high_goodwill_max_deal=333333.33,
            standard_down_payment_pct=0.10,
            high_goodwill_down_payment_pct=0.15,
            standard_down_payment_amount=50000,
            high_goodwill_down_payment_amount=50000,
            note="test"
        )
    )
    assert_raises(
        "wrong standard_max_deal caught",
        lambda: SBADealMath(
            liquid_cash=50000,
            standard_max_deal=250000,  # wrong — should be 500000
            high_goodwill_max_deal=333333,
            standard_down_payment_pct=0.10,
            high_goodwill_down_payment_pct=0.15,
            standard_down_payment_amount=50000,
            high_goodwill_down_payment_amount=50000,
            note="test"
        )
    )

    # PlannerBrief — template placeholder check
    print("\n── PlannerBrief ──")
    assert_raises(
        "template placeholder in search_queries caught",
        lambda: PlannerBrief(
            location_context=LocationContext(zip_code="66214"),
            sba_deal_math=SBADealMath(
                liquid_cash=50000,
                standard_max_deal=500000,
                high_goodwill_max_deal=333333,
                standard_down_payment_pct=0.10,
                high_goodwill_down_payment_pct=0.15,
                standard_down_payment_amount=50000,
                high_goodwill_down_payment_amount=50000,
                note="test"
            ),
            search_queries=["<placeholder query>"],  # bad
            radius_miles=50,
            zip_code="66214",
            research_instructions=None
        )
    )

    # RawSignal — verification_required enforcement
    print("\n── RawSignal ──")
    assert_raises(
        "verification_required=False caught",
        lambda: RawSignal(
            signal_id="test-01",
            canonical_listing_key="url:test",
            relevance_score=7,
            verification_required=False  # must be True
        )
    )

    # ScoutOutput — total_leads consistency check
    print("\n── ScoutOutput ──")
    assert_raises(
        "total_leads mismatch caught",
        lambda: ScoutOutput(
            leads=[],
            total_leads=5,  # mismatch
            triage_summary={}
        )
    )

    # LoanFeasibilityAssessment — minimum broker questions
    print("\n── LoanFeasibilityAssessment ──")
    assert_raises(
        "fewer than 5 broker questions caught",
        lambda: LoanFeasibilityAssessment(
            preliminary_screen=LoanScreen.UNKNOWN,
            preliminary_screen_reason="test",
            real_determination_disclaimer="This is a test disclaimer that is long enough",
            deal_math=DealMathResult(deal_math_note="test"),
            broker_questions=[
                BrokerQuestion(
                    priority="must_ask",
                    question="q",
                    why_it_matters="w",
                    what_good_looks_like="g"
                )
            ]  # only 1 — should fail
        )
    )

    # ValidatorOutput — needs_retry without guidance
    print("\n── ValidatorOutput ──")
    assert_raises(
        "needs_retry=True without retry_guidance caught",
        lambda: ValidatorOutput(
            accepted_leads=[],
            rejected_leads=[],
            retry_guidance=None,  # missing
            validation_summary={"needs_retry": True}
        )
    )

    # RetryAction enum
    print("\n── RetryAction enum ──")
    assert_ok(
        "valid RetryAction value",
        lambda: RetryAction("increase_pass2_hunts")
    )
    assert_raises(
        "invalid RetryAction value caught",
        lambda: RetryAction("do_something_random")
    )

    # Summary
    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED — {len(failures)} test(s) failed:")
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)