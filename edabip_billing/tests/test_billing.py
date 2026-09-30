"""
End-to-end tests for the billing module.

Runs against an in-memory SQLite DB (dependency-injected in place of
MySQL) with every Razorpay network call monkeypatched, since the
sandbox this was built in cannot reach api.razorpay.com. The business
logic exercised here - plan selection, invoice/payment generation,
usage math, permission enforcement, webhook reconciliation - is
identical regardless of which DB or payment gateway backs it.

Run with:  pytest -v
"""
import sys
import os
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import models, permissions, razorpay_service, seed_plans
from app.router import router as billing_router
from app.webhooks import _handle_subscription_payment_failed, _handle_subscription_charged
from fastapi import FastAPI


# --------------------------------------------------------------------------
# Test app + isolated in-memory DB per test
# --------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # SQLite ignores foreign key constraints unless explicitly told to
    # enforce them - MySQL InnoDB (the production target) enforces them
    # by default. Turning this on makes the test DB match production
    # behavior; it's what catches FK-constraint bugs (like DELETEing a
    # PaymentMethod that a Payment still references) before MySQL does.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    seed_plans.seed(session)
    yield session
    session.close()


@pytest.fixture()
def app(db_session):
    test_app = FastAPI()
    test_app.include_router(billing_router)

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


def make_user(role="billing_administrator", org_id=1, user_id=1):
    return permissions.User(id=user_id, org_id=org_id, role=role)


@pytest.fixture()
def as_admin(app):
    app.dependency_overrides[permissions.get_current_user] = lambda: make_user("billing_administrator")
    return TestClient(app)


@pytest.fixture()
def as_client_user(app):
    app.dependency_overrides[permissions.get_current_user] = lambda: make_user("client_user")
    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_razorpay(monkeypatch):
    """Stand in for every Razorpay network call."""
    import itertools
    counter = itertools.count(1)

    def fake_create_customer(org_id, email, name):
        return {"id": f"cust_test_{org_id}"}

    def fake_create_card_verification_order(customer_id, org_id):
        return {"id": "order_verify_1", "amount": 100, "currency": "INR"}

    def fake_verify_and_save_card(customer_id, order_id, payment_id, signature):
        return razorpay_service.CardDetails(
            razorpay_token_id=f"token_{payment_id}",
            brand="visa", last4="4242", exp_month=2, exp_year=2027,
        )

    def fake_detach_payment_method(customer_id, token_id):
        return None

    def fake_get_or_create_plan(razorpay_plan_id, name, amount_cents, currency, billing_cycle):
        return razorpay_plan_id or f"plan_{name.lower()}"

    def fake_create_subscription(customer_id, razorpay_plan_id, billing_cycle, org_id):
        return {"id": f"sub_{razorpay_plan_id}_{org_id}_{next(counter)}"}

    def fake_charge_saved_card_now(customer_id, token_id, amount_cents, currency, email, org_id):
        n = next(counter)
        return {"id": f"pay_auto_{n}", "amount": amount_cents, "currency": currency.upper(), "order_id": f"order_auto_{n}"}

    def fake_cancel_subscription(subscription_id, at_period_end=True):
        return {"id": subscription_id, "status": "cancelled"}

    def fake_fetch_subscription(subscription_id):
        return {"id": subscription_id, "status": "active"}

    def fake_create_hosted_invoice(customer_id, payment_id, description, amount_cents, currency):
        return {"id": f"rzpinv_{payment_id}", "short_url": f"https://razorpay.example/invoice/{payment_id}"}

    monkeypatch.setattr(razorpay_service, "create_customer", fake_create_customer)
    monkeypatch.setattr(razorpay_service, "create_card_verification_order", fake_create_card_verification_order)
    monkeypatch.setattr(razorpay_service, "verify_and_save_card", fake_verify_and_save_card)
    monkeypatch.setattr(razorpay_service, "detach_payment_method", fake_detach_payment_method)
    monkeypatch.setattr(razorpay_service, "get_or_create_plan", fake_get_or_create_plan)
    monkeypatch.setattr(razorpay_service, "create_subscription", fake_create_subscription)
    monkeypatch.setattr(razorpay_service, "charge_saved_card_now", fake_charge_saved_card_now)
    monkeypatch.setattr(razorpay_service, "cancel_subscription", fake_cancel_subscription)
    monkeypatch.setattr(razorpay_service, "fetch_subscription", fake_fetch_subscription)
    monkeypatch.setattr(razorpay_service, "create_hosted_invoice", fake_create_hosted_invoice)


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

