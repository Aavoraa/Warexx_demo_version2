from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from app import db
from app.models import User, Attendance
from datetime import datetime

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username, active=True).first()
        if user and user.check_password(password):
            login_user(user)
            # Auto-check-in for staff
            open_att = Attendance.query.filter_by(user_id=user.id, check_out=None).first()
            if not open_att:
                db.session.add(Attendance(user_id=user.id))
                db.session.commit()
            flash(f"Welcome, {user.full_name}!", "success")
            return redirect(url_for("main.dashboard"))
        flash("Invalid credentials.", "danger")
    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
    # Auto-checkout attendance
    open_att = Attendance.query.filter_by(user_id=current_user.id, check_out=None).first()
    if open_att:
        open_att.check_out = datetime.utcnow()
        db.session.commit()
    logout_user()
    flash("Logged out.", "info")
    return redirect(url_for("auth.login"))
