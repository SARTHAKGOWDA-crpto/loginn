"""
BR-SB-007: 'All subscription, billing, payment, invoice generation,
license allocation, and usage management activities shall be recorded
in immutable audit logs.'

Call `log_action()` inside the same DB transaction as the change it's
recording, so the audit row and the change commit (or roll back)
together - never log an action that didn't actually happen.
"""
import json
from typing import Optional

from sqlalchemy.orm import Session

from app.models import BillingAuditLog


def log_action(
    db: Session,
    *,
    org_id: int,
    actor_user_id: int,
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Optional[dict] = None,
) -> BillingAuditLog:
    entry = BillingAuditLog(
        org_id=org_id,
        actor_user_id=actor_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json.dumps(details, default=str) if details else None,
    )
    db.add(entry)
    db.flush()  # assigns entry.id without committing - caller controls the commit
    return entry
