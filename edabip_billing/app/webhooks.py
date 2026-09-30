"""
Razorpay webhook receiver.

WHY THIS FILE MATTERS FOR "make no mistakes": card payments are
asynchronous - a charge can succeed or fail seconds (or, for bank
debits, days) after the API call that started it returns. Webhooks are
Razorpay's documented, reliable mechanism for finding out what actually
happened, and are the reason this module doesn't try to guess payment
outcomes from the synchronous response alone. Every mutating router
endpoint writes an optimistic local record; this file is what
reconciles that record against reality - most importantly for renewals
Razorpay bills automatically on its own subscription schedule, which
this backend never sees a synchronous response for at all.

Mount with a RAW body route (no JSON parsing middleware in front of it,
and no auth dependency - Razorpay can't send your app's JWT). Example:

    from fastapi import FastAPI
    from app.webhooks import router as webhook_router
    app.include_router(webhook_router)

Register the endpoint URL (https://yourdomain.com/api/billing/webhooks/razorpay)
in the Razorpay Dashboard -> Settings -> Webhooks, subscribed to at
least: subscription.charged, subscription.pending, subscription.halted,
subscription.cancelled, payment.failed, refund.processed.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import SessionLocal
from app.models import (
    Subscription, SubscriptionStatus, SubscriptionHistory, SubscriptionAction,
    Invoice, InvoiceStatus, Payment, PaymentStatus, BillingCustomer,
    BillingAlert, AlertType, AlertPriority, AlertStatus,
)
from app import razorpay_service
from app.razorpay_service import RazorpayServiceError

router = APIRouter(prefix="/api/billing/webhooks", tags=["billing-webhooks"])


def _now() -> datetime:
    """See app/router.py's _now() docstring: naive UTC on purpose, to
    match what MySQL/SQLite actually return on read."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _db() -> Session:
    return SessionLocal()


@router.post("/razorpay")
async def razorpay_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("x-razorpay-signature", "")

    try:
        razorpay_service.verify_webhook_signature(payload, sig_header)
    except RazorpayServiceError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")

    event = json.loads(payload)

    handler = _HANDLERS.get(event.get("event"))
    if handler:
        db = _db()
        try:
            handler(db, event.get("payload", {}))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return {"received": True}


def _find_subscription_by_razorpay_id(db: Session, razorpay_subscription_id: str) -> Subscription | None:
    return db.query(Subscription).filter(Subscription.razorpay_subscription_id == razorpay_subscription_id).first()


def _new_invoice_number(invoice: Invoice) -> str:
    """Same id-derived scheme as router.py's _create_invoice_and_payment -
    race-free because it's built from the row's own DB-assigned auto-
    increment id, not a count of existing rows. Caller must have already
    flushed `invoice` so `.id` is assigned."""
    return f"INV-{_now().year}-{invoice.id:05d}"


def _handle_subscription_charged(db: Session, payload: dict) -> None:
    """Fires when Razorpay's OWN billing schedule auto-charges a
    subscription for its next cycle - the normal way a subscription
    renews, as opposed to the explicit POST /subscription/renew button
    (which charges the card directly and writes its own invoice/payment
    rows). BR-SB-004 requires an invoice for every successful payment,
    so if this cycle wasn't already recorded, one is created here."""
    razorpay_sub = payload.get("subscription", {}).get("entity", {})
    razorpay_payment = payload.get("payment", {}).get("entity", {})
    sub = _find_subscription_by_razorpay_id(db, razorpay_sub.get("id"))
    if not sub:
        return

    invoice = db.query(Invoice).filter(Invoice.razorpay_invoice_id == razorpay_payment.get("invoice_id")).first()
    payment = db.query(Payment).filter(Payment.razorpay_payment_id == razorpay_payment.get("id")).first()

    if payment:
        payment.status = PaymentStatus.SUCCESSFUL
        payment.paid_at = _now()
        if invoice:
            invoice.status = InvoiceStatus.PAID
    else:
        period_start = (
            datetime.fromtimestamp(razorpay_sub["current_start"], tz=timezone.utc).replace(tzinfo=None)
            if razorpay_sub.get("current_start") else sub.current_period_start
        )
        period_end = (
            datetime.fromtimestamp(razorpay_sub["current_end"], tz=timezone.utc).replace(tzinfo=None)
            if razorpay_sub.get("current_end") else sub.current_period_end
        )
        invoice = Invoice(
            invoice_number=f"TMP-{razorpay_payment.get('id', '')[-20:]}",  # placeholder, replaced right after flush
            org_id=sub.org_id,
            subscription_id=sub.id,
            period_start=period_start,
            period_end=period_end,
            issue_date=_now(),
            due_date=period_start,
            amount_cents=razorpay_payment.get("amount", sub.plan.price_cents),
            currency=razorpay_payment.get("currency", "inr").lower(),
            status=InvoiceStatus.PAID,
        )
        db.add(invoice)
        db.flush()
        invoice.invoice_number = _new_invoice_number(invoice)

        customer = db.query(BillingCustomer).filter(BillingCustomer.org_id == sub.org_id).first()
        if customer and razorpay_payment.get("id"):
            hosted = razorpay_service.create_hosted_invoice(
                customer.razorpay_customer_id, razorpay_payment["id"], "Subscription renewal",
                razorpay_payment.get("amount", sub.plan.price_cents), razorpay_payment.get("currency", "inr"),
            )
            if hosted:
                invoice.razorpay_invoice_id = hosted.get("id")
                invoice.hosted_invoice_pdf_url = hosted.get("short_url")
        db.flush()

        db.add(Payment(
            transaction_id=f"TXN-{razorpay_payment.get('id', '')[-16:].upper()}",
            org_id=sub.org_id,
            invoice_id=invoice.id,
            payment_method_id=None,  # webhook payload doesn't reliably carry which saved card was used
            razorpay_payment_id=razorpay_payment.get("id"),
            amount_cents=razorpay_payment.get("amount", invoice.amount_cents),
            currency=razorpay_payment.get("currency", "inr").lower(),
            status=PaymentStatus.SUCCESSFUL,
            reference_number=f"REF-{razorpay_payment.get('id', '')[-14:].upper()}",
            paid_at=_now(),
        ))

        sub.current_period_start = period_start
        sub.current_period_end = period_end
        db.add(SubscriptionHistory(
            subscription_id=sub.id, action=SubscriptionAction.RENEW,
            previous_plan_id=sub.plan_id, new_plan_id=sub.plan_id, effective_date=_now(),
        ))

    sub.status = SubscriptionStatus.ACTIVE
    sub.past_due_since = None

    # A successful payment resolves any open PAYMENT_DUE / PAYMENT_FAILURE alerts.
    db.query(BillingAlert).filter(
        BillingAlert.org_id == sub.org_id, BillingAlert.status == AlertStatus.ACTIVE,
        BillingAlert.alert_type.in_([AlertType.PAYMENT_DUE, AlertType.PAYMENT_FAILURE]),
    ).update({"status": AlertStatus.DISMISSED, "resolved_at": _now()}, synchronize_session=False)

    log_action(db, org_id=sub.org_id, actor_user_id=0, action="webhook.subscription_charged",
               entity_type="invoice", entity_id=invoice.id if invoice else None)


