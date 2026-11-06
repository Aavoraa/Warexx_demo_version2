"""What the CRM screens share — settings, form parsing, and choosing customers.

The screens themselves are the blueprints in app/routes/crm_*.py, one per menu
entry (app/modules.py). Everything here is imported at module level, never
inside a function: under the Essa backend the name `app` belongs to the
warehouse by the time a request runs (see app/utils.py).
"""
import json
from datetime import date, datetime, timedelta

from flask import request, session
from sqlalchemy import func, or_

from app import db, places
from app.models import (Category, CrmSetting, Customer, FeedbackQuestion, Floor, Invoice, InvoiceItem,
                        Location, PriceListItem, Product, User)


# ---------------------------------------------------------------------------
#  settings — one JSON document per key, with defaults filled in
# ---------------------------------------------------------------------------
#: Which customer fields the sign-up page shows: off · optional · required.
#: Name and mobile are not listed — a sign-up without them is not a customer.
REGISTRATION_FIELDS = (
    ("email", "Email"), ("gender", "Gender"), ("dob", "Date of birth"),
    ("married", "Married"), ("anniversary", "Wedding anniversary"),
    ("kids", "Kids (boys / girls)"), ("address", "Address"), ("city", "City"),
    ("area", "Area"), ("pincode", "PIN code"), ("religion", "Religion"),
)

DEFAULTS = {
    "feedback_form": {
        "title": "How did we do?",
        "intro": "Thank you for shopping with us. It takes a minute, and we read every answer.",
        "overall_label": "Overall, how was your visit?",
        "comment_label": "Anything else you'd like to tell us?",
        "thanks": "Thank you — your feedback has reached the store.",
        "show_comment": True,
        #: Empty means every active question, in their own order.
        "question_ids": [],
        #: A rating at or below this is flagged on the dashboard.
        "alert_at": 2,
    },
    "registration_form": {
        "title": "Join our customer club",
        "intro": "Sign up once and earn points on every bill, plus birthday offers.",
        "thanks": "You're registered. Show your mobile number at the counter to earn points.",
        "fields": {"email": "optional", "gender": "optional", "dob": "optional",
                   "married": "off", "anniversary": "optional", "kids": "off",
                   "address": "off", "city": "optional", "area": "optional",
                   "pincode": "off", "religion": "off"},
        #: Self sign-ups wait for the store to approve them unless this is on.
        "auto_approve": False,
        "customer_category": "",
    },
}


def setting(key):
    row = db.session.get(CrmSetting, key)
    out = json.loads(json.dumps(DEFAULTS.get(key, {})))     # a deep copy
    if row and row.value:
        try:
            stored = json.loads(row.value)
        except ValueError:
            stored = {}
        for k, v in stored.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update(v)
            else:
                out[k] = v
    return out


def save_setting(key, value):
    row = db.session.get(CrmSetting, key)
    if row is None:
        row = CrmSetting(key=key)
        db.session.add(row)
    row.value = json.dumps(value)


# ---------------------------------------------------------------------------
#  form and query-string parsing
# ---------------------------------------------------------------------------
def parse_date(raw):
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def arg_date(name, source=None):
    return parse_date((source or request.args).get(name))


