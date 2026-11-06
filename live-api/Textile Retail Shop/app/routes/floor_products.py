"""Store → Floor Products: what is kept on each floor, and moving it.

A product's floor (`Product.floor_id`) is where its pieces are kept — the place
you walk to, not a share of the stock. This screen shows each floor of a branch
with what is on it, lets a floor be narrowed by section, category, name or
stock, and moves products to another floor: a few ticked ones, or everything a
filter matches ("all of BOYS from the ground floor to the first").

Every move is written to `product_floor_moves` under one batch, so a manager can
see who rearranged what, and put a batch back if it was a mistake. Nothing here
changes a quantity — the till still sells from the one shop-wide stock figure.
"""
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app import db
# Imported here, never inside a function: mounted in the warehouse, a request-time
# `from app import …` reaches the warehouse's own package (see retail_reports.py).
from app.models import (Category, Floor, Invoice, InvoiceItem, Location, Product,
                        ProductFloorMove, User)
from app.paging import paged
from app.utils import role_required

floor_products_bp = Blueprint("floorplan", __name__)

#: How far back "sold lately" looks on the product list.
SOLD_DAYS = 30
#: Ids moved per UPDATE — keeps a move of a whole migrated section inside the
#: bound-parameter limits of both engines.
CHUNK = 500


def _floors(location_id=None):
    q = Floor.query.filter(Floor.active.isnot(False))
    if location_id:
        q = q.filter(Floor.location_id == location_id)
    return q.order_by(Floor.location_id, Floor.sort_order, Floor.name).all()


def _branches():
    """Branches that have floors, in name order."""
    ids = {f.location_id for f in _floors()}
    return Location.query.filter(Location.id.in_(ids or [0])).order_by(Location.name).all()


def _filtered(floor_key, args):
    """Active products on a floor ("none" for those on no floor), narrowed by the
    screen's filters. Returns the query and the filters it applied."""
    f = {k: (args.get(k) or "").strip() for k in ("q", "section", "category", "stock")}
    q = Product.query.filter(Product.active.isnot(False))
    if floor_key == "none":
        q = q.filter(Product.floor_id.is_(None))
    else:
        q = q.filter(Product.floor_id == int(floor_key))
    if f["section"] or f["category"]:
        q = q.join(Category, Category.id == Product.category_id)
        if f["section"]:
            q = q.filter(Category.section == f["section"])
        if f["category"]:
            q = q.filter(Category.name == f["category"])
    if f["q"]:
        like = f"%{f['q']}%"
        q = q.filter(Product.name.ilike(like) | Product.sku.ilike(like) | Product.barcode.ilike(like)
                     | Product.product_type.ilike(like) | Product.design_no.ilike(like))
    if f["stock"] == "in":
        q = q.filter(Product.stock_qty > 0)
    elif f["stock"] == "out":
        q = q.filter((Product.stock_qty <= 0) | Product.stock_qty.is_(None))
    return q, f


def _floor_figures(floor_ids):
    """{floor id or None: (products, pieces, value at cost)} for active products."""
    rows = (db.session.query(Product.floor_id, func.count(Product.id),
                             func.sum(func.coalesce(Product.stock_qty, 0)),
                             func.sum(func.coalesce(Product.stock_qty, 0) * func.coalesce(Product.cost_price, 0)))
            .filter(Product.active.isnot(False),
                    Product.floor_id.in_(floor_ids or [0]) | Product.floor_id.is_(None))
            .group_by(Product.floor_id).all())
    return {fid: (int(n or 0), float(pcs or 0), float(val or 0)) for fid, n, pcs, val in rows}


def _sold_lately(product_ids):
    """{product id: units sold in the last SOLD_DAYS days}."""
    if not product_ids:
        return {}
    since = datetime.utcnow() - timedelta(days=SOLD_DAYS)
    rows = (db.session.query(InvoiceItem.product_id, func.sum(InvoiceItem.quantity))
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .filter(Invoice.live(), Invoice.invoice_date >= since,
                    InvoiceItem.product_id.in_(product_ids))
            .group_by(InvoiceItem.product_id).all())
    return {pid: float(q or 0) for pid, q in rows}


def _categories_on(floor_key):
    """What a floor holds, category by category: [(section, category, products, pieces)]."""
    q = (db.session.query(Category.section, Category.name, func.count(Product.id),
                          func.sum(func.coalesce(Product.stock_qty, 0)))
         .select_from(Product).outerjoin(Category, Category.id == Product.category_id)
         .filter(Product.active.isnot(False)))
    q = q.filter(Product.floor_id.is_(None)) if floor_key == "none" else q.filter(Product.floor_id == int(floor_key))
    rows = q.group_by(Category.section, Category.name).all()
    out = [(s or "", c or "", int(n or 0), float(p or 0)) for s, c, n, p in rows]
    out.sort(key=lambda r: (r[0] or "~", r[1] or "~"))
    return out


