"""CRM → Customer Divert, Lead Management and Self Registration — people on their way
to being (or coming back as) customers.

  Customer Divert    a walk-in met at the door and sent to a floor and a
                     salesperson; closed as billed or left, so the day shows how
                     many came in and how many bought.
  Lead Management    an enquiry to follow up, with its calls and visits, until it
                     becomes a customer or is lost.
  Self Registration  the sign-up QR at the door (public page, `selfreg`), what its
                     form asks, and the sign-ups waiting for the shop to approve.
"""
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, or_

from app import crm, db
from app.models import (LEAD_SOURCES, LEAD_STATUSES, VISIT_STATUSES, Customer, CustomerVisit, Invoice,
                        Lead, LeadActivity, Location)
from app.utils import role_required

divert_bp = Blueprint("divert", __name__)
leads_bp = Blueprint("leads", __name__)
registration_bp = Blueprint("registration", __name__)
selfreg_bp = Blueprint("selfreg", __name__)


# ===========================================================================
#  Customer Divert
# ===========================================================================
@divert_bp.route("/")
@login_required
def index():
    day = crm.arg_date("day") or date.today()
    lo, hi = crm.day_range(day, day)
    _c, location, _f, _k = crm.here()
    visits = (CustomerVisit.query.filter(CustomerVisit.created_at >= lo, CustomerVisit.created_at < hi)
              .order_by(CustomerVisit.created_at.desc()).all())
    open_ = [v for v in visits if v.status in ("waiting", "attending")]
    done = [v for v in visits if v.status not in ("waiting", "attending")]
    billed = sum(1 for v in visits if v.status == "billed")
    by_floor, by_staff = {}, {}
    for v in visits:
        f = v.floor.name if v.floor else "No floor"
        s = v.staff.full_name if v.staff else "Unassigned"
        by_floor.setdefault(f, [0, 0])
        by_staff.setdefault(s, [0, 0])
        by_floor[f][0] += 1
        by_staff[s][0] += 1
        if v.status == "billed":
            by_floor[f][1] += 1
            by_staff[s][1] += 1
    floors = [f for f in crm.floors() if not location or f.location_id in (None, location.id)]
    return render_template("crm/divert.html", day=day, today=date.today(), open_=open_, done=done,
                           total=len(visits), billed=billed,
                           left=sum(1 for v in visits if v.status == "left"),
                           by_floor=sorted(by_floor.items(), key=lambda x: -x[1][0]),
                           by_staff=sorted(by_staff.items(), key=lambda x: -x[1][0]),
                           floors=floors, staff=crm.staff(), interests=[c.name for c in crm.product_categories()])


@divert_bp.route("/new", methods=["POST"])
@login_required
def new():
    phone = (request.form.get("customer_phone") or "").strip()
    name = (request.form.get("customer_name") or "").strip()
    if not (phone or name):
        flash("Take at least the customer's name or mobile.", "danger")
        return redirect(url_for("divert.index"))
    c = crm.find_customer(phone) if phone else None
    _co, location, _f, _k = crm.here()
    v = CustomerVisit(customer_id=c.id if c else None, name=(c.name if c else name)[:128] or None,
                      phone=phone[:32] or None, looking_for=crm.text("looking_for", 128),
                      party_size=max(1, request.form.get("party_size", type=int) or 1),
                      floor_id=request.form.get("floor_id", type=int) or None,
                      staff_id=request.form.get("staff_id", type=int) or None,
                      status="attending" if request.form.get("staff_id", type=int) else "waiting",
                      note=crm.text("note", 256), location_id=location.id if location else None,
                      diverted_by_id=current_user.id)
    db.session.add(v)
    db.session.commit()
    where = " → ".join(x for x in [v.floor.name if v.floor else None,
                                   v.staff.full_name if v.staff else None] if x)
    flash(f"{v.name or v.phone} sent {('to ' + where) if where else 'in'}"
          + (" — a returning customer." if c else "."), "success")
    return redirect(url_for("divert.index"))


def _bill_for(v, number):
    if number:
        return Invoice.query.filter(func.upper(Invoice.invoice_number) == number.upper()).first()
    if v.customer_id:                       # the customer's bill raised since they walked in
        return (Invoice.query.filter(Invoice.customer_id == v.customer_id, Invoice.live(),
                                     Invoice.invoice_date >= v.created_at - timedelta(hours=6))
                .order_by(Invoice.invoice_date.desc()).first())
    return None


