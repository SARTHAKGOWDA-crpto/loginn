"""
NEXUS AUTH  —  Flask + MySQL Authentication System
Run:  python app.py
"""

import os
import re
import secrets
import random
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config

# ─────────────────────────────────────────────────────────────
#  App & Extensions
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config.from_object(Config)

db   = SQLAlchemy(app)
mail = Mail(app)

# ─────────────────────────────────────────────────────────────
#  Models
# ─────────────────────────────────────────────────────────────
AVATAR_COLORS = [
    "#7F77DD", "#1D9E75", "#D85A30", "#D4537E",
    "#378ADD", "#639922", "#BA7517", "#E24B4A",
]

class User(db.Model):
    __tablename__ = "users"

    id              = db.Column(db.Integer, primary_key=True)
    full_name       = db.Column(db.String(120), nullable=False)
    username        = db.Column(db.String(60),  nullable=False, unique=True)
    email           = db.Column(db.String(180), nullable=False, unique=True)
    phone           = db.Column(db.String(20),  nullable=True)
    password_hash   = db.Column(db.String(255), nullable=False)
    avatar_color    = db.Column(db.String(7),   nullable=False, default="#7F77DD")
    role            = db.Column(db.Enum("user", "admin"), nullable=False, default="user")
    is_verified     = db.Column(db.Boolean,     nullable=False, default=False)
    is_locked       = db.Column(db.Boolean,     nullable=False, default=False)
    failed_attempts = db.Column(db.Integer,     nullable=False, default=0)
    last_login      = db.Column(db.DateTime,    nullable=True)
    created_at      = db.Column(db.DateTime,    nullable=False, default=datetime.utcnow)

    reset_tokens    = db.relationship("PasswordResetToken", backref="user", lazy=True, cascade="all, delete")
    login_logs      = db.relationship("LoginLog",           backref="user", lazy=True, cascade="all, delete")

    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)

    @property
    def initials(self) -> str:
        parts = self.full_name.split()
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id         = db.Column(db.Integer,  primary_key=True)
    user_id    = db.Column(db.Integer,  db.ForeignKey("users.id"), nullable=False)
    token      = db.Column(db.String(100), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean,  nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def is_valid(self) -> bool:
        return not self.used and self.expires_at > datetime.utcnow()


class LoginLog(db.Model):
    __tablename__ = "login_logs"

    id         = db.Column(db.Integer,  primary_key=True)
    user_id    = db.Column(db.Integer,  db.ForeignKey("users.id"), nullable=True)
    email      = db.Column(db.String(180), nullable=False)
    ip_address = db.Column(db.String(45), nullable=True)
    success    = db.Column(db.Boolean,  nullable=False, default=False)
    reason     = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────
def _ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr)


def _log(email: str, success: bool, user_id=None, reason: str = None):
    db.session.add(LoginLog(
        email=email, success=success,
        user_id=user_id, ip_address=_ip(), reason=reason
    ))
    db.session.commit()


