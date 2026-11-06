"""Physical stock audit — counting a floor, and what to do with the gap.

    Select floor → scan a tag → the system says what it believes →
    count what is there → save → …→ complete → review → approve → adjust

The whole module rests on one separation, and everything awkward about it is
that separation being kept:

**A COUNT IS NOT AN ADJUSTMENT.** Scanning a rack records what the shelf held
against what the books said. It changes no stock. Deciding to believe the count
and correct the ledger is a *later, separate, approved* act that writes real
movements (`apply_adjustment`). If a count silently corrected the books there
would be no independent record of what was ever actually on the floor — the one
thing an audit exists to produce — and a miscount would become the truth the
moment somebody typed it.

WHAT A FLOOR'S STOCK MEANS HERE. The shop keeps ONE quantity per product
(`Product.stock_qty`, the figure the till sells against and every screen reads).
It does not keep a quantity per storey, and this module does not invent one:
splitting the number across floors would need the till to know which storey each
sale came off, and it does not. What a floor has instead is the set of products
HELD on it — `Product.floor_id`, a place — so a floor's count is a count of the
garments that live there, each against the shop's own figure for it.

That is honest for a shop where a given SKU sits in one department, which is what
a textile showroom is. It is NOT honest if the same SKU is racked on two floors:
both counts would be measured against the same figure and both would look wrong.
The screen says so rather than pretending otherwise, and the fix, if a shop ever
needs it, is per-floor stock — a bigger change than this one, starting at the
till.

THE FLOOR MAPPING IS BUILT BY COUNTING. Nothing knows where anything is on the
first run, so scanning a garment on the second floor is what records it there.
A garment scanned on a floor it is not assigned to still counts — it is where the
piece IS — and is flagged, because "this was on the wrong floor" is a finding
somebody acts on.
"""
from datetime import datetime

from sqlalchemy import func

from app import billing_numbers, db, warehouse_items
from app.models import (AUDIT_MOVEMENT_REASON, Floor, Product, StockAudit,
                        StockAuditLine, StockMovement)


class AuditError(Exception):
    """Something the audit will not do, with the reason a person needs."""


# --------------------------------------------------------------------------
# what the books say, and how they got there
# --------------------------------------------------------------------------

def ledger_totals(product_ids):
    """{product_id: (in, out)} — every piece in and every piece out, per product.

    Summed from the movement ledger rather than tracked separately, because the
    ledger IS what stock is made of: `in − out` equals `stock_qty` exactly, and
    that identity is the whole point of showing both. A screen that asserted a
    system figure without showing how it got there is a screen somebody has to
    take on trust during the one job that exists not to.

    One grouped query rather than two per product: a floor runs to thousands of
    items and this is drawn on every page of the count.
    """
    if not product_ids:
        return {}
    # CASE rather than MAX(a, b): the two-argument MAX is SQLite's own, and on
    # Postgres MAX is an aggregate — the same query would be a syntax error on
    # the deployment while working perfectly on the warehouse PC.
    rows = (db.session.query(
        StockMovement.product_id,
        func.sum(db.case((StockMovement.change > 0, StockMovement.change), else_=0)),
        func.sum(db.case((StockMovement.change < 0, StockMovement.change), else_=0)))
        .filter(StockMovement.product_id.in_(list(product_ids)))
        .group_by(StockMovement.product_id).all())
    return {pid: (round(float(inward or 0), 3), round(-float(outward or 0), 3))
            for pid, inward, outward in rows}


def _line_for(audit, product, totals=None):
    """The audit's line for a product, created against today's books if new.

    Created lazily — when the floor is opened for products already placed there,
    and when a garment nobody had placed is scanned. Either way the figures are
    frozen at the moment the line appears, which is what makes the variance mean
    something later.
    """
    line = StockAuditLine.query.filter_by(audit_id=audit.id,
                                          product_id=product.id).first()
    if line is not None:
        return line
    if totals is None:
        totals = ledger_totals([product.id])
    inward, outward = totals.get(product.id, (0.0, 0.0))
    line = StockAuditLine(
        audit_id=audit.id, product_id=product.id,
        sku=product.sku, name=product.name,
        system_qty=round(float(product.stock_qty or 0), 3),
        purchase_qty=inward, sales_qty=outward, scans=0,
        found_off_floor=bool(product.floor_id and product.floor_id != audit.floor_id))
    db.session.add(line)
    return line


# --------------------------------------------------------------------------
# opening a count
# --------------------------------------------------------------------------

