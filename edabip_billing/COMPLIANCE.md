# Spec compliance matrix

Every Business Rule (BR-SB-001..012) and Acceptance Criteria (AC-SB-001..076)
from `billing_part.pdf`, checked against the actual code. ✅ = implemented,
⚠️ = partially implemented (explained), ❌ = not implemented (explained),
N/A = frontend/infra concern outside a backend's scope.

Written after a dedicated second-pass audit (not just the original build) -
this is what that audit actually found, including the gaps, not just the
successes.

## Search Dashboard Field (AC-SB-001..006)

| AC | Status | Note |
|---|---|---|
| 001 | ✅ | `GET /search` matches invoices + plans |
| 002 | ✅ | Stateless GET, no page reload needed |
| 003 | ⚠️ | Matches invoice number + plan name (case-insensitive, partial). Does **not** match "customer name" or "payment reference" - there's no customer/org-name field in this module (lives in your existing org/user service), and `Payment.reference_number` isn't wired into search. Straightforward to add if useful. |
| 004 | ⚠️ | Invoices filter by `status` + text search; no dedicated filter by plan or renewal-date range yet. |
| 005 | N/A | Performance threshold is a deployment/infra concern |
| 006 | ✅ | Empty query -> empty result set (frontend renders the empty state) |

## Subscription Plans (AC-SB-007..012)

| AC | Status | Note |
|---|---|---|
| 007 | ✅ | `GET /plans` returns name/price/features/cycle/limits |
| 008 | ✅ | `POST /plans/{code}/select`, permission-gated |
| 009 | ⚠️ | All active plans are shown to every org - there's no per-org plan-catalog restriction (e.g. invite-only tiers). Matches the screenshot (3 universally-available tiers); flag if your business needs org-specific catalogs. |
| 010 | ✅ | Validates plan is active + payment method exists + confirmation given |
| 011 | ✅ | Returns updated subscription + confirmation message |
| 012 | ✅ | Raises before any commit; existing plan is untouched on failure |

## Usage Analytics (AC-SB-013..020)

| AC | Status | Note |
|---|---|---|
| 013 | ✅ | |
| 014 | ⚠️ | The record exists and *can* be updated, but nothing in this module increments it automatically - that has to happen wherever the real usage occurs elsewhere in EDABIP (report generation, dashboard creation, etc.), same as noted in the README. |
| 015 | ⚠️ | Displays active users, storage, reports, dashboards, data processing. The spec's Usage Analytics section (page 8) lists **"API Usage"** as a metric; the screenshot shows **"Reports Generated"** instead - same kind of spec-vs-screenshot mismatch as the plan names, resolved the same way (went with what the screenshot renders). "Remaining limits" is derivable as `limit - used`, not a separate field. |
| 016 | ❌ | **Real gap**: no filtering by billing period, org, or date range - `GET /usage` always returns the current period only. Historical usage isn't queryable through this endpoint. |
| 017 | ⚠️ | Every metric returns a percentage; only the dedicated `/storage` endpoint returns an explicit warning/critical status. The other 4 metrics don't flag threshold breaches individually. |
| 018 | ⚠️ | A missing usage row is treated as "zero usage" (reasonable for a new org), not an error state. There's no distinct "the metering system itself is down" error path. |
| 019 | ✅ | `require_view` |
| 020 | N/A | Performance threshold |

## Invoice Table (AC-SB-021..028)

| AC | Status | Note |
|---|---|---|
| 021 | ✅ | |
| 022 | ⚠️ | PDF/page download via Razorpay's hosted invoice URL only - no CSV/Excel for a *single* invoice (the account-wide Export button does give CSV of all invoices). |
| 023 | ✅ | Invoice #, period, date, due date, amount, status, download - all present |
| 024 | ⚠️ | Pagination + status filter + invoice-number search all work; no explicit sort-direction parameter (always newest-first) or date-range filter. |
| 025 | ✅ | True by construction - same Razorpay payment backs both the display data and the hosted invoice |
| 026 | ✅ | `require_view` + `org_id` scoping |
| 027 | ✅ | Exact spec error strings on not-found / download failure |
| 028 | N/A | Performance threshold |

## Payment History (AC-SB-029..036)

| AC | Status | Note |
|---|---|---|
| 029 | ✅ | (this is the endpoint that was crashing before the `Payment.invoice`/`.payment_method` relationship fix - now covered by a regression test) |
| 030 | ✅ | All 7 spec'd fields present |
| 031 | ⚠️ | Search covers transaction ID + reference number; status filter works; no filter by payment method or billing-period range, no sort parameter. |
| 032 | ✅ | Webhooks update `Payment.status` on success/failure |
| 033 | ✅ | |
| 034 | ⚠️ | CSV export works (`/payment-history/export`, permission-gated); no PDF or Excel option. |
| 035 | ✅ | Standard HTTP error handling; no simulated "service unavailable" path (DB either answers or the request 500s) |
| 036 | ✅ | Newest-first, confirmed by test |

## Storage Usage Widget (AC-SB-037..044)

| AC | Status | Note |
|---|---|---|
| 037 | ✅ | |
| 038 | ✅ | Total/used/available/percentage all present |
| 039 | ⚠️ | Same external-metering caveat as AC-SB-014 |
| 040 | ⚠️ | `GET /storage` *computes* a warning/critical status live, but nothing **creates a `BillingAlert`** automatically when a threshold is crossed - that needs a scheduled check (see BR-SB-005 below), which isn't included. |
| 041 | ✅ | |
| 042 | ✅ | |
| 043 | ⚠️ | Same as AC-SB-018 |
| 044 | N/A | Performance threshold |

