# Pack Manager — CUBE Buildathon 2026 · Round 2

**An AI-assisted outbound package inspection agent.**

---

## Problem

A picker assembles an order and closes the box. If the wrong item or quantity goes in, the buyer receives a mis-ship: a refund, a return, a replacement shipment, and often a negative review. Nobody checks, because checking every box by hand costs more than the mis-ships do.

The Pack Manager checks whether the package contents visible in a photograph match the expected order before the box is sealed.

---

## Solution

The operator:
1. Enters the Pack Unit ID
2. Uploads one or more photos of the open package
3. Clicks **Inspect Package**

The system:
1. Looks up the expected order contents
2. Saves the evidence images to disk
3. Sends all images in **one** Gemini Vision call
4. Validates the AI output
5. Applies a **deterministic backend policy** to decide `SEAL` or `STOP & FIX`
6. Saves a traceable inspection record
7. Returns the result to the operator

The AI model provides observations. The backend policy decides the outcome.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python · Flask |
| AI Vision | Google Gemini 2.5 Flash (`google-genai`) |
| Image processing | Pillow |
| Environment | `python-dotenv` |
| Storage | Local filesystem (JSON records + image files) |
| Frontend | HTML · Vanilla CSS · Vanilla JavaScript |

---

## Setup

### Prerequisites

