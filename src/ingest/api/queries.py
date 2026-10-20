"""Read-side queries shared by the JSON API and the dashboard.

One definition of "today", of a contract filter, of a date range and of a
reason breakdown, so the two front doors can never disagree about a number.
"""

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ingest.db.models import Batch, RejectedRecord

STATUSES = ("pending", "processing", "completed", "partial", "failed")
TERMINAL = ("completed", "partial", "failed")


def parse_bound(value: str | None, *, end: bool) -> datetime | None:
    """Parse a ``from``/``to`` filter value. Raises ValueError when malformed.

    A bare date covers the whole day: ``from=2026-10-01`` starts at midnight
    UTC, ``to=2026-10-01`` runs until the following midnight (exclusive).
    """
    if not value:
        return None
    value = value.strip()
    if len(value) == 10:  # YYYY-MM-DD
        d = date.fromisoformat(value)
        start = datetime.combine(d, time.min, tzinfo=UTC)
        return start + timedelta(days=1) if end else start
    ts = datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _date_range(stmt: Select, date_from: str | None, date_to: str | None) -> Select:
    lo = parse_bound(date_from, end=False)
    hi = parse_bound(date_to, end=True)
    if lo is not None:
        stmt = stmt.where(Batch.created_at >= lo)
    if hi is not None:
        bare = date_to is not None and len(date_to.strip()) == 10
        stmt = stmt.where(Batch.created_at < hi if bare else Batch.created_at <= hi)
    return stmt


def _contract(stmt: Select, contract: str | None) -> Select:
    """Filter by ``name`` or ``name@vN`` / ``name@N``."""
    if not contract:
        return stmt
    name, _, version = contract.partition("@")
    stmt = stmt.where(Batch.contract_name == name)
    version = version.lower().lstrip("v")
    if version.isdigit():
        stmt = stmt.where(Batch.contract_version == int(version))
    return stmt