def open_audit(floor, user_id=None, note=None):
    """Start counting a floor. Refuses while one is already being counted.

    One open count per floor, because two people counting the same rack into two
    documents produce two variances for one shelf and no way to say which is the
    shop's answer.
    """
    if floor is None:
        raise AuditError("Choose a floor to count.")
    running = StockAudit.query.filter_by(floor_id=floor.id,
                                         status="in_progress").first()
    if running is not None:
        raise AuditError(f"{floor.name} is already being counted — "
                         f"{running.number}, started "
                         f"{running.started_at.strftime('%d-%m-%Y %H:%M')}. "
                         f"Finish or cancel that one first.")

    audit = StockAudit(
        number=billing_numbers.next_document_number("AUD", StockAudit.number),
        location_id=floor.location_id, floor_id=floor.id,
        status="in_progress", started_at=datetime.utcnow(),
        started_by_id=user_id, note=(note or "").strip()[:256] or None)
    db.session.add(audit)
    db.session.flush()

    # Everything already known to be on this floor, so the counter has a list to
    # work down rather than only a scanner. A shop that has never placed its
    # products starts with an empty list and fills it by scanning, which is the
    # ordinary first run.
    placed = Product.query.filter_by(floor_id=floor.id, active=True).all()
    totals = ledger_totals([p.id for p in placed])
    for product in placed:
        _line_for(audit, product, totals)
    db.session.commit()
    return audit


# --------------------------------------------------------------------------
# counting
# --------------------------------------------------------------------------

def scan(audit, code):
    """Resolve a scanned tag to a line on this count.

    Takes whatever the reader produced — a SKU, a printed barcode, a warehouse
    QR, a per-piece code — through the shop's one scan entry point, so a tag that
    works at the till works here.

    Returns (line, message). The message is what the screen says out loud: a
    garment already counted, or one found on a floor it does not belong to.
    """
    if not audit.is_open:
        raise AuditError(f"{audit.number} is {audit.status.replace('_', ' ')} — "
                         f"counting is finished.")
    product = warehouse_items.resolve_scan((code or "").strip())
    if product is None:
        raise AuditError(f"Nothing in the catalogue matches “{code}”.")

    line = _line_for(audit, product)
    db.session.flush()
    line.scans = (line.scans or 0) + 1

    message = ""
    if line.physical_qty is not None:
        # Said rather than silently added to: walking a rack twice and counting
        # it twice is the most ordinary way a physical count goes wrong.
        message = (f"Already counted — {product.name} was recorded as "
                   f"{_trim(line.physical_qty)}. Enter the count again to "
                   f"replace it.")
    elif line.found_off_floor:
        other = db.session.get(Floor, product.floor_id)
        message = (f"{product.name} is held on {other.name if other else 'another floor'}"
                   f" — counted here all the same, and flagged.")
    elif product.floor_id is None:
        # The count is what places it. Recorded now rather than on save, so the
        # next scan of the same garment on another floor reads as a move.
        product.floor_id = audit.floor_id
        message = f"{product.name} is now recorded as held on {audit.floor.name}."
    db.session.commit()
    return line, message


def count(audit, line, physical_qty, user_id=None, note=None):
    """Record what was actually on the shelf for one line."""
    if not audit.is_open:
        raise AuditError(f"{audit.number} is {audit.status.replace('_', ' ')} — "
                         f"counting is finished.")
    try:
        qty = round(float(physical_qty), 3)
    except (TypeError, ValueError):
        raise AuditError("A physical count must be a number.")
    if qty < 0:
        raise AuditError("A physical count cannot be negative — a shelf holds "
                         "none of something, never less than none.")
    line.physical_qty = qty
    line.counted_at = datetime.utcnow()
    line.counted_by_id = user_id
    if note is not None:
        line.note = (note or "").strip()[:256] or None
    db.session.commit()
    return line


def uncount(audit, line):
    """Put a line back to never-counted. Different from counting zero.

    Kept as its own action because "we have not looked at this yet" and "we
    looked and there are none" are the two findings an audit must never confuse,
    and a screen with only a quantity box cannot say the first.
    """
    if not audit.is_open:
        raise AuditError(f"{audit.number} is {audit.status.replace('_', ' ')} — "
                         f"counting is finished.")
    line.physical_qty = None
    line.counted_at = None
    line.counted_by_id = None
    db.session.commit()
    return line


def add_product(audit, product):
    """Put a product on the count by hand — for a tag that will not scan."""
    if not audit.is_open:
        raise AuditError(f"{audit.number} is {audit.status.replace('_', ' ')} — "
                         f"counting is finished.")
    line = _line_for(audit, product)
    if product.floor_id is None:
        product.floor_id = audit.floor_id
    db.session.commit()
    return line


# --------------------------------------------------------------------------
# the way through: complete → review → approve
# --------------------------------------------------------------------------

#: What each status may become. A flat table rather than a chain of ifs, so the
#: rule is readable and the screen and the server cannot disagree about it.
NEXT_STATUS = {
    "in_progress": {"completed", "cancelled"},
    "completed": {"reviewed", "in_progress", "cancelled"},
    "reviewed": {"approved", "completed", "cancelled"},
    "approved": set(),          # an approved count is finished; adjust it or not
    "cancelled": set(),
}

STATUS_STAMPS = {
    "completed": ("completed_at", "completed_by_id"),
    "reviewed": ("reviewed_at", "reviewed_by_id"),
    "approved": ("approved_at", "approved_by_id"),
}


