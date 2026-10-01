"""
app/app.py

Pack Manager — Flask application.

Routes:
    GET  /                              Main dashboard UI
    POST /inspect                       Run AI inspection on a package
    GET  /api/order/<unit_id>           Look up expected order for a unit
    GET  /api/records                   List inspection history (org-scoped)
    GET  /api/records/<record_id>       Get full record detail (org-scoped)
    POST /api/records/<record_id>/override  Record an operator override
    GET  /api/stats                     Dashboard statistics (org-scoped)

Tenancy isolation:
    For the demo, org_id comes from the order lookup (CSV).
    All record operations are scoped to the org_id of the inspected unit.
    A unit from org_demo_alpha cannot access records of org_demo_bravo.

Fail-open policy:
    If Gemini fails for any reason, the inspection is saved as PENDING_REVIEW.
    The operator is NOT blocked. Evidence images are always saved first.

Deterministic policy:
    The backend always decides SEAL / STOP_AND_FIX.
    The model provides observations. The policy decides the outcome.
"""

import os
import logging
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify

# Internal modules
# Ensure the app/ directory and project root are both on the path
import sys
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_APP_DIR)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.orders import get_order, parse_order_lines
from gemini import inspect_package
from records import (
    generate_record_id,
    save_evidence_images,
    save_record,
    load_record,
    list_records,
    apply_policy,
    validate_gemini_output,
    compute_content_hash,
    get_stats,
)


# --------------------------------------------------
# FLASK APP
# --------------------------------------------------

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --------------------------------------------------
# CONSTANTS
# --------------------------------------------------

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_IMAGES = 10


# --------------------------------------------------
# UTILITY
# --------------------------------------------------

def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_org_id_from_request():
    """
    Get the org_id for API queries.
    For demo: passed as query param ?org=org_demo_alpha
    Falls back to org_demo_alpha if not provided.
    """
    return request.args.get("org", "org_demo_alpha")


# --------------------------------------------------
# MAIN DASHBOARD
# --------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


# --------------------------------------------------
# ORDER LOOKUP
# --------------------------------------------------

@app.route("/api/order/<unit_id>")
def api_get_order(unit_id):
    """
    Look up the expected order for a unit ID.
    Used by the frontend to show expected contents before inspection.
    """

    if not unit_id or not unit_id.strip():
        return jsonify({
            "status": "error",
            "message": "Unit ID is required."
        }), 400

    order = get_order(unit_id.strip())

    if not order:
        return jsonify({
            "status": "not_found",
            "message": f"No order found for unit '{unit_id}'."
        }), 404

    return jsonify({
        "status": "ok",
        "unit_id": order["unit_id"],
        "org_id": order["org_id"],
        "order_id": order["order_id"],
        "channel": order["channel"],
        "order_lines": order["order_lines"],
        "order_lines_parsed": order["order_lines_parsed"],
    })


# --------------------------------------------------
# INSPECT PACKAGE
# --------------------------------------------------

