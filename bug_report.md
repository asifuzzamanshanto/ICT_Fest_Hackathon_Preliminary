# CoWork API Final Bug Report

This report summarizes the proven specification, security, and concurrency defects found during the audit against the official hackathon PDF. All fixes preserve the documented API contract and are covered by focused regression or black-box tests.

## Audit Verification

The following verification was completed during the audit before temporary stress-test files were removed from the final submission tree:

- `python3 -m compileall app tests` passed.
- `python3 -m pytest -q -s tests/test_audit_full.py` passed: 53 tests.
- `python3 -m pytest -q -s tests/test_regressions.py::test_cross_process_same_slot_create_cannot_double_book` passed.
- `python3 -m pytest -q -s tests/test_audit_full.py tests/test_regressions.py::test_cross_process_same_slot_create_cannot_double_book` passed: 54 tests.
- `python3 -m pytest -q -s` passed.
- Full pytest suite was repeated 10 times; every run passed with 69 tests.

## 1. Datetime Normalization

- Files: `app/timeutils.py`
- Rules: Business Rule 1
- Bug: offset-aware ISO datetimes had their timezone stripped instead of being converted to the equivalent UTC instant.
- Fix: convert aware datetimes with `astimezone(timezone.utc)` before storing/serializing.
- Proof: regression test creates a booking using a `+06:00` timestamp and verifies the returned time represents the same UTC instant.

## 2. JWT Expiry, Logout, and Refresh Persistence

- Files: `app/auth.py`, `app/routers/auth.py`, `app/models.py`
- Rules: Business Rule 8
- Bug: access tokens lasted 15 hours instead of 900 seconds, logout stored/rechecked inconsistent identifiers, refresh tokens were reusable, malformed signed token claims could return 500, and revocation state was only in memory.
- Fix: issue 15-minute access tokens, validate required token claims, revoke by `jti`, make refresh tokens single-use, and persist revoked/used token states in SQLite.
- Proof: tests verify expiry duration, malformed claim rejection, logout persistence after in-memory state is cleared, and refresh-token reuse rejection after in-memory state is cleared.

## 3. Token State IntegrityError Handling

- Files: `app/auth.py`
- Rules: Business Rules 8 and 16
- Bug: token-state persistence swallowed all `IntegrityError`s, hiding unrelated database failures.
- Fix: tolerate only proven duplicate `jti` writes for the same token state; re-raise other integrity errors.
- Proof: regression test confirms duplicate `jti` idempotency and verifies non-duplicate `IntegrityError` is not swallowed.

## 4. Registration Duplicate Username

- Files: `app/routers/auth.py`
- Rules: Business Rule 15, API error contract
- Bug: duplicate registration in the same organization could return the existing user instead of a contract error.
- Fix: return `409 USERNAME_TAKEN`.
- Proof: regression and audit tests verify duplicate usernames in one organization return the documented error.

## 5. Registration Race Handling

- Files: `app/routers/auth.py`
- Rules: Business Rules 15 and 16
- Bug: concurrent organization/user creation could leak raw `IntegrityError`s.
- Fix: catch organization/user uniqueness races, rollback, re-fetch the organization when appropriate, and map username conflicts to `409 USERNAME_TAKEN`.
- Proof: stress probes verified same-org/same-username races return one success and conflicts; distinct-user races in a new org produce one admin and the remaining members.

## 6. Room Value Validation

- Files: `app/schemas.py`
- Rules: Business Rules 2 and 16
- Bug: rooms could be created with negative capacity or negative hourly rate, causing invalid availability and negative pricing.
- Fix: require positive `capacity` and `hourly_rate_cents` at request validation.
- Proof: audit tests verify negative capacity and zero/negative rate are rejected with validation errors.

## 7. Booking Time, Duration, and Pricing Validation

- Files: `app/routers/bookings.py`
- Rules: Business Rules 2 and 3
- Bug: booking creation allowed past starts via a grace window, accepted `end_time <= start_time`, accepted fractional/sub-hour durations, accepted over-limit durations, and could price invalid ranges.
- Fix: require strictly future start time, `end_time > start_time`, whole-hour duration, and duration in the allowed range.
- Proof: audit tests cover past, reversed, fractional, over-limit, and normal pricing cases.

## 8. Booking Conflict Semantics

- Files: `app/routers/bookings.py`
- Rules: Business Rule 3
- Bug: overlap detection treated valid back-to-back bookings as conflicts and did not fully cover all overlap shapes.
- Fix: use the interval rule `existing.start < new.end and new.start < existing.end`.
- Proof: double-booking tests cover partial overlaps, contained overlaps, exact duplicate windows, and back-to-back bookings.

## 9. Double-Booking Race Across Threads and Processes

- Files: `app/routers/bookings.py`
- Rules: Business Rules 3 and 16
- Bug: an in-memory application lock protected only one process. Two app processes could both pass the conflict check and commit overlapping confirmed bookings for the same room.
- Fix: acquire a SQLite `BEGIN IMMEDIATE` transaction before the conflict/quota/reference critical section, keeping the existing thread lock for in-process safety.
- Proof: cross-process same-slot regression uses one explicit clean SQLite database shared by parent and child interpreters and verifies exactly one `201` and one `409`.