def set_status(audit, status, user_id=None):
    """Move a count along, or refuse with the reason."""
    if status not in NEXT_STATUS:
        raise AuditError(f"“{status}” is not an audit status.")
    allowed = NEXT_STATUS.get(audit.status, set())
    if status not in allowed:
        if not allowed:
            raise AuditError(f"{audit.number} is {audit.status} and cannot be "
                             f"changed.")
        raise AuditError(f"{audit.number} is {audit.status.replace('_', ' ')} — "
                         f"it can only become "
                         f"{' or '.join(sorted(s.replace('_', ' ') for s in allowed))}.")
    if status == "completed" and not audit.counted_lines:
        raise AuditError("Nothing has been counted yet, so there is nothing to "
                         "complete. Cancel it instead if the count is not "
                         "going ahead.")
    audit.status = status
    stamp = STATUS_STAMPS.get(status)
    if stamp:
        setattr(audit, stamp[0], datetime.utcnow())
        setattr(audit, stamp[1], user_id)
    if status == "in_progress":
        # Re-opened to finish counting. The completion stamp goes with it —
        # leaving it would say the count was finished at a time it was not.
        audit.completed_at = audit.completed_by_id = None
    db.session.commit()
    return audit


# --------------------------------------------------------------------------
# believing the count
# --------------------------------------------------------------------------

def apply_adjustment(audit, user_id=None):
    """Write the approved variance into the ledger. Once, and only once.

    A movement per line that disagrees, under the audit's own reason and against
    its number — never a silent write of `stock_qty`, so the correction is a row
    somebody can find and question afterwards, exactly like every other stock
    change in this shop.

    TWO KINDS OF LINE ARE LEFT ALONE, and both for the same reason: neither is
    evidence about the product's whole figure.

    **Uncounted lines.** "Nobody looked at it" is not a finding, and adjusting on
    that basis would turn an unfinished count into a shop-wide write-off.

    **Lines found off their floor.** A garment held upstairs and found down here
    was counted as a FINDING — it is in the wrong place — not as a census of that
    product. The shop keeps one figure per product, so writing this count into it
    would say the shop holds only the few pieces that had wandered, and quietly
    write off every one still sitting on the floor it belongs to. Its own floor's
    count is the authority on it; this one says where some of it was.

    Returns (moved, skipped) so the screen can say what it did and did not do.
    """
    if audit.status != "approved":
        raise AuditError(f"{audit.number} is {audit.status.replace('_', ' ')}. "
                         f"Only an approved count may move stock.")
    if audit.adjusted_at is not None:
        raise AuditError(f"{audit.number} was already applied on "
                         f"{audit.adjusted_at.strftime('%d-%m-%Y %H:%M')} — "
                         f"stock has been corrected once and must not be moved "
                         f"again.")

    moved = skipped = 0
    for line in audit.counted_lines:
        gap = line.difference
        if not gap:
            continue
        if line.found_off_floor:
            skipped += 1
            continue
        product = line.product
        if product is None:
            continue
        product.stock_qty = round((product.stock_qty or 0) + gap, 3)
        db.session.add(StockMovement(
            product_id=product.id, change=gap,
            reason=AUDIT_MOVEMENT_REASON,
            reference=f"{audit.number} @ {audit.floor.name if audit.floor else ''}".strip()))
        moved += 1

    audit.adjusted_at = datetime.utcnow()
    audit.adjusted_by_id = user_id
    db.session.commit()
    return moved, skipped


# --------------------------------------------------------------------------
# reading it back
# --------------------------------------------------------------------------

def floor_status(floor):
    """`not_started` · the live count's status · the last finished one's.

    What §12's table shows. Derived from the audits rather than stored on the
    floor: a stored flag is a second answer that drifts the moment a count is
    cancelled.
    """
    live = StockAudit.query.filter_by(floor_id=floor.id,
                                      status="in_progress").first()
    if live is not None:
        return "in_progress", live
    last = (StockAudit.query.filter(StockAudit.floor_id == floor.id,
                                    StockAudit.status != "cancelled")
            .order_by(StockAudit.id.desc()).first())
    return (last.status if last else "not_started"), last


def _trim(n):
    n = float(n or 0)
    return str(int(n)) if abs(n - int(n)) < 1e-6 else f"{n:g}"


def line_rows(audit, search="", status="", category_id=None, attributes=None):
    """The lines on screen, filtered the way §10 asks.

    Filtered in Python rather than in SQL. A floor's count is at most a few
    thousand lines, they are already loaded to draw the summary above the table,
    and the alternative is a query that has to reach through a product into a
    category for every attribute somebody might narrow by.
    """
    attributes = {k: v for k, v in (attributes or {}).items() if v}
    text = (search or "").strip().lower()
    out = []
    for line in audit.lines:
        p = line.product
        if status and line.line_status != status:
            continue
        if category_id and (p is None or p.category_id != category_id):
            continue
        if attributes and p is not None:
            if any((getattr(p, field, None) or "") != value
                   for field, value in attributes.items()):
                continue
        if text:
            hay = " ".join(str(x or "").lower() for x in (
                line.sku, line.name,
                getattr(p, "barcode", None), getattr(p, "design_no", None),
                getattr(p, "size", None), getattr(p, "color", None),
                getattr(p, "fabric", None), getattr(p, "warehouse_qr", None)))
            if text not in hay:
                continue
        out.append(line)
    return out