def test_plans_seeded_with_correct_values(db_session):
    plans = {p.code: p for p in db_session.query(models.Plan).all()}
    assert set(plans.keys()) == {"starter", "professional", "enterprise"}
    assert plans["starter"].price_cents == 2900
    assert plans["professional"].price_cents == 7900
    assert plans["enterprise"].price_cents == 19900
    # Enterprise limits must match the Usage Overview widget in the screenshot exactly.
    assert plans["enterprise"].max_users == 200
    assert plans["enterprise"].max_storage_gb == 2048
    assert plans["enterprise"].max_reports_per_month == 5000
    assert plans["enterprise"].max_dashboards == 100
    assert plans["enterprise"].max_data_processing_gb == 5120
    # Professional is the highlighted/"most popular" card on the pricing
    # section - the other two plans should not be marked featured.
    assert plans["professional"].is_featured is True
    assert plans["starter"].is_featured is False
    assert plans["enterprise"].is_featured is False


def test_list_plans_endpoint(as_admin):
    resp = as_admin.get("/api/billing/plans")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["plans"]) == 3
    codes = {p["code"] for p in data["plans"]}
    assert codes == {"starter", "professional", "enterprise"}
    starter = next(p for p in data["plans"] if p["code"] == "starter")
    assert starter["price"] == 29.0
    assert "Up to 5 Users" in starter["features"]
    assert "10 GB Storage" in starter["features"]
    assert starter["featured"] is False
    professional = next(p for p in data["plans"] if p["code"] == "professional")
    assert professional["featured"] is True


def test_select_plan_creates_subscription_and_invoice(as_admin, db_session):
    # No payment method yet -> should fail with 402 and the spec's exact copy.
    resp = as_admin.post("/api/billing/plans/professional/select", json={"confirmed": True})
    assert resp.status_code == 402
    assert resp.json()["detail"] == "Selected payment method is invalid."

    # Add a payment method first.
    resp = as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    assert resp.status_code == 201
    assert resp.json()["payment_methods"][0]["is_primary"] is True

    resp = as_admin.post("/api/billing/plans/professional/select", json={"confirmed": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan"]["code"] == "professional"
    assert body["invoice"]["amount"] == 79.0
    assert body["invoice"]["status"] == "paid"

    overview = as_admin.get("/api/billing/overview").json()
    assert overview["plan_name"] == "Professional"
    assert overview["subscription_status"] == "active"


def test_select_plan_requires_confirmation(as_admin):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    resp = as_admin.post("/api/billing/plans/starter/select", json={"confirmed": False})
    assert resp.status_code == 400


def test_select_unknown_plan_returns_spec_error_message(as_admin):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    resp = as_admin.post("/api/billing/plans/does-not-exist/select", json={"confirmed": True})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Selected subscription plan is unavailable."


def test_upgrade_flow_records_history_and_is_idempotent_on_same_plan(as_admin, db_session):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})
    resp = as_admin.post("/api/billing/plans/enterprise/select", json={"confirmed": True})
    assert resp.status_code == 200
    assert "upgrade" in resp.json()["message"].lower()

    history = db_session.query(models.SubscriptionHistory).all()
    actions = [h.action for h in history]
    assert models.SubscriptionAction.CREATE in actions
    assert models.SubscriptionAction.UPGRADE in actions

    # Selecting the same plan again should be a friendly no-op, not an error.
    resp = as_admin.post("/api/billing/plans/enterprise/select", json={"confirmed": True})
    assert resp.status_code == 200
    assert resp.json()["message"] == "This is already your current plan."


# --------------------------------------------------------------------------
# Usage analytics
# --------------------------------------------------------------------------