def _validate_password(pw: str) -> list[str]:
    errors = []
    if len(pw) < 8:
        errors.append("At least 8 characters required.")
    if not re.search(r"[A-Z]", pw):
        errors.append("At least one uppercase letter required.")
    if not re.search(r"[0-9]", pw):
        errors.append("At least one number required.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", pw):
        errors.append("At least one special character required.")
    return errors


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or user.role != "admin":
            flash("Administrator access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────
#  Routes — Auth
# ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password   = request.form.get("password", "")
        remember   = bool(request.form.get("remember"))

        # Find user by email or username
        user = (User.query.filter(
            (User.email == identifier) | (User.username == identifier)
        ).first())

        if not user:
            _log(identifier, False, reason="User not found")
            flash("Invalid credentials. Please try again.", "danger")
            return render_template("login.html")

        if user.is_locked:
            _log(identifier, False, user.id, "Account locked")
            flash("Your account has been locked after too many failed attempts. Reset your password to unlock.", "danger")
            return render_template("login.html")

        if not user.check_password(password):
            user.failed_attempts += 1
            if user.failed_attempts >= app.config["MAX_FAILED_ATTEMPTS"]:
                user.is_locked = True
                db.session.commit()
                _log(identifier, False, user.id, "Too many failed attempts — locked")
                flash("Account locked due to too many failed attempts.", "danger")
            else:
                db.session.commit()
                remaining = app.config["MAX_FAILED_ATTEMPTS"] - user.failed_attempts
                _log(identifier, False, user.id, "Wrong password")
                flash(f"Invalid credentials. {remaining} attempt(s) remaining.", "danger")
            return render_template("login.html")

        # Successful login
        user.failed_attempts = 0
        user.last_login = datetime.utcnow()
        db.session.commit()
        _log(identifier, True, user.id)

        session.permanent = remember
        session["user_id"]   = user.id
        session["user_name"] = user.full_name
        session["user_role"] = user.role

        flash(f"Welcome back, {user.full_name.split()[0]}! 👋", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        full_name  = request.form.get("full_name",  "").strip()
        username   = request.form.get("username",   "").strip().lower()
        email      = request.form.get("email",      "").strip().lower()
        phone      = request.form.get("phone",      "").strip()
        password   = request.form.get("password",   "")
        confirm_pw = request.form.get("confirm_password", "")
        terms      = request.form.get("terms")

        # ── Validation ──
        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not re.match(r"^[a-z0-9_]{3,30}$", username):
            errors.append("Username: 3–30 chars, letters/numbers/underscore only.")
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            errors.append("Enter a valid email address.")
        if password != confirm_pw:
            errors.append("Passwords do not match.")
        errors += _validate_password(password)
        if not terms:
            errors.append("You must accept the Terms & Conditions.")

        if User.query.filter_by(email=email).first():
            errors.append("That email address is already registered.")
        if User.query.filter_by(username=username).first():
            errors.append("That username is already taken.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html", form=request.form)

        # ── Create user ──
        user = User(
            full_name=full_name,
            username=username,
            email=email,
            phone=phone or None,
            avatar_color=random.choice(AVATAR_COLORS),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        flash("Account created! Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", form={})


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user  = User.query.filter_by(email=email).first()

        # Always show same message to prevent enumeration
        flash("If that email exists, a reset link has been sent.", "info")

        if user:
            token = secrets.token_urlsafe(48)
            expires = datetime.utcnow() + timedelta(
                minutes=app.config["PASSWORD_RESET_EXPIRY_MIN"]
            )
            db.session.add(PasswordResetToken(user_id=user.id, token=token, expires_at=expires))
            db.session.commit()

            reset_url = url_for("reset_password", token=token, _external=True)
            try:
                msg = Message(
                    "Reset your Nexus password",
                    recipients=[email],
                    html=render_template("email_reset.html", user=user, reset_url=reset_url),
                )
                mail.send(msg)
            except Exception:
                pass  # Silently fail if mail not configured

        return redirect(url_for("forgot_password"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    record = PasswordResetToken.query.filter_by(token=token).first()
    if not record or not record.is_valid():
        flash("Reset link is invalid or has expired.", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password   = request.form.get("password", "")
        confirm_pw = request.form.get("confirm_password", "")
        errors     = []

        if password != confirm_pw:
            errors.append("Passwords do not match.")
        errors += _validate_password(password)

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("reset_password.html", token=token)

        record.user.set_password(password)
        record.user.is_locked       = False
        record.user.failed_attempts = 0
        record.used = True
        db.session.commit()

        flash("Password reset successfully! You can now log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("You've been logged out. See you soon!", "info")
    return redirect(url_for("login"))


# ─────────────────────────────────────────────────────────────
#  Routes — App
# ─────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    user = User.query.get(session["user_id"])
    recent_logs = (LoginLog.query
                   .filter_by(user_id=user.id)
                   .order_by(LoginLog.created_at.desc())
                   .limit(5).all())
    total_users = User.query.count()
    return render_template("dashboard.html", user=user,
                           logs=recent_logs, total_users=total_users)


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = User.query.get(session["user_id"])

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_info":
            full_name = request.form.get("full_name", "").strip()
            phone     = request.form.get("phone", "").strip()
            if full_name:
                user.full_name = full_name
                session["user_name"] = full_name
            user.phone = phone or None
            db.session.commit()
            flash("Profile updated successfully.", "success")

        elif action == "change_password":
            current_pw = request.form.get("current_password", "")
            new_pw     = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if not user.check_password(current_pw):
                flash("Current password is incorrect.", "danger")
            elif new_pw != confirm_pw:
                flash("New passwords do not match.", "danger")
            else:
                errors = _validate_password(new_pw)
                if errors:
                    for e in errors:
                        flash(e, "danger")
                else:
                    user.set_password(new_pw)
                    db.session.commit()
                    flash("Password changed successfully.", "success")

        return redirect(url_for("profile"))

    return render_template("profile.html", user=user)


# ─────────────────────────────────────────────────────────────
#  API — Password strength checker (AJAX)
# ─────────────────────────────────────────────────────────────
@app.route("/api/password-strength", methods=["POST"])
def password_strength():
    pw = request.json.get("password", "")
    score = 0
    checks = {
        "length":    len(pw) >= 8,
        "uppercase": bool(re.search(r"[A-Z]", pw)),
        "number":    bool(re.search(r"[0-9]", pw)),
        "special":   bool(re.search(r"[!@#$%^&*(),.?\":{}|<>]", pw)),
        "long":      len(pw) >= 12,
    }
    score = sum(checks.values())
    labels = {0: "Too weak", 1: "Weak", 2: "Fair", 3: "Good", 4: "Strong", 5: "Very strong"}
    colors = {0: "#E24B4A", 1: "#E24B4A", 2: "#BA7517", 3: "#BA7517", 4: "#1D9E75", 5: "#1D9E75"}
    return jsonify(score=score, label=labels[score], color=colors[score], checks=checks)


# ─────────────────────────────────────────────────────────────
#  Bootstrap DB on first run
# ─────────────────────────────────────────────────────────────
@app.cli.command("init-db")
def init_db():
    db.create_all()
    print("✓  Database tables created.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True, port=5000)
