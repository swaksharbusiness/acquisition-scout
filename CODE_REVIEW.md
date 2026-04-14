# Acquisition Scout — Senior Engineer Code Review (Brutally Honest)

Date: 2026-04-14

This review is based on the repository contents on disk under `c:\Users\swaks\projects\acquisition-scout`.

## Executive summary (what’s actually going on)

1) **The project is incomplete / stubbed**: several “core” files are **0 bytes** (`main.py`, `server.py`, `agents/dashboard.py`, `docs/index.html`, `schemas/__init__.py`, `tests/__init__.py`). That means your “entire project” cannot run end-to-end as described.

2) **Schema drift is already a real problem**: `schemas/models.py` describes strict stage boundary contracts, but the agent code **does not validate outputs** against these models, and several outputs don’t match the schema names/shape.

3) **Your own sample runners are broken**: multiple `__main__` blocks call `researcher.run()` with the wrong keyword argument name (`seen_ids` vs `seen_canonical_keys`). Those scripts will crash immediately.

4) **You have a “scripts-in-a-folder” architecture**: repeated `sys.path.append(...)` hacks indicate this is not a proper Python package, which will keep causing import/runtime weirdness.

5) **Logging / error handling is CLI-grade**: `print()` everywhere, no log levels, and missing retry/backoff around network calls.

---

## Files reviewed (every file in repo)

Top-level:
- `.gitignore`
- `config_state.json`
- `config.py`
- `ideas_memory.json`
- `main.py` (**empty**)
- `requirements.txt`
- `server.py` (**empty**)
- `url_rules.py`

`agents/`:
- `agents/dashboard.py` (**empty**)
- `agents/loan_feasibility.py`
- `agents/planner.py`
- `agents/researcher.py`
- `agents/scout.py`
- `agents/validator.py`

`docs/`:
- `docs/index.html` (**empty**)

`schemas/`:
- `schemas/__init__.py` (**empty**)
- `schemas/models.py`

`tests/`:
- `tests/__init__.py` (**empty**)
- `tests/fixtures/sample_pass1.json`
- `tests/fixtures/sample_pass2.json`

`runs/`:
- `runs/20260412-205903/` (artifact folder)
- `runs/20260412-212608/` (artifact folder)

---

## Findings by file (with line numbers + fixes)

> Formatting: **Issue** → file:line(s), one-sentence description, then before/after snippet.

### `.gitignore`

#### Issue 1 — Missing ignores for run artifacts + incomplete Python cache patterns
**File:** `.gitignore:1-5`

**What’s wrong (1 sentence):** You’ll accidentally commit run artifacts and you’re not ignoring `__pycache__` folders robustly.

**Before**
```gitignore
/.env
__pycache__
*.pyc
.DS_Store
venv/
```

**After**
```gitignore
# env
.env
.venv/
venv/

# python
**/__pycache__/
*.pyc
.pytest_cache/

# app artifacts
runs/

# os
.DS_Store
```

---

### `requirements.txt`

#### Issue 2 — Unpinned dependencies make runs non-reproducible
**File:** `requirements.txt:1-5`

**What’s wrong (1 sentence):** “pip install” on a different day can silently change behavior or break the pipeline.

**Before**
```txt
openai
fastapi
uvicorn
python-dotenv
requests
```

**After (example pinning to versions detected in your environment)**
```txt
openai==2.31.0
fastapi==0.135.3
uvicorn==0.44.0
python-dotenv==1.2.2
requests==2.33.1
pydantic==2.12.5
```

#### Issue 3 — `pydantic` is used but not declared
**File:** `schemas/models.py:22` + `requirements.txt`

**What’s wrong (1 sentence):** You import `pydantic` directly but rely on transitive dependencies to pull it in.

**Fix:** add `pydantic==...` as shown above.

---

### `main.py` (EMPTY)

#### Issue 4 — The orchestrator entrypoint does not exist
**File:** `main.py` (0 bytes; line numbers N/A)

**What’s wrong (1 sentence):** There is no real pipeline runner even though multiple modules document a 6-stage pipeline.

**Before**
```py
# empty
```

