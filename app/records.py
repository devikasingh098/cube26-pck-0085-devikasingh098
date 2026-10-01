"""
app/records.py

Handles all inspection record operations for the Pack Manager.

Responsibilities:
- Generate unique record IDs
- Save uploaded evidence images to org-scoped directories
- Save and load JSON inspection records
- Enforce tenancy isolation (org_id scoping)
- Compute a content hash (SHA-256) for record integrity checking
- Apply deterministic Pack Manager policy

Policy rules (non-negotiable):
    ALL checks PASS  →  SEAL
    ANY check FAIL   →  STOP_AND_FIX
    ANY check UNCERTAIN → STOP_AND_FIX
    Cannot determine   → PENDING_REVIEW

The backend always decides. The AI model provides observations only.
"""

import os
import json
import hashlib
import random
import string
import shutil
from datetime import datetime, timezone
from pathlib import Path


# --------------------------------------------------
# PATHS
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent

RECORDS_DIR = BASE_DIR / "data" / "inspection_records"


# --------------------------------------------------
# GENERATE RECORD ID
# --------------------------------------------------

def generate_record_id():
    """
    Generate a unique inspection record ID.
    Format: REC-<YYYYMMDD>-<HHMMSS>-<random6>
    Example: REC-20260601-091500-K3F2X1
    """
    now = datetime.now(timezone.utc)
    date_part = now.strftime("%Y%m%d")
    time_part = now.strftime("%H%M%S")
    rand_part = "".join(
        random.choices(string.ascii_uppercase + string.digits, k=6)
    )
    return f"REC-{date_part}-{time_part}-{rand_part}"


# --------------------------------------------------
# SAVE EVIDENCE IMAGES (BEFORE GEMINI CALL)
# --------------------------------------------------

def save_evidence_images(record_id, org_id, file_objects):
    """
    Save uploaded image file objects to a permanent evidence directory
    BEFORE the Gemini call. This ensures evidence is always preserved
    even if the AI inspection fails.

    Args:
        record_id (str): The generated record ID.
        org_id (str): The organisation ID (for tenancy isolation).
        file_objects: List of Flask FileStorage objects.

    Returns:
        list: List of saved absolute file paths.
    """

    # org-scoped evidence directory
    evidence_dir = RECORDS_DIR / org_id / record_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    saved_paths = []

    for index, file_obj in enumerate(file_objects, start=1):

        if not file_obj or not file_obj.filename:
            continue

        # Sanitise: only use extension, generate safe name
        original_name = file_obj.filename
        ext = os.path.splitext(original_name)[1].lower()

        # Allow only image extensions
        allowed_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
        if ext not in allowed_exts:
            ext = ".jpg"

        safe_filename = f"evidence_{index:02d}{ext}"
        save_path = evidence_dir / safe_filename

        file_obj.save(str(save_path))
        saved_paths.append(str(save_path))

    return saved_paths


# --------------------------------------------------
# DETERMINISTIC POLICY
# --------------------------------------------------

REQUIRED_CHECKS = {
    "item_identification",
    "quantity_verification",
    "order_matching",
}

VALID_VERDICTS = {"PASS", "FAIL", "UNCERTAIN"}


def apply_policy(checks):
    """
    Apply the deterministic Pack Manager policy to a list of check results.

    Rules:
        ALL required checks PASS  → SEAL
        ANY required check FAIL   → STOP_AND_FIX
        ANY required check UNCERTAIN → STOP_AND_FIX
        Missing required checks   → STOP_AND_FIX
        No checks at all          → PENDING_REVIEW

    Args:
        checks (list): List of check dicts with at least 'check_key' and 'verdict'.

    Returns:
        str: One of SEAL, STOP_AND_FIX, PENDING_REVIEW
    """

    if not checks:
        return "PENDING_REVIEW"

    check_map = {}
    for check in checks:
        key = check.get("check_key", "")
        verdict = str(check.get("verdict", "UNCERTAIN")).upper()
        check_map[key] = verdict

    # If any required check is missing, cannot verify
    for required in REQUIRED_CHECKS:
        if required not in check_map:
            return "STOP_AND_FIX"

    # Apply policy rules
    all_pass = True
    for required in REQUIRED_CHECKS:
        verdict = check_map[required]
        if verdict == "FAIL":
            return "STOP_AND_FIX"
        if verdict == "UNCERTAIN":
            return "STOP_AND_FIX"
        if verdict != "PASS":
            return "STOP_AND_FIX"

    if all_pass:
        return "SEAL"

    return "STOP_AND_FIX"


# --------------------------------------------------
# VALIDATE GEMINI OUTPUT
# --------------------------------------------------

def validate_gemini_output(result):
    """
    Validate that Gemini's response contains the required checks
    with valid verdicts. Never trust raw model output.

    Args:
        result (dict): Parsed Gemini JSON output.

    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """

    if not isinstance(result, dict):
        return False, "Response is not a dict."

    checks = result.get("checks")
    if not isinstance(checks, list) or len(checks) == 0:
        return False, "Missing or empty 'checks' list."

    check_keys_found = set()

    for check in checks:

        key = check.get("check_key")
        verdict = str(check.get("verdict", "")).upper()
        confidence = check.get("confidence")

        if not key:
            return False, "A check is missing 'check_key'."

        if verdict not in VALID_VERDICTS:
            return False, (
                f"Check '{key}' has invalid verdict '{verdict}'. "
                f"Must be one of: PASS, FAIL, UNCERTAIN."
            )

        if confidence is not None:
            try:
                c = float(confidence)
                if not (0.0 <= c <= 1.0):
                    return False, (
                        f"Check '{key}' confidence {c} is out of range [0, 1]."
                    )
            except (TypeError, ValueError):
                return False, (
                    f"Check '{key}' has non-numeric confidence."
                )

        check_keys_found.add(key)

    for required in REQUIRED_CHECKS:
        if required not in check_keys_found:
            return False, f"Required check '{required}' is missing from response."

    return True, None


