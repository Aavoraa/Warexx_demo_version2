"""CRM → Gift Voucher — sell vouchers, look one up, and see what was sold.

A gift voucher is money paid in advance by one person for another to spend. It
is sold here (one sale = one payment, any number of vouchers by denomination)
and spent at Settlement as the Gift voucher tender; what is left on each is
worked out from the bills that spent it (app/vouchers.py), never stored.
"""
import secrets
from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from app import crm, db, vouchers
from app.models import (GIFT_VOUCHER_DENOMINATIONS, GIFT_VOUCHER_MODES, GIFT_VOUCHER_PAY_METHODS,
                        Company, Customer, GiftVoucher, GiftVoucherBatch)
from app.utils import generate_number, role_required

giftvouchers_bp = Blueprint("giftvouchers", __name__)

#: How long a voucher is good for when the screen is not told otherwise.
DEFAULT_VALID_DAYS = 365
#: One sale cannot cut more than this many vouchers — a typo of 500 for 5 is
#: half a thousand codes nobody asked for.
MAX_PER_SALE = 200


def _new_code():
    while True:
        code = "GV" + secrets.token_hex(4).upper()
        if not GiftVoucher.query.filter_by(code=code).first():
            return code


def _companies():
    return Company.query.filter(or_(Company.active.is_(None), Company.active.is_(True))) \
        .order_by(Company.name).all()


def _wanted():
    """[(value, count)] from the denomination grid and the free-value row."""
    out = []
    for d in GIFT_VOUCHER_DENOMINATIONS:
        n = request.form.get(f"d{d}", type=int) or 0
        if n < 0:
            raise ValueError("A denomination count cannot be negative.")
        if n:
            out.append((float(d), n))
    other_value = crm.form_float("other_value", 0) or 0
    other_count = request.form.get("other_count", type=int) or 0
    if other_value > 0 and other_count > 0:
        out.append((round(other_value, 2), other_count))
    return out


@giftvouchers_bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    company, location, _floor, counter = crm.here()
    if request.method == "POST":
        try:
            batch = _issue(company, location, counter)
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "danger")
            return redirect(url_for("giftvouchers.index"))
        flash(f"{len(batch.vouchers)} gift voucher{'s' if len(batch.vouchers) != 1 else ''} "
              f"worth ₹{batch.face_value:,.2f} issued as {batch.number}.", "success")
        return redirect(url_for("giftvouchers.batch", bid=batch.id))
    today = date.today()
    return render_template("crm/vouchers/issue.html", companies=_companies(), company=company,
                           location=location, counter=counter, modes=GIFT_VOUCHER_MODES,
                           methods=GIFT_VOUCHER_PAY_METHODS, staff=crm.staff(),
                           denominations=GIFT_VOUCHER_DENOMINATIONS, today=today,
                           valid_to=today + timedelta(days=DEFAULT_VALID_DAYS - 1))


def _issue(company, location, counter):
    mode = request.form.get("mode") if request.form.get("mode") in GIFT_VOUCHER_MODES else "direct"
    wanted = _wanted()
    count = sum(n for _, n in wanted)
    face = round(sum(v * n for v, n in wanted), 2)
    if not count:
        raise ValueError("Enter how many vouchers of each value to issue.")
    if count > MAX_PER_SALE:
        raise ValueError(f"{count} vouchers in one sale is more than the {MAX_PER_SALE} allowed — "
                         "check the counts.")
    customer = crm.customer_from_form("customer", required=(mode == "direct"))
    method, amount = None, 0.0
    if mode == "direct":
        method = request.form.get("method")
        if method not in GIFT_VOUCHER_PAY_METHODS:
            raise ValueError("Say how the vouchers were paid for.")
        amount = round(crm.form_float("amount", face) or 0, 2)
        if abs(amount - face) > 0.01:
            raise ValueError(f"The vouchers come to ₹{face:,.2f} but ₹{amount:,.2f} was entered as "
                             "received. A sold voucher is worth what was paid for it.")
    valid_from = crm.parse_date(request.form.get("valid_from")) or date.today()
    valid_to = crm.parse_date(request.form.get("valid_to"))
    if valid_to and valid_to < valid_from:
        raise ValueError("The voucher would expire before it starts.")
    company_id = request.form.get("company_id", type=int) or (company.id if company else None)
    batch = GiftVoucherBatch(
        number=generate_number("GVS", GiftVoucherBatch, "number"), mode=mode,
        customer_id=customer.id if customer else None,
        issued_on=crm.parse_date(request.form.get("issued_on")) or date.today(),
        valid_from=valid_from, valid_to=valid_to, method=method, amount=amount,
        bank=crm.text("bank", 64) if method in ("card", "bank") else None,
        reference=crm.text("reference", 64), remarks=crm.text("remarks", 256),
        issued_by_id=request.form.get("issued_by_id", type=int) or current_user.id,
        received_by=crm.text("received_by", 128), company_id=company_id,
        location_id=location.id if location else None, counter_id=counter.id if counter else None,
        created_by_id=current_user.id)
    db.session.add(batch)
    db.session.flush()
    given_to, given_phone = crm.text("given_to", 128), crm.text("given_to_phone", 32)
    for value, n in wanted:
        for _ in range(n):
            db.session.add(GiftVoucher(code=_new_code(), batch_id=batch.id, value=value,
                                       given_to=given_to, given_to_phone=given_phone))
    db.session.commit()
    return batch


