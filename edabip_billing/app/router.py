"""
FastAPI routes for the Subscription & Billing screen.

INTEGRATION: mount this in your main app with:
    from app.billing.router import router as billing_router
    app.include_router(billing_router)

Every endpoint below is annotated with the wireframe-spec component and
acceptance-criteria IDs it satisfies, so you can trace behavior back to
the requirements doc.
"""
import csv
import io
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.audit import log_action
from app.database import get_db
from app.models import (
    Plan, BillingCustomer, Subscription, SubscriptionHistory, SubscriptionAction, SubscriptionStatus,
    PaymentMethod, Invoice, InvoiceStatus, Payment, PaymentStatus,
    UsageRecord, BillingAlert, AlertType, AlertPriority, AlertStatus,
)
from app.permissions import require_view, require_manage, require_export, User
from app.schemas import (
    PlanOut, PlanListOut, PlanSelectIn, PlanChangeResultOut,
    BillingOverviewOut, UsageMetricOut, UsageOverviewOut, StorageUsageOut,
    InvoiceOut, InvoiceListOut, InvoiceDetailOut,
    BillingHistoryOut, MonthlyTotalOut,
    PaymentMethodOut, PaymentVerificationOrderOut, AttachPaymentMethodIn, PaymentMethodActionOut,
    PaymentOut, PaymentHistoryOut,
    AlertOut, AlertListOut, AlertUpdateIn,
    MessageOut, SearchResultOut,
)
from app.config import settings
from app import razorpay_service
from app.razorpay_service import RazorpayServiceError

router = APIRouter(prefix="/api/billing", tags=["billing"])


# ==========================================================================
# Shared helpers
# ==========================================================================

def _now() -> datetime:
    """Naive UTC 'now', deliberately not timezone-aware: MySQL/SQLite
    both strip tzinfo on read via SQLAlchemy's generic DateTime type (see
    README "Timezone handling"), so mixing aware `now()` values against
    values just read back from the DB raises TypeError on comparison.
    Uses the non-deprecated datetime.now(timezone.utc) internally, then
    drops tzinfo so it's comparable with everything the DB hands back."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cents_to_amount(cents: int) -> float:
    return round(cents / 100, 2)


def _enum_value(x) -> str:
    """Extracts the plain string from a (str, Enum) member, e.g.
    InvoiceStatus.PAID -> "paid". Deliberately does NOT use
    `isinstance(x, str)` to detect "already a plain string" - every
    enum in models.py subclasses str (`class X(str, enum.Enum)`), so
    isinstance(member, str) is always True and that check would never
    take the `.value` branch. It happens to be harmless today because
    both Pydantic and csv.writer read the raw string payload off these
    objects directly rather than calling `__str__` - but an f-string or
    a raw `str(x)` on the same value prints "InvoiceStatus.PAID", not
    "paid" (verified: `Enum.__str__` wins over `str.__str__` in the
    MRO). Checking for `.value` instead is correct for both an enum
    member and an already-plain string (which has no `.value`)."""
    return x.value if hasattr(x, "value") else x


def _get_subscription_or_404(db: Session, org_id: int) -> Subscription:
    sub = (
        db.query(Subscription)
        .filter(Subscription.org_id == org_id, Subscription.status != SubscriptionStatus.CANCELED)
        .order_by(Subscription.created_at.desc())
        .first()
    )
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No active subscription found.")
    return sub


def _plan_to_out(plan: Plan, current_plan_id: Optional[int]) -> PlanOut:
    return PlanOut(
        code=plan.code,
        name=plan.name,
        description=plan.description,
        price=_cents_to_amount(plan.price_cents),
        currency=plan.currency,
        billing_cycle=plan.billing_cycle,
        features=_feature_list(plan),
        is_current=(plan.id == current_plan_id),
        featured=plan.is_featured,
    )


def _feature_list(plan: Plan) -> List[str]:
    storage = (
        f"{plan.max_storage_gb / 1024:.0f} TB Storage"
        if plan.max_storage_gb >= 1024
        else f"{plan.max_storage_gb} GB Storage"
    )
    return [
        f"Up to {plan.max_users} Users",
        storage,
        f"{plan.max_reports_per_month:,} Reports / Month",
        plan.support_level,
    ]


def _format_period_label(start: datetime, end: datetime) -> str:
    if start.month == end.month and start.year == end.year:
        return f"{start:%b} {start.day} - {end:%b} {end.day}, {end.year}"
    return f"{start:%b} {start.day}, {start.year} - {end:%b} {end.day}, {end.year}"


def _invoice_to_out(inv: Invoice) -> InvoiceOut:
    return InvoiceOut(
        invoice_number=inv.invoice_number,
        date=inv.issue_date,
        period_start=inv.period_start,
        period_end=inv.period_end,
        period_label=_format_period_label(inv.period_start, inv.period_end),
        due_date=inv.due_date,
        amount=_cents_to_amount(inv.amount_cents),
        status=_enum_value(inv.status),
        download_available=inv.status != InvoiceStatus.VOID,
    )


def _next_transaction_id() -> str:
    return f"TXN-{uuid.uuid4().hex[:12].upper()}"


ALLOWED_SEARCH_CHARS = re.compile(r"^[a-zA-Z0-9 @.\-_]*$")


def _sanitize_search_term(q: Optional[str]) -> Optional[str]:
    """Field-Level Validation (spec page 5): trim, 2-100 chars,
    'Special Characters Not Allowed except @ . - _'. SQLAlchemy already
    parameterizes queries (no injection risk either way), this just
    enforces the spec's own character allow-list."""
    if q is None:
        return None
    q = q.strip()
    if not q:
        return None
    if len(q) < 2 or len(q) > 100:
        raise HTTPException(status_code=400, detail="Search must be between 2 and 100 characters.")
    if not ALLOWED_SEARCH_CHARS.match(q):
        raise HTTPException(status_code=400, detail="Search contains unsupported characters.")
    return q