@divert_bp.route("/<int:vid>/status", methods=["POST"])
@login_required
def set_status(vid):
    v = CustomerVisit.query.get_or_404(vid)
    status = request.form.get("status")
    if status not in VISIT_STATUSES:
        abort(400)
    if status == "billed":
        number = (request.form.get("invoice") or "").strip()
        inv = _bill_for(v, number)
        if number and inv is None:
            flash(f"No bill numbered {number}.", "danger")
            return redirect(url_for("divert.index"))
        v.invoice_id = inv.id if inv else None
    v.status = status
    v.closed_at = datetime.now() if status in ("billed", "left") else None
    db.session.commit()
    return redirect(url_for("divert.index", day=request.form.get("day") or None))


@divert_bp.route("/<int:vid>/move", methods=["POST"])
@login_required
def move(vid):
    v = CustomerVisit.query.get_or_404(vid)
    v.floor_id = request.form.get("floor_id", type=int) or None
    v.staff_id = request.form.get("staff_id", type=int) or None
    if v.staff_id and v.status == "waiting":
        v.status = "attending"
    db.session.commit()
    return redirect(url_for("divert.index"))


# ===========================================================================
#  Lead Management
# ===========================================================================
OPEN_LEADS = ("new", "contacted", "interested")


@leads_bp.route("/")
@login_required
def index():
    a = request.args
    status = a.get("status", "open")
    q = Lead.query
    if status == "open":
        q = q.filter(Lead.status.in_(OPEN_LEADS))
    elif status == "due":
        q = q.filter(Lead.status.in_(OPEN_LEADS), Lead.next_follow_up <= date.today())
    elif status in LEAD_STATUSES:
        q = q.filter(Lead.status == status)
    if a.get("source") in LEAD_SOURCES:
        q = q.filter(Lead.source == a["source"])
    if a.get("assigned", type=int):
        q = q.filter(Lead.assigned_to_id == a.get("assigned", type=int))
    if a.get("mine"):
        q = q.filter(Lead.assigned_to_id == current_user.id)
    text = (a.get("q") or "").strip()
    if text:
        q = q.filter(or_(Lead.name.ilike(f"%{text}%"), Lead.phone.ilike(f"%{text}%"),
                         Lead.interest.ilike(f"%{text}%")))
    leads = q.order_by(Lead.next_follow_up.is_(None), Lead.next_follow_up, Lead.created_at.desc()).limit(500).all()

    today = date.today()
    month = today.replace(day=1)
    counts = dict(db.session.query(Lead.status, func.count(Lead.id)).group_by(Lead.status).all())
    open_q = Lead.query.filter(Lead.status.in_(OPEN_LEADS))
    won_month = Lead.query.filter(Lead.status == "won", Lead.updated_at >= datetime.combine(month, datetime.min.time())).count()
    closed_month = Lead.query.filter(Lead.status.in_(("won", "lost")),
                                     Lead.updated_at >= datetime.combine(month, datetime.min.time())).count()
    return render_template(
        "crm/leads/list.html", leads=leads, status=status, statuses=LEAD_STATUSES, sources=LEAD_SOURCES,
        staff=crm.staff(), counts=counts, today=today,
        open_count=open_q.count(),
        due=open_q.filter(Lead.next_follow_up == today).count(),
        overdue=open_q.filter(Lead.next_follow_up < today).count(),
        pipeline=float(db.session.query(func.coalesce(func.sum(Lead.expected_value), 0))
                       .filter(Lead.status.in_(OPEN_LEADS)).scalar() or 0),
        won_month=won_month, closed_month=closed_month)


def _fill_lead(lead):
    lead.name = crm.text("name", 128) or lead.name
    lead.phone = crm.text("phone", 32)
    lead.email = crm.text("email", 128)
    lead.city = crm.text("city", 64)
    lead.source = request.form.get("source") if request.form.get("source") in LEAD_SOURCES else "other"
    lead.interest = crm.text("interest", 256)
    lead.expected_value = crm.form_float("expected_value")
    lead.next_follow_up = crm.parse_date(request.form.get("next_follow_up"))
    lead.assigned_to_id = request.form.get("assigned_to_id", type=int) or None


@leads_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        if not crm.text("name"):
            flash("Give the lead a name.", "danger")
            return redirect(url_for("leads.new"))
        _c, location, _f, _k = crm.here()
        lead = Lead(status="new", created_by_id=current_user.id,
                    location_id=location.id if location else None)
        _fill_lead(lead)
        existing = crm.find_customer(lead.phone) if lead.phone else None
        if existing:
            lead.customer_id = existing.id
        db.session.add(lead)
        db.session.flush()
        db.session.add(LeadActivity(lead_id=lead.id, kind="note", by_id=current_user.id,
                                    note=crm.text("note", 512) or "Lead added"))
        db.session.commit()
        flash(f"Lead {lead.name} added" + (f" — already a customer ({existing.name})." if existing else "."),
              "success")
        return redirect(url_for("leads.view", lid=lead.id))
    return render_template("crm/leads/form.html", lead=None, sources=LEAD_SOURCES, staff=crm.staff(),
                           default_follow_up=date.today() + timedelta(days=1))


