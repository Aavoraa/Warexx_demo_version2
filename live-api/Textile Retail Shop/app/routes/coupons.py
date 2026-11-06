"""CRM → Gift Coupon — the campaigns coupons are cut from, the codes, and what was spent.

Its own entry in the CRM menu (see app/modules.py): a coupon works differently
from a buy-X-get-Y scheme — a code a customer holds and presents, rather than a
rule the till applies by itself. Four tabs, as in the reference software:
Scheme (campaigns and issuing), Transaction History (what was redeemed),
Rewards to Coupon (loyalty points turned into coupons) and Settlement Coupon
Print (reprinting the coupon a bill earned).
"""
import hashlib
import secrets
from datetime import date, datetime

from flask import (Blueprint, current_app, flash, redirect, render_template, request, session,
                   url_for)
from flask_login import current_user, login_required
from sqlalchemy import func

from app import crm, db, messaging, vouchers
from app.models import (COUPON_KINDS, Coupon, CouponCampaign, CouponRedemption, Customer, Invoice,
                        LoyaltyTxn, ScheduledMessage)
from app.utils import role_required

coupons_bp = Blueprint("coupons", __name__)


def _float(name, default=0.0):
    try:
        v = request.form.get(name)
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


@coupons_bp.route("/")
@login_required
@role_required("admin", "manager")
def index():
    q = (request.args.get("q") or "").strip()
    coupons = Coupon.query
    if q:
        coupons = coupons.outerjoin(Customer, Customer.id == Coupon.customer_id).filter(
            db.or_(Coupon.code.ilike(f"%{q}%"), Customer.name.ilike(f"%{q}%"),
                   Customer.phone.ilike(f"%{q}%")))
    coupons = coupons.order_by(Coupon.id.desc()).limit(300).all()
    campaigns = CouponCampaign.query.order_by(CouponCampaign.active.desc(),
                                              CouponCampaign.id.desc()).all()
    spent = dict(db.session.query(CouponRedemption.coupon_id, func.sum(CouponRedemption.amount))
                 .filter(CouponRedemption.voided_at.is_(None))
                 .group_by(CouponRedemption.coupon_id).all())
    return render_template("coupons/index.html", coupons=coupons, campaigns=campaigns,
                           spent=spent, q=q, kinds=COUPON_KINDS, today=date.today())


@coupons_bp.route("/campaigns", methods=["POST"])
@login_required
@role_required("admin", "manager")
def create_campaign():
    name = (request.form.get("name") or "").strip()
    kind = request.form.get("kind") if request.form.get("kind") in COUPON_KINDS else "amount"
    value = _float("value")
    if not name or value <= 0 or (kind == "percent" and value > 100):
        flash("A campaign needs a name and a value (a percentage between 0 and 100).", "danger")
        return redirect(url_for("coupons.index"))
    uses = request.form.get("uses_per_coupon", type=int)
    c = CouponCampaign(
        name=name[:128], kind=kind, value=value, min_bill=_float("min_bill"),
        max_discount=_float("max_discount", None) if kind == "percent" else None,
        valid_days=max(1, request.form.get("valid_days", type=int) or 30),
        uses_per_coupon=uses if uses and uses > 0 else None,
        issue_at_settlement=bool(request.form.get("issue_at_settlement")),
        settlement_min_bill=_float("settlement_min_bill"),
        created_by_id=current_user.id)
    db.session.add(c)
    db.session.commit()
    flash(f"Campaign “{c.name}” created — {c.describe}.", "success")
    return redirect(url_for("coupons.index"))


@coupons_bp.route("/campaigns/<int:cid>/toggle", methods=["POST"])
@login_required
@role_required("admin", "manager")
def toggle_campaign(cid):
    c = CouponCampaign.query.get_or_404(cid)
    c.active = not c.active
    db.session.commit()
    flash(f"“{c.name}” is now {'active' if c.active else 'switched off'}.", "success")
    return redirect(url_for("coupons.index"))