def _paginate(total: int, page: int, page_size: int):
    total_pages = max(math.ceil(total / page_size), 1)
    page = min(max(page, 1), total_pages)
    offset = (page - 1) * page_size
    return page, offset, total_pages


# ==========================================================================
# Search  (AC-SB-001 .. AC-SB-006)
# ==========================================================================

@router.get("/search", response_model=SearchResultOut)
def search_billing(
    q: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_view),
):
    query = _sanitize_search_term(q)
    if not query:
        return SearchResultOut(invoices=[], plans=[], query=q or "")

    invoices = (
        db.query(Invoice)
        .filter(Invoice.org_id == user.org_id, Invoice.invoice_number.ilike(f"%{query}%"))
        .order_by(Invoice.issue_date.desc())
        .limit(25)
        .all()
    )
    plans = db.query(Plan).filter(Plan.is_active.is_(True), Plan.name.ilike(f"%{query}%")).all()
    sub = db.query(Subscription).filter(Subscription.org_id == user.org_id).first()
    current_plan_id = sub.plan_id if sub else None

    return SearchResultOut(
        invoices=[_invoice_to_out(i) for i in invoices],
        plans=[_plan_to_out(p, current_plan_id) for p in plans],
        query=query,
    )


# ==========================================================================
# Overview  (the 4 header cards) + Refresh
# ==========================================================================

def _build_overview(db: Session, org_id: int) -> BillingOverviewOut:
    sub = _get_subscription_or_404(db, org_id)
    plan = sub.plan

    due_invoice = (
        db.query(Invoice)
        .filter(
            Invoice.org_id == org_id,
            Invoice.status.in_([InvoiceStatus.PENDING, InvoiceStatus.OVERDUE]),
        )
        .order_by(Invoice.due_date.asc())
        .first()
    )
    last_payment = (
        db.query(Payment)
        .filter(Payment.org_id == org_id, Payment.status == PaymentStatus.SUCCESSFUL)
        .order_by(Payment.paid_at.desc())
        .first()
    )

    return BillingOverviewOut(
        plan_name=plan.name,
        billing_cycle_label=f"Billed {plan.billing_cycle.value.capitalize() if hasattr(plan.billing_cycle, 'value') else str(plan.billing_cycle).capitalize()}",
        period_start=sub.current_period_start,
        period_end=sub.current_period_end,
        days_remaining=max((sub.current_period_end - _now()).days, 0),
        amount_due=_cents_to_amount(due_invoice.amount_cents) if due_invoice else 0.0,
        amount_due_date=due_invoice.due_date if due_invoice else None,
        amount_due_label=(f"Due on {due_invoice.due_date:%b %d, %Y}" if due_invoice else "No payment due"),
        last_payment_amount=_cents_to_amount(last_payment.amount_cents) if last_payment else None,
        last_payment_date=last_payment.paid_at if last_payment else None,
        subscription_status=_enum_value(sub.status),
        last_refreshed=_now(),
    )


@router.get("/overview", response_model=BillingOverviewOut)
def get_overview(db: Session = Depends(get_db), user: User = Depends(require_view)):
    return _build_overview(db, user.org_id)


@router.post("/refresh", response_model=BillingOverviewOut)
def refresh_billing(db: Session = Depends(get_db), user: User = Depends(require_view)):
    """'Refresh' button - re-syncs subscription status from Razorpay
    (source of truth) before recomputing the overview, so a webhook we
    may have missed doesn't leave the UI stale."""
    sub = _get_subscription_or_404(db, user.org_id)
    if sub.razorpay_subscription_id:
        try:
            remote = razorpay_service.fetch_subscription(sub.razorpay_subscription_id)
            if remote.get("current_start"):
                sub.current_period_start = datetime.fromtimestamp(remote["current_start"], tz=timezone.utc).replace(tzinfo=None)
            if remote.get("current_end"):
                sub.current_period_end = datetime.fromtimestamp(remote["current_end"], tz=timezone.utc).replace(tzinfo=None)
            status_map = {
                "active": SubscriptionStatus.ACTIVE,
                "authenticated": SubscriptionStatus.ACTIVE,
                "pending": SubscriptionStatus.PAST_DUE,
                "halted": SubscriptionStatus.SUSPENDED,
                "cancelled": SubscriptionStatus.CANCELED,
                "completed": SubscriptionStatus.CANCELED,
            }
            sub.status = status_map.get(remote.get("status"), sub.status)
            db.commit()
        except RazorpayServiceError:
            db.rollback()  # stale local data is better than a 500 on a refresh click
    return _build_overview(db, user.org_id)


# ==========================================================================
# Usage Analytics  (AC-SB-013 .. AC-SB-020)  +  Storage Usage Widget
# ==========================================================================

def _ensure_usage_record(db: Session, org_id: int, period_start: datetime, period_end: datetime) -> UsageRecord:
    """Get-or-create the usage row for a billing period. Called eagerly
    whenever a subscription is created or renewed (so whatever part of
    the app increments usage counters always has a row to update), and
    defensively from the usage/storage GET endpoints as a fallback."""
    record = db.query(UsageRecord).filter(UsageRecord.org_id == org_id, UsageRecord.period_start == period_start).first()
    if not record:
        record = UsageRecord(org_id=org_id, period_start=period_start, period_end=period_end)
        db.add(record)
        db.flush()
    return record


def _current_usage_record(db: Session, org_id: int, sub: Subscription) -> UsageRecord:
    record = _ensure_usage_record(db, org_id, sub.current_period_start, sub.current_period_end)
    db.commit()
    db.refresh(record)
    return record


def _pct(used: float, limit: float) -> float:
    if limit <= 0:
        return 0.0
    return round(min(used / limit, 1.0) * 100, 1)