@leads_bp.route("/<int:lid>", methods=["GET", "POST"])
@login_required
def view(lid):
    lead = Lead.query.get_or_404(lid)
    if request.method == "POST":
        _fill_lead(lead)
        db.session.commit()
        flash("Lead updated.", "success")
        return redirect(url_for("leads.view", lid=lid))
    return render_template("crm/leads/view.html", lead=lead, statuses=LEAD_STATUSES, sources=LEAD_SOURCES,
                           staff=crm.staff(), today=date.today(), edit=request.args.get("edit") == "1")


@leads_bp.route("/<int:lid>/activity", methods=["POST"])
@login_required
def activity(lid):
    lead = Lead.query.get_or_404(lid)
    kind = request.form.get("kind") if request.form.get("kind") in ("call", "visit", "message", "note") else "note"
    note = crm.text("note", 512)
    status = request.form.get("status")
    parts = [note] if note else []
    if status in LEAD_STATUSES and status != lead.status:
        if status == "lost":
            lead.lost_reason = crm.text("lost_reason", 256) or note
        parts.append(f"Status {lead.status} → {status}")
        lead.status = status
    nxt = crm.parse_date(request.form.get("next_follow_up"))
    if nxt:
        lead.next_follow_up = nxt
        parts.append(f"Next follow-up {nxt.strftime('%d-%m-%Y')}")
    elif lead.status in ("won", "lost"):
        lead.next_follow_up = None
    if not parts:
        flash("Write what happened, or change the status.", "danger")
        return redirect(url_for("leads.view", lid=lid))
    lead.updated_at = datetime.utcnow()
    db.session.add(LeadActivity(lead_id=lid, kind=kind, note=" · ".join(parts)[:512], by_id=current_user.id))
    db.session.commit()
    return redirect(url_for("leads.view", lid=lid))


@leads_bp.route("/<int:lid>/convert", methods=["POST"])
@login_required
def convert(lid):
    """Won — the lead becomes (or is matched to) a customer."""
    lead = Lead.query.get_or_404(lid)
    c = lead.customer or (crm.find_customer(lead.phone) if lead.phone else None)
    made = c is None
    if made:
        c = Customer(name=lead.name, phone=lead.phone, email=lead.email, city=lead.city,
                     source="lead", customer_type="customer")
        db.session.add(c)
        db.session.flush()
        c.code = c.card_code
    lead.customer_id, lead.status, lead.next_follow_up = c.id, "won", None
    lead.updated_at = datetime.utcnow()
    db.session.add(LeadActivity(lead_id=lid, kind="status", by_id=current_user.id,
                                note=f"Won — {'added as' if made else 'matched to'} customer {c.code or c.card_code}"))
    db.session.commit()
    flash(f"{lead.name} is now customer {c.code or c.card_code}.", "success")
    return redirect(url_for("customers.edit_customer", cid=c.id) if made else url_for("leads.view", lid=lid))


# ===========================================================================
#  Self Registration — the manager's side
# ===========================================================================
def _signer():
    return URLSafeSerializer(current_app.config["SECRET_KEY"], salt="self-registration")


def join_token(location_id):
    """The token in a sign-up QR. It names the branch, so a sign-up is filed under
    the branch whose door it was scanned at — and a made-up link opens nothing."""
    return _signer().dumps(int(location_id or 0))