@app.route("/inspect", methods=["POST"])
def inspect_package_route():
    """
    Main inspection endpoint.

    Workflow:
    1. Validate inputs
    2. Look up order
    3. Generate record_id
    4. Save evidence images (BEFORE Gemini)
    5. Call Gemini (fail-open on error)
    6. Validate Gemini output
    7. Apply deterministic policy
    8. Save JSON record
    9. Return response

    Always returns 200 (even on Gemini failure) with:
    - status: completed | pending
    - decision: SEAL | STOP_AND_FIX | PENDING_REVIEW
    """

    # --------------------------------------------------
    # STEP 1: VALIDATE INPUTS
    # --------------------------------------------------

    unit_id = request.form.get("unit_id", "").strip()

    if not unit_id:
        return jsonify({
            "status": "error",
            "message": "Pack Unit ID is required."
        }), 400

    images = request.files.getlist("package_images")

    valid_images = [
        img for img in images
        if img and img.filename and img.filename.strip()
    ]

    if not valid_images:
        return jsonify({
            "status": "error",
            "message": "At least one package image is required."
        }), 400

    if len(valid_images) > MAX_IMAGES:
        return jsonify({
            "status": "error",
            "message": f"Maximum {MAX_IMAGES} images allowed per inspection."
        }), 400

    # Validate file types and sizes
    for img in valid_images:

        mime = img.mimetype or ""
        if mime not in ALLOWED_IMAGE_TYPES:
            # Check by extension as fallback
            ext = os.path.splitext(img.filename)[1].lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
                return jsonify({
                    "status": "error",
                    "message": (
                        f"File '{img.filename}' is not an allowed image type. "
                        f"Allowed: JPEG, PNG, WebP, GIF, BMP."
                    )
                }), 400

        # Check file size by reading into memory briefly
        img.stream.seek(0, 2)  # seek to end
        file_size = img.stream.tell()
        img.stream.seek(0)     # reset

        if file_size > MAX_FILE_SIZE_BYTES:
            return jsonify({
                "status": "error",
                "message": (
                    f"File '{img.filename}' exceeds the {MAX_FILE_SIZE_MB}MB limit."
                )
            }), 400


    # --------------------------------------------------
    # STEP 2: LOOK UP ORDER
    # --------------------------------------------------

    order = get_order(unit_id)

    if not order:
        return jsonify({
            "status": "error",
            "message": f"No order found for unit '{unit_id}'. Please check the Unit ID."
        }), 404


    # --------------------------------------------------
    # STEP 3: GENERATE RECORD ID
    # --------------------------------------------------

    record_id = generate_record_id()
    org_id = order["org_id"]
    captured_at = utc_now()

    logger.info(f"[Pack Manager] Starting inspection {record_id} for {unit_id} (org: {org_id})")


    # --------------------------------------------------
    # STEP 4: SAVE EVIDENCE IMAGES FIRST
    # (Before Gemini call — fail-open requires this)
    # --------------------------------------------------

    saved_image_paths = []

    try:
        saved_image_paths = save_evidence_images(
            record_id=record_id,
            org_id=org_id,
            file_objects=valid_images,
        )
        logger.info(f"[Pack Manager] Saved {len(saved_image_paths)} evidence images for {record_id}")

    except Exception as e:
        logger.error(f"[Pack Manager] Failed to save evidence images: {e}")
        # Even if image saving fails, we continue and flag as pending
        saved_image_paths = []


    # --------------------------------------------------
    # BUILD INITIAL RECORD SKELETON
    # --------------------------------------------------

    record = {
        "record_id": record_id,
        "schema_version": "1.0",
        "organization_id": org_id,
        "client_id": order.get("operator_id", "unknown"),
        "agent": "pack_manager",
        "subject": {
            "unit_id": unit_id,
            "order_id": order["order_id"],
        },
        "captured_at": captured_at,
        "operator_label": unit_id,
        "images": [
            os.path.basename(p) for p in saved_image_paths
        ],
        "order_lines": order["order_lines"],
        "order_lines_parsed": order["order_lines_parsed"],
        "channel": order["channel"],
        "checks": [],
        "outcome": {
            "decision": "PENDING_REVIEW",
            "decided_by": "pack_manager_policy",
            "decided_at": captured_at,
            "overrides": [],
        },
        "status": "pending",
        "errors": [],
        "content_hash": "",
    }


    # --------------------------------------------------
    # STEP 5: CALL GEMINI (FAIL-OPEN)
    # --------------------------------------------------

    gemini_success = False
    gemini_error_detail = None

    try:

        gemini_result = inspect_package(
            image_paths=saved_image_paths,
            expected_items_str=order["order_lines"],
            order_lines_parsed=order["order_lines_parsed"],
        )

        # --------------------------------------------------
        # STEP 6: VALIDATE GEMINI OUTPUT
        # --------------------------------------------------

        is_valid, validation_error = validate_gemini_output(gemini_result)

        if not is_valid:
            logger.warning(
                f"[Pack Manager] Gemini output invalid for {record_id}: {validation_error}"
            )
            gemini_error_detail = f"AI returned invalid output: {validation_error}"

        else:
            gemini_success = True

            # --------------------------------------------------
            # STEP 7: APPLY DETERMINISTIC POLICY
            # (Never trust the model's decision)
            # --------------------------------------------------

            checks = gemini_result.get("checks", [])
            model_version = gemini_result.get("model_version", "unknown")
            latency_ms = gemini_result.get("latency_ms", 0)
            reason = gemini_result.get("reason", "")

            decision = apply_policy(checks)
            decided_at = utc_now()

            record["checks"] = checks
            record["outcome"] = {
                "decision": decision,
                "decided_by": "pack_manager_policy",
                "decided_at": decided_at,
                "reason": reason,
                "overrides": [],
            }
            record["model_version"] = model_version
            record["latency_ms"] = latency_ms
            record["status"] = "completed"

            logger.info(
                f"[Pack Manager] Inspection {record_id} completed: "
                f"decision={decision}, model={model_version}, latency={latency_ms}ms"
            )

    except Exception as e:

        logger.error(f"[Pack Manager] Gemini error for {record_id}: {type(e).__name__}: {e}")
        gemini_error_detail = f"AI inspection failed: {type(e).__name__}: {str(e)}"


    # --------------------------------------------------
    # HANDLE GEMINI FAILURE (FAIL-OPEN)
    # --------------------------------------------------

    if not gemini_success:

        record["status"] = "pending"
        record["outcome"] = {
            "decision": "PENDING_REVIEW",
            "decided_by": "pack_manager_policy",
            "decided_at": utc_now(),
            "reason": "AI inspection unavailable. Manual review required.",
            "overrides": [],
        }
        record["errors"].append({
            "type": "gemini_failure",
            "detail": gemini_error_detail or "AI inspection failed.",
            "timestamp": utc_now(),
        })

        logger.warning(
            f"[Pack Manager] {record_id} marked PENDING_REVIEW due to Gemini failure."
        )


    # --------------------------------------------------
    # STEP 8: COMPUTE CONTENT HASH + SAVE RECORD
    # --------------------------------------------------

    record["content_hash"] = compute_content_hash(record)

    try:
        save_record(record)
        logger.info(f"[Pack Manager] Record saved: {record_id}")
    except Exception as e:
        logger.error(f"[Pack Manager] Failed to save record {record_id}: {e}")
        record["errors"].append({
            "type": "storage_error",
            "detail": str(e),
            "timestamp": utc_now(),
        })


    # --------------------------------------------------
    # STEP 9: RETURN RESPONSE
    # --------------------------------------------------

    return jsonify({
        "status": record["status"],
        "record_id": record_id,
        "unit_id": unit_id,
        "order_id": order["order_id"],
        "org_id": org_id,
        "channel": order["channel"],
        "expected_items": order["order_lines"],
        "order_lines_parsed": order["order_lines_parsed"],
        "captured_at": captured_at,
        "images_saved": len(saved_image_paths),
        "inspection": {
            "checks": record.get("checks", []),
            "decision": record["outcome"]["decision"],
            "decided_by": record["outcome"]["decided_by"],
            "reason": record["outcome"].get("reason", ""),
            "model_version": record.get("model_version", ""),
            "latency_ms": record.get("latency_ms", 0),
        },
        "content_hash": record["content_hash"],
        "errors": record.get("errors", []),
    })


