"""
Razorpay integration layer.

Why Razorpay: BR-SB-010 requires "payment processing... only through
approved and secure payment gateways that comply with applicable
security and compliance standards." Razorpay is PCI-DSS compliant and,
like the Stripe integration this replaces, the raw card never touches
this backend - Razorpay Checkout collects it in the browser and hands
this server back an order id, payment id and signature to verify.

This is the ONLY file that imports the `razorpay` package. Every other
file talks to `razorpay_service.*` functions, never to the SDK client
directly - that keeps the SDK version and error handling in one place.

How "saving a card" works here (Razorpay has no direct equivalent of
Stripe's zero-amount SetupIntent): we create a small verification order
for RAZORPAY_CARD_VERIFICATION_AMOUNT paise, the frontend completes it
with Razorpay Checkout using `save=1`, and once we've verified the
signature and pulled the resulting card token off the customer, we
immediately refund that verification charge. The card itself is never
billed for real until a plan is actually selected.

Recurring billing (subscription renewals) is handled by Razorpay's own
Subscriptions API once a plan is chosen - Razorpay charges the saved
token on its own schedule and lets us know what happened via webhook,
same as Stripe did.
"""
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import razorpay
from razorpay.errors import BadRequestError, GatewayError, ServerError, SignatureVerificationError

from app.config import settings

_client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


class RazorpayServiceError(Exception):
    """Raised for any Razorpay failure. `user_message` is safe to show to
    the end user (already mapped to the spec's error copy); `detail` is
    for server-side logs only."""

    def __init__(self, user_message: str, detail: str = ""):
        self.user_message = user_message
        self.detail = detail or user_message
        super().__init__(self.detail)


