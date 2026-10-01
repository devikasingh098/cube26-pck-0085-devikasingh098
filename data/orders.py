"""
data/orders.py

Order lookup and product catalogue helpers for the Pack Manager.

All data is synthetic. SKUs, orders, and operators are invented
for testing purposes only.
"""

import csv
import os


# --------------------------------------------------
# FILE PATHS
# --------------------------------------------------

DATA_DIR = os.path.dirname(__file__)

ORDERS_FILE = os.path.join(DATA_DIR, "pack_sample.csv")

CATALOG_FILE = os.path.join(DATA_DIR, "product_catalog.csv")


# --------------------------------------------------
# PRODUCT CATALOGUE LOADER
# --------------------------------------------------

_catalog_cache = None


def _load_catalog():
    """Load and cache the product catalogue from CSV."""
    global _catalog_cache

    if _catalog_cache is not None:
        return _catalog_cache

    _catalog_cache = {}

    if not os.path.exists(CATALOG_FILE):
        return _catalog_cache

    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sku = row.get("sku", "").strip()
            name = row.get("product_name", "").strip()
            description = row.get("description", "").strip()
            if sku:
                _catalog_cache[sku] = {
                    "name": name,
                    "description": description,
                }

    return _catalog_cache


def get_product_name(sku):
    """
    Return the human-readable product name for a given SKU.

    Uses the product_catalog.csv for lookup. If the SKU is not found
    in the catalogue (the CSV data uses some SKUs not in the catalogue),
    returns the SKU itself as fallback.

    Args:
        sku (str): The SKU string.

    Returns:
        str: Human-readable product name, or the original SKU if not found.
    """
    catalog = _load_catalog()
    entry = catalog.get(sku.strip())
    if entry:
        return entry["name"]
    return sku  # fallback: return SKU itself


# --------------------------------------------------
# PARSE ORDER LINES
# --------------------------------------------------

def parse_order_lines(order_lines_str):
    """
    Parse the order_lines field from the CSV into a structured list.

    The CSV format is: SKU-A:qty;SKU-B:qty
    Example: "SKU-CABLE-USBC:1;SKU-BOTTLE-750:2"

    Returns a list of dicts with keys: sku, quantity, name
    Example: [
        {"sku": "SKU-CABLE-USBC", "quantity": 1, "name": "USB-C Cable"},
        {"sku": "SKU-BOTTLE-750",  "quantity": 2, "name": "750ml Water Bottle"},
    ]

    Args:
        order_lines_str (str): Raw order_lines string from CSV.

    Returns:
        list: Parsed order line dicts.
    """

    if not order_lines_str or not order_lines_str.strip():
        return []

    result = []

    for part in order_lines_str.strip().split(";"):

        part = part.strip()
        if not part:
            continue

        if ":" in part:
            sku, qty_str = part.split(":", 1)
            sku = sku.strip()
            try:
                qty = int(qty_str.strip())
            except ValueError:
                qty = 1
        else:
            sku = part.strip()
            qty = 1

        result.append({
            "sku": sku,
            "quantity": qty,
            "name": get_product_name(sku),
        })

    return result


# --------------------------------------------------
# SINGLE ORDER LOOKUP
# --------------------------------------------------

def get_order(unit_id):
    """
    Look up an order by unit_id from pack_sample.csv.

    Args:
        unit_id (str): The unit ID to look up.

    Returns:
        dict or None: Order data dict, or None if not found.
    """

    if not os.path.exists(ORDERS_FILE):
        return None

    with open(ORDERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:

            if row.get("unit_id", "").strip() == unit_id.strip():

                order_lines_str = row.get("order_lines", "")
                parsed_lines = parse_order_lines(order_lines_str)

                return {
                    "record_id": row.get("record_id", "").strip(),
                    "unit_id": row.get("unit_id", "").strip(),
                    "org_id": row.get("org_id", "").strip(),
                    "order_id": row.get("order_id", "").strip(),
                    "channel": row.get("channel", "").strip(),
                    "order_lines": order_lines_str.strip(),
                    "order_lines_parsed": parsed_lines,
                    "operator_verdict": row.get("operator_verdict", "").strip(),
                    "captured_at": row.get("captured_at", "").strip(),
                }

    return None


# --------------------------------------------------
# ALL ORDERS (for validation)
# --------------------------------------------------

def get_all_unit_ids():
    """
    Return all known unit_ids from pack_sample.csv.

    Returns:
        set: Set of unit_id strings.
    """

    ids = set()

    if not os.path.exists(ORDERS_FILE):
        return ids

    with open(ORDERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            uid = row.get("unit_id", "").strip()
            if uid:
                ids.add(uid)

    return ids