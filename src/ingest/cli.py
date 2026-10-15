"""`ingest` command (Typer). Spec §9 — thin wrapper over core.pipeline."""

from pathlib import Path

import typer

from ingest.core.contracts import list_contracts, load_contract
from ingest.core.pipeline import get_batch, run_pipeline, stored_path
from ingest.db.session import init_db

app = typer.Typer(no_args_is_help=True)
contracts_app = typer.Typer(no_args_is_help=True)
app.add_typer(contracts_app, name="contracts")


@app.command()
def upload(
    file: Path = typer.Argument(..., exists=True, dir_okay=False),
    contract: str = typer.Option(..., "--contract", help="contract name or name@version"),
):
    """Validate FILE against CONTRACT and load accepted rows."""
    init_db()
    result = run_pipeline(file, contract)
    if result["skipped"]:
        typer.echo(f"already processed: {result['batch_id']} ({result['status']})")
    else:
        typer.echo(
            f"{result['batch_id']} {result['status']}: "
            f"{result['accepted_rows']}/{result['total_rows']} accepted, "
            f"{result['rejected_rows']} rejected"
        )


@app.command()
def reprocess(
    batch_id: str = typer.Argument(...),
    contract: str | None = typer.Option(
        None, "--contract", help="contract ref, default: batch's own"
    ),
):
    """Re-run a partial/failed batch, optionally under a newer contract version."""
    from ingest.db.session import get_engine

    init_db()
    engine = get_engine()
    batch = get_batch(engine, batch_id)
    if batch is None:
        raise typer.BadParameter(f"unknown batch {batch_id}")
    ref = contract or f"{batch.contract_name}@v{batch.contract_version}"
    before = (batch.accepted_rows, batch.rejected_rows)
    result = run_pipeline(stored_path(batch), ref, engine=engine, reprocess_batch_id=batch_id)
    typer.echo(
        f"{result['batch_id']} {result['status']}: before={before} "
        f"after=({result['accepted_rows']},{result['rejected_rows']})"
    )


@app.command(name="batches")
def list_batches(status: str | None = typer.Option(None, "--status")):
    """List batches, optionally filtered by status."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from ingest.db.models import Batch
    from ingest.db.session import get_engine

    init_db()
    with Session(get_engine()) as s:
        stmt = select(Batch).order_by(Batch.created_at.desc()).limit(50)
        if status:
            stmt = stmt.where(Batch.status == status)
        for b in s.execute(stmt).scalars():
            typer.echo(
                f"{b.batch_id[:8]} {b.status:10} {b.contract_name}@v{b.contract_version} "
                f"{b.file_name} {b.accepted_rows}/{b.total_rows}"
            )


@contracts_app.command(name="validate")
def contracts_validate():
    """Lint every contract file; fails fast with a readable error (run in CI)."""
    found = list_contracts()
    if not found:
        typer.echo("no contracts found")
        raise typer.Exit(1)
    bad = 0
    for name, version in sorted(found):
        try:
            c = load_contract(f"{name}@v{version}")
            typer.echo(f"ok {name}@v{version}: {len(c.columns)} cols, {len(c.rules)} rules")
        except (FileNotFoundError, ValueError) as e:
            typer.echo(f"INVALID {name}@v{version}: {e}")
            bad += 1
    if bad:
        raise typer.Exit(1)
