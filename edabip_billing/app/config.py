"""
Billing/Razorpay configuration.

Reads from environment variables so real keys never live in source
control. See ../.env.example for the full list.
"""
import os


class BillingSettings:
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET: str = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

    # Amount (in paise) charged - then immediately refunded - to verify
    # and tokenize a new card when there's no real purchase happening
    # yet (the "+ Add New Payment Method" button). 100 paise = INR 1,
    # which is Razorpay's minimum order amount for cards.
    RAZORPAY_CARD_VERIFICATION_AMOUNT: int = int(os.getenv("RAZORPAY_CARD_VERIFICATION_AMOUNT", "100"))

    # Grace period (BR-SB-011): days after a payment is overdue before we
    # suspend access. Product/business decision - adjust freely.
    DEFAULT_GRACE_PERIOD_DAYS: int = int(os.getenv("BILLING_GRACE_PERIOD_DAYS", "7"))

    # Storage/usage alert thresholds (BR-SB-005 / AC-SB-040).
    USAGE_WARNING_THRESHOLD_PCT: float = float(os.getenv("BILLING_USAGE_WARNING_PCT", "80"))
    USAGE_CRITICAL_THRESHOLD_PCT: float = float(os.getenv("BILLING_USAGE_CRITICAL_PCT", "95"))

    # Default currency. The wireframe spec (page 3) shows "Rs" pricing;
    # the actual frontend screenshot renders "$". Amounts are stored as
    # integer minor units (cents/paise) regardless of currency, so this
    # is safe to flip without a data migration - see README "Currency"
    # section before changing it in production. Razorpay settles
    # primarily in INR, so that's the sensible default now.
    DEFAULT_CURRENCY: str = os.getenv("BILLING_DEFAULT_CURRENCY", "inr")

    SUBSCRIPTION_EXPIRY_ALERT_DAYS: int = int(os.getenv("BILLING_EXPIRY_ALERT_DAYS", "5"))

    # Origins allowed to call this API from the browser. The React
    # frontend runs on Vite (localhost:5173 by default) which is a
    # different origin than this API, so without this the browser
    # blocks every request with a CORS error before it even reaches
    # FastAPI. Comma-separated list, e.g.
    # "http://localhost:5173,https://app.edabip.com"
    CORS_ORIGINS: list[str] = [
        origin.strip()
        for origin in os.getenv(
            "BILLING_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000",
        ).split(",")
        if origin.strip()
    ]


settings = BillingSettings()
