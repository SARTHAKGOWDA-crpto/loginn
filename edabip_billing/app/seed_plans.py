"""
Seeds the three plans shown in the "Choose the Right Plan for Your
Business" section of the screenshot.

Users / Storage / Reports / price come directly off the pricing cards,
so those are exact. `max_dashboards` and `max_data_processing_gb` are
NOT shown on the pricing cards - only the "Enterprise" numbers are
confirmed, because they match the Usage Overview widget's limits
(100 dashboards, 5 TB) for the org in the screenshot, which the
overview labels as an Enterprise account. Starter/Professional values
for those two fields are proportional placeholders - adjust them to
your actual product limits before going live (search "PLACEHOLDER"
below).

Run with:  python -m app.seed_plans
"""
from app.database import Base, SessionLocal, engine
from app.models import Plan, BillingCycle

PLAN_DEFINITIONS = [
    dict(
        code="starter",
        name="Starter",
        description="Ideal for startups and small teams who want to explore cloud solutions with essential tools.",
        price_cents=2900,
        max_users=5,
        max_storage_gb=10,
        max_reports_per_month=100,
        max_dashboards=5,            # PLACEHOLDER - proportional estimate, confirm actual limit
        max_data_processing_gb=250,  # PLACEHOLDER - proportional estimate, confirm actual limit
        sort_order=1,
        is_featured=False,
    ),
    dict(
        code="professional",
        name="Professional",
        description="For growing teams needing more power, storage, and smarter collaboration.",
        price_cents=7900,
        max_users=20,
        max_storage_gb=100,
        max_reports_per_month=1000,
        max_dashboards=25,            # PLACEHOLDER - proportional estimate, confirm actual limit
        max_data_processing_gb=1024,  # PLACEHOLDER - proportional estimate, confirm actual limit
        sort_order=2,
        # Frontend renders this as the highlighted/"most popular" card.
        is_featured=True,
    ),
    dict(
        code="enterprise",
        name="Enterprise",
        description="For large organizations that need top security, high performance, and dedicated support.",
        price_cents=19900,
        max_users=200,
        max_storage_gb=2048,           # 2 TB - confirmed via Usage Overview "Limit: 2TB"
        max_reports_per_month=5000,
        max_dashboards=100,            # confirmed via Usage Overview "Limit: 100 Dashboards"
        max_data_processing_gb=5120,   # 5 TB - confirmed via Usage Overview "Limit: 5TB"
        sort_order=3,
        is_featured=False,
    ),
]


def seed(db=None) -> list[Plan]:
    owns_session = db is None
    db = db or SessionLocal()
    created = []
    try:
        for definition in PLAN_DEFINITIONS:
            existing = db.query(Plan).filter(Plan.code == definition["code"]).first()
            if existing:
                for key, value in definition.items():
                    setattr(existing, key, value)
                created.append(existing)
                continue
            plan = Plan(billing_cycle=BillingCycle.MONTHLY, currency="inr", **definition)
            db.add(plan)
            created.append(plan)
        db.commit()
        for plan in created:
            db.refresh(plan)
        return created
    finally:
        if owns_session:
            db.close()


if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    plans = seed()
    for p in plans:
        print(f"  {p.code:12s} ${p.price_cents/100:>7.2f}/mo  users<={p.max_users}  storage<={p.max_storage_gb}GB")
    print(f"Seeded {len(plans)} plans.")