@router.get("/usage", response_model=UsageOverviewOut)
def get_usage(db: Session = Depends(get_db), user: User = Depends(require_view)):
    sub = _get_subscription_or_404(db, user.org_id)
    plan = sub.plan
    usage = _current_usage_record(db, user.org_id, sub)

    storage_tb_mode = plan.max_storage_gb >= 1024
    data_tb_mode = plan.max_data_processing_gb >= 1024

    metrics = [
        UsageMetricOut(
            key="active_users", label="Active Users",
            used=usage.active_users, limit=plan.max_users, unit="",
            percentage=_pct(usage.active_users, plan.max_users),
            display_used=f"{usage.active_users}", display_limit=f"{plan.max_users} Users",
        ),
        UsageMetricOut(
            key="storage", label="Storage Usage",
            used=usage.storage_used_gb, limit=plan.max_storage_gb,
            unit="TB" if storage_tb_mode else "GB",
            percentage=_pct(usage.storage_used_gb, plan.max_storage_gb),
            display_used=(f"{usage.storage_used_gb/1024:.1f} TB" if storage_tb_mode else f"{usage.storage_used_gb:.0f} GB"),
            display_limit=(f"{plan.max_storage_gb/1024:.0f} TB" if storage_tb_mode else f"{plan.max_storage_gb} GB"),
        ),
        UsageMetricOut(
            key="reports", label="Reports Generated",
            used=usage.reports_generated, limit=plan.max_reports_per_month, unit="Reports",
            percentage=_pct(usage.reports_generated, plan.max_reports_per_month),
            display_used=f"{usage.reports_generated:,}", display_limit=f"{plan.max_reports_per_month:,} Reports",
        ),
        UsageMetricOut(
            key="dashboards", label="Active Dashboards",
            used=usage.active_dashboards, limit=plan.max_dashboards, unit="",
            percentage=_pct(usage.active_dashboards, plan.max_dashboards),
            display_used=f"{usage.active_dashboards}", display_limit=f"{plan.max_dashboards} Dashboards",
        ),
        UsageMetricOut(
            key="data_processing", label="Data Processing",
            used=usage.data_processed_gb, limit=plan.max_data_processing_gb,
            unit="TB" if data_tb_mode else "GB",
            percentage=_pct(usage.data_processed_gb, plan.max_data_processing_gb),
            display_used=(f"{usage.data_processed_gb/1024:.1f} TB" if data_tb_mode else f"{usage.data_processed_gb:.0f} GB"),
            display_limit=(f"{plan.max_data_processing_gb/1024:.0f} TB" if data_tb_mode else f"{plan.max_data_processing_gb} GB"),
        ),
    ]
    return UsageOverviewOut(period_start=sub.current_period_start, period_end=sub.current_period_end, metrics=metrics)


@router.get("/storage", response_model=StorageUsageOut)
def get_storage_usage(db: Session = Depends(get_db), user: User = Depends(require_view)):
    sub = _get_subscription_or_404(db, user.org_id)
    plan = sub.plan
    usage = _current_usage_record(db, user.org_id, sub)
    pct = _pct(usage.storage_used_gb, plan.max_storage_gb)
    if pct >= settings.USAGE_CRITICAL_THRESHOLD_PCT:
        state = "critical"
    elif pct >= settings.USAGE_WARNING_THRESHOLD_PCT:
        state = "warning"
    else:
        state = "normal"
    return StorageUsageOut(
        total_gb=plan.max_storage_gb,
        used_gb=usage.storage_used_gb,
        available_gb=max(plan.max_storage_gb - usage.storage_used_gb, 0),
        percentage=pct,
        warning_threshold_pct=settings.USAGE_WARNING_THRESHOLD_PCT,
        critical_threshold_pct=settings.USAGE_CRITICAL_THRESHOLD_PCT,
        status=state,
    )


# ==========================================================================
# Invoice Table  (AC-SB-021 .. AC-SB-028)
# ==========================================================================

@router.get("/invoices", response_model=InvoiceListOut)
def list_invoices(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_view),
):
    q = db.query(Invoice).filter(Invoice.org_id == user.org_id)
    if status_filter:
        q = q.filter(Invoice.status == status_filter)
    term = _sanitize_search_term(search)
    if term:
        q = q.filter(Invoice.invoice_number.ilike(f"%{term}%"))

    total = q.count()
    page, offset, total_pages = _paginate(total, page, page_size)
    # Business Rule (page 10): "Latest invoices displayed first."
    rows = q.order_by(Invoice.issue_date.desc()).offset(offset).limit(page_size).all()

    return InvoiceListOut(
        items=[_invoice_to_out(i) for i in rows],
        page=page, page_size=page_size, total=total, total_pages=total_pages,
    )


@router.get("/invoices/{invoice_number}", response_model=InvoiceDetailOut)
def get_invoice(invoice_number: str, db: Session = Depends(get_db), user: User = Depends(require_view)):
    inv = db.query(Invoice).filter(Invoice.org_id == user.org_id, Invoice.invoice_number == invoice_number).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Selected invoice is unavailable.")
    base = _invoice_to_out(inv)
    return InvoiceDetailOut(**base.model_dump(), hosted_invoice_pdf_url=inv.hosted_invoice_pdf_url)


@router.get("/invoices/{invoice_number}/download")
def download_invoice(invoice_number: str, db: Session = Depends(get_db), user: User = Depends(require_view)):
    inv = db.query(Invoice).filter(Invoice.org_id == user.org_id, Invoice.invoice_number == invoice_number).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Selected invoice is unavailable.")
    if not inv.hosted_invoice_pdf_url:
        raise HTTPException(status_code=409, detail="Unable to download invoice.")
    # Razorpay hosts the actual invoice page (AC-SB-025: "shall match the
    # invoice displayed in the system" - true by construction, since it's
    # generated from the same payment this invoice record is built from).
    return RedirectResponse(inv.hosted_invoice_pdf_url)


# ==========================================================================
# Billing History  (card + bar chart)
# ==========================================================================

