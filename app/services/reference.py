"""Human-facing booking reference codes.

Codes are derived from the booking's primary key (id), guaranteeing
uniqueness across the database and across process restarts. The lookup
runs under the same booking lock used for create/cancel so the reference
is stable from the moment the booking is committed.
"""
from sqlalchemy.orm import Session

from ..models import Booking


def assign_reference_code(db: Session, booking: Booking) -> str:
    """Flush to populate ``booking.id``, then format the reference code."""
    db.flush()
    return f"CW-{booking.id:06d}"