@giftvouchers_bp.route("/batch/<int:bid>")
@login_required
def batch(bid):
    b = GiftVoucherBatch.query.get_or_404(bid)
    rows = [(v, vouchers.gift_voucher_balance(v)) for v in b.vouchers]
    return render_template("crm/vouchers/batch.html", b=b, rows=rows)


@giftvouchers_bp.route("/history")
@login_required
def history():
    """Transaction history — one voucher, a sale, or everything a mobile number holds."""
    q = (request.args.get("q") or "").strip()
    rows = []
    if q:
        query = (GiftVoucher.query.join(GiftVoucherBatch, GiftVoucherBatch.id == GiftVoucher.batch_id)
                 .outerjoin(Customer, Customer.id == GiftVoucherBatch.customer_id)
                 .filter(or_(func.upper(GiftVoucher.code) == q.upper(),
                             func.upper(GiftVoucherBatch.number) == q.upper(),
                             Customer.phone == q, GiftVoucher.given_to_phone == q)))
        for v in query.order_by(GiftVoucher.id.desc()).limit(300).all():
            used = vouchers.gift_voucher_used(v)
            rows.append((v, used, round(max(0.0, v.value - used), 2),
                         vouchers.gift_voucher_redemptions(v), _state(v, used)))
    return render_template("crm/vouchers/history.html", q=q, rows=rows)


def _state(v, used):
    b = v.batch
    if not v.active:
        return "withdrawn"
    if used >= (v.value or 0) - 0.01:
        return "spent"
    if b.valid_to and b.valid_to < date.today():
        return "expired"
    return "part-spent" if used > 0 else "valid"


@giftvouchers_bp.route("/search")
@login_required
def search():
    a = request.args
    q = GiftVoucherBatch.query
    if a.get("company_id", type=int):
        q = q.filter(GiftVoucherBatch.company_id == a.get("company_id", type=int))
    if a.get("location_id", type=int):
        q = q.filter(GiftVoucherBatch.location_id == a.get("location_id", type=int))
    d_from, d_to = crm.arg_date("from"), crm.arg_date("to")
    if d_from:
        q = q.filter(GiftVoucherBatch.issued_on >= d_from)
    if d_to:
        q = q.filter(GiftVoucherBatch.issued_on <= d_to)
    batches = q.order_by(GiftVoucherBatch.issued_on.desc(), GiftVoucherBatch.id.desc()).limit(500).all()
    sold = round(sum(b.amount or 0 for b in batches), 2)
    face = round(sum(b.face_value for b in batches), 2)
    return render_template("crm/vouchers/search.html", batches=batches, sold=sold, face=face,
                           companies=_companies(), locations=crm.locations())


@giftvouchers_bp.route("/<int:vid>/withdraw", methods=["POST"])
@login_required
@role_required("admin", "manager")
def withdraw(vid):
    v = GiftVoucher.query.get_or_404(vid)
    v.active = False
    db.session.commit()
    flash(f"{v.code} cancelled — it can no longer be spent. "
          f"₹{vouchers.gift_voucher_balance(v):,.2f} was left on it.", "success")
    return redirect(request.referrer or url_for("giftvouchers.history", q=v.code))
