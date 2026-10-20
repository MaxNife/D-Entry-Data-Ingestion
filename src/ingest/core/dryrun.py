"""Validate a file against a contract without loading anything (a dry run).

A dry run never creates a batch row, so a later real upload of the same file is
not skipped by the idempotency check. The result and a copy of the file are kept
under ``data/validations/`` so the user can ingest the very same file afterwards.

``start_validation`` records the job and returns at once; ``run_validation`` does the
work (in a background task) and saves progress after every chunk, so the result page
can show a live progress view.
"""

import json
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ingest.config import settings
from ingest.core import loader
from ingest.core.connections import target_engine
from ingest.core.contracts import load_contract
from ingest.core.errors import readable
from ingest.core.pipeline import _check_header, _chunk_frames, _originals, safe_file_name
from ingest.core.progress import estimate_rows
from ingest.core.validation import validate_chunk

MAX_RECORDS = 5000  # rejection records kept for display; counts always cover every row
SUMMARY_KEYS = (
    "id",
    "file_name",
    "contract_ref",
    "contract_name",
    "status",
    "total_rows",
    "rejected_rows",
    "created_at",
)


def validations_dir() -> Path:
    return Path(settings.upload_dir).parent / "validations"


def _path(validation_id: str) -> Path:
    if not validation_id or not all(c in "0123456789abcdef-" for c in validation_id):
        raise FileNotFoundError(validation_id)
    return validations_dir() / f"{validation_id}.json"


def _retry(op, attempts: int):
    """Windows refuses to replace or read a file another request has open (the page
    polls the progress file every second), so sharing violations are retried briefly."""
    for i in range(attempts):
        try:
            return op()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05)


def _save(result: dict, *, final: bool = False) -> None:
    """Atomic write: a page polling for progress never reads a half-written file.

    A progress update that loses the race is skipped (the next chunk writes again);
    the final result keeps trying for a few seconds.
    """
    path = _path(result["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, default=str), encoding="utf-8")
    try:
        _retry(lambda: os.replace(tmp, path), 100 if final else 5)
    except PermissionError:
        if final:
            raise


def start_validation(path: Path, contract_ref: str, *, file_name: str) -> dict:
    """Store a copy of the file and record a ``running`` validation. Raises for an unknown contract."""
    contract = load_contract(contract_ref)
    vid = str(uuid.uuid4())
    folder = validations_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stored = folder / f"{vid}{Path(file_name).suffix.lower() or '.csv'}"
    shutil.copyfile(path, stored)
    result = {
        "id": vid,
        "file_name": safe_file_name(file_name),
        "stored": stored.name,
        "contract_ref": f"{contract.name}@v{contract.version}",
        "contract_name": contract.name,
        "contract_version": contract.version,
        "target_table": contract.target_table,
        "created_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "expected_rows": estimate_rows(stored),
        "failure_reason": None,
        "total_rows": 0,
        "accepted_rows": 0,
        "rejected_rows": 0,
        "reasons": {},
        "columns": {},
        "records": [],
        "truncated": False,
    }
    _save(result)
    return result


def run_validation(validation_id: str, *, engine, chunk_size: int | None = None) -> dict:
    """Check every row of a started validation, saving progress after each chunk.

    Always ends with a final status: an unexpected error marks the validation as failed
    instead of leaving it "running" for ever.
    """
    try:
        return _run_validation(validation_id, engine=engine, chunk_size=chunk_size)
    except Exception as e:
        result = load_validation(validation_id)
        result.update(
            status="failed",
            failure_reason=f"the check stopped unexpectedly: {readable(e)}",
            finished_at=datetime.now(UTC).isoformat(),
        )
        _save(result, final=True)
        return result


def _run_validation(validation_id: str, *, engine, chunk_size: int | None) -> dict:
    result = load_validation(validation_id)
    contract = load_contract(result["contract_ref"])
    stored = stored_file(result)
    chunk_size = chunk_size or settings.chunk_size
    started = time.perf_counter()
    problem = _check_header(stored, contract)
    if problem:
        result.update(status="failed", failure_reason=problem)
    else:
        try:
            ref_values = loader.fetch_ref_values(target_engine(engine, contract), contract)
            source_of = {c.name: c.source_name for c in contract.columns}
            for internal, source in _chunk_frames(stored, contract, chunk_size):
                keep = [c.name for c in contract.columns if c.name in internal.columns]
                offset = result["total_rows"]
                originals = _originals(source, offset)
                chunk = validate_chunk(internal[keep], contract, offset, ref_values)
                bad = {r.row_number for r in chunk.rejections}
                result["total_rows"] += len(source)
                result["rejected_rows"] += len(bad)
                result["accepted_rows"] += len(source) - len(bad)
                for r in chunk.rejections:
                    result["reasons"][r.reason_code] = result["reasons"].get(r.reason_code, 0) + 1
                    header = source_of.get(r.column_name, r.column_name) or ""
                    result["columns"][header] = result["columns"].get(header, 0) + 1
                    if len(result["records"]) < MAX_RECORDS:
                        result["records"].append(
                            {
                                "row_number": r.row_number,
                                "reason_code": r.reason_code,
                                "reason_message": r.reason_message,
                                "column": header or None,
                                "value": r.offending_value,
                                "original": originals.get(r.row_number, {}),
                            }
                        )
                    else:
                        result["truncated"] = True
                _save(result)  # progress for the live view
        except Exception as e:
            result.update(status="failed", failure_reason=f"cannot read file: {e}")
        if result["status"] != "failed":
            result["status"] = "invalid" if result["rejected_rows"] else "valid"
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["finished_at"] = datetime.now(UTC).isoformat()
    result["reasons"] = dict(sorted(result["reasons"].items(), key=lambda kv: -kv[1]))
    result["columns"] = dict(sorted(result["columns"].items(), key=lambda kv: -kv[1]))
    _save(result, final=True)
    return result


def validate_file(
    path: Path, contract_ref: str, *, engine, file_name: str, chunk_size: int | None = None
) -> dict:
    """Start and finish a validation in one call (sources, scripts, tests)."""
    started = start_validation(path, contract_ref, file_name=file_name)
    return run_validation(started["id"], engine=engine, chunk_size=chunk_size)


def load_validation(validation_id: str) -> dict:
    p = _path(validation_id)
    if not p.exists():
        raise FileNotFoundError(validation_id)
    return json.loads(_retry(lambda: p.read_text(encoding="utf-8"), 40))


def stored_file(result: dict) -> Path:
    return validations_dir() / result["stored"]


def recent(limit: int = 8, *, contract: str | None = None, status: str | None = None) -> list[dict]:
    """Newest validations first, optionally for one contract (any version) or one status."""
    folder = validations_dir()
    if not folder.exists():
        return []
    out = []
    for f in folder.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if contract and d.get("contract_name") != contract.split("@")[0]:
            continue
        if status and d.get("status") != status:
            continue
        out.append({k: d.get(k) for k in SUMMARY_KEYS})
    out.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return out[:limit]