@router.get("/history", response_model=BillingHistoryOut)
def get_billing_history(
    months: int = Query(6, ge=1, le=24),
    db: Session = Depends(get_db),
    user: User = Depends(require_view),
):
    total_paid_cents = (
        db.query(Payment)
        .filter(Payment.org_id == user.org_id, Payment.status == PaymentStatus.SUCCESSFUL)
        .with_entities(Payment.amount_cents)
        .all()
    )
    total_paid = _cents_to_amount(sum(c for (c,) in total_paid_cents))

    total_invoices = db.query(Invoice).filter(Invoice.org_id == user.org_id).count()

    outstanding_cents = (
        db.query(Invoice)
        .filter(Invoice.org_id == user.org_id, Invoice.status.in_([InvoiceStatus.PENDING, InvoiceStatus.OVERDUE]))
        .with_entities(Invoice.amount_cents)
        .all()
    )
    outstanding = _cents_to_amount(sum(c for (c,) in outstanding_cents))

    # Monthly totals for the bar chart, oldest -> newest.
    monthly: List[MonthlyTotalOut] = []
    anchor = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for i in range(months - 1, -1, -1):
        # Step back i whole months from `anchor`, wrapping the year.
        y, m = anchor.year, anchor.month - i
        while m <= 0:
            m += 12
            y -= 1
        month_start = anchor.replace(year=y, month=m, day=1)
        next_m = m + 1
        next_y = y
        if next_m > 12:
            next_m = 1
            next_y += 1
        month_end = anchor.replace(year=next_y, month=next_m, day=1)

        paid = (
            db.query(Payment)
            .filter(
                Payment.org_id == user.org_id,
                Payment.status == PaymentStatus.SUCCESSFUL,
                Payment.paid_at >= month_start,
                Payment.paid_at < month_end,
            )
            .with_entities(Payment.amount_cents)
            .all()
        )
        monthly.append(MonthlyTotalOut(month=f"{month_start:%b}", year=y, amount=_cents_to_amount(sum(c for (c,) in paid))))

    return BillingHistoryOut(
        total_paid=total_paid, total_invoices=total_invoices, outstanding_amount=outstanding, monthly=monthly,
    )


# ==========================================================================
# Payment Method
# ==========================================================================

def _payment_method_to_out(pm: PaymentMethod) -> PaymentMethodOut:
    return PaymentMethodOut(
        id=pm.id, brand=pm.brand, last4=pm.last4, exp_month=pm.exp_month, exp_year=pm.exp_year,
        is_primary=pm.is_primary,
        display_label=f"{pm.brand.capitalize()} ending in {pm.last4}",
        expires_label=f"Expires {pm.exp_month:02d}/{pm.exp_year}",
    )


def _ensure_razorpay_customer(db: Session, org_id: int, user: User) -> str:
    """Org-scoped, NOT subscription-scoped - a customer (and their saved
    cards) can exist before any plan is chosen. Reused everywhere a
    Razorpay customer id is needed so we never create duplicate Razorpay
    customers for the same org."""
    record = db.query(BillingCustomer).filter(BillingCustomer.org_id == org_id).first()
    if record:
        return record.razorpay_customer_id
    customer = razorpay_service.create_customer(
        org_id=org_id,
        email=getattr(user, "email", f"org-{org_id}@example.com"),
        name=getattr(user, "name", f"Org {org_id}"),
    )
    db.add(BillingCustomer(org_id=org_id, razorpay_customer_id=customer["id"]))
    db.commit()
    return customer["id"]


@router.get("/payment-methods", response_model=List[PaymentMethodOut])
def list_payment_methods(db: Session = Depends(get_db), user: User = Depends(require_view)):
    rows = (
        db.query(PaymentMethod)
        .filter(PaymentMethod.org_id == user.org_id)
        .order_by(PaymentMethod.is_primary.desc(), PaymentMethod.created_at.desc())
        .all()
    )
    return [_payment_method_to_out(pm) for pm in rows]


@router.post("/payment-methods/setup-intent", response_model=PaymentVerificationOrderOut)
def create_payment_method_setup_intent(
    db: Session = Depends(get_db), user: User = Depends(require_manage),
):
    """Backs the '+ Add New Payment Method' button. The frontend opens
    Razorpay Checkout against the returned order_id to collect the card
    directly with Razorpay - it never touches this server. Works even
    before the org has a subscription (add a card, then pick a plan)."""
    customer_id = _ensure_razorpay_customer(db, user.org_id, user)
    try:
        order = razorpay_service.create_card_verification_order(customer_id, user.org_id)
    except RazorpayServiceError as e:
        raise HTTPException(status_code=502, detail=e.user_message)
    return PaymentVerificationOrderOut(
        order_id=order["id"], key_id=settings.RAZORPAY_KEY_ID,
        amount=order["amount"], currency=order["currency"],
    )


@router.post("/payment-methods", response_model=PaymentMethodActionOut, status_code=201)
def attach_payment_method(
    payload: AttachPaymentMethodIn, db: Session = Depends(get_db), user: User = Depends(require_manage),
):
    customer_id = _ensure_razorpay_customer(db, user.org_id, user)
    try:
        card = razorpay_service.verify_and_save_card(
            customer_id, payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature,
        )
    except RazorpayServiceError as e:
        raise HTTPException(status_code=422, detail=e.user_message)

    is_first = db.query(PaymentMethod).filter(PaymentMethod.org_id == user.org_id).count() == 0
    make_primary = payload.set_primary or is_first

    if make_primary:
        # Razorpay has no server-side concept of a customer's "default
        # payment method" the way Stripe does - which card is primary is
        # purely something we track ourselves and pass explicitly
        # whenever we charge a saved card for a renewal.
        db.query(PaymentMethod).filter(PaymentMethod.org_id == user.org_id).update({"is_primary": False})

    pm = PaymentMethod(
        org_id=user.org_id,
        razorpay_token_id=card.razorpay_token_id,
        brand=card.brand, last4=card.last4, exp_month=card.exp_month, exp_year=card.exp_year,
        is_primary=make_primary,
    )
    db.add(pm)
    log_action(db, org_id=user.org_id, actor_user_id=user.id, action="payment_method.add",
               entity_type="payment_method", details={"brand": card.brand, "last4": card.last4})
    db.commit()

    rows = db.query(PaymentMethod).filter(PaymentMethod.org_id == user.org_id).order_by(PaymentMethod.is_primary.desc()).all()
    return PaymentMethodActionOut(payment_methods=[_payment_method_to_out(p) for p in rows], message="Payment method added.")