def _receipt(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _wrap(fn, *args, error_message: str, **kwargs):
    try:
        return fn(*args, **kwargs)
    except BadRequestError as e:  # bad params, declined card, etc.
        raise RazorpayServiceError("Payment could not be completed.", str(e)) from e
    except SignatureVerificationError as e:
        raise RazorpayServiceError("Invalid payment signature.", str(e)) from e
    except (GatewayError, ServerError) as e:
        raise RazorpayServiceError("Unable to communicate with the billing service.", str(e)) from e
    except Exception as e:  # network hiccups, timeouts, etc.
        raise RazorpayServiceError(error_message, str(e)) from e


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------

def create_customer(org_id: int, email: str, name: str) -> dict:
    # fail_existing="0" means: if a customer with this email already
    # exists on our Razorpay account, hand back the existing one instead
    # of erroring out - keeps this safe to call more than once for the
    # same org without creating duplicates.
    return _wrap(
        _client.customer.create,
        {
            "name": name,
            "email": email,
            "fail_existing": "0",
            "notes": {"org_id": str(org_id)},
        },
        error_message="Unable to set up billing for this account.",
    )


# --------------------------------------------------------------------------
# Payment methods - see module docstring for how card-saving works
# without an actual purchase happening.
# --------------------------------------------------------------------------

def create_card_verification_order(customer_id: str, org_id: int) -> dict:
    """Backs the '+ Add New Payment Method' button. Razorpay Checkout on
    the frontend opens against this order and, once the customer enters
    their card, hands back a payment id/signature we can verify and a
    token we can charge later for renewals."""
    return _wrap(
        _client.order.create,
        {
            "amount": settings.RAZORPAY_CARD_VERIFICATION_AMOUNT,
            "currency": settings.DEFAULT_CURRENCY.upper(),
            "receipt": _receipt(f"cardverify-{org_id}"),
            "payment_capture": 1,
            "notes": {"org_id": str(org_id), "purpose": "card_verification"},
        },
        error_message="Unable to start payment method setup.",
    )


@dataclass
class CardDetails:
    razorpay_token_id: str
    brand: str
    last4: str
    exp_month: int
    exp_year: int


def verify_and_save_card(customer_id: str, order_id: str, payment_id: str, signature: str) -> CardDetails:
    """Confirms the verification payment really came from Razorpay (not
    someone replaying an old payment id), pulls the freshly-created card
    token off the customer, then refunds the small verification charge
    so the customer isn't actually out any money for just adding a card."""
    try:
        _client.utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
    except SignatureVerificationError as e:
        raise RazorpayServiceError("Selected payment method is invalid.", str(e)) from e

    payment = _wrap(_client.payment.fetch, payment_id, error_message="Selected payment method is invalid.")
    if payment.get("order_id") != order_id:
        raise RazorpayServiceError("Selected payment method is invalid.", "order_id mismatch on payment")

    tokens = _wrap(_client.token.all, customer_id, error_message="Unable to save payment method.")
    items = tokens.get("items", [])
    if not items:
        raise RazorpayServiceError("Unable to save payment method.", "no token found for customer after payment")
    # Razorpay returns newest-first, but sort defensively rather than
    # assume - the token from THIS payment is the one with the highest
    # created_at.
    token = max(items, key=lambda t: t.get("created_at", 0))
    card = token.get("card") or {}

    try:
        _client.payment.refund(payment_id, {"amount": settings.RAZORPAY_CARD_VERIFICATION_AMOUNT})
    except Exception:
        pass  # card is saved either way; the refund can be retried/reconciled manually if it ever fails

    return CardDetails(
        razorpay_token_id=token["id"],
        brand=(card.get("network") or "card").lower(),
        last4=card.get("last4", "0000"),
        exp_month=card.get("expiry_month") or 1,
        exp_year=card.get("expiry_year") or (time.localtime().tm_year + 1),
    )


def detach_payment_method(customer_id: str, token_id: str) -> None:
    _wrap(_client.token.delete, customer_id, token_id, error_message="Unable to remove payment method.")


# --------------------------------------------------------------------------
# Plans & Subscriptions
# --------------------------------------------------------------------------

def get_or_create_plan(razorpay_plan_id: Optional[str], name: str, amount_cents: int, currency: str, billing_cycle: str) -> str:
    """Plan.razorpay_plan_id is cached on the row after the first call so
    we never create duplicate Razorpay plans for the same internal plan."""
    if razorpay_plan_id:
        return razorpay_plan_id
    period = "monthly" if billing_cycle == "monthly" else "yearly"
    plan = _wrap(
        _client.plan.create,
        {
            "period": period,
            "interval": 1,
            "item": {"name": name, "amount": amount_cents, "currency": currency.upper()},
        },
        error_message="Unable to set up subscription plan.",
    )
    return plan["id"]


def _total_count_for_cycle(billing_cycle: str) -> int:
    # Razorpay subscriptions need a bounded number of cycles up front
    # (no "run forever" option) - 10 years' worth is effectively
    # indefinite for how this product is used, and select_plan()/
    # renew_subscription() re-create a fresh subscription on
    # upgrade/downgrade anyway.
    return 120 if billing_cycle == "monthly" else 10


def create_subscription(customer_id: str, razorpay_plan_id: str, billing_cycle: str, org_id: int) -> dict:
    return _wrap(
        _client.subscription.create,
        {
            "plan_id": razorpay_plan_id,
            "customer_id": customer_id,
            "total_count": _total_count_for_cycle(billing_cycle),
            "customer_notify": 1,
            "notes": {"org_id": str(org_id)},
        },
        error_message="Unable to activate subscription.",
    )


def fetch_subscription(subscription_id: str) -> dict:
    return _wrap(_client.subscription.fetch, subscription_id, error_message="Unable to retrieve subscription.")


def cancel_subscription(subscription_id: str, at_period_end: bool = True) -> dict:
    return _wrap(
        _client.subscription.cancel,
        subscription_id,
        {"cancel_at_cycle_end": 1 if at_period_end else 0},
        error_message="Unable to cancel subscription.",
    )


def charge_saved_card_now(customer_id: str, token_id: str, amount_cents: int, currency: str, email: str, org_id: int) -> dict:
    """Backs the manual 'Renew Subscription' button - charges the org's
    saved card token directly, right now, instead of waiting for
    Razorpay's own billing schedule to fire. Returns the payment dict on
    success (raises RazorpayServiceError otherwise)."""
    order = _wrap(
        _client.order.create,
        {
            "amount": amount_cents,
            "currency": currency.upper(),
            "receipt": _receipt(f"renew-{org_id}"),
            "payment_capture": 1,
            "notes": {"org_id": str(org_id), "purpose": "manual_renewal"},
        },
        error_message="Unable to renew subscription.",
    )
    return _wrap(
        _client.payment.createRecurring,
        {
            "email": email,
            "amount": amount_cents,
            "currency": currency.upper(),
            "order_id": order["id"],
            "customer_id": customer_id,
            "token": token_id,
            "recurring": "1",
            "description": "Subscription renewal",
        },
        error_message="Payment could not be processed.",
    )


# --------------------------------------------------------------------------
# Invoices - Razorpay's Invoice API gives us a real hosted page/PDF to
# link the "Download Invoice" button at, same role hosted_invoice_pdf
# played for Stripe. Best-effort: a failure here should never block the
# payment itself from going through, so callers treat this as optional.
# --------------------------------------------------------------------------

def create_hosted_invoice(customer_id: str, payment_id: str, description: str, amount_cents: int, currency: str) -> Optional[dict]:
    try:
        return _client.invoice.create({
            "type": "invoice",
            "customer_id": customer_id,
            "line_items": [{"name": description, "amount": amount_cents, "currency": currency.upper()}],
        })
    except Exception:
        return None


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------

def verify_webhook_signature(payload: bytes, signature: str) -> None:
    try:
        _client.utility.verify_webhook_signature(payload.decode("utf-8"), signature, settings.RAZORPAY_WEBHOOK_SECRET)
    except SignatureVerificationError as e:
        raise RazorpayServiceError("Invalid webhook signature.", str(e)) from e
