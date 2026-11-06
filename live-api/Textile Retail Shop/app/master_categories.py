"""Product categories, read from the warehouse master.

The shop used to invent its own short list in seed.py — "Sarees", "Casual
Blazers", "Slim Fit Jeans" — nineteen names that existed nowhere else. The
warehouse beside it already maintains the real one: GRN PRODUCT DETAILS.xlsx,
exported to backend/data/categories.json, and every GRN, purchase and stock
report codes against it. Two vocabularies for the same shelves is one too many,
so the shop reads that file instead of keeping a second, drifting copy.

The export lists 686 entries over four sheets, but OVERALL is the whole
vocabulary and KIDS / LADIES / MENS are disjoint subsets of it — 473 distinct
codes. `categories.name` is unique here, so each code is stored once, under the
section that actually owns it (KIDS / LADIES / MENS), or OVERALL for the general
and ACC- codes that belong to no department.

The sync is additive and runs on every start, so a master that gains codes shows
them without a migration. Codes it no longer lists are deleted only when nothing
is filed under them; any category still holding products keeps its row — the UI
groups those separately, under "Other", so a product is never silently cut loose
from its category by an edit to a spreadsheet.
"""
import json
import os
from pathlib import Path

from app import db
from app.dbpatch import apply_all
from app.models import Category

SHOP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MASTER = SHOP_DIR.parent / "backend" / "data" / "categories.json"

# The order the dropdowns group by. OVERALL is last because it is the catch-all:
# someone reaching for a code usually knows the department first.
SECTION_ORDER = ("MENS", "LADIES", "KIDS", "OVERALL")

# Where categories that predate the master (or were typed in by hand) collect.
UNSECTIONED = "Other"


def master_path():
    """The categories export. ESSA_CATEGORIES_JSON overrides it, which is what
    lets the shop run somewhere the warehouse folder isn't a sibling."""
    return Path(os.environ.get("ESSA_CATEGORIES_JSON") or DEFAULT_MASTER)


def load_master():
    """[(section, name), ...] for every distinct code, or [] if no master.

    A name listed under KIDS/LADIES/MENS also appears in OVERALL; the specific
    section wins, so each code comes back exactly once.
    """
    path = master_path()
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []

    section_of = {}
    for name in data.get("OVERALL") or []:
        name = (name or "").strip()
        if name:
            section_of.setdefault(name, "OVERALL")
    for section in ("KIDS", "LADIES", "MENS"):
        for name in data.get(section) or []:
            name = (name or "").strip()
            if name:
                section_of[name] = section

    def sort_key(item):
        name, section = item
        rank = SECTION_ORDER.index(section) if section in SECTION_ORDER else len(SECTION_ORDER)
        return (rank, name)

    return [(s, n) for n, s in sorted(section_of.items(), key=sort_key)]


def _ensure_section_column():
    """Patch the schema before any model query runs. Not just categories.section:
    pruning walks Category.products, so products must be current too."""
    apply_all()


def sync_master_categories():
    """Bring the categories table in line with the master. Safe to call always."""
    _ensure_section_column()          # before any Category query touches `section`

    entries = load_master()
    if not entries:
        # No master reachable — leave whatever the shop already has rather than
        # emptying the dropdown on a machine that just isn't next to the warehouse.
        return {"added": 0, "updated": 0, "pruned": 0, "total": Category.query.count()}

    existing = {c.name: c for c in Category.query.all()}
    added = updated = 0
    for section, name in entries:
        current = existing.get(name)
        if current is None:
            db.session.add(Category(name=name, section=section))
            added += 1
        elif current.section != section:
            current.section = section
            updated += 1

    master_names = {name for _, name in entries}
    pruned = 0
    for name, current in existing.items():
        if name in master_names or current.products:
            continue
        db.session.delete(current)
        pruned += 1

    db.session.commit()
    return {"added": added, "updated": updated, "pruned": pruned,
            "total": Category.query.count()}


def grouped_categories():
    """[(section, [Category, ...]), ...] for the dropdowns — master sections in
    SECTION_ORDER, then anything left over from before the master."""
    buckets = {}
    for c in Category.query.order_by(Category.name).all():
        buckets.setdefault(c.section or UNSECTIONED, []).append(c)

    order = [s for s in SECTION_ORDER if s in buckets]
    order += sorted(s for s in buckets if s not in SECTION_ORDER and s != UNSECTIONED)
    if UNSECTIONED in buckets:
        order.append(UNSECTIONED)
    return [(s, buckets[s]) for s in order]
