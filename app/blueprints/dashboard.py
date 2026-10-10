"""Dashboard and analytics pages."""

from __future__ import annotations

from flask import Blueprint, abort, redirect, url_for
from flask_login import current_user, login_required

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    if current_user.has_role("teacher"):
        return redirect(url_for("teacher.index"))
    if current_user.has_role("student"):
        return redirect(url_for("student.index"))
    if current_user.has_role("admin"):
        return redirect(url_for("admin.index"))
    abort(403)


@dashboard_bp.route("/analytics")
@login_required
def analytics():
    if current_user.has_role("teacher"):
        return redirect(url_for("teacher.attendance"))
    if current_user.has_role("student"):
        return redirect(url_for("student.index"))
    if current_user.has_role("admin"):
        return redirect(url_for("admin.reports"))
    abort(403)