def _handle_subscription_payment_failed(db: Session, payload: dict) -> None:
    """subscription.pending fires on the first missed charge (Razorpay
    is still retrying); subscription.halted fires once Razorpay gives up
    retrying - both land here since the local handling (grace period,
    alerts) is the same either way, just with a different end status."""
    razorpay_sub = payload.get("subscription", {}).get("entity", {})
    sub = _find_subscription_by_razorpay_id(db, razorpay_sub.get("id"))
    if not sub:
        return

    razorpay_payment = payload.get("payment", {}).get("entity", {})
    payment = db.query(Payment).filter(Payment.razorpay_payment_id == razorpay_payment.get("id")).first()
    if payment:
        payment.status = PaymentStatus.FAILED
        payment.failure_reason = "Payment could not be completed."

    invoice = db.query(Invoice).filter(Invoice.subscription_id == sub.id, Invoice.status == InvoiceStatus.PENDING).first()
    if invoice:
        invoice.status = InvoiceStatus.OVERDUE

    now = _now()
    if razorpay_sub.get("status") == "halted":
        # Razorpay has stopped retrying entirely - matches BR-SB-011's
        # "grace period exceeded" outcome regardless of how many days
        # have technically elapsed on our own grace_period_days clock.
        sub.status = SubscriptionStatus.SUSPENDED
    else:
        if sub.status != SubscriptionStatus.SUSPENDED:
            sub.status = SubscriptionStatus.PAST_DUE
        if sub.past_due_since is None:
            sub.past_due_since = now
        if sub.past_due_since and (now - sub.past_due_since).days >= (sub.grace_period_days or 0):
            sub.status = SubscriptionStatus.SUSPENDED

    db.add(BillingAlert(
        org_id=sub.org_id, alert_type=AlertType.PAYMENT_FAILURE, priority=AlertPriority.HIGH,
        status=AlertStatus.ACTIVE, title="Payment failed",
        message="Your last payment attempt failed. Please update your payment method to avoid service interruption.",
        related_entity_type="invoice", related_entity_id=invoice.id if invoice else None,
    ))
    log_action(db, org_id=sub.org_id, actor_user_id=0, action="webhook.subscription_payment_failed",
               entity_type="invoice", entity_id=invoice.id if invoice else None)


def _handle_subscription_cancelled(db: Session, payload: dict) -> None:
    razorpay_sub = payload.get("subscription", {}).get("entity", {})
    sub = _find_subscription_by_razorpay_id(db, razorpay_sub.get("id"))
    if not sub:
        return
    sub.status = SubscriptionStatus.CANCELED
    sub.canceled_at = _now()
    log_action(db, org_id=sub.org_id, actor_user_id=0, action="webhook.subscription_cancelled", entity_type="subscription", entity_id=sub.id)


def _handle_refund_processed(db: Session, payload: dict) -> None:
    razorpay_refund = payload.get("refund", {}).get("entity", {})
    razorpay_payment = payload.get("payment", {}).get("entity", {})
    payment = db.query(Payment).filter(Payment.razorpay_payment_id == razorpay_payment.get("id")).first()
    if not payment:
        return
    refund = Payment(
        transaction_id=f"TXN-RF-{razorpay_refund.get('id', '')[-12:].upper()}",
        org_id=payment.org_id,
        invoice_id=payment.invoice_id,
        payment_method_id=payment.payment_method_id,
        amount_cents=razorpay_refund.get("amount", payment.amount_cents),
        currency=payment.currency,
        status=PaymentStatus.REFUNDED,
        reference_number=f"REF-RF-{razorpay_refund.get('id', '')[-10:].upper()}",
        refunded_payment_id=payment.id,
        paid_at=_now(),
    )
    db.add(refund)
    log_action(db, org_id=payment.org_id, actor_user_id=0, action="webhook.refund_processed", entity_type="payment", entity_id=payment.id)


_HANDLERS = {
    "subscription.charged": _handle_subscription_charged,
    "subscription.pending": _handle_subscription_payment_failed,
    "subscription.halted": _handle_subscription_payment_failed,
    "subscription.cancelled": _handle_subscription_cancelled,
    "refund.processed": _handle_refund_processed,
}
