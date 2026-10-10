"""Authentication.

Nothing in this application required a login before now: an anonymous visitor could
delete students, rewrite attendance and export the entire roster including names, email
addresses and phone numbers.  Sessions are cookie-based via Flask-Login; the first
account is created with ``flask create-admin`` rather than shipping default credentials.
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.extensions import db, limiter
from app.models import Role, User
from app.utils.time import utcnow

auth_bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str:
    """Only follow a relative redirect target.

    An open redirect here would let a phishing link bounce off the login page onto an
    attacker's host with the site's own domain in the address bar.
    """
    if not target:
        return url_for("dashboard.index")
    parsed = urlparse(target)
    if parsed.netloc or parsed.scheme:
        return url_for("dashboard.index")
    return target


def _panel_home(user) -> str:
    """Resolve a user's normal post-login destination from their server-side role."""
    panel_endpoints = {
        Role.ADMIN.value: "admin.index",
        Role.TEACHER.value: "teacher.index",
        Role.STUDENT.value: "student.index",
    }
    return url_for(panel_endpoints.get(user.role, "dashboard.index"))


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 60 per hour", methods=["POST"])
def login():
    # An authenticated browser visiting the login page should return to its panel,
    # but a POST must still validate the submitted credentials. Otherwise a session
    # left signed in as Admin can silently ignore valid Teacher credentials and keep
    # redirecting to the Admin Panel.
    if current_user.is_authenticated and request.method == "GET":
        return redirect(_panel_home(current_user))

    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    remember = bool(request.form.get("remember"))

    user = db.session.execute(db.select(User).filter_by(username=username)).scalar_one_or_none()

    # One message for both "no such user" and "wrong password" so the form cannot be
    # used to enumerate accounts.
    if user is None or not user.check_password(password):
        flash("Incorrect username or password.", "error")
        return render_template("login.html", username=username), 401

    if not user.is_active:
        flash("That account has been deactivated.", "error")
        return render_template("login.html", username=username), 403

    login_user(user, remember=remember)
    user.last_login_at = utcnow()
    db.session.commit()
    flash(f"Welcome back, {user.username}.", "success")
    target = request.args.get("next") or request.form.get("next")
    return redirect(_safe_next(target) if target else _panel_home(user))


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))