@router.patch("/payment-methods/{payment_method_id}/primary", response_model=PaymentMethodActionOut)
def set_primary_payment_method(
    payment_method_id: int, db: Session = Depends(get_db), user: User = Depends(require_manage),
):
    pm = db.query(PaymentMethod).filter(PaymentMethod.id == payment_method_id, PaymentMethod.org_id == user.org_id).first()
    if not pm:
        raise HTTPException(status_code=404, detail="Selected payment method is invalid.")

    # No remote call needed here - see the note in attach_payment_method
    # about "primary" being a local-only concept for Razorpay.
    db.query(PaymentMethod).filter(PaymentMethod.org_id == user.org_id).update({"is_primary": False})
    pm.is_primary = True
    log_action(db, org_id=user.org_id, actor_user_id=user.id, action="payment_method.set_primary",
               entity_type="payment_method", entity_id=pm.id)
    db.commit()

    rows = db.query(PaymentMethod).filter(PaymentMethod.org_id == user.org_id).order_by(PaymentMethod.is_primary.desc()).all()
    return PaymentMethodActionOut(payment_methods=[_payment_method_to_out(p) for p in rows], message="Primary payment method updated.")


@router.delete("/payment-methods/{payment_method_id}", response_model=PaymentMethodActionOut)
def remove_payment_method(
    payment_method_id: int, db: Session = Depends(get_db), user: User = Depends(require_manage),
):
    pm = db.query(PaymentMethod).filter(PaymentMethod.id == payment_method_id, PaymentMethod.org_id == user.org_id).first()
    if not pm:
        raise HTTPException(status_code=404, detail="Selected payment method is invalid.")

    customer = db.query(BillingCustomer).filter(BillingCustomer.org_id == user.org_id).first()
    if customer:
        try:
            razorpay_service.detach_payment_method(customer.razorpay_customer_id, pm.razorpay_token_id)
        except RazorpayServiceError as e:
            raise HTTPException(status_code=502, detail=e.user_message)

    was_primary = pm.is_primary
    db.delete(pm)
    db.flush()

    if was_primary:
        # Promote the next-oldest remaining method so there's always a
        # default on file for renewals, if any are left.
        successor = (
            db.query(PaymentMethod)
            .filter(PaymentMethod.org_id == user.org_id)
            .order_by(PaymentMethod.created_at.asc())
            .first()
        )
        if successor:
            successor.is_primary = True

    log_action(db, org_id=user.org_id, actor_user_id=user.id, action="payment_method.remove", entity_type="payment_method", entity_id=payment_method_id)
    db.commit()

    rows = db.query(PaymentMethod).filter(PaymentMethod.org_id == user.org_id).order_by(PaymentMethod.is_primary.desc()).all()
    return PaymentMethodActionOut(payment_methods=[_payment_method_to_out(p) for p in rows], message="Payment method removed.")


# ==========================================================================
# Payment History  (AC-SB-029 .. AC-SB-036)
# ==========================================================================

def _payment_to_out(p: Payment, invoice_number: Optional[str], pm_label: Optional[str]) -> PaymentOut:
    return PaymentOut(
        transaction_id=p.transaction_id,
        payment_date=p.paid_at,
        amount=_cents_to_amount(p.amount_cents),
        payment_method_label=pm_label,
        status=_enum_value(p.status),
        invoice_number=invoice_number,
        reference_number=p.reference_number,
        failure_reason=p.failure_reason,
    )


@router.get("/payment-history", response_model=PaymentHistoryOut)
def list_payment_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    status_filter: Optional[str] = Query(None, alias="status"),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_view),
):
    q = db.query(Payment).filter(Payment.org_id == user.org_id)
    if status_filter:
        q = q.filter(Payment.status == status_filter)
    term = _sanitize_search_term(search)
    if term:
        q = q.filter(or_(Payment.transaction_id.ilike(f"%{term}%"), Payment.reference_number.ilike(f"%{term}%")))

    total = q.count()
    page, offset, total_pages = _paginate(total, page, page_size)
    # "Payments listed in descending order" (page 11)
    rows = q.order_by(Payment.created_at.desc()).offset(offset).limit(page_size).all()

    items = []
    for p in rows:
        inv_number = p.invoice.invoice_number if p.invoice else None
        pm_label = f"{p.payment_method.brand.capitalize()} ending in {p.payment_method.last4}" if p.payment_method else None
        items.append(_payment_to_out(p, inv_number, pm_label))

    return PaymentHistoryOut(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


@router.get("/payment-history/export")
def export_payment_history(db: Session = Depends(get_db), user: User = Depends(require_export)):
    rows = db.query(Payment).filter(Payment.org_id == user.org_id).order_by(Payment.created_at.desc()).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Transaction ID", "Payment Date", "Amount", "Status", "Invoice Number", "Reference Number"])
    for p in rows:
        writer.writerow([
            p.transaction_id,
            p.paid_at.isoformat() if p.paid_at else "",
            f"{_cents_to_amount(p.amount_cents):.2f}",
            _enum_value(p.status),
            p.invoice.invoice_number if p.invoice else "",
            p.reference_number,
        ])
    buffer.seek(0)
    log_action(db, org_id=user.org_id, actor_user_id=user.id, action="payment_history.export", entity_type="payment")
    db.commit()
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=payment_history.csv"},
    )


# ==========================================================================
# Subscription Plans  (AC-SB-007 .. AC-SB-012, Upgrade/Renew buttons)
# ==========================================================================