@coupons_bp.route("/issue", methods=["POST"])
@login_required
@role_required("admin", "manager")
def issue():
    campaign = CouponCampaign.query.get_or_404(request.form.get("campaign_id", type=int))
    if campaign.name == REWARD_CAMPAIGN:
        flash("Loyalty reward coupons are bought with points — use Rewards to Coupon.", "warning")
        return redirect(url_for("coupons.rewards"))
    count = max(1, min(request.form.get("count", type=int) or 1, 500))
    phone = (request.form.get("customer") or "").strip()
    customer = None
    if phone:
        customer = (Customer.query.filter(db.or_(Customer.phone == phone,
                                                 Customer.name.ilike(phone))).first())
        if customer is None:
            flash(f"No customer matches “{phone}”.", "danger")
            return redirect(url_for("coupons.index"))
    code = (request.form.get("code") or "").strip().upper()
    if code and (count > 1 or Coupon.query.filter(func.upper(Coupon.code) == code).first()):
        flash("A chosen code must be new, and can only be given to one coupon.", "danger")
        return redirect(url_for("coupons.index"))
    made = [vouchers.issue(campaign, customer_id=customer.id if customer else None,
                           user_id=current_user.id, code=code or None)
            for _ in range(count)]
    db.session.commit()
    flash(f"{len(made)} coupon(s) issued: " + ", ".join(c.code for c in made[:10])
          + ("…" if len(made) > 10 else ""), "success")
    return redirect(url_for("coupons.index"))


@coupons_bp.route("/<int:cid>/withdraw", methods=["POST"])
@login_required
@role_required("admin", "manager")
def withdraw(cid):
    c = Coupon.query.get_or_404(cid)
    c.active = False
    db.session.commit()
    flash(f"{c.code} withdrawn.", "success")
    return redirect(url_for("coupons.index"))


# ---------------------------------------------------------------------------
#  Transaction History — every redemption, with the bill it came off
# ---------------------------------------------------------------------------
@coupons_bp.route("/history")
@login_required
@role_required("admin", "manager")
def history():
    a = request.args
    q = (db.session.query(CouponRedemption, Coupon, Invoice)
         .join(Coupon, Coupon.id == CouponRedemption.coupon_id)
         .join(Invoice, Invoice.id == CouponRedemption.invoice_id)
         .filter(CouponRedemption.voided_at.is_(None)))
    if a.get("campaign_id", type=int):
        q = q.filter(Coupon.campaign_id == a.get("campaign_id", type=int))
    code = (a.get("code") or "").strip().upper()
    if code:
        q = q.filter(func.upper(Coupon.code) == code)
    lo, hi = crm.day_range(crm.arg_date("from"), crm.arg_date("to"))
    if lo:
        q = q.filter(Invoice.invoice_date >= lo)
    if hi:
        q = q.filter(Invoice.invoice_date < hi)
    rows = q.order_by(Invoice.invoice_date.desc()).limit(1000).all()
    issued = Coupon.query
    if a.get("campaign_id", type=int):
        issued = issued.filter(Coupon.campaign_id == a.get("campaign_id", type=int))
    if code:
        issued = issued.filter(func.upper(Coupon.code) == code)
    return render_template("coupons/history.html", rows=rows,
                           campaigns=CouponCampaign.query.order_by(CouponCampaign.name).all(),
                           count=issued.count(), consumed=len({c.id for _, c, _ in rows}),
                           disc=round(sum(r.amount or 0 for r, _, _ in rows), 2))


# ---------------------------------------------------------------------------
#  Rewards to Coupon — loyalty points turned into coupons
# ---------------------------------------------------------------------------
#: The campaign every points-bought coupon hangs off. Its own value is nought:
#: each coupon carries the rupees its points were worth (Coupon.value).
REWARD_CAMPAIGN = "Loyalty reward"
#: How long a reward coupon is good for.
REWARD_VALID_DAYS = 90
#: A PIN sent to the customer's mobile is good for this long.
PIN_MINUTES = 10


def _reward_campaign():
    c = CouponCampaign.query.filter_by(name=REWARD_CAMPAIGN).first()
    if c is None:
        c = CouponCampaign(name=REWARD_CAMPAIGN, kind="amount", value=0, min_bill=0,
                           valid_days=REWARD_VALID_DAYS, uses_per_coupon=1, active=True)
        db.session.add(c)
        db.session.flush()
    return c


def _pin_hash(customer_id, pin):
    key = current_app.config["SECRET_KEY"]
    return hashlib.sha256(f"{key}:{customer_id}:{pin}".encode()).hexdigest()


def _pin_needed():
    """A PIN only means something when it can reach the customer's phone."""
    url, _ = messaging.provider()
    return bool(url)


