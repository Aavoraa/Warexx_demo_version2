"""Physical stock audit — the screens. The rules are in app/audits.py.

Nothing here decides anything: every refusal, every status change and the
adjustment itself go through the service, so the same rule answers the browser,
the scanner and any future caller. A route that re-implemented "may this be
approved" would be a second answer to the question the audit exists to settle.
"""
import csv
import io
from datetime import date

from flask import (Blueprint, Response, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required

from app import audits, db
from app.master_categories import grouped_categories
from app.models import (Floor, Location, Product, StockAudit, StockAuditLine)
from app.utils import role_required

audits_bp = Blueprint("audits", __name__)

#: The attribute columns the count can be narrowed by, in the order somebody
#: narrows: what sort of garment, then which one. The same list the stock
#: checker uses — one vocabulary for finding an item, wherever you are looking.
ATTRIBUTES = [("size", "Size"), ("color", "Colour"), ("fabric", "Material"),
              ("product_type", "Type"), ("pattern", "Pattern"), ("fit", "Fit"),
              ("design_no", "Design No")]


def _attribute_options():
    """Every value actually present in the catalogue, for the dropdowns.

    Built from the stock rather than a fixed list, so a filter can only ever be
    offered when something is behind it.
    """
    out = {}
    for field, _label in ATTRIBUTES:
        col = getattr(Product, field)
        out[field] = [r[0] for r in db.session.query(col)
                      .filter(col.isnot(None), col != "")
                      .distinct().order_by(col).all()]
    return out


@audits_bp.route("/")
@login_required
def index():
    """Every floor, what it is doing, and the counts already done.

    The floor table §12 asks for: one row per storey with the status of its
    latest count, so "which floors have we done" is answered by looking rather
    than by remembering.
    """
    floors = (Floor.query.filter_by(active=True)
              .order_by(Floor.location_id, Floor.sort_order, Floor.name).all())
    rows = []
    for f in floors:
        status, audit = audits.floor_status(f)
        rows.append({"floor": f, "status": status, "audit": audit,
                     "products": Product.query.filter_by(
                         floor_id=f.id, active=True).count()})
    history = (StockAudit.query.order_by(StockAudit.id.desc()).limit(50).all())
    # Products nobody has placed on a floor yet. Said out loud because on a shop
    # that has never counted, that is ALL of them — and a floor audit that opens
    # empty looks broken rather than new.
    unplaced = Product.query.filter(Product.floor_id.is_(None),
                                    Product.active.is_(True)).count()
    return render_template("audits/index.html", rows=rows, history=history,
                           unplaced=unplaced,
                           locations=Location.query.filter_by(active=True).all())


@audits_bp.route("/start", methods=["POST"])
@login_required
def start():
    floor = Floor.query.get_or_404(request.form.get("floor_id", type=int))
    try:
        audit = audits.open_audit(floor, user_id=current_user.id,
                                  note=request.form.get("note"))
    except audits.AuditError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("audits.index"))
    flash(f"{audit.number} open — counting {floor.name}.", "success")
    return redirect(url_for("audits.detail", aid=audit.id))


@audits_bp.route("/<int:aid>")
@login_required
def detail(aid):
    audit = StockAudit.query.get_or_404(aid)
    search = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    category_id = request.args.get("category", type=int)
    chosen = {field: (request.args.get(field) or "").strip()
              for field, _ in ATTRIBUTES}

    lines = audits.line_rows(audit, search=search, status=status,
                            category_id=category_id, attributes=chosen)
    return render_template(
        "audits/detail.html", audit=audit, lines=lines,
        summary=audit.summary, q=search, status=status,
        category_id=category_id, chosen=chosen, attributes=ATTRIBUTES,
        options=_attribute_options(), category_groups=grouped_categories(),
        showing=len(lines), total=len(audit.lines),
        # Which buttons to draw, from the SAME table the server refuses by.
        # A screen that offered a step the service would reject is a screen
        # that teaches people to distrust it.
        allowed_next=audits.NEXT_STATUS.get(audit.status, set()))


