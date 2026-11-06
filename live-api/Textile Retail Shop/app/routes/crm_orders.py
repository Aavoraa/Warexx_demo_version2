"""CRM → Customer Orders — orders taken today for goods handed over later.

An order is a promise, not a sale: no stock moves and no tax is charged until it
is billed at the counter. Money paid against it is a CustomerAdvance carrying the
order's id, so the till already knows how to spend it (Advance tender), the
drawer already counts it, and a refund already works.
"""
import csv
import io
from datetime import date

from flask import Blueprint, Response, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from app import crm, db
from app.models import (ORDER_STATUSES, PAYMENT_METHODS, Customer, CustomerAdvance, CustomerOrder,
                        CustomerOrderLine, Invoice)
from app.paging import paged
from app.utils import generate_number

orders_bp = Blueprint("orders", __name__)

#: What the status filter offers beyond the statuses themselves.
OPEN = ("new", "confirmed", "in_progress", "ready")


def _filtered():
    status = request.args.get("status", "open")
    q = (request.args.get("q") or "").strip()
    d_from, d_to = crm.arg_date("from"), crm.arg_date("to")
    query = CustomerOrder.query.join(Customer, Customer.id == CustomerOrder.customer_id)
    if status == "open":
        query = query.filter(CustomerOrder.status.in_(OPEN))
    elif status == "due":
        query = query.filter(CustomerOrder.status.in_(OPEN),
                             CustomerOrder.delivery_date <= date.today())
    elif status in ORDER_STATUSES:
        query = query.filter(CustomerOrder.status == status)
    if q:
        query = query.filter(or_(CustomerOrder.number.ilike(f"%{q}%"), Customer.name.ilike(f"%{q}%"),
                                 Customer.phone.ilike(f"%{q}%"), CustomerOrder.contact_no.ilike(f"%{q}%")))
    if d_from:
        query = query.filter(CustomerOrder.order_date >= d_from)
    if d_to:
        query = query.filter(CustomerOrder.order_date <= d_to)
    return query, status, q, d_from, d_to


@orders_bp.route("/")
@login_required
def index():
    query, status, q, d_from, d_to = _filtered()
    page = paged(query.order_by(CustomerOrder.order_date.desc(), CustomerOrder.id.desc()))
    counts = dict(db.session.query(CustomerOrder.status, db.func.count(CustomerOrder.id))
                  .group_by(CustomerOrder.status).all())
    due = CustomerOrder.query.filter(CustomerOrder.status.in_(OPEN),
                                     CustomerOrder.delivery_date <= date.today()).count()
    return render_template("crm/orders/list.html", orders=page.items, page=page, status=status,
                           q=q, d_from=d_from, d_to=d_to, statuses=ORDER_STATUSES,
                           counts=counts, due=due, today=date.today())


@orders_bp.route("/export.csv")
@login_required
def export():
    query, *_ = _filtered()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Order no", "Order date", "Delivery date", "Customer", "Contact no", "City",
                "Category", "Received by", "Total qty", "Total amount", "Paid", "Status"])
    for o in query.order_by(CustomerOrder.order_date, CustomerOrder.id).limit(20000):
        w.writerow([o.number, o.order_date.strftime("%d-%m-%Y") if o.order_date else "",
                    o.delivery_date.strftime("%d-%m-%Y") if o.delivery_date else "",
                    o.customer.name, o.contact_no or o.customer.phone or "", o.city or "",
                    o.category or "", o.received_by.full_name if o.received_by else "",
                    o.total_qty, o.total_amount, o.paid_amount, o.status])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=customer-orders.csv"})


@orders_bp.route("/api/customer")
@login_required
def api_customer():
    """A customer by mobile, card or code — for every CRM form's mobile box."""
    c = crm.find_customer(request.args.get("q"))
    return jsonify(crm.customer_json(c) if c else {})


