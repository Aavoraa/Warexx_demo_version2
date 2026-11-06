"""CRM → Feedback Dashboard, Feedback Questions, Feedback Report, Feedback Form Config.

Four menu entries over one set of tables: CustomerFeedback (a response, with its
overall star rating and comment), FeedbackQuestion (what the form asks) and
FeedbackAnswer (one answer to one question). Responses come from the QR on the
bill (app/routes/feedback.py) and from staff at the counter (Customers →
Feedback).
"""
import csv
import io
from collections import Counter, defaultdict
from datetime import date, timedelta

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_login import login_required
from sqlalchemy import func

from app import crm, db
from app.models import FEEDBACK_KINDS, CustomerFeedback, FeedbackAnswer, FeedbackQuestion
from app.utils import role_required

crmfeedback_bp = Blueprint("crmfeedback", __name__)

SOURCES = (("link", "Bill QR"), ("counter", "Counter"))


def _window(default_days=30):
    today = date.today()
    d_from = crm.arg_date("from") or today - timedelta(days=default_days - 1)
    d_to = crm.arg_date("to") or today
    return d_from, d_to, *crm.day_range(d_from, d_to)


# ---------------------------------------------------------------------------
#  Dashboard
# ---------------------------------------------------------------------------
@crmfeedback_bp.route("/")
@login_required
@role_required("admin", "manager")
def dashboard():
    d_from, d_to, lo, hi = _window()
    cfg = crm.setting("feedback_form")
    alert_at = int(cfg.get("alert_at") or 2)
    in_window = [CustomerFeedback.created_at >= lo, CustomerFeedback.created_at < hi]
    n, avg = db.session.query(func.count(CustomerFeedback.id), func.avg(CustomerFeedback.rating)) \
        .filter(*in_window).one()
    dist = dict(db.session.query(CustomerFeedback.rating, func.count(CustomerFeedback.id))
                .filter(*in_window).group_by(CustomerFeedback.rating).all())
    by_source = dict(db.session.query(CustomerFeedback.source, func.count(CustomerFeedback.id))
                     .filter(*in_window).group_by(CustomerFeedback.source).all())
    happy = sum(dist.get(r, 0) for r in (4, 5))
    unhappy = sum(dist.get(r, 0) for r in range(1, alert_at + 1))

    # day by day — the window is a few weeks, so its rows are few enough to bucket here
    days = defaultdict(list)
    for created, rating in (db.session.query(CustomerFeedback.created_at, CustomerFeedback.rating)
                            .filter(*in_window).all()):
        days[created.date()].append(rating)
    trend, d = [], d_from
    while d <= d_to:
        r = days.get(d, [])
        trend.append((d.strftime("%d %b"), len(r), round(sum(r) / len(r), 2) if r else None))
        d += timedelta(days=1)

    # question by question
    per_q = []
    for q in FeedbackQuestion.query.order_by(FeedbackQuestion.sort_order, FeedbackQuestion.id).all():
        base = (db.session.query(FeedbackAnswer).join(CustomerFeedback,
                                                     CustomerFeedback.id == FeedbackAnswer.feedback_id)
                .filter(FeedbackAnswer.question_id == q.id, *in_window))
        count = base.count()
        if not count and not q.active:
            continue
        if q.kind in ("rating", "yesno"):
            score = base.with_entities(func.avg(FeedbackAnswer.score)).scalar()
            per_q.append({"q": q, "count": count, "score": float(score) if score is not None else None})
        elif q.kind == "choice":
            per_q.append({"q": q, "count": count,
                          "choices": Counter(v for (v,) in base.with_entities(FeedbackAnswer.value).all())})
        else:
            per_q.append({"q": q, "count": count,
                          "latest": [a.value for a in base.order_by(FeedbackAnswer.id.desc()).limit(3).all()]})

    low = (CustomerFeedback.query.filter(*in_window, CustomerFeedback.rating <= alert_at)
           .order_by(CustomerFeedback.created_at.desc()).limit(20).all())
    return render_template("crm/feedback/dashboard.html", d_from=d_from, d_to=d_to, n=n,
                           avg=float(avg) if avg else None, dist=dist, happy=happy, unhappy=unhappy,
                           by_source=by_source, trend=trend, per_q=per_q, low=low, alert_at=alert_at,
                           sources=dict(SOURCES))