**After (minimal example entrypoint)**
```py
from agents.planner import run as planner_run
from agents.researcher import run as researcher_run
from agents.scout import run as scout_run
from agents.loan_feasibility import run as loan_run
from agents.validator import run as validator_run
import config as cfg

def run_pipeline(zip_code: str, liquid_cash: float, radius_miles: int) -> dict:
    brief = planner_run(zip_code=zip_code, liquid_cash=liquid_cash, radius_miles=radius_miles)
    researcher_out = researcher_run(brief=brief, seen_canonical_keys=[], rejection_reasons=[], retry_actions=[], loop_number=1)
    scout_out = scout_run(researcher_output=researcher_out, brief=brief)
    loan_out = loan_run(scout_output=scout_out, brief=brief)
    return validator_run(loan_feasibility_output=loan_out, brief=brief, seen_ids=[], loop_number=1)

if __name__ == "__main__":
    print(run_pipeline(cfg.DEFAULT_ZIP, cfg.DEFAULT_LIQUID_CASH, cfg.DEFAULT_RADIUS_MILES))
```

---

### `server.py` (EMPTY)

#### Issue 5 — You depend on FastAPI/Uvicorn but there is no server
**File:** `server.py` (0 bytes; line numbers N/A)

**What’s wrong (1 sentence):** This can’t be used as an API/service because the server module is empty.

**Before**
```py
# empty
```

**After (minimal viable FastAPI)**
```py
from fastapi import FastAPI
from pydantic import BaseModel
import config as cfg
from main import run_pipeline

app = FastAPI(title="Acquisition Scout")

class RunRequest(BaseModel):
    zip_code: str = cfg.DEFAULT_ZIP
    liquid_cash: float = cfg.DEFAULT_LIQUID_CASH
    radius_miles: int = cfg.DEFAULT_RADIUS_MILES

@app.post("/run")
def run(req: RunRequest):
    return run_pipeline(req.zip_code, req.liquid_cash, req.radius_miles)
```

---

### `config.py`

#### Issue 6 — Import-time side effects (`load_dotenv()` runs on import)
**File:** `config.py:6-9`

**What’s wrong (1 sentence):** Importing config performs environment I/O, making tests and import order harder to reason about.

**Before**
```py
from dotenv import load_dotenv

load_dotenv()
```

**After**
```py
from dotenv import load_dotenv

def load_env() -> None:
    load_dotenv()

# call from main/server explicitly instead of import-time (preferred)
```

#### Issue 7 — Constants exist but are ignored by implementation
**File:** `config.py:20-23` vs `url_rules.py:358-369`

**What’s wrong (1 sentence):** `SERPER_ENDPOINT` and `MAX_SEARCH_RESULTS_PER_QUERY` are defined but not actually used by the search client.

**Before (url_rules hard-codes endpoint)**
```py
response = requests.post(
    "https://google.serper.dev/search",
    ...
)
```

**After**
```py
import config as cfg

response = requests.post(
    cfg.SERPER_ENDPOINT,
    ...
)
```

#### Issue 8 — Duplicate thresholds guarantee future drift
**File:** `config.py:219-227` vs `agents/validator.py:48-55`

**What’s wrong (1 sentence):** `MIN_LEADS_TO_PASS` and `CONFIDENCE_WEIGHTS` are duplicated in multiple places.

**Before (validator.py)**
```py
MIN_LEADS_TO_PASS = 2
CONFIDENCE_WEIGHTS = {"high": 3, "medium": 2, "low": 1}
```

**After**
```py
MIN_LEADS_TO_PASS = cfg.MIN_LEADS_TO_PASS
CONFIDENCE_WEIGHTS = cfg.CONFIDENCE_WEIGHTS
```

---

### `url_rules.py`

#### Issue 9 — `sys.path.append(...)` import hack
**File:** `url_rules.py:30`

**What’s wrong (1 sentence):** This breaks packaging and produces different behavior depending on how/where code is invoked.

**Before**
```py
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
```

**After**
```py
# Delete this; package the repo properly (see Architecture section).
```

#### Issue 10 — `normalize_url()` comment is misleading and the implementation is too aggressive
**File:** `url_rules.py:114-135`

**What’s wrong (1 sentence):** You claim to remove “common tracking parameters” but you actually strip *all* query params, which can break canonicalization.

**Before**
```py
"",   # query — strip tracking params
```

**After (keep query minus tracking keys)**
```py
from urllib.parse import parse_qsl, urlencode

TRACKING_KEYS = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","gclid","fbclid"}
qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_KEYS]

normalized = urlunparse((
    parsed.scheme.lower(),
    parsed.netloc.lower(),
    parsed.path.rstrip("/"),
    "",
    urlencode(qs, doseq=True),
    "",
))
```

#### Issue 11 — Uses MD5 for dedupe key hashing
**File:** `url_rules.py:320-328`