def test_usage_overview_matches_screenshot_shape(as_admin, db_session):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/enterprise/select", json={"confirmed": True})

    sub = db_session.query(models.Subscription).first()
    usage = db_session.query(models.UsageRecord).filter_by(org_id=1, period_start=sub.current_period_start).first()
    usage.active_users = 86
    usage.storage_used_gb = 1229  # ~1.2 TB
    usage.reports_generated = 1245
    usage.active_dashboards = 48
    usage.data_processed_gb = 2560  # 2.5 TB
    db_session.commit()

    resp = as_admin.get("/api/billing/usage")
    assert resp.status_code == 200
    metrics = {m["key"]: m for m in resp.json()["metrics"]}

    assert metrics["active_users"]["used"] == 86
    assert metrics["active_users"]["limit"] == 200
    assert metrics["active_users"]["percentage"] == 43.0
    assert metrics["storage"]["display_limit"] == "2 TB"
    assert metrics["reports"]["percentage"] == 24.9
    assert metrics["dashboards"]["limit"] == 100


# --------------------------------------------------------------------------
# Invoices
# --------------------------------------------------------------------------

def test_invoice_number_format_and_pagination(as_admin, db_session):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})
    as_admin.post("/api/billing/plans/professional/select", json={"confirmed": True})
    as_admin.post("/api/billing/plans/enterprise/select", json={"confirmed": True})

    resp = as_admin.get("/api/billing/invoices?page=1&page_size=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["total_pages"] == 2
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert item["invoice_number"].startswith("INV-")
    # newest first
    assert body["items"][0]["amount"] == 199.0

    all_invoices = db_session.query(models.Invoice).all()
    numbers = [inv.invoice_number for inv in all_invoices]
    assert len(numbers) == len(set(numbers)), "invoice numbers must be unique"
    assert not any(n.startswith("TMP-") for n in numbers), "placeholder must always be overwritten"
    import re as _re
    for n in numbers:
        assert _re.fullmatch(r"INV-\d{4}-\d{5}", n), f"unexpected invoice number format: {n}"


def test_invoice_not_found_returns_spec_error_message(as_admin):
    resp = as_admin.get("/api/billing/invoices/INV-2099-99999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Selected invoice is unavailable."


# --------------------------------------------------------------------------
# Payment methods
# --------------------------------------------------------------------------

def test_payment_method_lifecycle(as_admin):
    r1 = as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_visa", "razorpay_payment_id": "pay_pm_visa", "razorpay_signature": "sig_pm_visa"})
    assert r1.status_code == 201
    assert r1.json()["payment_methods"][0]["display_label"] == "Visa ending in 4242"
    assert r1.json()["payment_methods"][0]["is_primary"] is True

    r2 = as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_visa2", "razorpay_payment_id": "pay_pm_visa2", "razorpay_signature": "sig_pm_visa2"})
    methods = r2.json()["payment_methods"]
    assert len(methods) == 2
    primaries = [m for m in methods if m["is_primary"]]
    assert len(primaries) == 1  # only ever one primary

    second_id = next(m["id"] for m in methods if not m["is_primary"])
    r3 = as_admin.patch(f"/api/billing/payment-methods/{second_id}/primary")
    assert r3.status_code == 200
    assert any(m["id"] == second_id and m["is_primary"] for m in r3.json()["payment_methods"])

    r4 = as_admin.delete(f"/api/billing/payment-methods/{second_id}")
    assert r4.status_code == 200
    remaining = r4.json()["payment_methods"]
    assert len(remaining) == 1
    assert remaining[0]["is_primary"] is True  # auto-promoted survivor


def test_remove_payment_method_with_payment_history_preserves_the_payment(as_admin, db_session):
    """Regression test: billing_payments.payment_method_id has a foreign
    key to billing_payment_methods.id. Without ON DELETE SET NULL,
    removing a card that was actually used for a payment fails under
    MySQL InnoDB's default FK enforcement (confirmed via SQLite's
    foreign_keys pragma, which the db_session fixture now enables)."""
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})  # generates a real Payment row

    payment = db_session.query(models.Payment).first()
    assert payment.payment_method_id is not None
    pm_id = payment.payment_method_id

    resp = as_admin.delete(f"/api/billing/payment-methods/{pm_id}")
    assert resp.status_code == 200

    db_session.refresh(payment)
    assert payment.payment_method_id is None  # FK nulled, not blocked
    assert payment.amount_cents == 2900  # row still fully intact (starter plan price)
    assert payment.status == models.PaymentStatus.SUCCESSFUL


# --------------------------------------------------------------------------
# Billing history
# --------------------------------------------------------------------------