# --------------------------------------------------
# INSPECTION HISTORY (org-scoped)
# --------------------------------------------------

@app.route("/api/records")
def api_list_records():
    """
    List inspection records for an organisation.
    Enforces tenancy isolation: only returns records for the requested org.
    """

    org_id = get_org_id_from_request()

    if not org_id:
        return jsonify({
            "status": "error",
            "message": "org parameter is required."
        }), 400

    try:
        records = list_records(org_id=org_id, limit=100)
        return jsonify({
            "status": "ok",
            "org_id": org_id,
            "count": len(records),
            "records": records,
        })

    except Exception as e:
        logger.error(f"[Pack Manager] Error listing records for {org_id}: {e}")
        return jsonify({
            "status": "error",
            "message": "Failed to load inspection history."
        }), 500


# --------------------------------------------------
# RECORD DETAIL (org-scoped)
# --------------------------------------------------

@app.route("/api/records/<record_id>")
def api_get_record(record_id):
    """
    Get full detail for a single inspection record.
    Enforces tenancy isolation: record must belong to the requested org.
    """

    org_id = get_org_id_from_request()

    if not org_id:
        return jsonify({
            "status": "error",
            "message": "org parameter is required."
        }), 400

    if not record_id or not record_id.strip():
        return jsonify({
            "status": "error",
            "message": "Record ID is required."
        }), 400

    # load_record enforces org isolation: returns None if wrong org
    record = load_record(record_id=record_id.strip(), org_id=org_id)

    if record is None:
        return jsonify({
            "status": "not_found",
            "message": (
                f"Record '{record_id}' not found "
                f"for organisation '{org_id}'."
            )
        }), 404

    return jsonify({
        "status": "ok",
        "record": record,
    })


