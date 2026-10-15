"""Reject report CSV export. src/ingest/core/reports.py — spec §11."""

import csv
from collections.abc import Iterator

from sqlalchemy import select

from ingest.db.models import RejectedRecord


def rejected_row_groups(engine, batch_id: str) -> list[dict]:
    """One dict per rejected row_number: codes/messages joined with ' ;  ' + original row."""
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        rows = (
            s.execute(
                select(RejectedRecord)
                .where(RejectedRecord.batch_id == batch_id)
                .order_by(RejectedRecord.row_number)
            )
            .scalars()
            .all()
        )
    groups: dict[int, dict] = {}
    for r in rows:
        g = groups.setdefault(
            r.row_number,
            {
                "row_number": r.row_number,
                "codes": [],
                "messages": [],
                "original": r.original_row or {},
            },
        )
        g["codes"].append(r.reason_code)
        g["messages"].append(r.reason_message)
    return [groups[k] for k in sorted(groups)]


def report_fieldnames(groups: list[dict], original_headers: list[str]) -> list[str]:
    return ["row_number", "reason_codes", "reasons", *original_headers]


def iter_report_dicts(groups: list[dict], original_headers: list[str]) -> Iterator[dict]:
    for g in groups:
        row = {
            "row_number": g["row_number"],
            "reason_codes": "; ".join(g["codes"]),
            "reasons": " ;  ".join(g["messages"]),
        }
        for h in original_headers:
            row[h] = (g["original"] or {}).get(h, "")
        yield row


def write_reject_report(engine, batch_id: str, original_headers: list[str], fp) -> int:
    """Stream the report to a text file object. Returns rejected-row count."""
    groups = rejected_row_groups(engine, batch_id)
    w = csv.DictWriter(
        fp, fieldnames=report_fieldnames(groups, original_headers), extrasaction="ignore"
    )
    w.writeheader()
    for d in iter_report_dicts(groups, original_headers):
        w.writerow(d)
    return len(groups)