**What’s wrong (1 sentence):** MD5 collision risk is avoidable and shouldn’t be used for identity-ish keys.

**Before**
```py
hash_value = hashlib.md5(raw_string.encode()).hexdigest()[:12]
```

**After**
```py
hash_value = hashlib.sha256(raw_string.encode("utf-8")).hexdigest()[:16]
```

#### Issue 12 — Library functions use `print()` instead of `logging`
**File:** `url_rules.py:352-403`

**What’s wrong (1 sentence):** `print()` makes it impossible to control verbosity, route logs, or integrate into services.

**Before**
```py
print(f"  [search] HTTP {response.status_code} for query: {query[:60]}")
```

**After**
```py
import logging
logger = logging.getLogger(__name__)
logger.warning("[search] HTTP %s for query=%r", response.status_code, query[:60])
```

---

### `agents/planner.py`

#### Issue 13 — Import-time OpenAI client creation
**File:** `agents/planner.py:16`

**What’s wrong (1 sentence):** Importing the module has runtime side effects and makes tests/config errors happen at import time.

**Before**
```py
client = OpenAI(api_key=cfg.OPENAI_API_KEY)
```

**After**
```py
def _get_client() -> OpenAI:
    if not cfg.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAI(api_key=cfg.OPENAI_API_KEY)

def run(...):
    client = _get_client()
```

#### Issue 14 — `run()` is doing too many things
**File:** `agents/planner.py:19-242`

**What’s wrong (1 sentence):** Prompt construction, API I/O, parsing, and validation are all coupled, making changes risky.

**Before**
```py
def run(...):
    system_prompt = ...
    user_prompt = ...
    response = client.chat.completions.create(...)
    brief = json.loads(...)
    ...
```

**After (split responsibilities)**
```py
def _build_prompts(...) -> tuple[str, str]:
    ...

def _parse_json(raw: str, label: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{label} returned invalid JSON: {e}")
```

---

### `agents/researcher.py`

#### Issue 15 — Import-time OpenAI client creation
**File:** `agents/researcher.py:43`

**What’s wrong (1 sentence):** Same side-effect issue as planner; imports shouldn’t create network clients.

**Before**
```py
client = OpenAI(api_key=cfg.OPENAI_API_KEY)
```

**After**
```py
def _get_client() -> OpenAI:
    if not cfg.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAI(api_key=cfg.OPENAI_API_KEY)
```

#### Issue 16 — **Broken sample runner**: wrong keyword argument name
**File:** `agents/scout.py:349-355`, `agents/loan_feasibility.py:724-729`, `agents/validator.py:748-753`

**What’s wrong (1 sentence):** `researcher.run()` expects `seen_canonical_keys`, but sample code passes `seen_ids`, causing an immediate crash.

**Before (scout.py)**
```py
researcher_output = researcher_run(
    brief=brief,
    seen_ids=[],
    rejection_reasons=[]
)
```

**After**
```py
researcher_output = researcher_run(
    brief=brief,
    seen_canonical_keys=[],
    rejection_reasons=[],
    retry_actions=[],
)
```

#### Issue 17 — Price parsing is wrong for common formats (`$400k`, `$1.2M`, ranges)
**File:** `agents/researcher.py:761-769`

**What’s wrong (1 sentence):** `re.sub(r"[^\d.]", "", ...)` turns `$400k` into `400` instead of `400000` and mis-parses many real-world listings.

**Before**
```py
cleaned = re.sub(r"[^\d.]", "", asking_price_raw.split()[0])
asking_price_num = float(cleaned) if cleaned else None
```

**After (minimal but much safer)**
```py
def _parse_price(text: str) -> float | None:
    if not text:
        return None
    t = text.strip().lower().replace(",", "")
    token = next((p for p in re.split(r"\s+", t) if re.search(r"\d", p)), "")
    m = re.match(r"\$?(\d+(?:\.\d+)?)([km])?$", re.sub(r"[^0-9.km$]", "", token))
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2) == "k":
        val *= 1_000
    elif m.group(2) == "m":
        val *= 1_000_000
    return val

asking_price_num = _parse_price(asking_price_raw)
```

#### Issue 18 — Calls a private function from another module
**File:** `agents/researcher.py:635-639`

**What’s wrong (1 sentence):** `url_rules._detect_platform` is private; using it from another module is a guaranteed maintenance trap.

**Before**
```py
platform=url_rules._detect_platform(found_specific_url),
```

**After**
```py
# url_rules.py
def detect_platform(url: str) -> str | None:
    return _detect_platform(url)

# researcher.py
platform=url_rules.detect_platform(found_specific_url),
```