def form_float(name, default=None):
    raw = (request.form.get(name) or "").strip().replace(",", "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def form_int(name, default=None):
    raw = (request.form.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def text(name, limit=256, source=None):
    v = ((source or request.form).get(name) or "").strip()
    return v[:limit] or None


def day_range(start, end):
    """[start 00:00, day after end 00:00) — a whole-day filter on a DateTime."""
    lo = datetime.combine(start, datetime.min.time()) if start else None
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time()) if end else None
    return lo, hi


# ---------------------------------------------------------------------------
#  where this till is, and the lists the forms pick from
# ---------------------------------------------------------------------------
def here():
    """(company, location, floor, counter) of the till this session is at."""
    return places.resolve(*(session.get(k) for k in
                            ("company_id", "location_id", "floor_id", "counter_id")))


def locations():
    return Location.query.filter(or_(Location.active.is_(None), Location.active.is_(True))) \
        .order_by(Location.name).all()


def floors():
    return Floor.query.filter(or_(Floor.active.is_(None), Floor.active.is_(True))) \
        .order_by(Floor.location_id, Floor.sort_order, Floor.name).all()


def staff():
    return User.query.filter(or_(User.active.is_(None), User.active.is_(True))) \
        .order_by(User.full_name).all()


def distinct(col, limit=200):
    """The values already used in a column, for a datalist."""
    rows = (db.session.query(col).filter(col.isnot(None), col != "")
            .group_by(col).order_by(col).limit(limit).all())
    return [r[0] for r in rows]


def customer_categories():
    return distinct(Customer.category)


def active_customers():
    return or_(Customer.inactive.is_(None), Customer.inactive.is_(False))


def find_customer(q):
    """One customer by mobile, loyalty card, card code or exact name — or None."""
    q = (q or "").strip()
    if not q:
        return None
    hit = Customer.query.filter(or_(Customer.phone == q, Customer.alt_phone == q,
                                    Customer.loyalty_card == q)).first()
    if hit:
        return hit
    if q.upper().startswith("CUST") and q[4:].isdigit():
        return db.session.get(Customer, int(q[4:]))
    return Customer.query.filter(or_(func.upper(Customer.code) == q.upper(),
                                     func.lower(Customer.name) == q.lower())).first()


def customer_from_form(prefix="customer", source="counter", required=True):
    """The customer a form names in <prefix>_id / _phone / _name — found, or added.

    Found by id first (the lookup filled it in), then by mobile. A mobile nobody
    has, with a name, is a new customer: taking an order or selling a voucher to
    someone is exactly when the shop learns who they are. Raises ValueError with
    a sentence for the screen when there is nobody to name.
    """
    f = request.form
    cid = f.get(f"{prefix}_id", type=int)
    if cid:
        c = db.session.get(Customer, cid)
        if c:
            return c
    phone = (f.get(f"{prefix}_phone") or "").strip()
    name = (f.get(f"{prefix}_name") or "").strip()
    c = find_customer(phone) if phone else None
    if c:
        return c
    if not name:
        if required:
            raise ValueError("Enter the customer's mobile number and name.")
        return None
    c = Customer(name=name[:128], phone=phone[:32] or None, source=source, customer_type="customer")
    db.session.add(c)
    db.session.flush()
    c.code = c.card_code
    return c


def customer_json(c):
    return {"id": c.id, "name": c.name, "phone": c.phone, "code": c.code or c.card_code,
            "city": c.city, "category": c.category, "card": c.card_no,
            "points": round(c.loyalty_points or 0), "price_category_id": c.price_category_id}


# ---------------------------------------------------------------------------
#  choosing customers — the Send SMS screen's filter, and the analyser's
# ---------------------------------------------------------------------------
#: The filters a customer selection understands, as the query string spells them.
SELECT_FILTERS = ("category", "location_id", "dob_from", "dob_to", "bill_from", "bill_to",
                  "floor_id", "counter_id", "product_category", "brand", "size", "religion",
                  "city", "area", "value_from", "value_to", "exclude_from", "exclude_to")


def _md(d):
    return d.month * 100 + d.day


def select_customers(f, limit=2000):
    """Customers matching the filter dict `f`, with their spend — as rows of
    (Customer, total, bills, last bill). Everything is done in SQL: the book can
    hold 150K customers.

    A bill filter (dates, value, what was bought, where) means "customers with
    such bills"; the total and count are over those bills only. The birth-date
    filter compares day and month, so 01-11 to 30-11 finds every November
    birthday whatever the year.
    """
    conds = [Invoice.customer_id == Customer.id, Invoice.live()]
    lo, hi = day_range(parse_date(f.get("bill_from")), parse_date(f.get("bill_to")))
    if lo:
        conds.append(Invoice.invoice_date >= lo)
    if hi:
        conds.append(Invoice.invoice_date < hi)
    if f.get("location_id"):
        conds.append(Invoice.location_id == int(f["location_id"]))
    if f.get("floor_id"):
        conds.append(Invoice.floor_id == int(f["floor_id"]))
    if f.get("counter_id"):
        conds.append(Invoice.counter_id == int(f["counter_id"]))
    items = []
    if f.get("product_category"):
        items.append(Product.category_id == int(f["product_category"]))
    if f.get("brand"):
        items.append(Product.brand == f["brand"])
    if f.get("size"):
        items.append(Product.size == f["size"])
    if items:
        conds.append(Invoice.items.any(InvoiceItem.product.has(db.and_(*items))))

    spend = (db.session.query(Invoice.customer_id.label("cid"),
                              func.sum(Invoice.total).label("total"),
                              func.count(Invoice.id).label("bills"),
                              func.max(Invoice.invoice_date).label("last"))
             .filter(*conds[1:]).group_by(Invoice.customer_id).subquery())

    billish = bool(lo or hi or items or f.get("location_id") or f.get("floor_id")
                   or f.get("counter_id"))
    q = db.session.query(Customer, spend.c.total, spend.c.bills, spend.c.last)
    q = (q.join(spend, spend.c.cid == Customer.id) if billish
         else q.outerjoin(spend, spend.c.cid == Customer.id))
    q = q.filter(active_customers(), Customer.phone.isnot(None), Customer.phone != "")
    for key, col in (("category", Customer.category), ("religion", Customer.religion),
                     ("city", Customer.city), ("area", Customer.area)):
        if f.get(key):
            q = q.filter(col == f[key])
    vf, vt = _num(f.get("value_from")), _num(f.get("value_to"))
    if vf is not None:
        q = q.filter(func.coalesce(spend.c.total, 0) >= vf)
    if vt is not None:
        q = q.filter(func.coalesce(spend.c.total, 0) <= vt)

    df, dt = parse_date(f.get("dob_from")), parse_date(f.get("dob_to"))
    if df or dt:
        q = q.filter(Customer.dob.isnot(None))
    rows = q.order_by(Customer.name, Customer.id)

    # "Exclude customers who bought between…" — those who came back recently
    # need no reminder.
    ef, et = day_range(parse_date(f.get("exclude_from")), parse_date(f.get("exclude_to")))
    if ef or et:
        recent = db.session.query(Invoice.customer_id).filter(Invoice.live(),
                                                              Invoice.customer_id.isnot(None))
        if ef:
            recent = recent.filter(Invoice.invoice_date >= ef)
        if et:
            recent = recent.filter(Invoice.invoice_date < et)
        rows = rows.filter(~Customer.id.in_(recent))

    if not (df or dt):
        return rows.limit(limit).all()
    # Day-and-month matching is not portable SQL, so it is done here — on
    # customers that have a date of birth at all, which is a small share.
    lo_md, hi_md = _md(df or date(2000, 1, 1)), _md(dt or date(2000, 12, 31))
    out = []
    for r in rows.yield_per(1000):
        md = _md(r[0].dob)
        if (lo_md <= md <= hi_md) if lo_md <= hi_md else (md >= lo_md or md <= hi_md):
            out.append(r)
            if len(out) >= limit:
                break
    return out


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
#  the feedback form — which questions it asks, and reading the answers
# ---------------------------------------------------------------------------
def feedback_questions():
    """The questions the customer's page asks, in order (CRM → Feedback Form Config)."""
    cfg = setting("feedback_form")
    active = FeedbackQuestion.query.filter_by(active=True) \
        .order_by(FeedbackQuestion.sort_order, FeedbackQuestion.id).all()
    chosen = [int(i) for i in cfg.get("question_ids") or [] if str(i).isdigit()]
    if not chosen:
        return active
    by_id = {q.id: q for q in active}
    return [by_id[i] for i in chosen if i in by_id]


def read_answers(questions, form):
    """[(question, value, score)] from a submitted feedback form, or ValueError
    naming the first required question left blank."""
    out = []
    for q in questions:
        raw = (form.get(f"q{q.id}") or "").strip()
        if not raw:
            if q.required:
                raise ValueError(f"Please answer: {q.text}")
            continue
        score = None
        if q.kind == "rating":
            if raw not in ("1", "2", "3", "4", "5"):
                continue
            score = float(raw)
        elif q.kind == "yesno":
            if raw not in ("yes", "no"):
                continue
            score = 1.0 if raw == "yes" else 0.0
        elif q.kind == "choice" and raw not in q.option_list:
            continue
        out.append((q, raw[:512], score))
    return out


# ---------------------------------------------------------------------------
#  price lists — what a customer on a price category pays
# ---------------------------------------------------------------------------
def price_category_of(customer_id):
    """The active price list a customer buys on, or None."""
    if not customer_id:
        return None
    c = db.session.get(Customer, customer_id)
    cat = c.price_category if c else None
    return cat if cat and cat.active is not False else None


def list_prices(products, category):
    """{product id: price} for these products on `category`.

    A design priced on the list sells at that price; anything else at the list's
    discount off its own price. A list price is never above the ordinary price —
    a customer on a better list is not charged more for being on it.
    """
    out = {}
    if not category:
        return {p.id: p.selling_price for p in products}
    designs = {p.design_no for p in products if p.design_no}
    priced = dict(db.session.query(PriceListItem.design_no, PriceListItem.price)
                  .filter(PriceListItem.category_id == category.id,
                          PriceListItem.design_no.in_(designs)).all()) if designs else {}
    pct = category.discount_pct or 0
    for p in products:
        base = p.selling_price or 0
        price = priced.get(p.design_no) if p.design_no in priced else round(base * (1 - pct / 100.0), 2)
        out[p.id] = round(min(price, base), 2) if base else round(price, 2)
    return out


def product_categories():
    return Category.query.order_by(Category.name).all()


def brands():
    return distinct(Product.brand)


def sizes():
    return distinct(Product.size)