## 10. Member Quota Race

- Files: `app/routers/bookings.py`
- Rules: Business Rule 4
- Bug: concurrent member bookings could pass the quota check before any transaction committed.
- Fix: run quota checks inside the serialized booking critical section.
- Proof: concurrency tests verify only the allowed number of member bookings can be confirmed in the quota window.

## 11. Rate-Limit Race

- Files: `app/services/ratelimit.py`, `app/routers/bookings.py`
- Rules: Business Rule 5
- Bug: rate-limit counters were not thread-safe and artificial race windows allowed inconsistent counts.
- Fix: guard rate-limit bucket updates with a lock and count booking attempts before validation.
- Proof: rate-limit probes verify 20 allowed attempts and later attempts return `429 RATE_LIMITED`.

## 12. Reference Code Uniqueness

- Files: `app/services/reference.py`, `app/routers/bookings.py`
- Rules: Business Rule 7
- Bug: reference codes used an in-memory counter that reset on process restart and could collide.
- Fix: flush the booking row first and derive `CW-{id:06d}` from the database primary key.
- Proof: sequential and concurrent booking probes verify distinct reference codes.

## 13. Notification Deadlock

- Files: `app/services/notifications.py`
- Rules: Business Rule 16
- Bug: create and cancel notifications acquired email/audit locks in opposite orders, creating an AB-BA deadlock risk.
- Fix: acquire locks in the same order for all notification paths.
- Proof: concurrent create/cancel stress completes without hanging.

## 14. Booking Visibility and Pagination

- Files: `app/routers/bookings.py`
- Rules: Business Rules 10 and 11
- Bug: booking list pagination skipped the first page, ignored `limit`, sorted incorrectly, and members could read another member's booking in the same organization.
- Fix: sort by ascending start time and id, use `(page - 1) * limit`, honor `limit`, and return 404 for non-owner member reads.
- Proof: audit tests cover pagination, ordering, invalid pagination, and member cross-read denial.

## 15. Cancellation Refund Consistency

- Files: `app/routers/bookings.py`, `app/services/refunds.py`
- Rules: Business Rule 6
- Bug: refund tiers mishandled boundary cases, half-refund rounding/storage could diverge from the response, refund logs committed separately before cancellation, and concurrent cancels could create multiple refund logs.
- Fix: implement the documented tiers, compute cents with integer arithmetic, write refund logs in the same transaction as cancellation, and serialize cancellation.
- Proof: tests cover refund tiers, exactly-48-hour boundary behavior, double cancel rejection, and concurrent cancellation producing one refund log.

## 16. Fresh Availability, Reports, and Stats

- Files: `app/routers/rooms.py`, `app/routers/admin.py`
- Rules: Business Rules 12, 13, and 14
- Bug: availability and usage reports could return stale cached data, and stats used in-memory counters that could drift after cancellation, concurrency, or restart.
- Fix: compute availability, reports, and stats directly from current database rows.
- Proof: audit tests verify availability scoping/sorting, report shape/content, and stats counting only confirmed bookings.

## 17. Multi-Tenant Export Scoping

- Files: `app/routers/admin.py`, `app/services/export.py`
- Rules: Business Rule 9
- Bug: `GET /admin/export?include_all=true&room_id=...` accepted a foreign organization's `room_id` and returned an empty export instead of treating it as inaccessible.
- Fix: validate that `room_id` belongs to the caller's organization before export; return `404 ROOM_NOT_FOUND` for foreign or missing rooms; keep export queries scoped by organization.
- Proof: audit and regression tests verify foreign `room_id` export returns `404 ROOM_NOT_FOUND`.

## 18. Cross-Organization Resource Isolation

- Files: `app/routers/rooms.py`, `app/routers/bookings.py`, `app/routers/admin.py`, `app/services/export.py`
- Rules: Business Rule 9
- Bug: several resource paths needed explicit same-organization checks to avoid IDOR-style access or inconsistent empty responses.
- Fix: enforce organization ownership for rooms, availability, stats, bookings, reports, and exports.
- Proof: audit tests cover cross-org room access, booking creation, availability, stats, export, and bogus identifiers.

## 19. Error Contract and Edge-Case Hardening

- Files: `app/auth.py`, `app/routers/auth.py`, `app/routers/bookings.py`, `app/routers/rooms.py`, `app/routers/admin.py`
- Rules: Business Rule 16, API Contract
- Bug: malformed IDs, malformed/tampered auth, bad date ranges, and injected token claims could produce incorrect status codes or server errors.
- Fix: validate inputs at the router/schema boundary and map known failure modes to the documented error contract.
- Proof: audit tests cover missing auth, bogus auth headers, tampered tokens, bad `sub` claims, invalid dates, bogus room IDs, and no stack trace leakage.

## Audit Coverage Used

Temporary audit coverage included:

- Full black-box checks for the PDF business rules and API contract.
- Focused regressions for auth, multi-tenancy, validation, booking, export, and token persistence.
- Concurrency stress checks for same-slot booking, partial overlaps, quota, rate limiting, cancellation, and cross-process transaction races.

These temporary audit files were removed from the final submission tree so no extra test/source files are required beyond the application fixes and this report.

## Final Status

No proven remaining critical or high-risk bug was found after the final verification runs.