# --------------------------------------------------------------------------
# counting — the scan-driven path §11 describes
# --------------------------------------------------------------------------

def _line_json(line, message=""):
    """One line as the counting screen draws it.

    Everything §6 asks a scan to show, in one payload: the product, every
    attribute worth reading off a rack, what the books say and how they got
    there. The screen never has to fetch a second thing to describe what was
    just scanned.
    """
    p = line.product
    return {
        "line_id": line.id, "product_id": line.product_id,
        "sku": line.sku, "name": line.name,
        "category": p.category.name if p and p.category else "",
        "size": (p.size if p else "") or "", "color": (p.color if p else "") or "",
        "fabric": (p.fabric if p else "") or "",
        "pattern": (p.pattern if p else "") or "", "fit": (p.fit if p else "") or "",
        "product_type": (p.product_type if p else "") or "",
        "design_no": (p.design_no if p else "") or "",
        "hsn": (p.hsn_code if p else "") or "", "unit": (p.unit if p else "") or "",
        "barcode": (p.barcode if p else "") or "",
        "qr": (p.warehouse_qr if p else "") or "",
        "mrp": (p.mrp if p else None), "cost_price": (p.cost_price if p else None),
        "selling_price": (p.selling_price if p else None),
        "floor": p.floor.name if p and p.floor else "",
        "purchase_qty": line.purchase_qty, "sales_qty": line.sales_qty,
        "system_qty": line.system_qty, "physical_qty": line.physical_qty,
        "difference": line.difference, "line_status": line.line_status,
        "scans": line.scans or 0, "off_floor": bool(line.found_off_floor),
        "note": line.note or "", "message": message,
    }


@audits_bp.route("/<int:aid>/scan", methods=["POST"])
@login_required
def scan(aid):
    audit = StockAudit.query.get_or_404(aid)
    data = request.get_json(silent=True) or request.form
    try:
        line, message = audits.scan(audit, data.get("code"))
    except audits.AuditError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    return jsonify(_line_json(line, message))


@audits_bp.route("/<int:aid>/count", methods=["POST"])
@login_required
def set_count(aid):
    audit = StockAudit.query.get_or_404(aid)
    data = request.get_json(silent=True) or request.form
    # Looked up WITHIN this audit, never by id alone: a line id from another
    # count would otherwise be writable through this route.
    line = StockAuditLine.query.filter_by(
        id=int(data.get("line_id") or 0), audit_id=audit.id).first()
    if line is None:
        return jsonify({"error": "That line is not on this count."}), 404
    try:
        qty = data.get("physical_qty")
        if qty in (None, ""):
            audits.uncount(audit, line)
        else:
            audits.count(audit, line, qty, user_id=current_user.id,
                         note=data.get("note"))
    except audits.AuditError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    return jsonify({"line": _line_json(line), "summary": audit.summary})


@audits_bp.route("/<int:aid>/add", methods=["POST"])
@login_required
def add(aid):
    """Put a product on the count by hand — a tag that will not scan."""
    audit = StockAudit.query.get_or_404(aid)
    data = request.get_json(silent=True) or request.form
    product = Product.query.get(int(data.get("product_id") or 0))
    if product is None:
        return jsonify({"error": "No such product."}), 404
    try:
        line = audits.add_product(audit, product)
    except audits.AuditError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    return jsonify(_line_json(line, f"{product.name} added to the count."))


@audits_bp.route("/api/products")
@login_required
def api_products():
    """Search the catalogue, for the add-by-hand box."""
    q = (request.args.get("q") or "").strip()
    query = Product.query.filter(Product.active.is_(True))
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Product.name.ilike(like),
                                    Product.sku.ilike(like),
                                    Product.barcode.ilike(like)))
    rows = query.order_by(Product.name).limit(30).all()
    return jsonify([{"id": p.id, "sku": p.sku, "name": p.name,
                     "stock": p.stock_qty,
                     "floor": p.floor.name if p.floor else "",
                     "size": p.size or "", "color": p.color or ""}
                    for p in rows])


