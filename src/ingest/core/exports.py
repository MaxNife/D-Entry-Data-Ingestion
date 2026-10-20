"""File writers behind every download: rejected rows as-is, table exports, query results.

CSV is written as plain UTF-8 without a BOM so a downloaded file can be fixed and
re-uploaded under the same contract unchanged.
"""

import csv
import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ingest.core.reports import rejected_row_groups

FORMATS = {
    "csv": ("text/csv", "CSV"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "Excel"),
    "json": ("application/json", "JSON"),
    "parquet": ("application/vnd.apache.parquet", "Parquet"),
}


def _plain(v):
    """A value every writer accepts: Excel rejects tz-aware datetimes, JSON rejects Decimal."""
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.replace(tzinfo=None)
    return v


def _json_default(v):
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def write_rows(headers: Sequence[str], rows: Iterable[Sequence], fmt: str, dest: Path) -> Path:
    """Write ``rows`` (sequences aligned with ``headers``) to ``dest`` in ``fmt``."""
    if fmt not in FORMATS:
        raise ValueError(f"unsupported format {fmt!r}; choose one of {', '.join(FORMATS)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        with dest.open("w", newline="", encoding="utf-8") as fp:
            w = csv.writer(fp)
            w.writerow(headers)
            for r in rows:
                w.writerow(["" if v is None else v for v in r])
    elif fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("data")
        ws.append(list(headers))
        for r in rows:
            ws.append([_plain(v) for v in r])
        wb.save(dest)
    elif fmt == "json":
        with dest.open("w", encoding="utf-8") as fp:
            fp.write("[")
            for i, r in enumerate(rows):
                fp.write(",\n" if i else "\n")
                fp.write(json.dumps(dict(zip(headers, r, strict=False)), default=_json_default))
            fp.write("\n]\n")
    else:  # parquet: one in-memory frame so every chunk shares a schema
        import pandas as pd

        frame = pd.DataFrame([[_plain(v) for v in r] for r in rows], columns=list(headers))
        for col in frame.columns:  # pyarrow cannot infer a column mixing Decimal and str
            if frame[col].dtype == object:
                frame[col] = frame[col].map(lambda v: str(v) if isinstance(v, Decimal) else v)
        frame.to_parquet(dest, index=False)
    return dest


def original_headers(groups: list[dict]) -> list[str]:
    """The upload's headers in file order, recovered from the stored original rows."""
    headers: list[str] = []
    for g in groups:
        for k in g["original"] or {}:
            if k not in headers:
                headers.append(k)
    return headers


def rejected_rows_as_original(engine, batch_id: str) -> tuple[list[str], list[list]]:
    """The rejected rows exactly as they appeared in the upload (original headers and order)."""
    groups = rejected_row_groups(engine, batch_id)
    headers = original_headers(groups)
    return headers, [[(g["original"] or {}).get(h) for h in headers] for g in groups]