# --------------------------------------------------
# OPERATOR OVERRIDE
# --------------------------------------------------

@app.route("/api/records/<record_id>/override", methods=["POST"])
def api_override_record(record_id):
    """
    Record an operator override for an inspection decision.

    The original AI decision is NEVER erased.
    The override stores: original_decision, new_decision, reason, operator, timestamp.

    Body (JSON):
        new_decision: "SEAL" | "STOP_AND_FIX"
        reason: str
        operator: str
    """

    org_id = request.args.get("org", "")

    if not org_id:
        return jsonify({
            "status": "error",
            "message": "org parameter is required."
        }), 400

    record = load_record(record_id=record_id.strip(), org_id=org_id)

    if record is None:
        return jsonify({
            "status": "not_found",
            "message": f"Record '{record_id}' not found for organisation '{org_id}'."
        }), 404

    body = request.get_json(silent=True) or {}

    new_decision = body.get("new_decision", "").strip().upper()
    reason = body.get("reason", "").strip()
    operator = body.get("operator", "unknown_operator").strip()

    if new_decision not in {"SEAL", "STOP_AND_FIX"}:
        return jsonify({
            "status": "error",
            "message": "new_decision must be SEAL or STOP_AND_FIX."
        }), 400

    if not reason:
        return jsonify({
            "status": "error",
            "message": "A reason is required for the override."
        }), 400

    # Preserve original decision
    original_decision = record["outcome"]["decision"]

    override_entry = {
        "original_decision": original_decision,
        "new_decision": new_decision,
        "reason": reason,
        "operator": operator,
        "timestamp": utc_now(),
    }

    record["outcome"]["overrides"].append(override_entry)
    record["outcome"]["decision"] = new_decision
    record["outcome"]["decided_by"] = f"operator_override:{operator}"
    record["outcome"]["decided_at"] = utc_now()

    # Recompute content hash after override
    record["content_hash"] = compute_content_hash(record)

    try:
        save_record(record)
        logger.info(
            f"[Pack Manager] Override recorded for {record_id}: "
            f"{original_decision} → {new_decision} by {operator}"
        )
    except Exception as e:
        logger.error(f"[Pack Manager] Failed to save override for {record_id}: {e}")
        return jsonify({
            "status": "error",
            "message": "Failed to save override."
        }), 500

    return jsonify({
        "status": "ok",
        "record_id": record_id,
        "original_decision": original_decision,
        "new_decision": new_decision,
        "override": override_entry,
    })


# --------------------------------------------------
# DASHBOARD STATS
# --------------------------------------------------

@app.route("/api/stats")
def api_get_stats():
    """
    Return dashboard statistics for an organisation.
    Computed from actual stored records.
    """

    org_id = get_org_id_from_request()

    try:
        stats = get_stats(org_id)
        return jsonify({
            "status": "ok",
            "org_id": org_id,
            "stats": stats,
        })

    except Exception as e:
        logger.error(f"[Pack Manager] Error getting stats for {org_id}: {e}")
        return jsonify({
            "status": "error",
            "message": "Failed to load statistics."
        }), 500


# --------------------------------------------------
# ENTRY POINT
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)