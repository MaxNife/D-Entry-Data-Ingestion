"""In-app correction of rejected rows (decision 2026-10-20, overriding spec §2).

The grid edits a *copy* of a batch's rejected rows. Re-validation runs the real
contract checks on the edited rows; resubmitting writes them out as a new file
with the original headers, which becomes a new batch into the same target table.
The original batch and its rejections are never changed.
"""

import csv
from pathlib import Path

import pandas as pd

from ingest.core import loader
from ingest.core.connections import target_engine
from ingest.core.contracts import Contract
from ingest.core.exports import original_headers
from ingest.core.pipeline import _rename
from ingest.core.reports import rejected_row_groups
from ingest.core.validation import validate_chunk
from ingest.db.models import RejectedRecord

MAX_FIX_ROWS = 2000  # beyond this, download the rows and fix them in a spreadsheet


def fix_grid(engine, batch_id: str, contract: Contract | None) -> dict:
    """Rejected rows with their original values and the reasons attached to each cell."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    groups = rejected_row_groups(engine, batch_id)
    headers = original_headers(groups)
    source_of = {c.name: c.source_name for c in contract.columns} if contract else {}
    with Session(engine) as s:
        recs = s.execute(
            select(
                RejectedRecord.row_number,
                RejectedRecord.column_name,
                RejectedRecord.reason_code,
                RejectedRecord.reason_message,
            ).where(RejectedRecord.batch_id == batch_id)
        ).all()
    errors: dict[int, dict[str, list[str]]] = {}
    for row_number, column, code, message in recs:
        header = source_of.get(column, column) if column else "_row"
        errors.setdefault(row_number, {}).setdefault(header or "_row", []).append(
            f"{code}: {message}"
        )
    grid = [
        {
            "row_number": g["row_number"],
            "values": [
                "" if (v := (g["original"] or {}).get(h)) is None else str(v) for h in headers
            ],
            "errors": errors.get(g["row_number"], {}),
        }
        for g in groups
    ]
    return {
        "headers": headers,
        "rows": grid[:MAX_FIX_ROWS],
        "total": len(grid),
        "truncated": len(grid) > MAX_FIX_ROWS,
    }


def revalidate(engine, contract: Contract, headers: list[str], rows: list[list[str]]) -> dict:
    """Run the contract's checks on edited rows. Returns per-row errors keyed by grid index."""
    missing = [c.source_name for c in contract.columns if c.source_name not in headers]
    if missing:
        return {"missing": missing, "errors": {}, "bad": len(rows), "good": 0}
    source = pd.DataFrame(
        [[None if (v is None or str(v).strip() == "") else str(v) for v in r] for r in rows],
        columns=headers,
        dtype=object,
    )
    internal = _rename(source.copy(), contract)
    keep = [c.name for c in contract.columns if c.name in internal.columns]
    refs = loader.fetch_ref_values(target_engine(engine, contract), contract)
    result = validate_chunk(internal[keep], contract, 0, refs)
    source_of = {c.name: c.source_name for c in contract.columns}
    errors: dict[int, dict[str, list[str]]] = {}
    for r in result.rejections:
        header = source_of.get(r.column_name, r.column_name) if r.column_name else "_row"
        errors.setdefault(r.row_number - 1, {}).setdefault(header or "_row", []).append(
            f"{r.reason_code}: {r.reason_message}"
        )
    return {"missing": [], "errors": errors, "bad": len(errors), "good": len(rows) - len(errors)}


def write_fixed_file(headers: list[str], rows: list[list[str]], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow(headers)
        w.writerows(rows)
    return dest