#### Issue 19 — `Path.write_text()` without encoding and without atomic write
**File:** `agents/researcher.py:56-69`

**What’s wrong (1 sentence):** Output encoding is implicit and partial writes can corrupt artifacts if the process crashes mid-write.

**Before**
```py
artifact_path.write_text(json.dumps(data, indent=2))
```

**After (encoding + atomic pattern)**
```py
tmp = artifact_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
tmp.replace(artifact_path)
```

---

### `agents/scout.py`

#### Issue 20 — Import-time OpenAI client creation
**File:** `agents/scout.py:19`

**What’s wrong (1 sentence):** Same side-effect issue; this will explode in tests/linters and makes import order matter.

**Before**
```py
client = OpenAI(api_key=cfg.OPENAI_API_KEY)
```

**After**
```py
def _get_client() -> OpenAI:
    if not cfg.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    return OpenAI(api_key=cfg.OPENAI_API_KEY)
```

#### Issue 21 — You have schemas but you don’t validate LLM output
**File:** `agents/scout.py:273-279`

**What’s wrong (1 sentence):** `json.loads()` only proves “valid JSON”, not “correct shape”, so drift will silently corrupt the pipeline.

**Before**
```py
result = json.loads(raw)
```

**After**
```py
from schemas.models import ScoutOutput

result = json.loads(raw)
ScoutOutput.model_validate(result)
```

---

### `agents/loan_feasibility.py`

#### Issue 22 — Typo in ineligible keywords reduces screening quality
**File:** `agents/loan_feasibility.py:40`

**What’s wrong (1 sentence):** `"insurance compan"` looks accidental and makes your keyword screen worse.

**Before**
```py
"insurance compan",
```

**After**
```py
"insurance company",
```

#### Issue 23 — No retry/backoff around OpenAI calls
**File:** `agents/loan_feasibility.py:456-466`

**What’s wrong (1 sentence):** Transient OpenAI network errors will hard-crash the whole run.

**Before**
```py
response = client.chat.completions.create(...)
```

**After (sketch)**
```py
import time

def with_retries(fn, tries=3, base_delay=1.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(base_delay * (2 ** i))
    raise last

response = with_retries(lambda: client.chat.completions.create(...))
```

#### Issue 24 — `price_within_any_budget` logic is subtly wrong for explicit False
**File:** `agents/loan_feasibility.py:234-237`

**What’s wrong (1 sentence):** Using `or` means `False or False` becomes `False` (fine) but `None or False` becomes `False` (looks like a hard failure when it’s actually “unknown/false”).

**Before**
```py
price_within_any_budget = (
    dm_result.get("within_standard_max_deal") or
    dm_result.get("within_high_goodwill_max_deal")
)
```

**After**
```py
std = dm_result.get("within_standard_max_deal")
hi = dm_result.get("within_high_goodwill_max_deal")
price_within_any_budget = True if (std is True or hi is True) else (False if (std is False and hi is False) else None)
```

---

### `agents/validator.py`

#### Issue 25 — Duplicated constants drift from `config.py`
**File:** `agents/validator.py:48-55` vs `config.py:219-227`

**What’s wrong (1 sentence):** Two sources of truth guarantees inconsistent behavior when one changes.

**Before**
```py
MIN_LEADS_TO_PASS = 2
CONFIDENCE_WEIGHTS = { ... }
```

**After**
```py
MIN_LEADS_TO_PASS = cfg.MIN_LEADS_TO_PASS
CONFIDENCE_WEIGHTS = cfg.CONFIDENCE_WEIGHTS
```

#### Issue 26 — Output shape contradicts schema (`approved_leads` vs `accepted_leads`)
**File:** `agents/validator.py:691-712` vs `schemas/models.py:459-469`

**What’s wrong (1 sentence):** The schema and implementation disagree on field names and validation.result string values.

**Before**
```py
result = {
  "approved_leads": approved_leads,
  ...
}
```

**After (pick one and enforce it)**
```py
result = {
  "accepted_leads": approved_leads,
  "rejected_leads": rejected_leads,
  ...
}
```

---

### `schemas/models.py`

#### Issue 27 — Over-engineered schemas that aren’t enforced (and don’t match reality)
**File:** `schemas/models.py:1-10` (and throughout)

**What’s wrong (1 sentence):** You wrote extensive boundary models but you never validate agent outputs against them, so they provide almost no safety.

**Before**
```py
# schema exists, but agents never call model_validate()
```

