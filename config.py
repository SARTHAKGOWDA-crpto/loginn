import os
from datetime import timedelta

class Config:
    # ── Flask ──────────────────────────────────────────────────
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production-use-secrets-module")
    DEBUG      = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    # ── MySQL (via PyMySQL) ────────────────────────────────────
    DB_HOST     = os.environ.get("DB_HOST",     "localhost")
    DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
    DB_USER     = os.environ.get("DB_USER",     "root")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "12345")
    DB_NAME     = os.environ.get("DB_NAME",     "nexus_auth")

    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
        "?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Sessions & Security ────────────────────────────────────
    SESSION_COOKIE_HTTPONLY  = True
    SESSION_COOKIE_SAMESITE  = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    MAX_FAILED_ATTEMPTS        = 5          # lock after N bad logins
    PASSWORD_RESET_EXPIRY_MIN  = 30         # token lifetime in minutes

    # ── Mail (optional – configure for real email) ─────────────
    MAIL_SERVER   = os.environ.get("MAIL_SERVER",   "smtp.gmail.com")
    MAIL_PORT     = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS  = True
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "noreply@nexus.dev")