@coupons_bp.route("/rewards", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def rewards():
    point_value = current_app.config["LOYALTY_POINT_VALUE"]
    if request.method == "POST":
        c = db.session.get(Customer, request.form.get("customer_id", type=int) or 0)
        if c is None:
            flash("Find the customer first.", "danger")
            return redirect(url_for("coupons.rewards"))
        back = redirect(url_for("coupons.rewards", mobile=c.phone or c.card_no))
        if c.loyalty_disabled:
            flash(f"Loyalty is switched off for {c.name}.", "danger")
            return back
        mode = "multiple" if request.form.get("mode") == "multiple" else "single"
        value = crm.form_float("value", 0) or 0
        count = max(1, min(request.form.get("count", type=int) or 1, 50)) if mode == "multiple" else 1
        if value <= 0:
            flash("Enter the coupon value.", "danger")
            return back
        points = round(value * count / point_value, 2)
        if points > (c.loyalty_points or 0) + 0.001:
            flash(f"{count} × ₹{value:g} needs {points:g} points; {c.name} has {c.loyalty_points:g}.", "danger")
            return back
        if _pin_needed():
            pin = (request.form.get("pin") or "").strip()
            sent = session.get("reward_pin") or {}
            fresh = sent.get("customer_id") == c.id and \
                (datetime.utcnow().timestamp() - sent.get("at", 0)) < PIN_MINUTES * 60
            if not pin or not fresh or _pin_hash(c.id, pin) != sent.get("hash"):
                flash("The PIN does not match the one sent to the customer's mobile "
                      f"(it is good for {PIN_MINUTES} minutes).", "danger")
                return back
            session.pop("reward_pin", None)
        campaign = _reward_campaign()
        made = [vouchers.issue(campaign, customer_id=c.id, via="loyalty", user_id=current_user.id,
                               value=round(value, 2)) for _ in range(count)]
        c.loyalty_points = round((c.loyalty_points or 0) - points, 2)
        db.session.add(LoyaltyTxn(customer_id=c.id, points=-points,
                                  reason=f"coupon {made[0].code}" + (f" +{count - 1}" if count > 1 else "")))
        db.session.commit()
        flash(f"{count} coupon(s) of ₹{value:g} made for {c.name} from {points:g} points: "
              + ", ".join(x.code for x in made), "success")
        return redirect(url_for("coupons.settlement_print", code=made[0].code))

    q = (request.args.get("mobile") or request.args.get("card") or "").strip()
    c = crm.find_customer(q) if q else None
    return render_template("coupons/rewards.html", c=c, q=q, point_value=point_value,
                           pin_needed=_pin_needed(),
                           max_value=round((c.loyalty_points or 0) * point_value, 2) if c else 0)


@coupons_bp.route("/rewards/pin", methods=["POST"])
@login_required
@role_required("admin", "manager")
def reward_pin():
    c = Customer.query.get_or_404(request.form.get("customer_id", type=int))
    if not c.phone:
        flash(f"{c.name} has no mobile number to send a PIN to.", "danger")
        return redirect(url_for("coupons.rewards", card=c.card_no))
    pin = f"{secrets.randbelow(10000):04d}"
    session["reward_pin"] = {"customer_id": c.id, "hash": _pin_hash(c.id, pin),
                             "at": datetime.utcnow().timestamp()}
    db.session.add(ScheduledMessage(customer_id=c.id, phone=c.phone, kind="custom",
                                    body=f"Your PIN to turn loyalty points into a coupon at "
                                         f"{current_app.config.get('SHOP_NAME', 'our store')} is {pin}. "
                                         "Do not share it with anyone but the cashier.",
                                    created_by_id=current_user.id))
    db.session.flush()
    counts = messaging.send_due()
    db.session.commit()
    flash(f"PIN sent to {c.phone}." if counts.get("sent") else
          "The PIN could not be sent — check the SMS gateway under Customers → Messages.",
          "success" if counts.get("sent") else "warning")
    return redirect(url_for("coupons.rewards", mobile=c.phone))


# ---------------------------------------------------------------------------
#  Settlement Coupon Print — the coupon a bill earned, printed again
# ---------------------------------------------------------------------------
@coupons_bp.route("/print")
@login_required
@role_required("admin", "manager")
def settlement_print():
    code = (request.args.get("code") or "").strip()
    found = []
    if code:
        cp = vouchers.find_coupon(code)
        if cp:
            found = [cp]
        else:                                   # a bill number: every coupon it earned
            inv = Invoice.query.filter(func.upper(Invoice.invoice_number) == code.upper()).first()
            if inv:
                found = Coupon.query.filter_by(issued_invoice_id=inv.id).all()
    location_id = request.args.get("location_id", type=int)
    if location_id:
        found = [c for c in found if not c.issued_invoice or c.issued_invoice.location_id == location_id]
    return render_template("coupons/print.html", code=code, found=found, today=date.today(),
                           locations=crm.locations())