def _lines_from_form(order):
    f = request.form
    descs = f.getlist("line_description")
    order.lines.clear()
    for i, desc in enumerate(descs):
        desc = (desc or "").strip()
        if not desc:
            continue

        def at(name, i=i):
            vals = f.getlist(name)
            return (vals[i] if i < len(vals) else "").strip()

        try:
            qty = float(at("line_qty") or 1)
            rate = float(at("line_rate") or 0)
        except ValueError:
            raise ValueError(f"Line “{desc}”: quantity and rate must be numbers.")
        if qty <= 0 or rate < 0:
            raise ValueError(f"Line “{desc}”: the quantity must be more than 0.")
        order.lines.append(CustomerOrderLine(description=desc[:256], design_no=at("line_design")[:64] or None,
                                             size=at("line_size")[:32] or None,
                                             color=at("line_color")[:32] or None,
                                             qty=qty, rate=rate))
    if not order.lines:
        raise ValueError("Add at least one item to the order.")


def _fill(order):
    order.order_date = crm.parse_date(request.form.get("order_date")) or date.today()
    order.delivery_date = crm.parse_date(request.form.get("delivery_date"))
    if order.delivery_date and order.delivery_date < order.order_date:
        raise ValueError("The delivery date is before the order date.")
    order.contact_no = crm.text("contact_no", 32) or order.customer.phone
    order.city = crm.text("city", 64) or order.customer.city
    order.category = crm.text("category", 64) or order.customer.category
    order.received_by_id = request.form.get("received_by_id", type=int) or current_user.id
    order.notes = crm.text("notes", 512)
    _lines_from_form(order)


def _take_payment(order, amount, method, reference=None):
    _company, location, _floor, counter = crm.here()
    adv = CustomerAdvance(number=generate_number("ADV", CustomerAdvance, "number"),
                          customer_id=order.customer_id, amount=round(amount, 2),
                          method=method if method in PAYMENT_METHODS else "cash",
                          reference=reference, note=f"Against order {order.number}",
                          cashier_id=current_user.id, order_id=order.id,
                          company_id=_company.id if _company else None,
                          location_id=location.id if location else None,
                          counter_id=counter.id if counter else None)
    db.session.add(adv)
    return adv


def _form(order, customer=None):
    if request.method == "POST":           # sent back after a refusal: keep what was typed
        f = request.form
        lines = [{"description": d, "design_no": dn, "size": s, "color": c, "qty": q, "rate": r}
                 for d, dn, s, c, q, r in zip(f.getlist("line_description"), f.getlist("line_design"),
                                              f.getlist("line_size"), f.getlist("line_color"),
                                              f.getlist("line_qty"), f.getlist("line_rate")) if d.strip()]
    elif order:
        lines = [{"description": l.description, "design_no": l.design_no or "", "size": l.size or "",
                  "color": l.color or "", "qty": l.qty, "rate": l.rate} for l in order.lines]
    else:
        lines = []
    return render_template("crm/orders/form.html", order=order, customer=customer, lines=lines,
                           staff=crm.staff(), methods=PAYMENT_METHODS, posted=request.form,
                           categories=crm.customer_categories(), today=date.today())


@orders_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        try:
            customer = crm.customer_from_form("customer")
            _company, location, _f, _c = crm.here()
            order = CustomerOrder(number=generate_number("ORD", CustomerOrder, "number"),
                                  customer_id=customer.id, customer=customer, status="new",
                                  created_by_id=current_user.id,
                                  location_id=location.id if location else None)
            _fill(order)
            db.session.add(order)
            db.session.flush()
            paid = crm.form_float("advance", 0) or 0
            if paid < 0:
                raise ValueError("The advance cannot be negative.")
            adv = _take_payment(order, paid, request.form.get("method"),
                                crm.text("reference", 64)) if paid > 0 else None
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "danger")
            return _form(None, None), 400
        flash(f"Order {order.number} taken for {order.customer.name}"
              + (f", with ₹{adv.amount:,.2f} paid as advance {adv.number}." if adv else "."), "success")
        return redirect(url_for("orders.view", oid=order.id))
    cust = db.session.get(Customer, request.args.get("customer_id", type=int) or 0)
    return _form(None, cust)