@router.get("/plans", response_model=PlanListOut)
def list_plans(db: Session = Depends(get_db), user: User = Depends(require_view)):
    plans = db.query(Plan).filter(Plan.is_active.is_(True)).order_by(Plan.sort_order.asc()).all()
    sub = db.query(Subscription).filter(Subscription.org_id == user.org_id, Subscription.status != SubscriptionStatus.CANCELED).first()
    current_plan_id = sub.plan_id if sub else None
    return PlanListOut(plans=[_plan_to_out(p, current_plan_id) for p in plans])


def _create_invoice_and_payment(
    db: Session, *, org_id: int, subscription_id: int, plan: Plan,
    period_start: datetime, period_end: datetime, payment_method: Optional[PaymentMethod],
    razorpay_payment: Optional[dict] = None,
) -> tuple[Invoice, Payment]:
    """BR-SB-004: 'auto-generate an invoice after every successful
    payment, subscription activation, upgrade, or renewal.' Creates both
    the Invoice and its Payment row atomically."""
    # invoice_number is derived from the row's own auto-increment `id`
    # (assigned by the DB, guaranteed unique - see the flush-then-rename
    # below), NOT from counting existing rows. Counting has a race: two
    # concurrent requests can both count the same N and both try to
    # create "INV-2026-000N", and the second fails its unique
    # constraint. A temporary placeholder is needed for the first
    # flush only because invoice_number is NOT NULL + unique, so it
    # can't be left empty even for an instant.
    invoice = Invoice(
        invoice_number=f"TMP-{uuid.uuid4().hex[:20]}",
        org_id=org_id,
        subscription_id=subscription_id,
        period_start=period_start,
        period_end=period_end,
        issue_date=_now(),
        due_date=period_start,
        amount_cents=plan.price_cents,
        currency=plan.currency,
        status=InvoiceStatus.PAID,
    )
    db.add(invoice)
    db.flush()  # assigns invoice.id
    invoice.invoice_number = f"INV-{_now().year}-{invoice.id:05d}"

    # Best-effort: ask Razorpay for a real hosted invoice page so the
    # "Download Invoice" button has something real to link to. This
    # should never block the payment that already succeeded, so a
    # failure here just leaves hosted_invoice_pdf_url empty.
    if razorpay_payment:
        customer = db.query(BillingCustomer).filter(BillingCustomer.org_id == org_id).first()
        if customer:
            hosted = razorpay_service.create_hosted_invoice(
                customer.razorpay_customer_id, razorpay_payment["id"],
                f"{plan.name} plan ({period_start:%b %d} - {period_end:%b %d})",
                plan.price_cents, plan.currency,
            )
            if hosted:
                invoice.razorpay_invoice_id = hosted.get("id")
                invoice.hosted_invoice_pdf_url = hosted.get("short_url")
    db.flush()

    payment = Payment(
        transaction_id=_next_transaction_id(),
        org_id=org_id,
        invoice_id=invoice.id,
        payment_method_id=payment_method.id if payment_method else None,
        razorpay_payment_id=razorpay_payment.get("id") if razorpay_payment else None,
        amount_cents=plan.price_cents,
        currency=plan.currency,
        status=PaymentStatus.SUCCESSFUL,
        reference_number=f"REF-{uuid.uuid4().hex[:10].upper()}",
        paid_at=_now(),
    )
    db.add(payment)
    db.flush()
    return invoice, payment


def _primary_payment_method(db: Session, org_id: int) -> Optional[PaymentMethod]:
    return db.query(PaymentMethod).filter(PaymentMethod.org_id == org_id, PaymentMethod.is_primary.is_(True)).first()


def _charge_for_plan(db: Session, *, org_id: int, user_email: str, plan: Plan, payment_method: PaymentMethod) -> dict:
    """Actually moves money: charges the org's saved card token for one
    billing cycle of `plan`, right now. Used for brand-new subscriptions
    and plan changes alike, since Razorpay's own subscription schedule
    only covers cycles AFTER the one being started here."""
    customer = db.query(BillingCustomer).filter(BillingCustomer.org_id == org_id).first()
    if not customer:
        raise HTTPException(status_code=402, detail="Selected payment method is invalid.")
    try:
        return razorpay_service.charge_saved_card_now(
            customer.razorpay_customer_id, payment_method.razorpay_token_id,
            plan.price_cents, plan.currency, user_email, org_id,
        )
    except RazorpayServiceError as e:
        raise HTTPException(status_code=402, detail=e.user_message)


