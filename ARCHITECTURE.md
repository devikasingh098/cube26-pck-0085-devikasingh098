# Architecture — Pack Manager

**CUBE Buildathon 2026 · Round 2 · Commerce Context**

---

## System Overview

The Pack Manager is an AI-assisted outbound package inspection agent. It receives uploaded photographs of an open package, compares them against the expected order contents, and produces a traceable inspection record with a deterministic operational decision.

The system is explicitly **not** a general-purpose chatbot. It is a focused, operational tool for warehouse packing stations.

---

## Core Design Principles

| Principle | Implementation |
|---|---|
| Evidence first | Images are saved to disk **before** any AI call |
| Fail-open | Gemini failure → `PENDING_REVIEW` record, not a 500 error |
| Deterministic decisions | Backend policy decides `SEAL/STOP_AND_FIX`; model provides observations only |
| UNCERTAIN is first-class | Model is instructed to return UNCERTAIN when evidence is insufficient |
| One model call per package | All images for one package go in one Gemini call |
| Tenancy isolation | Records and images are stored in org-scoped directories; cross-org access returns 404 |
| Honest records | Content hash documents what is covered; no blockchain claims |

---

## Request Flow

```
Operator (browser)
    │
    │  POST /inspect
    │  multipart: unit_id + [package_images…]
    ▼
Flask (app/app.py)
    │
    ├─ 1. Validate inputs (unit_id, file types, file sizes)
    │
    ├─ 2. Look up order from pack_sample.csv (data/orders.py)
    │       → returns org_id, order_id, order_lines, parsed SKUs + names
    │
    ├─ 3. Generate record_id  (REC-YYYYMMDD-HHMMSS-RANDOM6)
    │
    ├─ 4. SAVE evidence images to disk   ← happens BEFORE Gemini
    │       data/inspection_records/<org_id>/<record_id>/evidence_01.jpg …
    │
    ├─ 5. Call Gemini Vision  (app/gemini.py)
    │       All images + inspection prompt → ONE model call
    │       Returns per-check verdicts: PASS / FAIL / UNCERTAIN
    │
    │   [If Gemini fails for any reason]
    │   └─ Skip to step 7 with status=pending
    │
    ├─ 6. Validate Gemini output  (app/records.py)
    │       Check verdicts are PASS/FAIL/UNCERTAIN
    │       Check all required keys are present
    │       If invalid → treat as Gemini failure
    │
    ├─ 7. Apply deterministic Pack Manager policy  (app/records.py)
    │       ALL checks PASS  → SEAL
    │       ANY check FAIL   → STOP_AND_FIX
    │       ANY check UNCERTAIN → STOP_AND_FIX
    │       Gemini unavailable → PENDING_REVIEW
    │       (model's own decision field is ignored)
    │
    ├─ 8. Compute SHA-256 content hash
    │
    ├─ 9. Save JSON inspection record to disk
    │       data/inspection_records/<org_id>/<record_id>/record.json
    │
    └─ 10. Return JSON response to frontend
              status: completed | pending
              decision: SEAL | STOP_AND_FIX | PENDING_REVIEW
              checks: [...per-check results...]
              record_id, content_hash, images_saved, errors
```

---

## Directory Structure

```
cube26-pck-0085-devikasingh098/
│
├── app/
│   ├── app.py           Flask routes and inspection orchestration
│   ├── gemini.py        Gemini Vision integration (one call per package)
│   ├── records.py       Record storage, policy, validation, content hash
│   └── templates/
│       └── index.html   Single-page operations dashboard
│
├── data/
│   ├── orders.py        Order lookup + product name enrichment
│   ├── pack_sample.csv  Synthetic order data (30 units, 2 orgs)
│   ├── product_catalog.csv  SKU → product name mapping
│   └── inspection_records/
│       ├── org_demo_alpha/
│       │   └── REC-…/
│       │       ├── evidence_01.jpg
│       │       ├── evidence_02.jpg
│       │       └── record.json
│       └── org_demo_bravo/
│           └── REC-…/
│               ├── evidence_01.jpg
│               └── record.json
│
├── eval/
│   ├── eval_report.md   Evaluation methodology + results
│   └── eval_cases.json  Documented test cases
│
├── .env                 GEMINI_API_KEY (gitignored)
├── .gitignore
├── requirements.txt
├── README.md
└── ARCHITECTURE.md      (this file)
```

---

## Inspection Record Schema

Every completed inspection produces a `record.json` following this contract:

