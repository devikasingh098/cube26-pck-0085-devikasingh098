"""
app/gemini.py

Gemini Vision integration for the Pack Manager.

This module handles ONE inspection call per package:
  - All uploaded photos are sent in a single model call
  - The model returns structured JSON with per-check verdicts
  - The backend validates the output and applies deterministic policy

The model provides OBSERVATIONS only.
The backend (records.apply_policy) decides the final SEAL / STOP_AND_FIX.

UNCERTAIN is a valid and encouraged verdict when evidence is insufficient.
Do not convert UNCERTAIN into PASS.
"""

import os
import io
import json
import time
import mimetypes
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types, errors
from PIL import Image as PILImage


# --------------------------------------------------
# ENVIRONMENT + CLIENT
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise ValueError(
        "GEMINI_API_KEY was not found in the project .env file. "
        "Create a .env file at the project root with: GEMINI_API_KEY=your_key_here"
    )


client = genai.Client(
    api_key=api_key,
    http_options=types.HttpOptions(timeout=120000)
)


# --------------------------------------------------
# MODEL CONFIGURATION
#
# For NEW API keys, Google requires gemini-3.8-flash.
# gemini-2.5-flash is no longer available to new users.
# --------------------------------------------------

MODEL_NAME = "gemini-3.8-flash"

FALLBACK_MODELS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]


# --------------------------------------------------
# GEMINI CALL WITH RETRY + FALLBACK
# --------------------------------------------------

def call_gemini(contents, retries=2):
    """
    Call the Gemini API with retry logic and model fallback.

    Tries the primary model first. If it fails with a ServerError,
    retries up to 'retries' times with exponential backoff.
    If all retries fail, moves to the next fallback model.

    ClientErrors (bad request, quota exceeded) abort immediately
    for that model and try the next fallback.

    Args:
        contents: Gemini contents (list of images + prompt text).
        retries (int): Attempts per model before moving to fallback.

    Returns:
        tuple: (response, model_name_used)

    Raises:
        Exception: If all models and all retries fail.
    """

    models_to_try = [MODEL_NAME] + FALLBACK_MODELS
    last_error = None

    for model in models_to_try:

        for attempt in range(retries):

            try:

                print(
                    f"[Pack Manager] Trying Gemini model: {model} "
                    f"(attempt {attempt + 1}/{retries})"
                )

                response = client.models.generate_content(
                    model=model,
                    contents=contents
                )

                print(f"[Pack Manager] Gemini call succeeded with model: {model}")
                return response, model

            except errors.ServerError as e:

                last_error = e
                print(
                    f"[Pack Manager] Gemini {model} ServerError "
                    f"(attempt {attempt + 1}): {e}"
                )
                time.sleep(2 ** attempt)

            except errors.ClientError as e:

                last_error = e
                print(
                    f"[Pack Manager] Gemini {model} ClientError "
                    f"(aborting this model): {e}"
                )
                break  # Don't retry ClientErrors for this model

    raise last_error or Exception("All Gemini models failed.")


# --------------------------------------------------
# BUILD INSPECTION PROMPT
# --------------------------------------------------