# --------------------------------------------------------------------------
# the way through
# --------------------------------------------------------------------------

@audits_bp.route("/<int:aid>/status", methods=["POST"])
@login_required
def status(aid):
    audit = StockAudit.query.get_or_404(aid)
    want = (request.form.get("status") or "").strip()
    # Approving is what lets stock move afterwards, so it is a manager's to
    # give. Counting is not — that is the job of whoever is walking the floor.
    if want == "approved" and not current_user.is_manager:
        flash("Approving a count is a manager's decision — it is what allows "
              "the stock figures to be changed.", "danger")
        return redirect(url_for("audits.detail", aid=aid))
    try:
        audits.set_status(audit, want, user_id=current_user.id)
    except audits.AuditError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("audits.detail", aid=aid))
    flash(f"{audit.number} is now {want.replace('_', ' ')}.", "success")
    return redirect(url_for("audits.detail", aid=aid))


@audits_bp.route("/<int:aid>/adjust", methods=["POST"])
@login_required
@role_required("admin", "manager")
def adjust(aid):
    audit = StockAudit.query.get_or_404(aid)
    try:
        moved, skipped = audits.apply_adjustment(audit, user_id=current_user.id)
    except audits.AuditError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("audits.detail", aid=aid))
    flash(f"{audit.number} applied — {moved} product(s) corrected, each as a "
          f"stock movement you can find in the ledger.", "success")
    if skipped:
        flash(f"{skipped} line(s) were counted but NOT adjusted: they are held on "
              f"another floor, so this count found where some of them were, not "
              f"how many the shop has. Their own floor's count settles that.",
              "warning")
    return redirect(url_for("audits.detail", aid=aid))


# --------------------------------------------------------------------------
# reading it back
# --------------------------------------------------------------------------

@audits_bp.route("/<int:aid>/export")
@login_required
def export(aid):
    """The count as a CSV — §15's row, in the order §15 lists it."""
    audit = StockAudit.query.get_or_404(aid)
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["Floor", "Store", "Audit", "SKU", "Product", "Category",
                  "Design No", "Size", "Colour", "Material", "Pattern", "Fit",
                  "Type", "MRP", "Cost", "Selling price", "QR",
                  "Purchase qty", "Sales qty", "System stock",
                  "Physical stock", "Difference", "Status", "Counted by",
                  "Counted at", "Note"])
    for line in audit.lines:
        p = line.product
        out.writerow([
            audit.floor.name if audit.floor else "",
            audit.location.name if audit.location else "",
            audit.number, line.sku, line.name,
            p.category.name if p and p.category else "",
            (p.design_no if p else "") or "", (p.size if p else "") or "",
            (p.color if p else "") or "", (p.fabric if p else "") or "",
            (p.pattern if p else "") or "", (p.fit if p else "") or "",
            (p.product_type if p else "") or "",
            (p.mrp if p else "") or "", (p.cost_price if p else "") or "",
            (p.selling_price if p else "") or "",
            (p.warehouse_qr if p else "") or "",
            line.purchase_qty, line.sales_qty, line.system_qty,
            "" if line.physical_qty is None else line.physical_qty,
            "" if line.physical_qty is None else line.difference,
            line.line_status.replace("_", " "),
            line.counted_by.full_name if line.counted_by else "",
            line.counted_at.strftime("%d-%m-%Y %H:%M") if line.counted_at else "",
            line.note or "",
        ])
    name = f"{audit.number}-{(audit.floor.name if audit.floor else 'floor')}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="{name.replace(" ", "-")}"'})


@audits_bp.route("/<int:aid>/print")
@login_required
def print_audit(aid):
    audit = StockAudit.query.get_or_404(aid)
    return render_template("audits/print.html", audit=audit,
                           summary=audit.summary, today=date.today())