def test_billing_history_totals(as_admin):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/enterprise/select", json={"confirmed": True})

    resp = as_admin.get("/api/billing/history")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_paid"] == 199.0
    assert body["total_invoices"] == 1  # a plain int, not a "$"-prefixed string
    assert isinstance(body["total_invoices"], int)
    assert body["outstanding_amount"] == 0.0
    assert len(body["monthly"]) == 6


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------

def test_client_user_can_view_but_not_manage(as_client_user):
    resp = as_client_user.get("/api/billing/plans")
    assert resp.status_code == 200

    resp = as_client_user.post("/api/billing/plans/starter/select", json={"confirmed": True})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "You do not have permission to perform this action."

    resp = as_client_user.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_x", "razorpay_payment_id": "pay_pm_x", "razorpay_signature": "sig_pm_x"})
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# Search validation (page 5 spec: 2-100 chars, blocked special characters)
# --------------------------------------------------------------------------

def test_search_validation_rules(as_admin):
    resp = as_admin.get("/api/billing/search?q=a")  # too short
    assert resp.status_code == 400

    resp = as_admin.get("/api/billing/search?q=" + "a" * 101)  # too long
    assert resp.status_code == 400

    resp = as_admin.get("/api/billing/search?q=INV%3B%20DROP%20TABLE")  # "INV; DROP TABLE"
    assert resp.status_code == 400  # semicolon not in the allow-list

    resp = as_admin.get("/api/billing/search?q=INV-2025")
    assert resp.status_code == 200


def test_search_finds_invoice_by_partial_number(as_admin):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})
    inv_number = as_admin.get("/api/billing/invoices").json()["items"][0]["invoice_number"]

    resp = as_admin.get(f"/api/billing/search?q={inv_number[-6:]}")
    assert resp.status_code == 200
    assert any(i["invoice_number"] == inv_number for i in resp.json()["invoices"])


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def test_alerts_list_and_update(as_admin, db_session):
    alert = models.BillingAlert(
        org_id=1, alert_type=models.AlertType.PAYMENT_DUE, priority=models.AlertPriority.HIGH,
        status=models.AlertStatus.ACTIVE, title="Payment due soon", message="Your invoice is due in 5 days.",
    )
    db_session.add(alert)
    db_session.commit()
    db_session.refresh(alert)

    resp = as_admin.get("/api/billing/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_count"] == 1
    assert body["high_priority_count"] == 1

    resp = as_admin.patch(f"/api/billing/alerts/{alert.id}", json={"status": "acknowledged"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "acknowledged"

    resp = as_admin.get("/api/billing/alerts")
    assert resp.json()["active_count"] == 0


def test_no_enum_class_repr_leaks_into_responses(as_admin):
    """Regression test: (str, Enum) members are always `isinstance(x, str)`
    True, so a naive `isinstance(x, str) else x.value` check never takes
    the .value branch. Every status/type field in every response - JSON
    and CSV alike - must render as the plain value ("paid"), never the
    Python repr ("InvoiceStatus.PAID")."""
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})

    overview = as_admin.get("/api/billing/overview")
    assert "Enum" not in overview.text and "." not in overview.json()["subscription_status"]

    invoices = as_admin.get("/api/billing/invoices")
    assert "Status." not in invoices.text

    history = as_admin.get("/api/billing/payment-history")
    assert "Status." not in history.text

    csv_export = as_admin.get("/api/billing/export")
    assert "Status." not in csv_export.text

    payment_csv = as_admin.get("/api/billing/payment-history/export")
    assert "Status." not in payment_csv.text


