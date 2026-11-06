"""CRM → Price List — price categories, the designs priced on them, and who buys on them.

Three panes, as in the reference software: Category, Design, Customers. A
category is a list a customer can be put on ("Wholesale", "10% DIS"). A design
priced on it sells at that price; any other item at the category's discount.
The Billing Counter charges it as soon as the customer is attached
(pos.api_product / pos.api_list_prices, through crm.list_prices).
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from app import crm, db
from app.models import Customer, PriceCategory, PriceListItem, Product
from app.utils import role_required

pricelist_bp = Blueprint("pricelist", __name__)


@pricelist_bp.route("/")
@login_required
@role_required("admin", "manager")
def index():
    cats = PriceCategory.query.order_by(PriceCategory.active.desc(), PriceCategory.name).all()
    cat = db.session.get(PriceCategory, request.args.get("cat", type=int) or 0) or (cats[0] if cats else None)
    dq = (request.args.get("dq") or "").strip()
    cq = (request.args.get("cq") or "").strip()
    items, customers, designs = [], [], {}
    counts = dict(db.session.query(Customer.price_category_id, func.count(Customer.id))
                  .filter(Customer.price_category_id.isnot(None))
                  .group_by(Customer.price_category_id).all())
    if cat:
        iq = PriceListItem.query.filter_by(category_id=cat.id)
        if dq:
            iq = iq.filter(PriceListItem.design_no.ilike(f"%{dq}%"))
        items = iq.order_by(PriceListItem.design_no).limit(1000).all()
        # what each design is, and what it ordinarily sells for
        nos = [i.design_no for i in items]
        if nos:
            for no, name, price in (db.session.query(Product.design_no, func.min(Product.name),
                                                     func.max(Product.selling_price))
                                    .filter(Product.design_no.in_(nos))
                                    .group_by(Product.design_no).all()):
                designs[no] = (name, price)
        cust = Customer.query.filter_by(price_category_id=cat.id)
        if cq:
            cust = cust.filter(db.or_(Customer.name.ilike(f"%{cq}%"), Customer.phone.ilike(f"%{cq}%")))
        customers = cust.order_by(Customer.name).limit(500).all()
    return render_template("crm/pricelist.html", cats=cats, cat=cat, items=items, designs=designs,
                           customers=customers, counts=counts, dq=dq, cq=cq)


@pricelist_bp.route("/designs")
@login_required
@role_required("admin", "manager")
def design_search():
    """Design numbers in the catalogue, for the "Create design" box."""
    q = (request.args.get("q") or "").strip()
    rows = []
    if len(q) >= 2:
        rows = (db.session.query(Product.design_no, func.min(Product.name), func.max(Product.selling_price))
                .filter(Product.design_no.ilike(f"%{q}%")).group_by(Product.design_no)
                .order_by(Product.design_no).limit(20).all())
    return {"designs": [{"design_no": d, "name": n, "price": p} for d, n, p in rows]}


@pricelist_bp.route("/category", methods=["POST"])
@login_required
@role_required("admin", "manager")
def save_category():
    cid = request.form.get("id", type=int)
    name = crm.text("name", 64)
    if not name:
        flash("Give the price category a name.", "danger")
        return redirect(url_for("pricelist.index", cat=cid))
    pct = crm.form_float("discount_pct", 0) or 0
    if not 0 <= pct <= 100:
        flash("The discount is a percentage between 0 and 100.", "danger")
        return redirect(url_for("pricelist.index", cat=cid))
    clash = PriceCategory.query.filter(func.lower(PriceCategory.name) == name.lower(),
                                       PriceCategory.id != (cid or 0)).first()
    if clash:
        flash(f"There is already a price category called {clash.name}.", "danger")
        return redirect(url_for("pricelist.index", cat=clash.id))
    cat = db.session.get(PriceCategory, cid) if cid else None
    if cat is None:
        cat = PriceCategory(active=True)
        db.session.add(cat)
    cat.name, cat.discount_pct = name, pct
    db.session.commit()
    flash(f"Price category {cat.name} saved.", "success")
    return redirect(url_for("pricelist.index", cat=cat.id))


@pricelist_bp.route("/category/<int:cid>/toggle", methods=["POST"])
@login_required
@role_required("admin", "manager")
def toggle_category(cid):
    cat = PriceCategory.query.get_or_404(cid)
    cat.active = not cat.active
    db.session.commit()
    flash(f"{cat.name} is now {'in use' if cat.active else 'switched off — its customers pay ordinary prices'}.",
          "success")
    return redirect(url_for("pricelist.index", cat=cid))


@pricelist_bp.route("/<int:cid>/prices", methods=["POST"])
@login_required
@role_required("admin", "manager")
def save_prices(cid):
    """Save Prices — every edited price in the Design pane, and any design added."""
    cat = PriceCategory.query.get_or_404(cid)
    changed = removed = 0
    for item in PriceListItem.query.filter_by(category_id=cat.id).all():
        if request.form.get(f"del_{item.id}"):
            db.session.delete(item)
            removed += 1
            continue
        raw = request.form.get(f"price_{item.id}")
        if raw is None:
            continue
        try:
            price = round(float(raw), 2)
        except ValueError:
            continue
        if price >= 0 and price != item.price:
            item.price = price
            changed += 1
    new_no = crm.text("new_design", 64)
    if new_no:
        price = crm.form_float("new_price")
        if price is None or price < 0:
            flash(f"Give design {new_no} a price.", "danger")
            db.session.rollback()
            return redirect(url_for("pricelist.index", cat=cid))
        item = PriceListItem.query.filter_by(category_id=cat.id, design_no=new_no).first()
        if item:
            item.price = round(price, 2)
        else:
            db.session.add(PriceListItem(category_id=cat.id, design_no=new_no, price=round(price, 2)))
        changed += 1
    db.session.commit()
    flash(f"{cat.name}: {changed} price(s) saved, {removed} design(s) taken off.", "success")
    return redirect(url_for("pricelist.index", cat=cid))


@pricelist_bp.route("/<int:cid>/customers", methods=["POST"])
@login_required
@role_required("admin", "manager")
def add_customer(cid):
    cat = PriceCategory.query.get_or_404(cid)
    c = crm.find_customer(request.form.get("customer"))
    if c is None:
        flash(f"No customer with the mobile, card or code “{request.form.get('customer', '')}”.", "danger")
    else:
        was = c.price_category.name if c.price_category else None
        c.price_category_id = cat.id
        db.session.commit()
        flash(f"{c.name} now buys on {cat.name}" + (f" (was {was})." if was and was != cat.name else "."),
              "success")
    return redirect(url_for("pricelist.index", cat=cid))


@pricelist_bp.route("/<int:cid>/customers/remove", methods=["POST"])
@login_required
@role_required("admin", "manager")
def remove_customers(cid):
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    n = 0
    for c in Customer.query.filter(Customer.id.in_(ids), Customer.price_category_id == cid).all():
        c.price_category_id = None
        n += 1
    db.session.commit()
    flash(f"{n} customer(s) back on ordinary prices.", "success")
    return redirect(url_for("pricelist.index", cat=cid))