**After**
```py
from schemas.models import ScoutOutput

raw = json.loads(llm_json)
validated = ScoutOutput.model_validate(raw)
return validated.model_dump()
```

#### Issue 28 — Enum values don’t match actual pipeline output
**File:** `schemas/models.py:72-75` vs `agents/validator.py:638-640`

**What’s wrong (1 sentence):** Schema expects `accepted_for_dashboard`, code outputs `approved`.

**Before**
```py
class ValidationResult(str, Enum):
    ACCEPTED_FOR_DASHBOARD = "accepted_for_dashboard"
```

**After (align with code or change code to match schema)**
```py
class ValidationResult(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
```

---

### `tests/` + fixtures

#### Issue 29 — Fixtures exist, but there are no runnable tests
**Files:** `tests/__init__.py` (empty), `tests/fixtures/*.json`

**What’s wrong (1 sentence):** You have sample data but no automated assertions, so regressions will only be discovered in live runs.

**Before**
```py
# tests/__init__.py empty
```

**After (minimal pytest example)**
```py
# tests/test_url_rules.py
from url_rules import classify_url

def test_classify_url_specific():
    out = classify_url("https://www.bizbuysell.com/business-for-sale/hvac/987654/")
    assert out["is_specific"] is True
```

---

### `config_state.json` / `ideas_memory.json`

#### Issue 30 — Persistence is stubbed; no code loads/saves these
**Files:** `config_state.json:1`, `ideas_memory.json:1`

**What’s wrong (1 sentence):** “memory” and “runtime config” are foundational concepts in your docs, but there’s no implementation tying them into the pipeline.

**Before**
```json
{}
```

**After (design direction)**
```py
# memory.py
def load_memory(path: str) -> list[str]: ...
def save_memory(path: str, keys: list[str]) -> None: ...
```

---

### Empty modules (`agents/dashboard.py`, `docs/index.html`, `schemas/__init__.py`, `tests/__init__.py`)

#### Issue 31 — Entire stages/modules are missing
**Files:**
- `agents/dashboard.py` (0 bytes)
- `docs/index.html` (0 bytes)
- `schemas/__init__.py` (0 bytes)
- `tests/__init__.py` (0 bytes)

**What’s wrong (1 sentence):** The pipeline claims these components exist but they currently provide zero functionality.

**Fix:** either implement them or remove references until they’re real.

---

## Architecture / Engineering guidance (what I’d do as a senior owning this)

1) **Turn this into a proper package**
- Create `acquisition_scout/` (package dir) and move modules under it.
- Remove all `sys.path.append(...)` hacks.
- Add `pyproject.toml` and use editable installs for dev.

2) **Enforce stage-boundary schema validation**
- Every agent should:
  - parse JSON
  - `model_validate()` against the stage schema
  - return `.model_dump()`

3) **Centralize IO + retries**
- All OpenAI calls through one wrapper with retry/backoff.
- All Serper calls through one wrapper with consistent logging, timeout, and error classification.

4) **Split giant modules by concern**
- `agents/researcher.py` is doing ~4 jobs; split it before it becomes unmaintainable.

---

## Final lists

### Must fix now (crashes / incorrect behavior / guaranteed drift)
1) **Broken sample runners:** `researcher.run()` called with `seen_ids` keyword in multiple places (`agents/scout.py:349-355`, `agents/loan_feasibility.py:724-729`, `agents/validator.py:748-753`).
2) **Empty core files:** `main.py`, `server.py`, `agents/dashboard.py`, `docs/index.html` are empty, so “the project” isn’t actually whole.
3) **Schema drift + no validation:** schemas are not enforced; validator output fields don’t match schema.
4) **Price parsing bug:** `agents/researcher.py:761-769` will mis-parse common listing formats.
5) **Config constants unused:** `SERPER_ENDPOINT` is ignored; hard-coded endpoint exists in `url_rules.py`.

### Should fix soon (high-leverage maintainability)
1) Replace `print()` with `logging` across the codebase.
2) Remove import-time OpenAI client construction; instantiate inside `run()` or via DI.
3) Package the repo and remove `sys.path.append` usage.
4) Add retry/backoff to all external calls (OpenAI + Serper).
5) Add real tests (at least URL rules + schema validation tests).

### Nice to have (clarity / polish)
1) Pin dependencies; add a lockfile or constraints.
2) Make artifact writes atomic and consistent (`utf-8`, tmp+replace).
3) Improve canonicalization (query param handling) and extend URL rules test cases.