def build_prompt(expected_items_str, order_lines_parsed=None):
    """
    Build the Gemini inspection prompt.

    Uses enriched product names if parsed order lines are available.
    Falls back to the raw order_lines string.

    Args:
        expected_items_str (str): Raw order_lines string.
        order_lines_parsed (list): Optional parsed order lines with names.

    Returns:
        str: The complete prompt to send to Gemini.
    """

    # Build a human-readable expected contents description
    if order_lines_parsed:
        lines = []
        for item in order_lines_parsed:
            name = item.get("name") or item.get("sku", "Unknown item")
            qty = item.get("quantity", 1)
            sku = item.get("sku", "")
            lines.append(f"  - {name} (SKU: {sku}) × {qty}")
        expected_description = "\n".join(lines)
    else:
        expected_description = expected_items_str

    prompt = f"""
You are an AI Pack Manager inspection agent for an outbound fulfilment operation.

You are inspecting ONE package before it is sealed and shipped to a buyer.

MULTIPLE PHOTOGRAPHS MAY BE PROVIDED.
All photographs belong to the SAME package inspection.
Treat all photographs as a single combined evidence set.


EXPECTED ORDER CONTENTS (what should be in this package):

{expected_description}


CRITICAL EVIDENCE RULES — READ CAREFULLY:

1.  Only use information that is explicitly visible in the photographs.
    Never invent a product, SKU, quantity, label, or order requirement.

2.  The expected items listed above are the ONLY items that should be in the box.

3.  Do NOT add an item to the expected list because it appears in a photograph.
    A visible item that is NOT in the expected list is an EXTRA (unexpected) item.

4.  Use ALL photographs together as combined evidence.

5.  Do NOT double-count the same physical item if it appears in multiple photographs.
    Only count each unique physical item once.

6.  If an expected item is not visible in any photograph, treat its observed quantity as 0.

7.  If an item's identity cannot be reliably established from the photographs, use UNCERTAIN.

8.  If the photographs are blurry, too dark, or do not show enough detail, use UNCERTAIN.

9.  UNCERTAIN is a valid, first-class inspection verdict.
    Use it whenever evidence is insufficient.

10. Never force ambiguous evidence into PASS or FAIL.
    UNCERTAIN is better than a wrong verdict.

11. If a clearly different item is visible instead of the expected item, use FAIL.

12. If an unexpected extra item is clearly visible, order_matching must be FAIL.


PERFORM EXACTLY THESE THREE CHECKS:


CHECK 1: item_identification
  Question: Can all expected item types be clearly identified from the photographs?

  PASS:     Every expected item type is clearly identifiable in the photographs.
  FAIL:     At least one expected item is clearly absent and a different item is present.
  UNCERTAIN: The photographs do not provide enough visual evidence to identify
             one or more expected items. (blurry, hidden, unclear, label unreadable)


CHECK 2: quantity_verification
  Question: Does the observed quantity of each expected item match the expected quantity?

  For each expected item:
    expected_quantity = the quantity from the order (above)
    observed_quantity = count of that physical item clearly visible in ALL photographs

  Do NOT double-count items visible in multiple photos.
  If quantity cannot be reliably determined, use UNCERTAIN.

  PASS:     Every expected item is visible in exactly the expected quantity.
  FAIL:     An expected item is present but in the wrong quantity
            (too few or too many of that specific expected item).
  UNCERTAIN: Quantity cannot be reliably established from the photographs.


CHECK 3: order_matching
  Question: Do the total visible package contents exactly match the expected order?

  PASS:     All expected items in correct quantities, and NO unexpected extra items.
  FAIL:     An expected item is missing, the wrong item is present,
            quantities mismatch, OR an unexpected extra item is visible.
  UNCERTAIN: The photographs do not provide enough evidence to determine
             whether the complete contents match the expected order.


EXAMPLE (for guidance):

  Expected: 500-Piece Puzzle × 1

  Photographs show: 500-Piece Puzzle × 1  AND  USB-C Cable × 1

  Correct interpretation:
    item_identification = PASS  (puzzle is identifiable)
    quantity_verification = PASS (puzzle quantity is correct)
    order_matching = FAIL (extra cable is present that was not ordered)


CONFIDENCE:
  Return a number from 0.0 to 1.0 representing your confidence in each check verdict,
  based on the visual quality and clarity of the evidence.
  Do NOT use high confidence to convert UNCERTAIN into PASS.
  UNCERTAIN means evidence is insufficient, regardless of confidence.


RETURN ONLY VALID JSON — no markdown, no explanation, no extra text.

{{
  "checks": [
    {{
      "check_key": "item_identification",
      "verdict": "PASS",
      "confidence": 0.95,
      "detail": "Explain specifically what you observed and why you chose this verdict."
    }},
    {{
      "check_key": "quantity_verification",
      "verdict": "PASS",
      "confidence": 0.95,
      "detail": "Explain specifically what quantity you observed and how it compares to expected."
    }},
    {{
      "check_key": "order_matching",
      "verdict": "PASS",
      "confidence": 0.95,
      "detail": "Explain whether contents match completely, including any extra items found."
    }}
  ],
  "reason": "Brief overall explanation of inspection findings."
}}

Allowed verdicts: PASS, FAIL, UNCERTAIN
Do NOT include a "decision" field — the backend computes the decision.
"""

    return prompt