def test_webhook_creates_invoice_for_razorpays_own_automatic_renewal(as_admin, db_session):
    """Regression test for a real gap: Razorpay's own automatic billing
    cycle (the normal way a subscription renews, with no call to
    POST /subscription/renew) fires subscription.charged for a cycle
    this backend never created locally. The handler must CREATE the
    local Invoice/Payment/SubscriptionHistory rows, not silently no-op -
    otherwise auto-renewed invoices would never appear in the Invoice
    Table or count toward Total Paid (BR-SB-004)."""
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})

    sub = db_session.query(models.Subscription).first()
    assert sub.razorpay_subscription_id is not None  # set for real by select_plan in this design

    invoices_before = db_session.query(models.Invoice).count()
    payments_before = db_session.query(models.Payment).count()

    period_start_ts = int(sub.current_period_end.timestamp())
    period_end_ts = period_start_ts + 30 * 24 * 3600
    fake_webhook_payload = {
        "subscription": {"entity": {
            "id": sub.razorpay_subscription_id,
            "current_start": period_start_ts,
            "current_end": period_end_ts,
        }},
        "payment": {"entity": {
            "id": "pay_auto_renewed_1",
            "amount": 2900,
            "currency": "INR",
            "invoice_id": "rzpinv_not_yet_seen",
        }},
    }
    _handle_subscription_charged(db_session, fake_webhook_payload)
    db_session.commit()

    assert db_session.query(models.Invoice).count() == invoices_before + 1
    assert db_session.query(models.Payment).count() == payments_before + 1

    new_payment = db_session.query(models.Payment).filter_by(razorpay_payment_id="pay_auto_renewed_1").first()
    assert new_payment is not None
    assert new_payment.status == models.PaymentStatus.SUCCESSFUL

    new_invoice = db_session.query(models.Invoice).filter_by(id=new_payment.invoice_id).first()
    assert new_invoice is not None
    assert new_invoice.status == models.InvoiceStatus.PAID
    assert new_invoice.amount_cents == 2900
    assert new_invoice.invoice_number.startswith("INV-")
    assert not new_invoice.invoice_number.startswith("TMP-")

    history_actions = [h.action for h in db_session.query(models.SubscriptionHistory).all()]
    assert history_actions.count(models.SubscriptionAction.RENEW) == 1

    db_session.refresh(sub)
    expected_start = datetime.fromtimestamp(period_start_ts, tz=timezone.utc).replace(tzinfo=None, microsecond=0)
    assert sub.current_period_start.replace(microsecond=0) == expected_start


# --------------------------------------------------------------------------
# Webhook handlers (called directly - signature verification is Razorpay
# SDK code already covered by Razorpay's own test suite, not ours to
# re-test)
# --------------------------------------------------------------------------

def test_webhook_payment_failed_suspends_after_grace_period(as_admin, db_session):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})

    sub = db_session.query(models.Subscription).first()
    sub.grace_period_days = 0  # force immediate suspension for this test
    db_session.commit()
    payment = db_session.query(models.Payment).first()

    fake_webhook_payload = {
        "subscription": {"entity": {"id": sub.razorpay_subscription_id, "status": "pending"}},
        "payment": {"entity": {"id": payment.razorpay_payment_id}},
    }
    _handle_subscription_payment_failed(db_session, fake_webhook_payload)
    db_session.commit()

    db_session.refresh(sub)
    db_session.refresh(payment)
    assert payment.status == models.PaymentStatus.FAILED
    assert sub.status == models.SubscriptionStatus.SUSPENDED

    alerts = db_session.query(models.BillingAlert).filter_by(org_id=1, alert_type=models.AlertType.PAYMENT_FAILURE).all()
    assert len(alerts) == 1
    assert alerts[0].priority == models.AlertPriority.HIGH


def test_webhook_payment_succeeded_clears_alert(as_admin, db_session):
    as_admin.post("/api/billing/payment-methods", json={"razorpay_order_id": "order_pm_test_1", "razorpay_payment_id": "pay_pm_test_1", "razorpay_signature": "sig_pm_test_1"})
    as_admin.post("/api/billing/plans/starter/select", json={"confirmed": True})

    sub = db_session.query(models.Subscription).first()
    sub.status = models.SubscriptionStatus.PAST_DUE
    payment = db_session.query(models.Payment).first()
    db_session.add(models.BillingAlert(
        org_id=1, alert_type=models.AlertType.PAYMENT_DUE, priority=models.AlertPriority.HIGH,
        status=models.AlertStatus.ACTIVE, title="t", message="m",
    ))
    db_session.commit()

    fake_webhook_payload = {
        "subscription": {"entity": {"id": sub.razorpay_subscription_id}},
        "payment": {"entity": {"id": payment.razorpay_payment_id}},
    }
    _handle_subscription_charged(db_session, fake_webhook_payload)
    db_session.commit()

    db_session.refresh(sub)
    assert sub.status == models.SubscriptionStatus.ACTIVE
    active_alerts = db_session.query(models.BillingAlert).filter_by(status=models.AlertStatus.ACTIVE).count()
    assert active_alerts == 0