@router.post("/plans/{plan_code}/select", response_model=PlanChangeResultOut)
def select_plan(
    plan_code: str, payload: PlanSelectIn,
    db: Session = Depends(get_db), user: User = Depends(require_manage),
):
    """Handles the 'Select Plan' and 'Upgrade Plan' buttons. Field-Level
    Validation (page 6): Plan Selection Mandatory, Single Active Plan
    Allowed. Upgrade Plan Button spec (page 15): Permission = Billing
    Administrator/Account Owner, Payment Required = Yes, Confirmation =
    Required."""
    if not payload.confirmed:
        raise HTTPException(status_code=400, detail="Confirmation is required to change your subscription plan.")

    plan = db.query(Plan).filter(Plan.code == plan_code, Plan.is_active.is_(True)).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Selected subscription plan is unavailable.")

    payment_method = (
        db.query(PaymentMethod).filter(PaymentMethod.id == payload.payment_method_id, PaymentMethod.org_id == user.org_id).first()
        if payload.payment_method_id else _primary_payment_method(db, user.org_id)
    )
    if not payment_method:
        raise HTTPException(status_code=402, detail="Selected payment method is invalid.")

    sub = db.query(Subscription).filter(Subscription.org_id == user.org_id, Subscription.status != SubscriptionStatus.CANCELED).first()
    now = _now()
    user_email = getattr(user, "email", f"org-{user.org_id}@example.com")

    # ---- Case 1: no subscription yet -> create ----
    if not sub:
        customer_id = _ensure_razorpay_customer(db, user.org_id, user)
        razorpay_payment = _charge_for_plan(db, org_id=user.org_id, user_email=user_email, plan=plan, payment_method=payment_method)

        razorpay_sub_id = None
        try:
            plan.razorpay_plan_id = razorpay_service.get_or_create_plan(
                plan.razorpay_plan_id, plan.name, plan.price_cents, plan.currency, plan.billing_cycle,
            )
            razorpay_sub = razorpay_service.create_subscription(customer_id, plan.razorpay_plan_id, plan.billing_cycle, user.org_id)
            razorpay_sub_id = razorpay_sub["id"]
        except RazorpayServiceError:
            pass  # first cycle is already paid for above; auto-renewal setup can be retried later

        period_end = now + timedelta(days=30 if plan.billing_cycle == "monthly" else 365)
        sub = Subscription(
            org_id=user.org_id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE,
            razorpay_customer_id=customer_id, razorpay_subscription_id=razorpay_sub_id,
            current_period_start=now, current_period_end=period_end,
            grace_period_days=settings.DEFAULT_GRACE_PERIOD_DAYS,
        )
        db.add(sub)
        db.flush()

        invoice, payment = _create_invoice_and_payment(
            db, org_id=user.org_id, subscription_id=sub.id, plan=plan,
            period_start=now, period_end=period_end, payment_method=payment_method,
            razorpay_payment=razorpay_payment,
        )
        history = SubscriptionHistory(
            subscription_id=sub.id, action=SubscriptionAction.CREATE,
            previous_plan_id=None, new_plan_id=plan.id, payment_id=payment.id, effective_date=now,
        )
        db.add(history)
        _ensure_usage_record(db, user.org_id, now, period_end)
        log_action(db, org_id=user.org_id, actor_user_id=user.id, action="subscription.create",
                   entity_type="subscription", entity_id=sub.id, details={"plan": plan.code})
        db.commit()
        return PlanChangeResultOut(
            subscription_id=sub.id, plan=_plan_to_out(plan, plan.id), status="active",
            effective_date=now, invoice=_invoice_to_out(invoice),
            message=f"Subscribed to the {plan.name} plan.",
        )

    # ---- Case 2: already on this plan ----
    if sub.plan_id == plan.id:
        return PlanChangeResultOut(
            subscription_id=sub.id, plan=_plan_to_out(plan, plan.id),
            status=_enum_value(sub.status),
            effective_date=sub.current_period_start, invoice=None,
            message="This is already your current plan.",
        )

    # ---- Case 3: upgrade or downgrade an existing subscription ----
    previous_plan = sub.plan
    action = SubscriptionAction.UPGRADE if plan.price_cents >= previous_plan.price_cents else SubscriptionAction.DOWNGRADE

    razorpay_payment = _charge_for_plan(db, org_id=user.org_id, user_email=user_email, plan=plan, payment_method=payment_method)

    if sub.razorpay_subscription_id:
        try:
            # Simplest correct way to swap a Razorpay subscription onto a
            # different plan is to stop billing the old one and start a
            # fresh one on the new plan - Razorpay subscriptions don't
            # support changing plan_id on an existing subscription
            # in-place the way Stripe's subscription items do.
            razorpay_service.cancel_subscription(sub.razorpay_subscription_id, at_period_end=False)
            plan.razorpay_plan_id = razorpay_service.get_or_create_plan(
                plan.razorpay_plan_id, plan.name, plan.price_cents, plan.currency, plan.billing_cycle,
            )
            new_razorpay_sub = razorpay_service.create_subscription(
                sub.razorpay_customer_id, plan.razorpay_plan_id, plan.billing_cycle, user.org_id,
            )
            sub.razorpay_subscription_id = new_razorpay_sub["id"]
        except RazorpayServiceError as e:
            raise HTTPException(status_code=402, detail=e.user_message)

    sub.plan_id = plan.id
    db.flush()

    invoice, payment = _create_invoice_and_payment(
        db, org_id=user.org_id, subscription_id=sub.id, plan=plan,
        period_start=now, period_end=sub.current_period_end, payment_method=payment_method,
        razorpay_payment=razorpay_payment,
    )
    history = SubscriptionHistory(
        subscription_id=sub.id, action=action,
        previous_plan_id=previous_plan.id, new_plan_id=plan.id, payment_id=payment.id, effective_date=now,
    )
    db.add(history)

    # Resolve any open "storage/license limit" alerts that no longer
    # apply now that limits have changed (best-effort - not spec-mandated
    # but keeps the Billing Alerts panel accurate after an upgrade).
    if action == SubscriptionAction.UPGRADE:
        db.query(BillingAlert).filter(
            BillingAlert.org_id == user.org_id,
            BillingAlert.status == AlertStatus.ACTIVE,
            BillingAlert.alert_type.in_([AlertType.STORAGE_LIMIT_REACHED, AlertType.LICENSE_EXPIRY]),
        ).update({"status": AlertStatus.DISMISSED, "resolved_at": now}, synchronize_session=False)

    log_action(db, org_id=user.org_id, actor_user_id=user.id, action=f"subscription.{action.value}",
               entity_type="subscription", entity_id=sub.id,
               details={"from": previous_plan.code, "to": plan.code})
    db.commit()

    return PlanChangeResultOut(
        subscription_id=sub.id, plan=_plan_to_out(plan, plan.id), status="active",
        effective_date=now, invoice=_invoice_to_out(invoice),
        message=f"Subscription {action.value}d to the {plan.name} plan.",
    )


