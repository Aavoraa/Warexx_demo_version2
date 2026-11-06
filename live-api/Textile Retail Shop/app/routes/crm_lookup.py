"""CRM → Loyalty Management — the points a customer has earned and spent.

It is also where a manager puts a correction on the record — as a ledger row
with a reason, never by overwriting the balance.
"""
import csv
import io

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from app import crm, db
from app.models import Customer, Invoice, LoyaltyTxn
from app.utils import role_required

loyalty_bp = Blueprint("loyalty", __name__)


# ---------------------------------------------------------------------------
#  Loyalty Management
# ---------------------------------------------------------------------------
def _loyalty_query():
    a = request.args
    q = Customer.query
    filtered = False
    for key, col in (("card", None), ("mobile", Customer.phone), ("email", Customer.email),
                     ("name", Customer.name)):
        val = (a.get(key) or "").strip()
        if not val:
            continue
        filtered = True
        if key == "card":
            c = crm.find_customer(val)
            q = q.filter(Customer.id == (c.id if c else -1))
        elif key == "mobile":
            q = q.filter(or_(Customer.phone.ilike(f"%{val}%"), Customer.alt_phone.ilike(f"%{val}%")))
        else:
            q = q.filter(col.ilike(f"%{val}%"))
    if (a.get("q") or "").strip():                     # the customer page's "Loyalty" link
        c = crm.find_customer(a["q"])
        q = q.filter(Customer.id == (c.id if c else -1))
        filtered = True
    return q, filtered


def _period_points(customer_ids, lo, hi):
    """{customer_id: (earned, redeemed, adjusted)} over the bill-date window."""
    if not customer_ids:
        return {}
    q = (db.session.query(LoyaltyTxn.customer_id, LoyaltyTxn.reason, func.sum(LoyaltyTxn.points))
         .filter(LoyaltyTxn.customer_id.in_(customer_ids)))
    if lo:
        q = q.filter(LoyaltyTxn.created_at >= lo)
    if hi:
        q = q.filter(LoyaltyTxn.created_at < hi)
    out = {}
    for cid, reason, pts in q.group_by(LoyaltyTxn.customer_id, LoyaltyTxn.reason).all():
        e, r, adj = out.get(cid, (0.0, 0.0, 0.0))
        pts = float(pts or 0)
        if reason == "earn":
            e += pts
        elif reason == "redeem":
            r += -pts
        else:
            adj += pts
        out[cid] = (round(e, 2), round(r, 2), round(adj, 2))
    return out


@loyalty_bp.route("/")
@login_required
def index():
    q, filtered = _loyalty_query()
    summary = request.args.get("summary", "1" if not request.args else "") == "1"
    lo, hi = crm.day_range(crm.arg_date("from"), crm.arg_date("to"))
    customers = (q.order_by(Customer.loyalty_points.desc(), Customer.id).limit(200).all()
                 if filtered or summary else [])
    points = _period_points([c.id for c in customers], lo, hi)
    ledger = []
    if customers and not summary:
        lq = LoyaltyTxn.query.filter(LoyaltyTxn.customer_id.in_([c.id for c in customers]))
        if lo:
            lq = lq.filter(LoyaltyTxn.created_at >= lo)
        if hi:
            lq = lq.filter(LoyaltyTxn.created_at < hi)
        ledger = lq.order_by(LoyaltyTxn.created_at.desc()).limit(1000).all()
    by_id = {c.id: c for c in customers}
    bills = {i.id: i for i in Invoice.query.filter(
        Invoice.id.in_({t.invoice_id for t in ledger if t.invoice_id})).all()} if ledger else {}
    totals = db.session.query(func.coalesce(func.sum(Customer.loyalty_points), 0),
                              func.count(Customer.id)).filter(Customer.loyalty_points > 0).one()
    return render_template("crm/loyalty.html", customers=customers, points=points, ledger=ledger,
                           by_id=by_id, bills=bills, summary=summary, filtered=filtered,
                           outstanding=round(float(totals[0] or 0), 2), holders=totals[1])


@loyalty_bp.route("/export.csv")
@login_required
def export():
    q, _ = _loyalty_query()
    lo, hi = crm.day_range(crm.arg_date("from"), crm.arg_date("to"))
    customers = q.filter(Customer.loyalty_points != 0).order_by(Customer.name, Customer.id).limit(50000).all()
    points = _period_points([c.id for c in customers], lo, hi)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Card", "Customer", "Mobile", "Earned", "Redeemed", "Adjusted", "Balance", "Total spent"])
    for c in customers:
        e, r, adj = points.get(c.id, (0, 0, 0))
        w.writerow([c.card_no, c.name, c.phone or "", e, r, adj, round(c.loyalty_points or 0, 2),
                    round(c.total_spent or 0, 2)])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=loyalty.csv"})


@loyalty_bp.route("/adjust", methods=["POST"])
@login_required
@role_required("admin", "manager")
def adjust():
    c = db.session.get(Customer, request.form.get("customer_id", type=int) or 0)
    pts = crm.form_float("points")
    reason = crm.text("reason", 48)
    if c is None or not pts or not reason:
        flash("Give the points (+ to add, − to take off) and the reason.", "danger")
        return redirect(request.referrer or url_for("loyalty.index"))
    if (c.loyalty_points or 0) + pts < -0.001:
        flash(f"{c.name} has {c.loyalty_points:.0f} points — that would take them below zero.", "danger")
        return redirect(request.referrer or url_for("loyalty.index"))
    c.loyalty_points = round((c.loyalty_points or 0) + pts, 2)
    db.session.add(LoyaltyTxn(customer_id=c.id, points=pts,
                              reason=f"adjust: {reason} ({current_user.username})"[:64]))
    db.session.commit()
    flash(f"{pts:+g} points for {c.name}. Balance now {c.loyalty_points:g}.", "success")
    return redirect(request.referrer or url_for("loyalty.index", card=c.card_no, summary=""))