- Python 3.10+
- A Google Gemini API key ([get one here](https://aistudio.google.com/app/apikey))

### Install

```bash
pip install -r requirements.txt
```

### Environment

Create a `.env` file at the project root (same level as `app/`):

```
GEMINI_API_KEY=your_api_key_here
```

> ⚠️ **Never commit `.env` to Git.** It is already in `.gitignore`.

### Run

```bash
$env:FLASK_APP="app/app.py"
python -m flask run
```

Then open `http://127.0.0.1:5000` in your browser.

---

## How It Works

### 1. Order Lookup

The unit ID is looked up in `data/pack_sample.csv`. This returns the expected order contents as a list of SKUs and quantities. Product names are enriched from `data/product_catalog.csv`.

### 2. Evidence Saving (Fail-Open)

Before calling the AI model, all uploaded images are saved to:

```
data/inspection_records/<org_id>/<record_id>/evidence_01.jpg
```

This ensures evidence is preserved even if the AI call fails.

### 3. Gemini Vision Inspection

All images for one package are sent in **one** Gemini API call. The prompt instructs the model to:

- Use all photos as combined evidence for the same package
- Not double-count items visible in multiple photos
- Return structured JSON with three checks
- Use `UNCERTAIN` when evidence is insufficient

The model returns:

```json
{
  "checks": [
    {"check_key": "item_identification", "verdict": "PASS", "confidence": 0.95, "detail": "..."},
    {"check_key": "quantity_verification", "verdict": "PASS", "confidence": 0.92, "detail": "..."},
    {"check_key": "order_matching",        "verdict": "PASS", "confidence": 0.90, "detail": "..."}
  ],
  "reason": "..."
}
```

### 4. Deterministic Policy

The backend ignores any decision the model might suggest. It applies:

```
ALL checks PASS     → SEAL
ANY check FAIL      → STOP_AND_FIX
ANY check UNCERTAIN → STOP_AND_FIX
AI unavailable      → PENDING_REVIEW
```

### 5. Record Saving

A JSON inspection record is saved to:
```
data/inspection_records/<org_id>/<record_id>/record.json
```

The record includes all checks, the decision, a content hash, and any errors.

---

## UNCERTAIN Handling

`UNCERTAIN` is not a low-confidence PASS.

`UNCERTAIN` means: *"The visual evidence is insufficient to make a reliable determination."*

Examples:
- Blurry or dark photo
- Package contents hidden or partially visible
- Label cannot be read
- Item is ambiguous — could be more than one product

When any check is `UNCERTAIN`, the decision is `STOP_AND_FIX`. The operator must manually verify and re-inspect.

The UI shows `UNCERTAIN` as a visually distinct amber state. It is never hidden or merged with `PASS`.

---

## Fail-Open Behavior

If Gemini times out, returns an error, or returns invalid output:

1. The uploaded images are already saved (saved **before** the AI call)
2. The inspection record is saved with `status: pending`
3. The decision is set to `PENDING_REVIEW`
4. A human-readable error message is shown in the UI
5. The operator is **not blocked** — they can continue with other units

The application never returns an unhandled 500 error for AI failures.

---

## Multi-Image Inspection

Multiple photos of the same package are all sent in **one** Gemini call. The prompt explicitly instructs the model:

- All photos represent the same package
- Use them together as combined evidence
- Do not double-count an item visible in multiple photos
- Only count each physical item once

---

## Tenancy Isolation

Records and images are stored in organisation-scoped directories:

```
data/inspection_records/org_demo_alpha/<record_id>/
data/inspection_records/org_demo_bravo/<record_id>/
```

A record belonging to `org_demo_alpha` cannot be accessed by `org_demo_bravo`, even if the record ID is guessed. The API returns 404 for any cross-org access attempt.

Switch between organisations using the org selector in the inspection history panel.

---

## Security

- `GEMINI_API_KEY` is read from `.env` server-side only
- The API key is never included in HTML, JavaScript, or API responses
- `.env` is in `.gitignore` and must never be committed
- User-provided filenames are not used as paths (path traversal prevention)
- File extension is sanitised before saving evidence images

---

## Evidence Record

Every inspection produces a JSON record following the evidence contract:

```json
{
    "record_id": "REC-20260601-091500-K3F2X1",
    "schema_version": "1.0",
    "organization_id": "org_demo_alpha",
    "agent": "pack_manager",
    "subject": {"unit_id": "UNIT-0006", "order_id": "ORD-DUMMY-50006"},
    "captured_at": "2026-06-01T09:15:00Z",
    "images": ["evidence_01.jpg"],
    "checks": [...],
    "outcome": {
        "decision": "SEAL",
        "decided_by": "pack_manager_policy",
        "overrides": []
    },
    "status": "completed",
    "content_hash": "sha256:..."
}
```

The `content_hash` is a SHA-256 hash of the record JSON for integrity checking. It is not a blockchain anchor or cryptographically immutable record.

---

## Operator Overrides

An operator can override an AI decision via `POST /api/records/<id>/override`. The original AI decision is **never erased**. The override is appended:

```json
{
    "original_decision": "STOP_AND_FIX",
    "new_decision": "SEAL",
    "reason": "Manual verification completed by senior operator.",
    "operator": "op_dana",
    "timestamp": "..."
}
```

The UI shows both the AI decision and the override.

---

## Inspection History

The history panel shows all inspection records for the selected organisation. Switching the org selector (`org_demo_alpha` / `org_demo_bravo`) demonstrates tenancy isolation — each org sees only its own records.

Click any record row to view full detail in a modal.

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Dashboard UI |
| `POST` | `/inspect` | Run package inspection |
| `GET` | `/api/order/<unit_id>` | Get expected order for unit |
| `GET` | `/api/records?org=<org>` | List records (org-scoped) |
| `GET` | `/api/records/<id>?org=<org>` | Get record detail (org-scoped) |
| `POST` | `/api/records/<id>/override` | Record operator override |
| `GET` | `/api/stats?org=<org>` | Dashboard statistics |

---

## Sample Units for Testing

| Unit ID | Org | Expected | Test Case |
|---|---|---|---|
| `UNIT-0006` | org_demo_bravo | USB-C Cable × 1 | Happy path — correct package |
| `UNIT-0027` | org_demo_bravo | Puzzle × 1 | Extra item (+ cable) — STOP_AND_FIX |
| `UNIT-0034` | org_demo_alpha | Candle × 2 + Bottle × 1 | Extra item (+ cable) — STOP_AND_FIX |
| `UNIT-0044` | org_demo_alpha | Candle × 1 | Wrong item (bottle) — STOP_AND_FIX |
| `UNIT-0078` | org_demo_bravo | Candle × 2 | Extra item (+ cable) — STOP_AND_FIX |
| `UNIT-0009` | org_demo_bravo | Puzzle × 1 + Bottle × 1 | Multi-item order |
| `UNIT-0033` | org_demo_bravo | Protein Powder × 2 | Quantity check (× 2) |

---

## Limitations

1. **Vision model accuracy is unverified at scale.** The core assumption — that a general vision model can reliably identify SKUs and verify quantities across a long-tail catalogue — has not been validated beyond the evaluation set.
2. **Synthetic data only.** All orders, SKUs, and operators are invented. This is not a real Amazon or 3PL data source.
3. **No authentication.** The org isolation is enforced at storage level; there is no login system. In production, authentication would gate which org a user can query.
4. **Flat-file storage.** Not suitable for high-volume production without a database.
5. **SKU inconsistency finding.** Some SKUs in `pack_sample.csv` (e.g. `SKU-TOWEL-BLU`) do not appear in `product_catalog.csv` (which has `SKU-TOWEL-BATH`). These are synthetic data inconsistencies raised as findings per RULES.md.

---

## Evaluation

See `eval/eval_report.md` for the evaluation methodology, results, false positives/negatives, UNCERTAIN cases, and documented failure modes.

---

## Architecture

See `ARCHITECTURE.md` for the full system architecture diagram.

---

*CUBE Buildathon · Commerce Context · Pack Manager · devikasingh098*