```json
{
    "record_id": "REC-20260601-091500-K3F2X1",
    "schema_version": "1.0",
    "organization_id": "org_demo_alpha",
    "client_id": "op_dana",
    "agent": "pack_manager",
    "subject": {
        "unit_id": "UNIT-0006",
        "order_id": "ORD-DUMMY-50006"
    },
    "captured_at": "2026-06-01T09:15:00Z",
    "operator_label": "UNIT-0006",
    "images": ["evidence_01.jpg", "evidence_02.jpg"],
    "order_lines": "SKU-CABLE-USBC:1",
    "order_lines_parsed": [
        {"sku": "SKU-CABLE-USBC", "quantity": 1, "name": "USB-C Cable"}
    ],
    "channel": "shopify",
    "checks": [
        {
            "check_key": "item_identification",
            "verdict": "PASS",
            "confidence": 0.95,
            "detail": "USB-C cable clearly visible and identifiable.",
            "model_version": "gemini-2.5-flash",
            "latency_ms": 2100
        },
        {
            "check_key": "quantity_verification",
            "verdict": "PASS",
            "confidence": 0.92,
            "detail": "One USB-C cable observed, matching expected quantity of 1.",
            "model_version": "gemini-2.5-flash",
            "latency_ms": 2100
        },
        {
            "check_key": "order_matching",
            "verdict": "PASS",
            "confidence": 0.90,
            "detail": "Contents match order. No unexpected items visible.",
            "model_version": "gemini-2.5-flash",
            "latency_ms": 2100
        }
    ],
    "outcome": {
        "decision": "SEAL",
        "decided_by": "pack_manager_policy",
        "decided_at": "2026-06-01T09:15:02Z",
        "reason": "All items correctly identified and counted. No extra items.",
        "overrides": []
    },
    "model_version": "gemini-2.5-flash",
    "latency_ms": 2100,
    "status": "completed",
    "errors": [],
    "content_hash": "a3f2c1...sha256..."
}
```

**Pending record** (when Gemini fails):
```json
{
    "status": "pending",
    "outcome": {
        "decision": "PENDING_REVIEW",
        "decided_by": "pack_manager_policy",
        "reason": "AI inspection unavailable. Manual review required."
    },
    "errors": [
        {
            "type": "gemini_failure",
            "detail": "...",
            "timestamp": "..."
        }
    ]
}
```

---

## API Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Main dashboard UI |
| `POST` | `/inspect` | Run AI package inspection |
| `GET` | `/api/order/<unit_id>` | Look up expected order |
| `GET` | `/api/records?org=<org>` | List inspection history (org-scoped) |
| `GET` | `/api/records/<id>?org=<org>` | Get full record detail (org-scoped) |
| `POST` | `/api/records/<id>/override` | Record operator override |
| `GET` | `/api/stats?org=<org>` | Dashboard statistics |

---

## Inspection Checks

| Check | PASS | FAIL | UNCERTAIN |
|---|---|---|---|
| `item_identification` | All expected items clearly identifiable | Wrong item present | Evidence insufficient to identify |
| `quantity_verification` | Correct quantity clearly visible | Wrong quantity | Quantity cannot be determined |
| `order_matching` | All items match, no extras | Missing item, wrong item, or extra item | Insufficient evidence for complete match |

---

## Decision Policy (Deterministic — Backend Only)

```python
def apply_policy(checks):
    # Missing required checks → STOP_AND_FIX
    # ANY FAIL               → STOP_AND_FIX
    # ANY UNCERTAIN          → STOP_AND_FIX
    # ALL PASS               → SEAL
    # No checks (AI down)    → PENDING_REVIEW
```

The model's own decision field is explicitly ignored. The backend always applies this policy.

---

## Tenancy Isolation

Records and evidence images are stored in org-scoped directories:

```
data/inspection_records/<org_id>/<record_id>/
```

The `load_record()` and `list_records()` functions only search within the requesting org's directory. Even if a caller guesses a record ID from another org, they receive a 404 — because the record physically lives in the other org's directory.

The `save_evidence_images()` function also uses the org-scoped path.

**Test:** UNIT-0006 belongs to `org_demo_bravo`. Requesting its record as `org_demo_alpha` returns 404.

---

## Gemini Integration

- **Model**: `gemini-2.5-flash` (primary) with fallbacks to `gemini-2.0-flash`, `gemini-1.5-flash`
- **Call pattern**: All images for one package in **one** model call
- **Retry**: 2 attempts per model on `ServerError`; immediate next model on `ClientError`
- **Timeout**: 120 seconds
- **Output**: Structured JSON with `checks[]` containing `check_key`, `verdict`, `confidence`, `detail`
- **Validation**: Backend validates all verdicts are `PASS/FAIL/UNCERTAIN` before accepting output

---

## Content Hash

The `content_hash` field contains a SHA-256 hash of the record JSON (excluding the `content_hash` field itself), serialised with sorted keys.

**What it covers**: Detects accidental corruption or untracked modification of the saved record file.

**What it does NOT claim**: This is not a blockchain anchor, not tamper-proof in a cryptographic adversarial sense, and not an immutable ledger entry. It is a content hash for integrity checking.

---

## Limitations

1. **No user authentication**: `org_id` is derived from the order lookup. In production, authentication would be required.
2. **Flat file storage**: Records are stored as JSON files. A database would be needed for production scale.
3. **Synthetic data**: All orders, SKUs, and operators are invented. Some SKUs in `pack_sample.csv` do not match `product_catalog.csv` exactly (see `eval/eval_report.md` for this finding).
4. **Vision model accuracy**: The core assumption — that a general vision model can reliably identify and count SKUs — is unverified at scale. `UNCERTAIN` is the designed response for low-confidence cases.
5. **No real-time image viewing**: Evidence images are saved to the server filesystem; the UI shows filenames but not the images themselves (no static file serving for evidence).
6. **Single-server deployment**: Not horizontally scalable without shared storage.