# ---------------------------------------------------------------------------
#  Questions
# ---------------------------------------------------------------------------
@crmfeedback_bp.route("/questions")
@login_required
@role_required("admin", "manager")
def questions():
    qs = FeedbackQuestion.query.order_by(FeedbackQuestion.active.desc(), FeedbackQuestion.sort_order,
                                         FeedbackQuestion.id).all()
    answered = dict(db.session.query(FeedbackAnswer.question_id, func.count(FeedbackAnswer.id))
                    .group_by(FeedbackAnswer.question_id).all())
    edit = db.session.get(FeedbackQuestion, request.args.get("edit", type=int) or 0)
    return render_template("crm/feedback/questions.html", qs=qs, answered=answered, edit=edit,
                           kinds=FEEDBACK_KINDS)


@crmfeedback_bp.route("/questions/save", methods=["POST"])
@login_required
@role_required("admin", "manager")
def question_save():
    qid = request.form.get("id", type=int)
    text = crm.text("text", 256)
    kind = request.form.get("kind") if request.form.get("kind") in FEEDBACK_KINDS else "rating"
    options = "\n".join(o.strip() for o in (request.form.get("options") or "").splitlines() if o.strip())
    if not text:
        flash("Write the question.", "danger")
        return redirect(url_for("crmfeedback.questions", edit=qid))
    if kind == "choice" and len(options.splitlines()) < 2:
        flash("A choice question needs at least two options, one per line.", "danger")
        return redirect(url_for("crmfeedback.questions", edit=qid))
    q = db.session.get(FeedbackQuestion, qid) if qid else None
    if q is None:
        last = db.session.query(func.max(FeedbackQuestion.sort_order)).scalar() or 0
        q = FeedbackQuestion(sort_order=last + 1, active=True)
        db.session.add(q)
    elif q.kind != kind and FeedbackAnswer.query.filter_by(question_id=q.id).first():
        flash("This question has answers, so its kind cannot change — retire it and add a new one.", "danger")
        return redirect(url_for("crmfeedback.questions", edit=qid))
    q.text, q.kind, q.options = text, kind, options if kind == "choice" else None
    q.required = bool(request.form.get("required"))
    db.session.commit()
    flash("Question saved.", "success")
    return redirect(url_for("crmfeedback.questions"))