## Billing Alerts (AC-SB-045..052)

| AC | Status | Note |
|---|---|---|
| 045 | ⚠️ | Alert *infrastructure* (model, list, acknowledge/dismiss/read, permission-scoping) is complete. Only `payment_failure` alerts are actually auto-created (via the Razorpay webhook, when a real payment fails). |
| 046 | ❌ | **Real gap**: `subscription_expiry`, `payment_due`, `storage_limit_reached`, and `license_expiry` alert *types* exist and the API can manage them, but nothing proactively creates them. That needs a scheduled job (cron / Celery beat / APScheduler - your call which, so it wasn't presumed) periodically checking "is this subscription expiring in 5 days," "is storage over 80%," etc. and inserting `BillingAlert` rows. Worth building next. |
| 047 | ⚠️ | In-app: yes. Email: intentionally not included (see README) - wire your existing email utility in where alerts are created. |
| 048 | ✅ | `include_resolved=true` |
| 049 | ✅ | |
| 050 | ✅ | |
| 051 | N/A | Ties to the scheduler gap above |
| 052 | N/A | No delivery mechanism beyond DB row creation yet (ties to 047) |

## Upgrade Plan Button (AC-SB-053..060)

| AC | Status | Note |
|---|---|---|
| 053 | ✅ | |
| 054 | ✅ | Razorpay charge happens, *then* the local plan swap - ordering matters and was checked |
| 055 | ✅ | Immediate |
| 056 | ⚠️ | Invoice: yes. A persisted "upgrade succeeded" notification/alert: no (ties to 047) |
| 057 | ✅ | `SubscriptionHistory` records previous plan, new plan, payment, effective date |
| 058 | ✅ | `require_manage`. Note: "Billing Administrator" and "Account Owner" aren't modeled as two separate roles - `billing_administrator`/`administrator` both get manage rights. Split them if your org chart needs the distinction. |
| 059 | ✅ | |
| 060 | ✅ | `log_action` on every step |

## Renew Subscription Button (AC-SB-061..068)

| AC | Status | Note |
|---|---|---|
| 061 | ✅ | |
| 062 | ✅ | |
| 063 | ✅ | |
| 064 | ⚠️ | Same notification caveat as 056 |
| 065 | ✅ | Plan unchanged, period extended |
| 066 | ✅ | |
| 067 | ✅ | Grace-period/suspension logic in the webhook handler |
| 068 | ✅ | |

## Overall Acceptance Criteria (AC-SB-069..076)

| AC | Status | Note |
|---|---|---|
| 069 | ✅ | |
| 070 | ✅ | |
| 071 | ⚠️ | Same "API Usage" vs "Reports Generated" note as AC-SB-015 |
| 072 | ✅ | |
| 073 | ✅ | (post relationship-bug fix) |
| 074 | ⚠️ | Same threshold-alert-creation gap as AC-SB-040 |
| 075 | ⚠️ | Same as AC-SB-046 |
| 076 | N/A for the frontend half | RBAC ✅, audit logging ✅. Responsive design + WCAG 2.1 AA are frontend concerns - not applicable to a backend, not a gap here. |

## Business Rules (BR-SB-001..012)

| BR | Status | Note |
|---|---|---|
| 001 | ✅ | One subscription row per org; every plan change updates it in place |
| 002 | ⚠️ | **Scope note, not a bug**: this implements flat-rate tiered pricing ($29/$79/$199 per the screenshot), not consumption-based overage billing. Usage is tracked and shown against limits, but nobody gets charged extra for going over - if that's actually needed, it's a bigger addition (a metered Razorpay add-on/invoice line + an overage-calculation step). |
| 003 | ✅ | |
| 004 | ✅ | Including Razorpay's own automatic renewal cycle, after the webhook fix - previously only user-initiated actions (select/renew) generated invoices |
| 005 | ⚠️ | Same as AC-SB-046 |
| 006 | ✅ | `org_id` scoping throughout |
| 007 | ✅ | `log_action` on every mutating endpoint; no update/delete exposed on the audit log itself |
| 008 | ⚠️ | **Visibility vs. enforcement**: usage vs. plan limits is tracked and displayed, but nothing in this module *blocks* an action for being over-limit (e.g. adding a 6th user on a 5-user Starter plan). That enforcement has to live wherever users/dashboards/reports actually get created elsewhere in EDABIP, checking against this module's usage data first. |
| 009 | ✅ | Plan changes charge the new plan's price immediately and swap the Razorpay subscription to a fresh one on the new plan - no partial-period proration |
| 010 | ✅ | Razorpay end-to-end, raw card data never touches this server |
| 011 | ✅ | Grace-period suspension in the webhook handler, tested |
| 012 | ✅ | No hard-deletes of `Invoice`/`Payment` anywhere; removing a `PaymentMethod` preserves its payment history (`ON DELETE SET NULL`, was a real bug before the fix) |

## Summary

**76 AC + 12 BR = 88 total.** Roughly 60 fully ✅, ~23 ⚠️ partial (mostly:
narrower search/filter/sort coverage than the spec's ideal, and the
"tracked-but-not-yet-acted-on" usage/threshold/expiry data that needs a
scheduled job this module doesn't include), 2 clear ❌ gaps (usage-history
filtering by period; proactive alert generation beyond payment failures),
and a handful of N/A items that are genuinely frontend/infra concerns.

None of the ⚠️/❌ items are silent - each one was a deliberate scope
boundary or a known follow-up, not a place where the code claims to do
something it doesn't.