@floor_products_bp.route("/")
@login_required
def index():
    branches = _branches()
    loc = request.args.get("location", type=int)
    if loc not in {b.id for b in branches}:
        loc = branches[0].id if branches else None
    floors = _floors(loc)
    figures = _floor_figures([fl.id for fl in floors])
    floor_key = request.args.get("floor") or (str(floors[0].id) if floors else "none")
    if floor_key != "none" and not any(str(fl.id) == floor_key for fl in floors):
        floor_key = str(floors[0].id) if floors else "none"
    here = next((fl for fl in floors if str(fl.id) == floor_key), None)

    query, filters = _filtered(floor_key, request.args)
    page = paged(query.order_by(Product.category_id, Product.name, Product.id))
    sold = _sold_lately([p.id for p in page.items])
    sections = [s for (s,) in db.session.query(Category.section).filter(Category.section.isnot(None))
                .distinct().order_by(Category.section).all() if s]
    cats = _categories_on(floor_key)
    moves = (db.session.query(ProductFloorMove.batch, func.count(ProductFloorMove.id),
                              func.min(ProductFloorMove.moved_at), func.min(ProductFloorMove.from_floor_id),
                              func.max(ProductFloorMove.from_floor_id), func.min(ProductFloorMove.to_floor_id),
                              func.min(ProductFloorMove.moved_by_id), func.min(ProductFloorMove.note),
                              func.min(ProductFloorMove.undone_by))
             .group_by(ProductFloorMove.batch)
             .order_by(func.min(ProductFloorMove.moved_at).desc()).limit(12).all())
    names = {fl.id: fl.name for fl in Floor.query.all()}
    users ={u.id: u.full_name for u in User.query.filter(User.id.in_({m[6] for m in moves if m[6]} or {0})).all()}
    history = [{"batch": b, "count": n, "when": at, "from": names.get(f1, "No floor") if f1 == f2 else "several floors",
                "to": names.get(t, "No floor"), "by": users.get(u, "—"), "note": note, "undone": undone}
               for b, n, at, f1, f2, t, u, note, undone in moves]
    return render_template(
        "floor_products/index.html", branches=branches, location_id=loc, floors=floors,
        figures=figures, floor_key=floor_key, here=here, page=page, sold=sold, filters=filters,
        sections=sections, cats=cats, history=history, all_floors=_floors(), sold_days=SOLD_DAYS,
        can_move=current_user.is_manager)


def _back():
    """Where to return after a move: the page that sent it, if it is one of ours."""
    back = (request.form.get("back") or "").strip()
    if back.startswith("/") and not back.startswith("//") and "\\" not in back:
        return back
    return url_for("floorplan.index")


def _move(ids, to_floor_id, note):
    """Put these products on `to_floor_id` (None: on no floor). Returns (batch, moved)."""
    batch = datetime.utcnow().strftime("%y%m%d") + secrets.token_hex(3)
    moved = 0
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        rows = (db.session.query(Product.id, Product.floor_id)
                .filter(Product.id.in_(chunk)).all())
        rows = [(pid, fid) for pid, fid in rows if fid != to_floor_id]
        if not rows:
            continue
        db.session.bulk_insert_mappings(ProductFloorMove, [
            {"batch": batch, "product_id": pid, "from_floor_id": fid, "to_floor_id": to_floor_id,
             "note": note or None, "moved_by_id": current_user.id, "moved_at": datetime.utcnow()}
            for pid, fid in rows])
        (Product.query.filter(Product.id.in_([pid for pid, _ in rows]))
         .update({Product.floor_id: to_floor_id}, synchronize_session=False))
        moved += len(rows)
    db.session.commit()
    return batch, moved


@floor_products_bp.route("/move", methods=["POST"])
@login_required
@role_required("admin", "manager")
def move():
    back = _back()
    raw = request.form.get("to_floor") or ""
    to_floor = None
    if raw != "none":
        to = Floor.query.get(int(raw)) if raw.isdigit() else None
        if to is None:
            flash("Choose the floor to move them to.", "warning")
            return redirect(back)
        to_floor = to.id
    if request.form.get("scope") == "all":
        # everything the floor's filters match, every page of it
        query, _f = _filtered(request.form.get("floor") or "none", request.form)
        cat = (request.form.get("only_category") or "").strip()
        if cat:
            query = (query.join(Category, Category.id == Product.category_id) if not _f["category"] and not _f["section"]
                     else query).filter(Category.name == cat)
        ids = [pid for (pid,) in query.with_entities(Product.id).all()]
    else:
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    if not ids:
        flash("Tick the products to move first.", "warning")
        return redirect(back)
    batch, moved = _move(ids, to_floor, (request.form.get("note") or "").strip()[:256])
    where = Floor.query.get(to_floor).name if to_floor else "no floor"
    if moved:
        flash(f"{moved} product{'s' if moved != 1 else ''} moved to {where}.", "success")
    else:
        flash(f"Nothing moved — they were already on {where}.", "info")
    return redirect(back)


@floor_products_bp.route("/undo/<batch>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def undo(batch):
    rows = ProductFloorMove.query.filter_by(batch=batch, undone_by=None).all()
    if not rows:
        flash("That move has already been put back.", "info")
        return redirect(_back())
    redo = datetime.utcnow().strftime("%y%m%d") + secrets.token_hex(3)
    back_n, skipped = 0, 0
    for r in rows:
        p = db.session.get(Product, r.product_id)
        if p is None or p.floor_id != r.to_floor_id:       # moved again since — leave it be
            skipped += 1
            continue
        db.session.add(ProductFloorMove(batch=redo, product_id=p.id, from_floor_id=p.floor_id,
                                        to_floor_id=r.from_floor_id, note=f"Put back ({batch})",
                                        moved_by_id=current_user.id))
        p.floor_id = r.from_floor_id
        r.undone_by = redo
        back_n += 1
    db.session.commit()
    flash(f"{back_n} product{'s' if back_n != 1 else ''} put back where they were"
          + (f"; {skipped} had been moved again since and were left." if skipped else "."), "success")
    return redirect(_back())
