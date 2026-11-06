"""CRM → CRM Analyser — who buys, how often, how much, and who has stopped coming.

Every figure is an aggregate in SQL: the customer book can hold 150K people and
the bills several times that, so nothing here walks either table row by row.
Only the per-customer spend for the chosen window comes back to Python, to be
put into bands.
"""
from datetime import date, timedelta

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import func

from app import crm, db, messaging
from app.models import Customer, Invoice
from app.utils import role_required

analyser_bp = Blueprint("analyser", __name__)

SPEND_BANDS = ((0, 1000, "Under ₹1K"), (1000, 5000, "₹1K–5K"), (5000, 10000, "₹5K–10K"),
               (10000, 25000, "₹10K–25K"), (25000, None, "₹25K+"))
VISIT_BANDS = ((1, 1, "1 visit"), (2, 2, "2"), (3, 5, "3–5"), (6, None, "6+"))
#: Not seen for this long, after buying before, is lapsed.
LAPSED_DAYS = 90


def _bills(lo, hi, location_id):
    q = [Invoice.live(), Invoice.invoice_date >= lo, Invoice.invoice_date < hi]
    if location_id:
        q.append(Invoice.location_id == location_id)
    return q


def _band(v, bands, upper_inclusive):
    """The label of the band `v` falls in. Rupee bands stop short of their upper
    bound (₹1,000 is in ₹1K–5K); visit bands include it (2 visits is "2")."""
    for lo, hi, label in bands:
        if v >= lo and (hi is None or (v <= hi if upper_inclusive else v < hi)):
            return label
    return bands[-1][2]


@analyser_bp.route("/")
@login_required
@role_required("admin", "manager")
def index():
    today = date.today()
    d_from = crm.arg_date("from") or today - timedelta(days=89)
    d_to = crm.arg_date("to") or today
    location_id = request.args.get("location_id", type=int)
    lo, hi = crm.day_range(d_from, d_to)
    cond = _bills(lo, hi, location_id)

    # ---- the window's bills, with and without a customer on them ----
    total_sales, total_bills = db.session.query(func.coalesce(func.sum(Invoice.total), 0),
                                                func.count(Invoice.id)).filter(*cond).one()
    cust_sales, cust_bills = (db.session.query(func.coalesce(func.sum(Invoice.total), 0),
                                               func.count(Invoice.id))
                              .filter(*cond, Invoice.customer_id.isnot(None)).one())
    per = (db.session.query(Invoice.customer_id, func.sum(Invoice.total), func.count(Invoice.id))
           .filter(*cond, Invoice.customer_id.isnot(None)).group_by(Invoice.customer_id).all())
    buyers = len(per)
    repeat = sum(1 for _, _, n in per if n >= 2)
    spend_bands = {b[2]: 0 for b in SPEND_BANDS}
    visit_bands = {b[2]: 0 for b in VISIT_BANDS}
    for _, total, n in per:
        spend_bands[_band(float(total or 0), SPEND_BANDS, False)] += 1
        visit_bands[_band(int(n), VISIT_BANDS, True)] += 1

    # ---- who joined, and who has gone quiet ----
    customers = Customer.query.filter(crm.active_customers()).count()
    new = Customer.query.filter(Customer.created_at >= lo, Customer.created_at < hi).count()
    first_bills = (db.session.query(Invoice.customer_id, func.min(Invoice.invoice_date).label("first"))
                   .filter(Invoice.live(), Invoice.customer_id.isnot(None))
                   .group_by(Invoice.customer_id).subquery())
    first_timers = (db.session.query(func.count()).select_from(first_bills)
                    .filter(first_bills.c.first >= lo, first_bills.c.first < hi).scalar() or 0)
    last_bills = (db.session.query(Invoice.customer_id.label("cid"), func.max(Invoice.invoice_date).label("last"),
                                   func.sum(Invoice.total).label("spent"), func.count(Invoice.id).label("bills"))
                  .filter(Invoice.live(), Invoice.customer_id.isnot(None))
                  .group_by(Invoice.customer_id).subquery())
    lapsed_since = crm.day_range(today - timedelta(days=LAPSED_DAYS), None)[0]
    lapsed = (db.session.query(func.count()).select_from(last_bills)
              .filter(last_bills.c.last < lapsed_since).scalar() or 0)
    at_risk = (db.session.query(Customer, last_bills.c.last, last_bills.c.spent, last_bills.c.bills)
               .join(last_bills, last_bills.c.cid == Customer.id)
               .filter(last_bills.c.last < lapsed_since, crm.active_customers())
               .order_by(last_bills.c.spent.desc()).limit(15).all())

    # ---- the best customers in the window ----
    top = (db.session.query(Customer, func.sum(Invoice.total).label("spent"),
                            func.count(Invoice.id).label("bills"), func.max(Invoice.invoice_date))
           .join(Invoice, Invoice.customer_id == Customer.id).filter(*cond)
           .group_by(Customer.id).order_by(func.sum(Invoice.total).desc()).limit(15).all())

    # ---- by who they are ----
    def split(col, limit=8):
        rows = (db.session.query(func.coalesce(col, ""), func.count(func.distinct(Invoice.customer_id)),
                                 func.sum(Invoice.total))
                .join(Invoice, Invoice.customer_id == Customer.id).filter(*cond)
                .group_by(func.coalesce(col, "")).order_by(func.sum(Invoice.total).desc()).all())
        out = [(k or "Not recorded", n, float(v or 0)) for k, n, v in rows]
        if len(out) > limit:
            rest = out[limit:]
            out = out[:limit] + [("Others", sum(r[1] for r in rest), sum(r[2] for r in rest))]
        return out

    # ---- twelve months of new customers and buyers ----
    months = []
    m = date(today.year, today.month, 1)
    for _ in range(12):
        nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        mlo, mhi = crm.day_range(m, nxt - timedelta(days=1))
        months.append((m.strftime("%b %y"),
                       Customer.query.filter(Customer.created_at >= mlo, Customer.created_at < mhi).count(),
                       db.session.query(func.count(func.distinct(Invoice.customer_id)))
                       .filter(*_bills(mlo, mhi, location_id), Invoice.customer_id.isnot(None)).scalar() or 0))
        m = date(m.year - (m.month == 1), (m.month - 2) % 12 + 1, 1)
    months.reverse()

    return render_template(
        "crm/analyser.html", d_from=d_from, d_to=d_to, location_id=location_id,
        locations=crm.locations(), customers=customers, new=new, first_timers=first_timers,
        buyers=buyers, repeat=repeat, lapsed=lapsed, lapsed_days=LAPSED_DAYS,
        total_sales=float(total_sales or 0), total_bills=total_bills,
        cust_sales=float(cust_sales or 0), cust_bills=cust_bills,
        spend_bands=spend_bands, visit_bands=visit_bands, top=top, at_risk=at_risk,
        by_category=split(Customer.category), by_city=split(Customer.city),
        by_gender=split(Customer.gender, 4), months=months,
        birthdays=messaging.due_wishes("birthday", 7), anniversaries=messaging.due_wishes("anniversary", 7),
        lapsed_from=(today - timedelta(days=LAPSED_DAYS)).isoformat(), today=today)
