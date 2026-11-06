"""CRM → Send SMS — choose customers by who they are and what they bought, and message them.

The choosing is crm.select_customers. The sending is the shop's existing message
queue (app/messaging.py, the log under Customers → Messages): every message is
a ScheduledMessage row, sent through the configured gateway when it falls due,
and kept in the log as "logged" when there is no gateway rather than pretending
it went.
"""
import re
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import crm, db, messaging
from app.models import LEAD_SOURCES, LEAD_STATUSES, Customer, Lead, ScheduledMessage
from app.utils import role_required

sms_bp = Blueprint("sms", __name__)

#: One send cannot queue more than this many messages.
MAX_RECIPIENTS = 5000
PHONE = re.compile(r"^\+?\d{10,13}$")


def _filters(source):
    return {k: (source.get(k) or "").strip() for k in crm.SELECT_FILTERS}


def _leads(f):
    q = Lead.query.filter(Lead.phone.isnot(None), Lead.phone != "")
    if f.get("lead_status") in LEAD_STATUSES:
        q = q.filter(Lead.status == f["lead_status"])
    if f.get("lead_source") in LEAD_SOURCES:
        q = q.filter(Lead.source == f["lead_source"])
    return q.order_by(Lead.created_at.desc()).limit(MAX_RECIPIENTS).all()


@sms_bp.route("/")
@login_required
@role_required("admin", "manager")
def index():
    a = request.args
    source = a.get("source") if a.get("source") in ("customers", "leads") else "customers"
    f = _filters(a)
    asked = a.get("run") == "1" or any(f.values())
    rows, leads = [], []
    if source == "leads":
        leads = _leads({"lead_status": a.get("lead_status"), "lead_source": a.get("lead_source")}) if asked else []
    elif asked:
        rows = crm.select_customers(f, limit=MAX_RECIPIENTS)
    url, _ = messaging.provider()
    return render_template("crm/sms.html", f=f, source=source, rows=rows, leads=leads, asked=asked,
                           categories=crm.customer_categories(), locations=crm.locations(),
                           floors=crm.floors(), product_categories=crm.product_categories(),
                           brands=crm.brands(), sizes=crm.sizes(),
                           religions=crm.distinct(Customer.religion), cities=crm.distinct(Customer.city),
                           areas=crm.distinct(Customer.area), limit=MAX_RECIPIENTS,
                           lead_statuses=LEAD_STATUSES, lead_sources=LEAD_SOURCES, provider=bool(url))


@sms_bp.route("/send", methods=["POST"])
@login_required
@role_required("admin", "manager")
def send():
    body = (request.form.get("message") or "").strip()
    link = (request.form.get("url") or "").strip()
    if link:
        body = f"{body} {link}".strip()
    if not body:
        flash("Write the message first.", "danger")
        return redirect(request.referrer or url_for("sms.index"))
    when = None
    raw = (request.form.get("when") or "").strip()
    if raw:
        try:
            when = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            when = None

    ids = {int(x) for x in request.form.getlist("ids") if x.isdigit()}
    customers = Customer.query.filter(Customer.id.in_(ids)).all() if ids else []
    lead_ids = {int(x) for x in request.form.getlist("lead_ids") if x.isdigit()}
    leads = Lead.query.filter(Lead.id.in_(lead_ids)).all() if lead_ids else []
    extra = [p for p in (x.strip() for x in request.form.getlist("extra")) if PHONE.match(p)]

    # one message per number, whoever it belongs to
    seen, queued = set(), 0
    to_customers = []
    for c in customers:
        if c.phone and c.phone not in seen:
            seen.add(c.phone)
            to_customers.append(c)
    if len(to_customers) + len(leads) + len(extra) > MAX_RECIPIENTS:
        flash(f"That is more than {MAX_RECIPIENTS:,} messages in one go — narrow the selection.", "danger")
        return redirect(request.referrer or url_for("sms.index"))
    queued += messaging.queue_message(to_customers, body, when=when, kind="offer", user_id=current_user.id)
    for who in leads + extra:
        phone = who.phone if isinstance(who, Lead) else who
        if phone in seen:
            continue
        seen.add(phone)
        name = who.name if isinstance(who, Lead) else ""
        db.session.add(ScheduledMessage(customer_id=None, phone=phone, kind="offer",
                                        body=body.replace("{name}", name or "there")
                                        .replace("{shop}", messaging.render("{shop}", None)),
                                        scheduled_for=when or datetime.now(),
                                        created_by_id=current_user.id))
        queued += 1
    if not queued:
        flash("Tick at least one customer, or add a mobile number to the list.", "danger")
        return redirect(request.referrer or url_for("sms.index"))
    db.session.commit()
    msg = f"{queued:,} message(s) queued"
    if not when and request.form.get("now"):
        counts = messaging.send_due()
        db.session.commit()
        msg += (f" — {counts.get('sent', 0)} sent, {counts.get('logged', 0)} logged without a gateway, "
                f"{counts.get('failed', 0)} failed")
    elif when:
        msg += f" for {when.strftime('%d-%m-%Y %H:%M')}"
    flash(msg + ".", "success")
    return redirect(url_for("customers.messages"))