@registration_bp.route("/", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def index():
    cfg = crm.setting("registration_form")
    if request.method == "POST":
        for key, n in (("title", 80), ("intro", 300), ("thanks", 300)):
            cfg[key] = (request.form.get(key) or "").strip()[:n] or crm.DEFAULTS["registration_form"][key]
        cfg["fields"] = {k: (request.form.get(f"f_{k}") if request.form.get(f"f_{k}") in
                             ("off", "optional", "required") else "off")
                         for k, _ in crm.REGISTRATION_FIELDS}
        cfg["auto_approve"] = bool(request.form.get("auto_approve"))
        cfg["customer_category"] = crm.text("customer_category", 64) or ""
        crm.save_setting("registration_form", cfg)
        db.session.commit()
        flash("Sign-up form saved.", "success")
        return redirect(url_for("registration.index"))
    _c, here_loc, _f, _k = crm.here()
    loc = db.session.get(Location, request.args.get("location_id", type=int) or 0) or here_loc
    link = url_for("selfreg.join", token=join_token(loc.id if loc else 0), _external=True)
    pending = (Customer.query.filter(Customer.awaiting_approval.is_(True))
               .order_by(Customer.created_at.desc()).limit(300).all())
    recent = (Customer.query.filter(Customer.source == "self")
              .order_by(Customer.created_at.desc()).limit(10).all())
    return render_template("crm/registration.html", cfg=cfg, fields=crm.REGISTRATION_FIELDS, link=link,
                           loc=loc, locations=crm.locations(), pending=pending, recent=recent,
                           categories=crm.customer_categories())


@registration_bp.route("/review", methods=["POST"])
@login_required
@role_required("admin", "manager")
def review():
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    action = request.form.get("action")
    rows = Customer.query.filter(Customer.id.in_(ids), Customer.awaiting_approval.is_(True)).all()
    for c in rows:
        c.awaiting_approval = False
        if action == "reject":
            c.inactive = True
    db.session.commit()
    flash(f"{len(rows)} sign-up(s) {'approved' if action != 'reject' else 'rejected — kept as inactive'}.",
          "success")
    return redirect(url_for("registration.index"))


@registration_bp.route("/poster")
@login_required
@role_required("admin", "manager")
def poster():
    _c, here_loc, _f, _k = crm.here()
    loc = db.session.get(Location, request.args.get("location_id", type=int) or 0) or here_loc
    link = url_for("selfreg.join", token=join_token(loc.id if loc else 0), _external=True)
    return render_template("crm/registration_poster.html", link=link, loc=loc,
                           cfg=crm.setting("registration_form"))


# ===========================================================================
#  Self Registration — the customer's page (no login)
# ===========================================================================
@selfreg_bp.route("/<token>", methods=["GET", "POST"])
def join(token):
    try:
        location_id = int(_signer().loads(token))
    except (BadSignature, ValueError, TypeError):
        abort(404)
    loc = db.session.get(Location, location_id) if location_id else None
    cfg = crm.setting("registration_form")
    fields = cfg["fields"]
    if request.method != "POST":
        return render_template("selfreg/join.html", cfg=cfg, fields=fields, loc=loc, posted={})

    f = request.form
    if f.get("website"):                       # the box people cannot see; bots fill it
        return render_template("selfreg/join.html", cfg=cfg, fields=fields, loc=loc, posted={}, done=True)
    name, phone = (f.get("name") or "").strip()[:128], (f.get("phone") or "").strip().replace(" ", "")[:32]

    def again(msg):
        return render_template("selfreg/join.html", cfg=cfg, fields=fields, loc=loc, posted=f, error=msg)

    if not name or not phone.lstrip("+").isdigit() or not 10 <= len(phone.lstrip("+")) <= 13:
        return again("Please give your name and a 10-digit mobile number.")
    for key, label in crm.REGISTRATION_FIELDS:
        if fields.get(key) == "required":
            filled = (f.get("kids_boys") or f.get("kids_girls")) if key == "kids" else f.get(key)
            if not (filled or "").strip():
                return again(f"Please fill in: {label}.")
    if Customer.query.filter(or_(Customer.phone == phone, Customer.alt_phone == phone)).first():
        # Do not say whose number it is — this page is public.
        return render_template("selfreg/join.html", cfg=cfg, fields=fields, loc=loc, posted={}, done=True,
                               already=True)

    def on(key):
        return fields.get(key) in ("optional", "required")

    c = Customer(name=name, phone=phone, source="self", customer_type="customer",
                 registered_location_id=loc.id if loc else None,
                 awaiting_approval=not cfg.get("auto_approve"),
                 category=cfg.get("customer_category") or None)
    if on("email"):
        c.email = (f.get("email") or "").strip()[:128] or None
    if on("gender") and f.get("gender") in ("female", "male", "other"):
        c.gender = f.get("gender")
    if on("dob"):
        c.dob = crm.parse_date(f.get("dob"))
    if on("married"):
        c.married = f.get("married") == "yes"
    if on("anniversary"):
        c.anniversary = crm.parse_date(f.get("anniversary"))
    if on("kids"):
        c.kids_boys = max(0, int(f.get("kids_boys") or 0)) if (f.get("kids_boys") or "").isdigit() else None
        c.kids_girls = max(0, int(f.get("kids_girls") or 0)) if (f.get("kids_girls") or "").isdigit() else None
    for key, n in (("address", 256), ("city", 64), ("area", 64), ("pincode", 12), ("religion", 32)):
        if on(key):
            setattr(c, key, (f.get(key) or "").strip()[:n] or None)
    db.session.add(c)
    db.session.flush()
    c.code = c.card_code
    db.session.commit()
    return render_template("selfreg/join.html", cfg=cfg, fields=fields, loc=loc, posted={}, done=True,
                           card=c.card_code)
