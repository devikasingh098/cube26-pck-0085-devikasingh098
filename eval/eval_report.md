# Evaluation Report — Pack Manager

**CUBE Buildathon 2026 · Round 2**

---

## Methodology

### Evaluation Approach

The Pack Manager was evaluated on two axes:

1. **Unit test cases** (8 cases): Defined test scenarios covering the required decision paths.
2. **Synthetic dataset evaluation**: The 30 units in `pack_sample.csv` provide a synthetic reference set with `observed_in_box` and `operator_verdict` labels. These are used to verify decision logic, not as ground truth.

> ⚠️ **Note on ground truth**: `operator_verdict` in `pack_sample.csv` is explicitly stated to be "sometimes wrong on purpose" (per `data/README.md`). It is NOT treated as ground truth. It is used only as a reference point for comparing system decisions.

### What Was Evaluated

For each test case / eval unit, the following were recorded:

- Expected decision (`SEAL` / `STOP_AND_FIX` / `PENDING_REVIEW`)
- System decision (from `apply_policy()`)
- Per-check verdicts: `item_identification`, `quantity_verification`, `order_matching`
- Whether the decision was correct
- Failure mode (if any)
- UNCERTAIN cases

### Evaluation Dimensions Per Check

For each of the three checks, we track:

| Metric | Description |
|---|---|
| True Positive (TP) | System says FAIL/UNCERTAIN, ground truth is FAIL/UNCERTAIN |
| True Negative (TN) | System says PASS, ground truth is PASS |
| False Positive (FP) | System says FAIL/UNCERTAIN, ground truth is PASS |
| False Negative (FN) | System says PASS, ground truth is FAIL/UNCERTAIN |
| UNCERTAIN | System returned UNCERTAIN |

---

## Documented Test Cases

### TEST 1 — Correct Package (Happy Path)

- **Unit**: UNIT-0006
- **Expected contents**: USB-C Cable × 1
- **Test image**: Photo of open box containing one USB-C cable
- **Expected checks**: All PASS
- **Expected decision**: SEAL
- **Rationale**: If evidence clearly shows the correct item in the correct quantity, all three checks should PASS and the backend policy should return SEAL.

### TEST 2 — Wrong Item

- **Unit**: UNIT-0044
- **Expected contents**: Candle × 1
- **Observed in box**: Bottle × 1 (per CSV)
- **Test image**: Photo of open box containing a water bottle (not a candle)
- **Expected checks**: `item_identification` = FAIL, `order_matching` = FAIL
- **Expected decision**: STOP_AND_FIX
- **Rationale**: A clearly different item is present instead of the expected item.

### TEST 3 — Ambiguous / Blurry Image

- **Unit**: Any valid unit
- **Test image**: Deliberately blurry, dark, or low-resolution photo where contents cannot be identified
- **Expected checks**: All UNCERTAIN
- **Expected decision**: STOP_AND_FIX
- **Rationale**: UNCERTAIN is a first-class outcome. Insufficient visual evidence must not produce PASS.

### TEST 4 — Extra Item Present

- **Unit**: UNIT-0027
- **Expected contents**: Puzzle × 1
- **Observed in box**: Puzzle × 1 + Cable × 1 (per CSV)
- **Test image**: Photo showing puzzle and an extra USB-C cable
- **Expected checks**: `item_identification` = PASS, `quantity_verification` = PASS, `order_matching` = FAIL
- **Expected decision**: STOP_AND_FIX
- **Rationale**: Expected item is correct, but an unexpected extra item makes order_matching FAIL.

### TEST 5 — Wrong Quantity

- **Unit**: UNIT-0033
- **Expected contents**: Protein Powder × 2
- **Test image**: Photo showing only one protein powder container
- **Expected checks**: `quantity_verification` = FAIL, `order_matching` = FAIL
- **Expected decision**: STOP_AND_FIX
- **Rationale**: Expected item is identifiable but wrong quantity is visible.

### TEST 6 — Gemini Timeout / Failure (Fail-Open)

- **Unit**: Any valid unit
- **Test setup**: Use an invalid API key or simulate network failure
- **Expected behaviour**:
  - Images are saved before the Gemini call
  - Record is saved with `status: pending`
  - Decision is `PENDING_REVIEW`
  - Application returns HTTP 200 (not 500)
  - UI shows "Pending Review — AI inspection unavailable"
- **Rationale**: Fail-open is mandatory. The operator must not be blocked by AI unavailability.

### TEST 7 — Multiple Photos of Same Package

- **Unit**: UNIT-0006
- **Test images**: 3 photos of the same package (front, inside, label)
- **Expected behaviour**:
  - All 3 images are sent in ONE Gemini call
  - Evidence from all photos is combined
  - Items visible in multiple photos are not double-counted
  - One inspection record is created
- **Rationale**: Multi-image batching is a core requirement.

### TEST 8 — Cross-Organisation Access (Tenancy Isolation)

- **Test setup**:
  - Inspect UNIT-0006 (belongs to `org_demo_bravo`)
  - Note the record_id created
  - Request that record_id via `/api/records/<id>?org=org_demo_alpha`
- **Expected behaviour**: HTTP 404 — record not found for that organisation
- **Rationale**: Tenancy isolation must prevent cross-org record access even if the record ID is known.

---

## Policy Logic Verification (Deterministic)

The `apply_policy()` function was unit-tested with all decision paths:

| Checks Input | Expected Output | Result |
|---|---|---|
| All PASS | SEAL | ✅ Verified |
| item_identification = FAIL | STOP_AND_FIX | ✅ Verified |
| quantity_verification = FAIL | STOP_AND_FIX | ✅ Verified |
| order_matching = FAIL | STOP_AND_FIX | ✅ Verified |
| item_identification = UNCERTAIN | STOP_AND_FIX | ✅ Verified |
| quantity_verification = UNCERTAIN | STOP_AND_FIX | ✅ Verified |
| order_matching = UNCERTAIN | STOP_AND_FIX | ✅ Verified |
| Missing required check | STOP_AND_FIX | ✅ Verified |
| Empty checks list | PENDING_REVIEW | ✅ Verified |

---

## Synthetic Dataset Reference

From `pack_sample.csv` (30 units):

| `operator_verdict` | Count | Notes |
|---|---|---|
| `seal` | 28 | Expected: correct package |
| `stop_and_fix` | 2 | UNIT-0027 (extra cable), UNIT-0078 (extra cable) |

Units where `observed_in_box` deliberately differs from `order_lines`:

| Unit | Expected | Observed | Operator Verdict | Notes |
|---|---|---|---|---|
| UNIT-0027 | Puzzle × 1 | Puzzle × 1 + Cable × 1 | stop_and_fix | Extra item case |
| UNIT-0034 | Candle × 2 + Bottle × 1 | Candle × 2 + Bottle × 1 + Cable × 1 | **seal** | ⚠️ Operator verdict appears wrong — extra item present |
| UNIT-0044 | Candle × 1 | Bottle × 1 | **seal** | ⚠️ Operator verdict appears wrong — wrong item |
| UNIT-0078 | Candle × 2 | Candle × 2 + Cable × 1 | stop_and_fix | Extra item case |

> **Finding**: UNIT-0034 and UNIT-0044 have `operator_verdict = seal` despite `observed_in_box` showing mismatches. This supports the spec statement that "operator_verdict is sometimes wrong on purpose." These units demonstrate why AI verification is valuable and why `operator_verdict` is not ground truth.

---

## Findings

### Finding 1: SKU Inconsistency Between Data Files

`pack_sample.csv` uses SKUs not present in `product_catalog.csv`:

| CSV SKU | Catalog SKU | Notes |
|---|---|---|
| `SKU-TOWEL-BLU` | `SKU-TOWEL-BATH` | Different suffix |
| `SKU-CANDLE-3` | `SKU-CANDLE-VAN` | Different suffix |
| `SKU-SERUM-30` | `SKU-SERUM-VITC` | Different suffix |
| `SKU-PROT-1KG` | `SKU-PROTEIN-1KG` | Different prefix |
| `SKU-LAMP-LED` | `SKU-LAMP-DESK` | Different suffix |
| `SKU-MUG-11` | `SKU-MUG-CERAMIC` | Different suffix |
| `SKU-LEASH-6FT` | `SKU-LEASH-DOG` | Different suffix |

**Impact**: Product name enrichment falls back to the raw SKU for these items. The Gemini prompt therefore receives SKU strings instead of human-readable names for approximately 7 of the 10 product types.

**Raised as**: `finding` per RULES.md §"Contradictions are findings."

### Finding 2: Operator Verdict Discrepancies

UNIT-0034 and UNIT-0044 show clear content mismatches in `observed_in_box` but have `operator_verdict = seal`. This is an intentional data quality test per the sample data spec.

---

## Known Failure Modes

| Failure Mode | Description | Mitigation |
|---|---|---|
| Gemini model unavailable | API timeout or server error | Fail-open: PENDING_REVIEW record saved |
| Blurry/dark photos | Model cannot identify contents | UNCERTAIN verdict + STOP_AND_FIX |
| SKU not in catalog | Product name not enriched | Falls back to raw SKU string in prompt |
| Multi-item order confusion | Model may double-count items in multiple photos | Prompt explicitly instructs no double-counting |
| Similar-looking products | Model may misidentify items that look alike | UNCERTAIN is the designed response |
| Very small items | Items too small to identify reliably | UNCERTAIN verdict |

---

## Evaluation Limitations

1. **No unseen test images**: Due to buildathon constraints, the evaluation set uses the same synthetic data as development. A production evaluation would use 50+ unseen units with independently labelled ground truth from two human evaluators.
2. **No human labeller**: Ground truth has not been independently labelled by two human evaluators as specified.
3. **Synthetic images only**: Vision model accuracy on real warehouse photos may differ significantly from synthetic test images.
4. **Per-SKU accuracy not measured**: The model's ability to distinguish between specific SKUs (e.g. Puzzle vs Cable) was not systematically measured across all 10 product types.

---

## Recommended Evaluation Protocol (Production)

For a rigorous evaluation following the buildathon specification:

1. Capture 50+ new package photos not used during development (unseen data)
2. Two independent human evaluators label each unit:
   - Per-check verdicts: PASS / FAIL / UNCERTAIN
   - Final decision: SEAL / STOP_AND_FIX
3. Calculate inter-annotator agreement (Cohen's κ)
4. Run the Pack Manager on the same 50+ units
5. Compare system verdicts to human consensus
6. Report per-check: FP, FN, UNCERTAIN count, precision, recall
7. Report failure modes by category
8. Do NOT cherry-pick examples

### Metrics to Report

```
For each check (item_identification, quantity_verification, order_matching):
    True Positives (correct FAIL/UNCERTAIN detection)
    False Positives (FAIL/UNCERTAIN when should be PASS)
    False Negatives (PASS when should be FAIL/UNCERTAIN)
    UNCERTAIN rate
    Precision = TP / (TP + FP)
    Recall    = TP / (TP + FN)

Overall:
    Decision accuracy (SEAL vs STOP_AND_FIX)
    PENDING_REVIEW rate (AI failure rate)
    Average latency per inspection
    P95 latency
```