@crmfeedback_bp.route("/questions/<int:qid>/<action>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def question_action(qid, action):
    q = FeedbackQuestion.query.get_or_404(qid)
    if action in ("up", "down"):
        qs = FeedbackQuestion.query.order_by(FeedbackQuestion.sort_order, FeedbackQuestion.id).all()
        i = qs.index(q)
        j = i - 1 if action == "up" else i + 1
        if 0 <= j < len(qs):
            qs[i], qs[j] = qs[j], qs[i]
            for n, x in enumerate(qs):
                x.sort_order = n + 1
    elif action == "toggle":
        q.active = not q.active
        flash(f"“{q.text}” is {'back on the form' if q.active else 'retired — its answers are kept'}.", "success")
    elif action == "delete":
        if FeedbackAnswer.query.filter_by(question_id=q.id).first():
            flash("This question has answers — retire it instead, so they keep their question.", "danger")
            return redirect(url_for("crmfeedback.questions"))
        db.session.delete(q)
        flash("Question deleted.", "success")
    db.session.commit()
    return redirect(url_for("crmfeedback.questions"))


# ---------------------------------------------------------------------------
#  Report
# ---------------------------------------------------------------------------
def _report_query():
    d_from, d_to, lo, hi = _window()
    a = request.args
    q = CustomerFeedback.query.filter(CustomerFeedback.created_at >= lo, CustomerFeedback.created_at < hi)
    if a.get("rating_max", type=int):
        q = q.filter(CustomerFeedback.rating <= a.get("rating_max", type=int))
    if a.get("rating_min", type=int):
        q = q.filter(CustomerFeedback.rating >= a.get("rating_min", type=int))
    if a.get("source") in dict(SOURCES):
        q = q.filter(CustomerFeedback.source == a["source"])
    if a.get("comments") == "1":
        q = q.filter(CustomerFeedback.comments.isnot(None), CustomerFeedback.comments != "")
    return q.order_by(CustomerFeedback.created_at.desc()), d_from, d_to


def _report_rows(q, limit):
    rows = q.limit(limit).all()
    ans = defaultdict(dict)
    if rows:
        for a in FeedbackAnswer.query.filter(FeedbackAnswer.feedback_id.in_([r.id for r in rows])).all():
            ans[a.feedback_id][a.question_id] = a.value
    used = {qid for d in ans.values() for qid in d}
    cols = [q for q in FeedbackQuestion.query.order_by(FeedbackQuestion.sort_order, FeedbackQuestion.id).all()
            if q.active or q.id in used]
    return rows, ans, cols


@crmfeedback_bp.route("/report")
@login_required
@role_required("admin", "manager")
def report():
    q, d_from, d_to = _report_query()
    rows, ans, cols = _report_rows(q, 1000)
    return render_template("crm/feedback/report.html", rows=rows, ans=ans, cols=cols, d_from=d_from,
                           d_to=d_to, sources=SOURCES)


@crmfeedback_bp.route("/report.csv")
@login_required
@role_required("admin", "manager")
def report_csv():
    q, *_ = _report_query()
    rows, ans, cols = _report_rows(q, 50000)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Date", "Customer", "Mobile", "Bill", "Rating", *[c.text for c in cols], "Comments", "From"])
    for r in rows:
        w.writerow([r.created_at.strftime("%d-%m-%Y %H:%M"), r.customer.name if r.customer else "",
                    r.customer.phone if r.customer else "", r.invoice.invoice_number if r.invoice else "",
                    r.rating, *[ans[r.id].get(c.id, "") for c in cols], r.comments or "",
                    dict(SOURCES).get(r.source, r.source)])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=feedback-report.csv"})


# ---------------------------------------------------------------------------
#  Form config
# ---------------------------------------------------------------------------
@crmfeedback_bp.route("/form", methods=["GET", "POST"])
@login_required
@role_required("admin", "manager")
def form_config():
    cfg = crm.setting("feedback_form")
    if request.method == "POST":
        for key, n in (("title", 80), ("intro", 300), ("overall_label", 120), ("comment_label", 120),
                       ("thanks", 300)):
            cfg[key] = (request.form.get(key) or "").strip()[:n] or crm.DEFAULTS["feedback_form"][key]
        cfg["show_comment"] = bool(request.form.get("show_comment"))
        cfg["alert_at"] = max(1, min(request.form.get("alert_at", type=int) or 2, 4))
        # "every active question" is stored as no list at all, so a question
        # added later joins the form without anybody coming back here
        cfg["question_ids"] = ([] if request.form.get("which") == "all" else
                               [int(x) for x in request.form.getlist("question_ids") if x.isdigit()])
        crm.save_setting("feedback_form", cfg)
        db.session.commit()
        flash("Feedback form saved. Every new bill's QR opens it.", "success")
        return redirect(url_for("crmfeedback.form_config"))
    active = FeedbackQuestion.query.filter_by(active=True) \
        .order_by(FeedbackQuestion.sort_order, FeedbackQuestion.id).all()
    chosen = cfg.get("question_ids") or []
    return render_template("crm/feedback/form.html", cfg=cfg, active=active,
                           chosen=set(chosen) if chosen else {q.id for q in active},
                           all_chosen=not chosen)


@crmfeedback_bp.route("/form/preview")
@login_required
@role_required("admin", "manager")
def form_preview():
    return render_template("feedback/give.html", inv=None, form=crm.setting("feedback_form"),
                           questions=crm.feedback_questions(), posted={}, preview=True,
                           thanks=request.args.get("thanks") == "1")