def batches_stmt(
    *,
    status: str | None = None,
    contract: str | None = None,
    q: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Select:
    stmt = select(Batch).order_by(Batch.created_at.desc())
    if status:
        stmt = stmt.where(Batch.status == status)
    stmt = _contract(stmt, contract)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(Batch.file_name.ilike(like) | Batch.batch_id.ilike(like))
    return _date_range(stmt, date_from, date_to)


def count(s: Session, stmt: Select) -> int:
    return s.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar_one()


def metrics_today(s: Session) -> dict:
    """The four dashboard cards, for the current UTC day."""
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    today = (Batch.created_at >= start) & (Batch.created_at < start + timedelta(days=1))
    batches, accepted, rejected, failed = s.execute(
        select(
            func.count(),
            func.coalesce(func.sum(Batch.accepted_rows), 0),
            func.coalesce(func.sum(Batch.rejected_rows), 0),
            func.count().filter(Batch.status == "failed"),
        ).where(today)
    ).one()
    return {
        "batches_today": int(batches),
        "rows_accepted_today": int(accepted),
        "rows_rejected_today": int(rejected),
        "failed_batches_today": int(failed),
    }


def reason_breakdown(s: Session, batch_id: str) -> tuple[int, list[dict]]:
    """(distinct rejected rows, one entry per reason code, most frequent first)."""
    counts = s.execute(
        select(RejectedRecord.reason_code, func.count(), func.min(RejectedRecord.reason_message))
        .where(RejectedRecord.batch_id == batch_id)
        .group_by(RejectedRecord.reason_code)
        .order_by(func.count().desc(), RejectedRecord.reason_code)
    ).all()
    n_rows = s.execute(
        select(func.count(func.distinct(RejectedRecord.row_number))).where(
            RejectedRecord.batch_id == batch_id
        )
    ).scalar_one()
    reasons = [
        {
            "reason_code": code,
            "count": n,
            "pct": round(100 * n / n_rows, 1) if n_rows else 0.0,
            "sample_message": message,
        }
        for code, n, message in counts
    ]
    return n_rows, reasons


def audit_stmt(
    *,
    reason_code: str | None = None,
    contract: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Select:
    """Batches with matching rejections, most affected first."""
    stmt = (
        select(
            RejectedRecord.batch_id,
            Batch.file_name,
            Batch.contract_name,
            Batch.contract_version,
            func.count().label("count"),
            Batch.created_at,
            Batch.status,
        )
        .join(Batch, Batch.batch_id == RejectedRecord.batch_id)
        .group_by(
            RejectedRecord.batch_id,
            Batch.file_name,
            Batch.contract_name,
            Batch.contract_version,
            Batch.created_at,
            Batch.status,
        )
        .order_by(func.count().desc(), Batch.created_at.desc())
    )
    if reason_code:
        stmt = stmt.where(RejectedRecord.reason_code == reason_code)
    stmt = _contract(stmt, contract)
    return _date_range(stmt, date_from, date_to)


def reason_totals(
    s: Session,
    *,
    contract: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[tuple[str, int]]:
    """Rejection counts per reason code across every matching batch."""
    stmt = (
        select(RejectedRecord.reason_code, func.count())
        .join(Batch, Batch.batch_id == RejectedRecord.batch_id)
        .group_by(RejectedRecord.reason_code)
        .order_by(func.count().desc(), RejectedRecord.reason_code)
    )
    stmt = _date_range(_contract(stmt, contract), date_from, date_to)
    return [(code, int(n)) for code, n in s.execute(stmt).all()]


def runs_by_day(s: Session, days: int = 14) -> list[dict]:
    """One entry per UTC day (oldest first) with batch counts per status."""
    today = datetime.now(UTC).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, time.min, tzinfo=UTC)
    rows = s.execute(select(Batch.created_at, Batch.status).where(Batch.created_at >= start)).all()
    buckets = {first + timedelta(days=i): dict.fromkeys(STATUSES, 0) for i in range(days)}
    for created, status in rows:
        day = (created if created.tzinfo else created.replace(tzinfo=UTC)).astimezone(UTC).date()
        if day in buckets and status in buckets[day]:
            buckets[day][status] += 1
    return [{"day": d, "total": sum(c.values()), **c} for d, c in buckets.items()]


def _seconds(b: Batch) -> float | None:
    if b.started_at is None or b.finished_at is None:
        return None
    return (b.finished_at - b.started_at).total_seconds()


def contract_health(s: Session, recent: int = 10) -> dict[str, dict]:
    """Per contract name: run counts, row totals, average duration and the latest runs."""
    agg = s.execute(
        select(
            Batch.contract_name,
            func.count(),
            func.count().filter(Batch.status == "completed"),
            func.count().filter(Batch.status.in_(TERMINAL)),
            func.coalesce(func.sum(Batch.accepted_rows), 0),
            func.coalesce(func.sum(Batch.rejected_rows), 0),
        ).group_by(Batch.contract_name)
    ).all()
    out = {}
    for name, runs, clean, finished, accepted, rejected in agg:
        last = (
            s.execute(
                select(Batch)
                .where(Batch.contract_name == name)
                .order_by(Batch.created_at.desc())
                .limit(recent)
            )
            .scalars()
            .all()
        )
        secs = [x for x in map(_seconds, last) if x is not None]
        out[name] = {
            "runs": int(runs),
            "clean_runs": int(clean),
            "finished_runs": int(finished),
            "accepted": int(accepted),
            "rejected": int(rejected),
            "avg_seconds": sum(secs) / len(secs) if secs else None,
            "recent": [
                {
                    "batch_id": b.batch_id,
                    "status": b.status,
                    "file_name": b.file_name,
                    "created_at": b.created_at.isoformat() if b.created_at else None,
                }
                for b in reversed(last)
            ],
        }
    return out


def column_totals(s: Session, contract: str) -> list[tuple[str | None, int]]:
    """Rejection counts per column for one contract (``name`` or ``name@vN``)."""
    stmt = (
        select(RejectedRecord.column_name, func.count())
        .join(Batch, Batch.batch_id == RejectedRecord.batch_id)
        .group_by(RejectedRecord.column_name)
        .order_by(func.count().desc(), RejectedRecord.column_name)
    )
    return [(col, int(n)) for col, n in s.execute(_contract(stmt, contract)).all()]