@router.post("/subscription/renew", response_model=PlanChangeResultOut)
def renew_subscription(db: Session = Depends(get_db), user: User = Depends(require_manage)):
    """'Renew Subscription' button (page 16): Active Subscription
    Required, Payment Required = Yes, Confirmation = Required (frontend
    is expected to confirm before calling this)."""
    sub = _get_subscription_or_404(db, user.org_id)
    plan = sub.plan
    payment_method = _primary_payment_method(db, user.org_id)
    if not payment_method:
        raise HTTPException(status_code=402, detail="Selected payment method is invalid.")

    user_email = getattr(user, "email", f"org-{user.org_id}@example.com")
    razorpay_payment = _charge_for_plan(db, org_id=user.org_id, user_email=user_email, plan=plan, payment_method=payment_method)

    now = _now()
    new_period_end = sub.current_period_end + (
        timedelta(days=30) if plan.billing_cycle == "monthly" else timedelta(days=365)
    )
    sub.current_period_start = sub.current_period_end
    sub.current_period_end = new_period_end
    sub.status = SubscriptionStatus.ACTIVE
    sub.past_due_since = None
    db.flush()

    invoice, payment = _create_invoice_and_payment(
        db, org_id=user.org_id, subscription_id=sub.id, plan=plan,
        period_start=sub.current_period_start, period_end=sub.current_period_end,
        payment_method=payment_method, razorpay_payment=razorpay_payment,
    )
    history = SubscriptionHistory(
        subscription_id=sub.id, action=SubscriptionAction.RENEW,
        previous_plan_id=plan.id, new_plan_id=plan.id, payment_id=payment.id, effective_date=now,
    )
    db.add(history)
    _ensure_usage_record(db, user.org_id, sub.current_period_start, sub.current_period_end)
    log_action(db, org_id=user.org_id, actor_user_id=user.id, action="subscription.renew",
               entity_type="subscription", entity_id=sub.id)
    db.commit()

    return PlanChangeResultOut(
        subscription_id=sub.id, plan=_plan_to_out(plan, plan.id), status="active",
        effective_date=now, invoice=_invoice_to_out(invoice),
        message="Subscription renewed successfully.",
    )


@router.post("/subscription/cancel", response_model=MessageOut)
def cancel_subscription(
    at_period_end: bool = Query(True),
    db: Session = Depends(get_db), user: User = Depends(require_manage),
):
    sub = _get_subscription_or_404(db, user.org_id)
    if sub.razorpay_subscription_id:
        try:
            razorpay_service.cancel_subscription(sub.razorpay_subscription_id, at_period_end=at_period_end)
        except RazorpayServiceError as e:
            raise HTTPException(status_code=502, detail=e.user_message)

    if at_period_end:
        sub.cancel_at_period_end = True
    else:
        sub.status = SubscriptionStatus.CANCELED
        sub.canceled_at = _now()

    history = SubscriptionHistory(
        subscription_id=sub.id, action=SubscriptionAction.CANCEL,
        previous_plan_id=sub.plan_id, new_plan_id=sub.plan_id, effective_date=_now(),
    )
    db.add(history)
    log_action(db, org_id=user.org_id, actor_user_id=user.id, action="subscription.cancel", entity_type="subscription", entity_id=sub.id)
    db.commit()

    msg = "Your subscription will be canceled at the end of the current billing period." if at_period_end else "Your subscription has been canceled."
    return MessageOut(message=msg)


# ==========================================================================
# Billing Alerts  (AC-SB-045 .. AC-SB-052)
# ==========================================================================

@router.get("/alerts", response_model=AlertListOut)
def list_alerts(
    include_resolved: bool = Query(False),
    db: Session = Depends(get_db), user: User = Depends(require_view),
):
    q = db.query(BillingAlert).filter(BillingAlert.org_id == user.org_id)
    if not include_resolved:
        q = q.filter(BillingAlert.status == AlertStatus.ACTIVE)
    priority_rank = {AlertPriority.HIGH: 0, AlertPriority.MEDIUM: 1, AlertPriority.LOW: 2}
    rows = q.order_by(BillingAlert.created_at.desc()).all()
    rows.sort(key=lambda a: priority_rank.get(a.priority, 3))

    active_count = sum(1 for a in rows if a.status == AlertStatus.ACTIVE)
    high_count = sum(1 for a in rows if a.status == AlertStatus.ACTIVE and a.priority == AlertPriority.HIGH)

    return AlertListOut(
        alerts=[AlertOut.model_validate(a) for a in rows],
        active_count=active_count, high_priority_count=high_count,
    )


@router.patch("/alerts/{alert_id}", response_model=AlertOut)
def update_alert(
    alert_id: int, payload: AlertUpdateIn,
    db: Session = Depends(get_db), user: User = Depends(require_view),
):
    """Spec (page 15): 'Users shall be able to acknowledge, dismiss, or
    mark billing alerts as read.' Any authenticated billing user can
    manage their own view of alerts - this intentionally uses
    require_view, not require_manage."""
    alert = db.query(BillingAlert).filter(BillingAlert.id == alert_id, BillingAlert.org_id == user.org_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if payload.status not in {s.value for s in AlertStatus}:
        raise HTTPException(status_code=400, detail="Invalid alert status.")

    alert.status = payload.status
    if payload.status in ("acknowledged", "dismissed"):
        alert.resolved_at = _now()
    log_action(db, org_id=user.org_id, actor_user_id=user.id, action=f"alert.{payload.status}", entity_type="billing_alert", entity_id=alert.id)
    db.commit()
    db.refresh(alert)
    return AlertOut.model_validate(alert)


# ==========================================================================
# Export  (top-right "Export" button)
# ==========================================================================

@router.get("/export")
def export_billing_data(db: Session = Depends(get_db), user: User = Depends(require_export)):
    rows = db.query(Invoice).filter(Invoice.org_id == user.org_id).order_by(Invoice.issue_date.desc()).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Invoice #", "Date", "Period Start", "Period End", "Amount", "Status"])
    for inv in rows:
        writer.writerow([
            inv.invoice_number, inv.issue_date.date().isoformat(),
            inv.period_start.date().isoformat(), inv.period_end.date().isoformat(),
            f"{_cents_to_amount(inv.amount_cents):.2f}",
            _enum_value(inv.status),
        ])
    buffer.seek(0)
    log_action(db, org_id=user.org_id, actor_user_id=user.id, action="billing.export", entity_type="invoice")
    db.commit()
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=billing_export.csv"},
    )