# --------------------------------------------------
# COMPUTE CONTENT HASH
# --------------------------------------------------

def compute_content_hash(record):
    """
    Compute a SHA-256 hash of the inspection record for integrity checking.

    The hash covers the stable record fields (not the content_hash field itself).
    This is a content hash — it is NOT blockchain-anchored, tamper-proof, or
    cryptographically immutable in the blockchain sense.

    It allows detecting accidental corruption or untracked modifications
    to the saved JSON file.

    Args:
        record (dict): The inspection record dict.

    Returns:
        str: Hex-encoded SHA-256 hash string.
    """

    # Create a copy without the content_hash field
    record_copy = {k: v for k, v in record.items() if k != "content_hash"}

    # Stable, deterministic JSON serialisation
    canonical = json.dumps(record_copy, sort_keys=True, ensure_ascii=True)

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------
# SAVE RECORD
# --------------------------------------------------

def save_record(record):
    """
    Save the inspection record JSON to disk.

    Path: data/inspection_records/<org_id>/<record_id>/record.json

    Args:
        record (dict): Full inspection record following the evidence contract.

    Returns:
        str: Path to the saved record file.
    """

    org_id = record.get("organization_id", "unknown_org")
    record_id = record.get("record_id", "unknown_record")

    record_dir = RECORDS_DIR / org_id / record_id
    record_dir.mkdir(parents=True, exist_ok=True)

    record_path = record_dir / "record.json"

    with open(str(record_path), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

    return str(record_path)


# --------------------------------------------------
# LOAD RECORD (with tenancy isolation)
# --------------------------------------------------

def load_record(record_id, org_id):
    """
    Load an inspection record by ID, enforcing tenancy isolation.

    A record belonging to org_demo_alpha must not be accessible
    to org_demo_bravo, even if someone guesses the record ID.

    Args:
        record_id (str): The record ID to load.
        org_id (str): The requesting organisation ID.

    Returns:
        dict or None: The record dict, or None if not found / wrong org.
    """

    # Only look inside this org's directory
    record_path = RECORDS_DIR / org_id / record_id / "record.json"

    if not record_path.exists():
        return None

    try:
        with open(str(record_path), "r", encoding="utf-8") as f:
            record = json.load(f)

        # Double-check org isolation at the record level
        if record.get("organization_id") != org_id:
            return None

        return record

    except (json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------
# LIST RECORDS (org-scoped)
# --------------------------------------------------

def list_records(org_id, limit=100):
    """
    List inspection records for a specific organisation.

    Enforces tenancy isolation: only records belonging to org_id
    are returned.

    Args:
        org_id (str): The organisation ID to scope the listing.
        limit (int): Maximum number of records to return (most recent first).

    Returns:
        list: List of summary dicts (record_id, unit_id, order_id, decision, status, captured_at).
    """

    org_dir = RECORDS_DIR / org_id

    if not org_dir.exists():
        return []

    summaries = []

    for record_dir in org_dir.iterdir():

        if not record_dir.is_dir():
            continue

        record_path = record_dir / "record.json"

        if not record_path.exists():
            continue

        try:
            with open(str(record_path), "r", encoding="utf-8") as f:
                record = json.load(f)

            # Enforce isolation even within org directory
            if record.get("organization_id") != org_id:
                continue

            outcome = record.get("outcome", {})
            subject = record.get("subject", {})

            summaries.append({
                "record_id": record.get("record_id", ""),
                "unit_id": subject.get("unit_id", ""),
                "order_id": subject.get("order_id", ""),
                "decision": outcome.get("decision", ""),
                "status": record.get("status", ""),
                "captured_at": record.get("captured_at", ""),
                "organization_id": record.get("organization_id", ""),
            })

        except (json.JSONDecodeError, OSError):
            continue

    # Sort by captured_at descending (most recent first)
    summaries.sort(key=lambda r: r.get("captured_at", ""), reverse=True)

    return summaries[:limit]


# --------------------------------------------------
# GET EVIDENCE IMAGE PATHS FOR A RECORD
# --------------------------------------------------

def get_evidence_paths(record_id, org_id):
    """
    Get the list of saved evidence image paths for a record.
    Only returns paths within the org's directory (tenancy isolation).

    Args:
        record_id (str): The record ID.
        org_id (str): The organisation ID.

    Returns:
        list: List of absolute path strings for evidence images.
    """

    evidence_dir = RECORDS_DIR / org_id / record_id
    if not evidence_dir.exists():
        return []

    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    paths = []

    for f in sorted(evidence_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in image_exts:
            paths.append(str(f))

    return paths


# --------------------------------------------------
# DASHBOARD STATS
# --------------------------------------------------

def get_stats(org_id):
    """
    Compute dashboard statistics for an organisation from stored records.

    Args:
        org_id (str): The organisation ID.

    Returns:
        dict: Stats with keys total, seal, stop_and_fix, pending_review.
    """

    records = list_records(org_id, limit=10000)

    stats = {
        "total": len(records),
        "seal": 0,
        "stop_and_fix": 0,
        "pending_review": 0,
    }

    for rec in records:
        decision = rec.get("decision", "")
        if decision == "SEAL":
            stats["seal"] += 1
        elif decision == "STOP_AND_FIX":
            stats["stop_and_fix"] += 1
        else:
            stats["pending_review"] += 1

    return stats
