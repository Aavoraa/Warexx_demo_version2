"""The customer's own feedback page — reached from the QR on their bill.

No login: it is for the customer, on their phone. What stops anybody filling it
in for any bill is the token — the bill's id signed with the shop's secret key —
so a link can only be made by printing the bill it belongs to, and one bill takes
one answer.

What the page asks is set under CRM → Feedback Form Config and CRM → Feedback
Questions (app/crm.py): an overall star rating, then each chosen question.
"""
from itsdangerous import BadSignature, URLSafeSerializer
from flask import Blueprint, abort, current_app, render_template, request

from app import crm, db
from app.models import CustomerFeedback, FeedbackAnswer, Invoice

feedback_bp = Blueprint("feedback", __name__)


def _invoice_for(token):
    try:
        iid = URLSafeSerializer(current_app.config["SECRET_KEY"], salt="feedback").loads(token)
    except BadSignature:
        abort(404)
    inv = db.session.get(Invoice, int(iid))
    if inv is None or inv.is_cancelled:
        abort(404)
    return inv


def _page(inv, **kw):
    return render_template("feedback/give.html", inv=inv, form=crm.setting("feedback_form"),
                           questions=crm.feedback_questions(), posted=request.form, **kw)


@feedback_bp.route("/<token>", methods=["GET", "POST"])
def give(token):
    inv = _invoice_for(token)
    already = CustomerFeedback.query.filter_by(invoice_id=inv.id, source="link").first()
    if request.method == "POST" and not already:
        rating = request.form.get("rating", type=int)
        if rating not in (1, 2, 3, 4, 5):
            return _page(inv, error="Pick a rating from 1 to 5 stars.")
        try:
            answers = crm.read_answers(crm.feedback_questions(), request.form)
        except ValueError as e:
            return _page(inv, error=str(e))
        fb = CustomerFeedback(customer_id=inv.customer_id, invoice_id=inv.id, rating=rating,
                              comments=(request.form.get("comments") or "").strip()[:2000] or None,
                              source="link")
        db.session.add(fb)
        db.session.flush()
        for q, value, score in answers:
            db.session.add(FeedbackAnswer(feedback_id=fb.id, question_id=q.id, value=value, score=score))
        db.session.commit()
        return _page(inv, thanks=True)
    return _page(inv, thanks=bool(already))
