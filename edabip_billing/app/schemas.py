"""
Pydantic schemas for the Subscription & Billing API.

Field names/shapes are deliberately matched to what the existing React
frontend already renders (see the "Billing" screenshot) so no frontend
changes are required - only the backend adapts. Money is always
serialized as a plain float in major units (dollars), already converted
from the `*_cents` integer stored in the DB, so the frontend never has
to do that math.
"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict


def cents_to_amount(cents: int) -> float:
    return round(cents / 100, 2)


def amount_to_cents(amount: float) -> int:
    return round(amount * 100)


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    description: str
    price: float
    currency: str
    billing_cycle: str
    features: List[str]
    is_current: bool = False
    featured: bool = False


class PlanListOut(BaseModel):
    plans: List[PlanOut]


class PlanSelectIn(BaseModel):
    # Frontend just needs to hit "Select Plan" / "Upgrade Plan" - this
    # lets it optionally force a confirmation step per the spec's
    # "Upgrade Plan Button: Confirmation Required".
    confirmed: bool = True
    payment_method_id: Optional[int] = Field(
        default=None,
        description="Billing PaymentMethod.id to charge. Defaults to the org's primary method.",
    )


class PlanChangeResultOut(BaseModel):
    subscription_id: int
    plan: PlanOut
    status: str
    effective_date: datetime
    invoice: Optional["InvoiceOut"] = None
    message: str


# --------------------------------------------------------------------------
# Billing overview (the 4 header cards)
# --------------------------------------------------------------------------

class BillingOverviewOut(BaseModel):
    plan_name: str
    billing_cycle_label: str            # "Billed Monthly"
    period_start: datetime
    period_end: datetime
    days_remaining: int
    amount_due: float
    amount_due_date: Optional[datetime]
    amount_due_label: str               # "Due on May 31, 2025" / "No payment due"
    last_payment_amount: Optional[float]
    last_payment_date: Optional[datetime]
    subscription_status: str
    last_refreshed: datetime


# --------------------------------------------------------------------------
# Usage analytics
# --------------------------------------------------------------------------

class UsageMetricOut(BaseModel):
    key: str            # "active_users" | "storage" | "reports" | "dashboards" | "data_processing"
    label: str          # "Active Users"
    used: float
    limit: float
    unit: str            # "" (plain count) | "TB" | "Reports" | "GB"
    percentage: float
    display_used: str    # "86" or "1.2 TB" - pre-formatted so the frontend can render as-is
    display_limit: str   # "200 Users" / "2 TB" / "5,000 Reports"


class UsageOverviewOut(BaseModel):
    period_start: datetime
    period_end: datetime
    metrics: List[UsageMetricOut]


class StorageUsageOut(BaseModel):
    """Dedicated Storage Usage Widget (spec page 12) - a superset of the
    'Storage Usage' entry inside UsageOverviewOut, for a detail view."""
    total_gb: float
    used_gb: float
    available_gb: float
    percentage: float
    warning_threshold_pct: float
    critical_threshold_pct: float
    status: str   # "normal" | "warning" | "critical"


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------

class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    invoice_number: str
    date: datetime = Field(validation_alias="issue_date")
    period_start: datetime
    period_end: datetime
    period_label: str          # "Apr 1 - Apr 30,2025" ready-to-render
    due_date: datetime
    amount: float
    status: str
    download_available: bool


class InvoiceListOut(BaseModel):
    items: List[InvoiceOut]
    page: int
    page_size: int
    total: int
    total_pages: int


class InvoiceDetailOut(InvoiceOut):
    hosted_invoice_pdf_url: Optional[str] = None


# --------------------------------------------------------------------------
# Billing history (card + bar chart)
# --------------------------------------------------------------------------

class MonthlyTotalOut(BaseModel):
    month: str      # "Jan"
    year: int
    amount: float


class BillingHistoryOut(BaseModel):
    total_paid: float
    total_invoices: int          # a COUNT. The current frontend mock shows
                                  # this with a leading "$" ("$16") - that's
                                  # a display bug on the frontend side, see
                                  # README. This field is a plain integer.
    outstanding_amount: float
    monthly: List[MonthlyTotalOut]


# --------------------------------------------------------------------------
# Payment methods
# --------------------------------------------------------------------------

class PaymentMethodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    brand: str
    last4: str
    exp_month: int
    exp_year: int
    is_primary: bool
    display_label: str    # "Visa ending in 4242"
    expires_label: str    # "Expires 02/2027"


class PaymentVerificationOrderOut(BaseModel):
    """What the frontend needs to open Razorpay Checkout for the
    '+ Add New Payment Method' flow."""
    order_id: str
    key_id: str
    amount: int  # paise
    currency: str


class AttachPaymentMethodIn(BaseModel):
    """What Razorpay Checkout's handler callback gives us back once the
    customer finishes entering their card."""
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    set_primary: bool = False


class PaymentMethodActionOut(BaseModel):
    payment_methods: List[PaymentMethodOut]
    message: str


# --------------------------------------------------------------------------
# Payment history
# --------------------------------------------------------------------------

class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    transaction_id: str
    payment_date: Optional[datetime] = Field(validation_alias="paid_at")
    amount: float
    payment_method_label: Optional[str]
    status: str
    invoice_number: Optional[str]
    reference_number: str
    failure_reason: Optional[str] = None


class PaymentHistoryOut(BaseModel):
    items: List[PaymentOut]
    page: int
    page_size: int
    total: int
    total_pages: int


# --------------------------------------------------------------------------
# Billing alerts
# --------------------------------------------------------------------------

class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    type: str = Field(validation_alias="alert_type")
    priority: str
    status: str
    title: str
    message: str
    created_at: datetime
    resolved_at: Optional[datetime] = None


class AlertListOut(BaseModel):
    alerts: List[AlertOut]
    active_count: int
    high_priority_count: int


class AlertUpdateIn(BaseModel):
    status: str   # "acknowledged" | "dismissed" | "read"


# --------------------------------------------------------------------------
# Generic
# --------------------------------------------------------------------------

class MessageOut(BaseModel):
    message: str


class ErrorOut(BaseModel):
    detail: str


class SearchResultOut(BaseModel):
    invoices: List[InvoiceOut]
    plans: List[PlanOut]
    query: str


PlanChangeResultOut.model_rebuild()