@orders_bp.route("/<int:oid>")
@login_required
def view(oid):
    o = CustomerOrder.query.get_or_404(oid)
    return render_template("crm/orders/view.html", o=o, statuses=ORDER_STATUSES,
                           methods=PAYMENT_METHODS)


@orders_bp.route("/<int:oid>/print")
@login_required
def print_slip(oid):
    o = CustomerOrder.query.get_or_404(oid)
    return render_template("crm/orders/print.html", o=o)


@orders_bp.route("/<int:oid>/edit", methods=["GET", "POST"])
@login_required
def edit(oid):
    o = CustomerOrder.query.get_or_404(oid)
    if o.status in ("delivered", "cancelled"):
        flash(f"{o.number} is {o.status} and can no longer be changed.", "warning")
        return redirect(url_for("orders.view", oid=oid))
    if request.method == "POST":
        try:
            _fill(o)
            db.session.commit()
        except ValueError as e:
            db.session.rollback()
            flash(str(e), "danger")
            return redirect(url_for("orders.edit", oid=oid))
        flash(f"{o.number} updated.", "success")
        return redirect(url_for("orders.view", oid=oid))
    return _form(o, o.customer)


@orders_bp.route("/<int:oid>/status", methods=["POST"])
@login_required
def set_status(oid):
    o = CustomerOrder.query.get_or_404(oid)
    status = request.form.get("status")
    if status not in ORDER_STATUSES:
        flash("Pick a status.", "danger")
    elif o.status in ("delivered", "cancelled"):
        flash(f"{o.number} is already {o.status}.", "warning")
    else:
        o.status = status
        bill = (request.form.get("invoice") or "").strip()
        if status == "delivered" and bill:
            inv = Invoice.query.filter_by(invoice_number=bill).first()
            if inv is None:
                flash(f"No bill numbered {bill} — the order was not marked delivered.", "danger")
                return redirect(url_for("orders.view", oid=oid))
            o.invoice_id = inv.id
        db.session.commit()
        msg = f"{o.number} is now {status.replace('_', ' ')}."
        if status == "cancelled" and o.paid_amount > 0:
            msg += (f" ₹{o.paid_amount:,.2f} paid against it is still the customer's advance — "
                    "spend it on a bill or refund it from Customers → Advances.")
        flash(msg, "success")
    return redirect(request.form.get("next") or url_for("orders.view", oid=oid))


@orders_bp.route("/bulk-status", methods=["POST"])
@login_required
def bulk_status():
    status = request.form.get("status")
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    if status not in ORDER_STATUSES or not ids:
        flash("Tick the orders and pick a status.", "danger")
        return redirect(request.referrer or url_for("orders.index"))
    n = 0
    for o in CustomerOrder.query.filter(CustomerOrder.id.in_(ids)).all():
        if o.status not in ("delivered", "cancelled"):
            o.status = status
            n += 1
    db.session.commit()
    flash(f"{n} order{'s' if n != 1 else ''} marked {status.replace('_', ' ')}.", "success")
    return redirect(request.referrer or url_for("orders.index"))


@orders_bp.route("/<int:oid>/pay", methods=["POST"])
@login_required
def pay(oid):
    o = CustomerOrder.query.get_or_404(oid)
    amount = crm.form_float("amount", 0) or 0
    if amount <= 0:
        flash("Enter the amount the customer is paying.", "danger")
        return redirect(url_for("orders.view", oid=oid))
    if o.status == "cancelled":
        flash(f"{o.number} is cancelled.", "danger")
        return redirect(url_for("orders.view", oid=oid))
    adv = _take_payment(o, amount, request.form.get("method"), crm.text("reference", 64))
    db.session.commit()
    flash(f"₹{adv.amount:,.2f} taken against {o.number} as advance {adv.number}. "
          "It is spent at the counter under Advance when the order is billed.", "success")
    return redirect(url_for("customers.advance_receipt", aid=adv.id))