# --------------------------------------------------
# MAIN INSPECTION FUNCTION
# --------------------------------------------------

def inspect_package(image_paths, expected_items_str, order_lines_parsed=None):
    """
    Run Gemini Vision inspection on a package.

    Sends ALL images in ONE call. Returns structured check results
    with model version and latency added by this function.

    The caller is responsible for:
      - Saving images before calling this function
      - Applying deterministic policy (records.apply_policy)
      - Handling GeminiError (fail-open)

    Args:
        image_paths (list): List of absolute paths to saved evidence images.
        expected_items_str (str): Raw order_lines string.
        order_lines_parsed (list): Optional enriched parsed order lines.

    Returns:
        dict: {
            "checks": [...],          # per-check results
            "reason": "...",          # overall reason from model
            "model_version": "...",   # model actually used
            "latency_ms": 1234,       # total call latency
        }

    Raises:
        Exception: If all Gemini models fail (caller must handle).
    """

    start_time = time.time()


    # --------------------------------------------------
    # LOAD ALL PACKAGE IMAGES AS NORMALISED JPEG BYTES
    # Use Pillow to normalise every image to JPEG in memory.
    # This guarantees clean, valid bytes regardless of the
    # original upload format (PNG, WebP, HEIC, etc.).
    # --------------------------------------------------

    image_parts = []

    for image_path in image_paths:
        # Open with Pillow and normalise to RGB JPEG
        with PILImage.open(image_path) as pil_img:
            # Convert to RGB to handle RGBA/palette/CMYK uploads safely
            if pil_img.mode not in ("RGB", "L"):
                pil_img = pil_img.convert("RGB")

            buf = io.BytesIO()
            pil_img.save(buf, format="JPEG", quality=92)
            jpeg_bytes = buf.getvalue()

        image_parts.append(
            types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg")
        )


    # --------------------------------------------------
    # BUILD PROMPT
    # --------------------------------------------------

    prompt = build_prompt(expected_items_str, order_lines_parsed)


    # --------------------------------------------------
    # ONE GEMINI CALL FOR ALL IMAGES
    # --------------------------------------------------

    # Build contents: image parts first, then the text prompt
    contents = image_parts + [prompt]


    response, model_used = call_gemini(contents)


    # --------------------------------------------------
    # CALCULATE LATENCY
    # --------------------------------------------------

    elapsed_ms = int((time.time() - start_time) * 1000)


    # --------------------------------------------------
    # CLEAN AND PARSE RESPONSE
    # --------------------------------------------------

    text = response.text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.replace("```json", "")
        text = text.replace("```", "")
        text = text.strip()

    result = json.loads(text)


    # --------------------------------------------------
    # ADD MODEL VERSION AND LATENCY TO EACH CHECK
    # --------------------------------------------------

    checks = result.get("checks", [])

    for check in checks:
        check["model_version"] = model_used
        check["latency_ms"] = elapsed_ms
        # Normalise verdict to uppercase
        check["verdict"] = str(check.get("verdict", "UNCERTAIN")).upper()
        # Clamp confidence to [0, 1]
        try:
            check["confidence"] = max(0.0, min(1.0, float(check.get("confidence", 0.5))))
        except (TypeError, ValueError):
            check["confidence"] = 0.5

    result["model_version"] = model_used
    result["latency_ms"] = elapsed_ms

    # Remove any decision field — backend decides, not the model
    result.pop("decision", None)

    return result