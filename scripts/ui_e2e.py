"""End-to-end UI test of the Ingest Platform: every feature, driven only through the browser.

Usage: python scripts/ui_e2e.py <base_url> <work_dir> tests/fixtures

Expects a server started against an empty, migrated database (with a ref_customers table
holding C001-C005), plus the external databases the connection steps use:
  - PostgreSQL ``ingest_extdemo`` on localhost:5432 (postgres/postgres) with ``orders_feed``
  - SQL Server on 127.0.0.1:14330 (sa / Ingest_Test_2026!) with database ``ingest_dest_e2e``
  - ``<work_dir>/demo.sqlite``
Writes screenshots to <work_dir>/shots and prints a pass/fail table; exits 1 on any failure.
"""

import csv
import io
import json
import re
import sys
import traceback
from contextlib import contextmanager, suppress
from pathlib import Path

import pandas as pd
from playwright.sync_api import expect, sync_playwright

BASE, WORK, FX = sys.argv[1].rstrip("/"), Path(sys.argv[2]), Path(sys.argv[3]).resolve()
SHOTS = WORK / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
TERMINAL = re.compile(r"^(completed|partial|failed)$")
results: list[tuple[str, bool, str]] = []
errors: list[str] = []
expect.set_options(timeout=20_000)


def variant(name: str, src: str, edit) -> Path:
    """A copy of a fixture with a small change, so its SHA-256 (and batch) is new."""
    p = WORK / name
    p.write_text(edit((FX / src).read_text(encoding="utf-8")), encoding="utf-8")
    return p


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror on {page.url}: {e}"))
    # 404/400 responses are expected where the test probes them (unknown batch, invalid contract, blocked SQL).
    page.on(
        "console",
        lambda m: (
            m.type == "error"
            and not re.search(r"status of 40[04]", m.text)
            and errors.append(f"console on {page.url}: {m.text}")
        ),
    )
    page.on("dialog", lambda d: d.accept())  # confirm() prompts: reprocess, resubmit, delete

    @contextmanager
    def step(name: str):
        slug = re.sub(r"\W+", "-", name.lower()).strip("-")[:60]
        try:
            yield
            results.append((name, True, ""))
            page.screenshot(path=str(SHOTS / f"ok-{len(results):02d}-{slug}.png"), full_page=True)
        except Exception as e:
            msg = f"{type(e).__name__}: {(str(e).splitlines() or [repr(e)])[0][:220]}"
            results.append((name, False, msg))
            with suppress(Exception):  # a broken page must not stop the run
                page.screenshot(
                    path=str(SHOTS / f"FAIL-{len(results):02d}-{slug}.png"), full_page=True
                )
            traceback.print_exc(limit=1)

    def nav(label: str):
        page.locator("nav.sidebar").get_by_role("link", name=label, exact=True).click()
        page.wait_for_load_state()

    def wait_terminal():
        badge = page.locator(".page-head [data-testid=status-badge]")
        expect(badge).to_have_attribute("data-status", TERMINAL, timeout=45_000)
        page.wait_for_timeout(300)
        page.reload()
        return page.locator(".page-head [data-testid=status-badge]").get_attribute("data-status")

    def batch_id() -> str:
        return page.url.split("/batches/")[1].split("?")[0].split("/")[0]

    def download(click) -> bytes:
        with page.expect_download() as d:
            click()
        return Path(d.value.path()).read_bytes()

    def upload_page(file: Path, contract: str, validate=False):
        nav("Add data")
        page.set_input_files("#upload-file-upload", str(file))
        page.select_option("#upload-contract-upload", contract)
        page.get_by_test_id("validate-submit" if validate else "upload-submit").click()
        page.wait_for_load_state()

    def settled_validation() -> str:
        """Validations run in the background; wait for the live view to finish."""
        status = page.get_by_test_id("validation-status")
        expect(status).not_to_have_attribute("data-status", "running", timeout=60_000)
        return status.get_attribute("data-status")

    def sql(statement: str, *, keys: bool = False):
        page.wait_for_selector(".CodeMirror")
        page.evaluate("s => window.sqlEditor.setValue(s)", statement)
        if keys:
            page.locator(".CodeMirror textarea").focus()
            page.keyboard.press("Control+Enter")
        else:
            page.get_by_test_id("sql-run").click()
        expect(page.locator("[data-running]")).to_be_hidden(timeout=30_000)

    # ---- 0. first run: create the admin ----------------------------------------------------
    with step("First visit opens setup and creates the admin"):
        page.goto(BASE + "/")
        page.wait_for_url("**/setup")
        page.get_by_test_id("setup-workspace").fill("Acme Data")
        page.get_by_test_id("setup-name").fill("Ada Admin")
        page.get_by_test_id("setup-email").fill("ada@example.com")
        page.get_by_test_id("setup-password").fill("correct horse battery")
        page.get_by_test_id("setup-confirm").fill("correct horse battery")
        page.get_by_test_id("setup-submit").click()
        page.wait_for_url(BASE + "/")
        expect(page.locator(".brand-name")).to_have_text("Acme Data")
        expect(page.locator("a[href='/docs'], a[href='/health']")).to_have_count(0)

    # ---- 1. shell ------------------------------------------------------------------------
    with step("Every sidebar destination opens"):
        page.goto(BASE + "/")
        for label, path in [
            ("Add data", "/upload"),
            ("Contracts", "/contracts"),
            ("Tables", "/tables"),
            ("Connections", "/connections"),
            ("Validations", "/validations"),
            ("Rejection audit", "/rejections"),
            ("SQL editor", "/query"),
            ("Runs", "/"),
        ]:
            nav(label)
            assert page.url.rstrip("/").endswith(path.rstrip("/")) or page.url.endswith(path), (
                label,
                page.url,
            )
            expect(page.locator("main h1").first).to_be_visible()

    with step("Sidebar collapses to an icon rail and back"):
        page.get_by_role("button", name="Toggle sidebar").click()
        assert "nav-collapsed" in page.locator("html").get_attribute("class")
        page.get_by_role("button", name="Toggle sidebar").click()
        assert "nav-collapsed" not in (page.locator("html").get_attribute("class") or "")

    # ---- 2. ingest through the New ingestion drawer ------------------------------------------
    with step("Upload a dirty file from the New ingestion drawer"):
        page.locator("nav.sidebar .new-btn").click()
        drawer = page.locator("dialog#new-ingestion")
        expect(drawer).to_be_visible()
        drawer.locator("#upload-file-home").set_input_files(str(FX / "dirty_transactions.csv"))
        drawer.locator("#upload-contract-home").select_option("txn_test@v1")
        drawer.get_by_test_id("upload-submit").click()
        page.wait_for_url("**/batches/**")
        assert wait_terminal() == "partial"
        dirty_id = batch_id()
        assert page.get_by_test_id("counter-total").inner_text() == "7"
        n_rejected = int(page.get_by_test_id("counter-rejected").inner_text())
        assert n_rejected >= 3, n_rejected  # 3, plus C999 when ref_customers exists

    with step("Reason breakdown filters the rejected records"):
        reasons = page.get_by_test_id("reason-row")
        assert reasons.count() >= 2
        code = reasons.first.get_attribute("data-reason-code")
        reasons.first.click()
        page.wait_for_load_state()
        assert f"reason_code={code}" in page.url
        rows = page.get_by_test_id("rejection-row")
        assert rows.count() >= 1
        assert all(code in rows.nth(i).inner_text() for i in range(rows.count()))

    with step("Original row opens beside a rejection"):
        page.get_by_test_id("original-row-toggle").first.click()
        expect(page.locator(".orig-pop").first).to_be_visible()

    with step("Reject report downloads with reasons"):
        data = download(lambda: page.get_by_test_id("download-report").click()).decode("utf-8")
        head = next(csv.reader(io.StringIO(data)))
        assert head[:3] == ["row_number", "reason_codes", "reasons"], head

    with step("Rejected rows download as the original file"):
        page.get_by_test_id("rejected-rows-menu").click()
        data = download(lambda: page.get_by_test_id("rejected-rows-csv").click()).decode("utf-8")
        got = list(csv.reader(io.StringIO(data)))
        src = list(csv.reader((FX / "dirty_transactions.csv").open(encoding="utf-8")))
        assert got[0] == src[0], got[0]
        assert all(r in src for r in got[1:]) and len(got) == n_rejected + 1, got

    # ---- 3. fix rows in the app --------------------------------------------------------------
    with step("Fix rejected rows in the grid and resubmit"):
        page.goto(f"{BASE}/batches/{dirty_id}")
        page.get_by_test_id("fix-rows").click()
        page.wait_for_url("**/fix")
        headers = [
            h.get_attribute("data-h") for h in page.locator("#fix-grid thead [data-h]").all()
        ]
        col = {h: i for i, h in enumerate(headers)}
        rows = page.locator("#fix-grid tbody tr[data-row]")
        assert rows.count() == n_rejected
        page.get_by_test_id("fix-contract").select_option("txn_test@v1")
        for i in range(rows.count()):
            cells = rows.nth(i).locator("[data-cell]")
            amount = cells.nth(col["Amount"]).input_value()
            try:
                cells.nth(col["Amount"]).fill(f"{abs(float(amount)):.2f}")
            except ValueError:
                cells.nth(col["Amount"]).fill("12.75")
            if cells.nth(col["Type"]).input_value() not in ("PAYMENT", "TRANSFER", "CASH_OUT"):
                cells.nth(col["Type"]).fill("PAYMENT")
            if cells.nth(col["CustomerId"]).input_value() not in (
                "C001",
                "C002",
                "C003",
                "C004",
                "C005",
            ):
                cells.nth(col["CustomerId"]).fill("C001")
        page.get_by_test_id("fix-validate").click()
        expect(page.get_by_test_id("fix-summary")).to_contain_text("selected rows pass")
        page.get_by_test_id("fix-submit").click()
        page.wait_for_url(re.compile(r".*/batches/[^/]+\?fixed_from=.*"))
        assert wait_terminal() == "completed"
        expect(page.get_by_text("Created from fixed rows")).to_be_visible()
        assert page.get_by_test_id("counter-accepted").inner_text() == str(n_rejected)

    with step("Original batch is unchanged after the fix"):
        page.goto(f"{BASE}/batches/{dirty_id}")
        assert page.get_by_test_id("counter-rejected").inner_text() == str(n_rejected)
        assert (
            page.locator(".page-head [data-testid=status-badge]").get_attribute("data-status")
            == "partial"
        )

    # ---- 4. reprocess and the version-conflict guard -----------------------------------------
    with step("Reprocess a partial batch onto the latest contract"):
        upload_page(FX / "mixed.csv", "txn_test@v1")
        assert wait_terminal() == "partial"
        before = page.get_by_test_id("counter-accepted").inner_text()
        page.get_by_test_id("reprocess-button").click()
        page.wait_for_load_state()
        expect(page.get_by_test_id("reprocess-result")).to_be_visible()
        assert page.get_by_test_id("contract-version").inner_text() == "txn_test@v2"
        assert int(page.get_by_test_id("counter-accepted").inner_text()) >= int(before)

    with step("Reprocess is turned off when the file already has a batch on that version"):
        twin = variant("twin.csv", "mixed.csv", lambda t: t.replace("T1001", "T1101"))
        upload_page(twin, "txn_test@v2")
        wait_terminal()
        upload_page(twin, "txn_test@v1")
        wait_terminal()
        expect(page.get_by_test_id("twin-batch")).to_be_visible()
        assert page.get_by_test_id("reprocess-button").get_attribute("aria-disabled") == "true"
        page.get_by_test_id("twin-batch").get_by_role("link").click()
        page.wait_for_load_state()
        assert page.get_by_test_id("contract-version").inner_text() == "txn_test@v2"

    # ---- 5. validate only ----------------------------------------------------------------------
    with step("Validate only loads nothing, then Ingest this file loads it"):
        dry = variant("dry_run.csv", "mixed.csv", lambda t: t.replace("T1001", "T1201"))
        upload_page(dry, "txn_test@v1", validate=True)
        page.wait_for_url("**/validations/**")
        assert settled_validation() == "invalid"
        nav("Runs")
        expect(page.get_by_text("dry_run.csv")).to_have_count(0)
        page.go_back()
        page.get_by_test_id("validation-ingest").click()
        page.wait_for_url("**/batches/**")
        assert wait_terminal() == "partial"

    with step("Validate only reports an unreadable file clearly"):
        upload_page(FX / "missing_header.csv", "txn_test@v1", validate=True)
        assert settled_validation() == "failed"
        expect(page.get_by_text("missing required header")).to_be_visible()

    with step("A large file shows live progress while it is checked"):
        rows = ["TxnId,Amount,Type,CustomerId,TxnDate,Memo"]
        rows += [
            f"T{6000 + i:04d},{(i % 97) + 1}.25,PAYMENT,C00{i % 5 + 1},2026-10-01,row {i}"
            for i in range(900)
        ]
        big = WORK / "big_check.csv"
        big.write_text("\n".join(rows) + "\n", encoding="utf-8")
        upload_page(big, "txn_test@v1", validate=True)
        page.wait_for_url("**/validations/**")
        expect(page.get_by_test_id("progress")).to_be_visible()
        expect(page.get_by_test_id("progress-pct")).to_be_visible()
        assert settled_validation() == "valid"

    with step("Validations page filters by contract"):
        nav("Validations")
        assert page.get_by_test_id("validation-row").count() >= 3
        page.get_by_test_id("validations-contract").select_option("score_test")
        page.wait_for_url("**contract=score_test**")
        expect(page.get_by_test_id("validation-row")).to_have_count(0)

    with step("Add data lists only the selected contract's validations"):
        nav("Add data")
        page.select_option("#upload-contract-upload", "txn_test")
        expect(page.get_by_test_id("recent-validations")).to_contain_text("dry_run.csv")
        page.select_option("#upload-contract-upload", "score_test")
        expect(page.get_by_test_id("recent-validations")).to_contain_text(
            "No file has been validated against score_test"
        )

    # ---- 6. contracts ----------------------------------------------------------------------------
    with step("Draft a contract from a sample file and save it"):
        nav("Contracts")
        page.get_by_test_id("new-contract").click()
        page.get_by_test_id("infer-file").set_input_files(str(FX / "clean_scores.csv"))
        page.fill("#infer-name", "class_scores")
        page.get_by_role("button", name="Draft contract").click()
        page.wait_for_load_state()
        assert page.locator("[data-col]").count() == 3
        score = page.locator("[data-col]").filter(
            has=page.locator('[data-k="source_name"][value="Score"]')
        )
        assert score.locator('[data-k="type"]').input_value() == "int"
        score.locator('[data-k="min"]').fill("0")
        score.locator('[data-k="max"]').fill("100")
        page.fill("#f-table", "class_scores")
        page.get_by_test_id("contract-save").click()
        page.wait_for_url("**/contracts/class_scores?v=1&saved=1")
        expect(page.get_by_text("Saved class_scores@v1")).to_be_visible()

    with step("Edit a contract: saving creates v2 and the diff shows it"):
        page.get_by_test_id("edit-contract").click()
        grade = page.locator("[data-col]").filter(
            has=page.locator('[data-k="source_name"][value="Grade"]')
        )
        grade.locator('[data-k="in_set"]').fill("A, B, C, D, F")
        page.get_by_test_id("contract-save").click()
        page.wait_for_url("**/contracts/class_scores?v=2&saved=1")
        page.locator(".tabs").get_by_role("tab", name=re.compile("Versions")).click()
        expect(page.locator(".changes").first).to_contain_text("in set")

    with step("Delete an unused contract version"):
        page.goto(f"{BASE}/contracts/class_scores?v=2")
        page.get_by_test_id("delete-contract").click()
        page.wait_for_url("**deleted=2")
        expect(page.locator(".tree").get_by_text("v2")).to_have_count(0)

    with step("An invalid contract is refused with the reason"):
        page.goto(f"{BASE}/contracts/new")
        page.fill("#f-name", "bad_one")
        page.fill("#f-table", "batches")
        first = page.locator("[data-col]").first
        first.locator('[data-k="source_name"]').fill("Value")
        page.get_by_test_id("contract-save").click()
        expect(page.get_by_test_id("contract-error")).to_contain_text("reserved")

    with step("Author a contract in the YAML tab"):
        page.goto(f"{BASE}/contracts/new")
        page.get_by_role("tab", name="YAML").click()
        page.locator("[data-yaml]").fill(
            "contract: yaml_made\ndescription: written as YAML\nsource: {formats: [csv]}\n"
            "target: {table: yaml_made, key: row_hash}\ncolumns:\n"
            "  - {name: name, source_name: Name, type: str, required: true}\n"
            "  - {name: score, source_name: Score, type: int, required: true}\n"
            "  - {name: grade, source_name: Grade, type: str}\n"
        )
        page.get_by_test_id("contract-save").click()
        page.wait_for_url("**/contracts/yaml_made?v=1&saved=1")

    with step("Ingest with the new contract from its page"):
        page.goto(f"{BASE}/contracts/class_scores")
        page.get_by_role("link", name="Ingest with class_scores@v1").click()
        assert page.locator("#upload-contract-upload").input_value() == "class_scores@v1"
        page.set_input_files("#upload-file-upload", str(FX / "clean_scores.csv"))
        page.get_by_test_id("upload-submit").click()
        page.wait_for_url("**/batches/**")
        assert wait_terminal() == "completed"

    # ---- 7. tables and export ---------------------------------------------------------------------
    with step("Tables lists the new table with its row count"):
        nav("Tables")
        row = page.get_by_test_id("table-row").filter(has_text="class_scores")
        expect(row).to_have_count(1)
        n_rows = len(list(csv.DictReader((FX / "clean_scores.csv").open(encoding="utf-8"))))
        expect(row).to_contain_text(str(n_rows))
        row.get_by_role("link").first.click()
        page.wait_for_url("**/tables/class_scores")

    with step("Search, Schema and Loads tabs on a table"):
        first_name = next(csv.DictReader((FX / "clean_scores.csv").open(encoding="utf-8")))["Name"]
        page.get_by_label("Search rows").fill(first_name)
        page.get_by_label("Search rows").press("Enter")
        page.wait_for_load_state()
        expect(page.locator(".rows-table tbody tr")).to_have_count(1)
        page.get_by_role("tab", name=re.compile("Schema")).click()
        expect(page.locator("main table")).to_contain_text("added by the platform")
        page.get_by_role("tab", name=re.compile("Loads")).click()
        assert page.get_by_test_id("batch-row").count() >= 1

    for fmt, label in [("csv", "CSV"), ("xlsx", "Excel"), ("json", "JSON"), ("parquet", "Parquet")]:
        with step(f"Export the table as {label} and read it back"):
            page.goto(f"{BASE}/tables/class_scores")
            page.get_by_test_id("export-menu").click()
            page.locator(".menu-form").get_by_label(label, exact=True).check()
            body = download(lambda: page.get_by_test_id("export-submit").click())
            buf = io.BytesIO(body)
            frame = {
                "csv": pd.read_csv,
                "xlsx": pd.read_excel,
                "parquet": pd.read_parquet,
                "json": lambda b: pd.DataFrame(json.loads(b.getvalue())),
            }[fmt](buf)
            assert len(frame) == n_rows, (fmt, len(frame))
            assert "row_key" not in frame.columns

    # ---- 8. SQL editor ------------------------------------------------------------------------------
    with step("SQL editor lists tables and runs a query with Ctrl+Enter"):
        nav("SQL editor")
        expect(page.get_by_test_id("sql-tree")).to_contain_text("class_scores")
        sql(
            "SELECT grade, count(*) AS n FROM class_scores GROUP BY grade ORDER BY grade", keys=True
        )
        expect(page.get_by_test_id("sql-count")).to_contain_text("row")
        assert page.locator("[data-testid=sql-results] tbody tr").count() >= 1

    with step("SQL editor exports results and remembers recent queries"):
        page.get_by_test_id("sql-export").click()
        body = download(lambda: page.locator("[data-export=csv]").click())
        assert body.decode("utf-8").splitlines()[0] == "grade,n"
        page.locator("[data-recent-menu] > summary").click()
        assert page.locator(".recent-q").count() >= 1
        page.keyboard.press("Escape")

    with step("Top 100 on a table runs a query"):
        page.goto(BASE + "/query")
        row = page.locator(".obj-row").filter(has_text="class_scores")
        row.hover()
        row.locator("[data-top]").click()
        expect(page.locator("[data-testid=sql-results] tbody tr")).to_have_count(n_rows)

    with step("SQL editor refuses writes and the credentials table"):
        for statement in ("DELETE FROM batches", "SELECT * FROM connections"):
            sql(statement)
            expect(page.get_by_test_id("sql-error")).to_be_visible()

    # ---- 9. rejection audit ---------------------------------------------------------------------------
    with step("Rejection audit filters by reason"):
        nav("Rejection audit")
        options = page.get_by_test_id("audit-reason-code").locator("option")
        assert options.count() > 1
        page.get_by_test_id("audit-reason-code").select_option(index=1)
        page.get_by_test_id("audit-submit").click()
        page.wait_for_load_state()
        assert page.get_by_test_id("audit-row").count() >= 1

    # ---- 10. connections --------------------------------------------------------------------------------
    def new_connection(kind_label: str, fields: dict, *, save=True):
        nav("Connections")
        page.get_by_test_id("new-connection").click()
        page.locator(".kind-card").filter(has_text=kind_label).click()
        for testid, value in fields.items():
            page.get_by_test_id(testid).fill(value)
        page.get_by_test_id("conn-test").click()
        result = page.get_by_test_id("conn-test-result")
        expect(result).to_be_visible()
        ok = result.get_attribute("data-ok") == "1"
        if save:
            page.get_by_test_id("conn-save").click()
            page.wait_for_url(re.compile(r".*/connections/[a-z_]+\?saved=1"))
        return ok

    with step("A wrong password fails the connection test with the server's message"):
        ok = new_connection(
            "PostgreSQL",
            {
                "conn-name": "broken",
                "conn-host": "localhost",
                "conn-port": "5432",
                "conn-db": "ingest_extdemo",
                "conn-user": "postgres",
                "conn-pass": "wrong",
            },
            save=False,
        )
        assert not ok
        expect(page.locator(".conn-err")).to_contain_text("password")

    with step("Add a PostgreSQL connection"):
        assert new_connection(
            "PostgreSQL",
            {
                "conn-name": "extdemo",
                "conn-host": "localhost",
                "conn-port": "5432",
                "conn-db": "ingest_extdemo",
                "conn-user": "postgres",
                "conn-pass": "postgres",
            },
        )
        expect(page.locator(".tree").get_by_text("orders_feed")).to_be_visible()

    with step("Add a SQL Server connection (port defaults to 1433)"):
        nav("Connections")
        page.get_by_test_id("new-connection").click()
        page.locator(".kind-card").filter(has_text="SQL Server").click()
        assert page.get_by_test_id("conn-port").input_value() == "1433"
        page.goto(BASE + "/connections")
        assert new_connection(
            "SQL Server",
            {
                "conn-name": "sqlsrv",
                "conn-host": "127.0.0.1",
                "conn-port": "14330",
                "conn-db": "ingest_dest_e2e",
                "conn-user": "sa",
                "conn-pass": "Ingest_Test_2026!",
            },
        )

    with step("Add a SQLite connection (server fields hide)"):
        nav("Connections")
        page.get_by_test_id("new-connection").click()
        page.locator(".kind-card").filter(has_text="SQLite").click()
        expect(page.get_by_test_id("conn-host")).to_be_hidden()
        page.goto(BASE + "/connections")
        assert new_connection("SQLite", {"conn-name": "lite", "conn-db": str(WORK / "demo.sqlite")})
        nav("Connections")
        expect(page.get_by_test_id("connection-row")).to_have_count(3)

    # ---- 11. a database table as a source -------------------------------------------------------------
    with step("Preview a source table"):
        page.goto(BASE + "/connections/extdemo")
        page.locator(".tree").get_by_text("orders_feed").click()
        page.wait_for_url("**table=orders_feed")
        expect(page.locator(".rows-table tbody tr")).to_have_count(6)

    with step("Validate a source table against a contract"):
        page.get_by_test_id("use-contract").select_option("txn_test")
        page.get_by_test_id("use-validate").click()
        page.wait_for_url("**/validations/**")
        assert settled_validation() == "invalid"

    with step("Import a source table as a batch"):
        page.goto(BASE + "/connections/extdemo?table=orders_feed")
        page.get_by_test_id("use-contract").select_option("txn_test")
        page.get_by_test_id("use-import").click()
        page.wait_for_url("**/batches/**")
        assert wait_terminal() == "partial"
        assert page.get_by_test_id("counter-accepted").inner_text() == "3"

    # ---- 12. a database as the destination --------------------------------------------------------------
    with step("Draft a contract from a source table, pointing it at SQL Server"):
        page.goto(BASE + "/connections/extdemo?table=orders_feed")
        page.get_by_test_id("use-draft").click()
        page.wait_for_load_state()
        page.fill("#f-name", "orders_sql")
        page.fill("#f-table", "orders_sql")
        page.get_by_test_id("contract-db").select_option("sqlsrv")
        page.get_by_test_id("contract-save").click()
        page.wait_for_url("**/contracts/orders_sql?v=1&saved=1")
        expect(page.locator(".props").first).to_contain_text("sqlsrv")

    with step("Ingest into SQL Server and browse the table there"):
        page.get_by_role("link", name="Ingest with orders_sql@v1").click()
        page.set_input_files("#upload-file-upload", str(FX / "clean_transactions.csv"))
        page.get_by_test_id("upload-submit").click()
        page.wait_for_url("**/batches/**")
        assert wait_terminal() == "completed", page.locator(".callout").all_inner_texts()
        nav("Tables")
        row = page.get_by_test_id("table-row").filter(has_text="sqlsrv")
        expect(row).to_contain_text("orders_sql")
        row.get_by_role("link").first.click()
        page.wait_for_url("**/tables/orders_sql@sqlsrv")
        expect(page.locator(".rows-table tbody tr")).to_have_count(5)

    with step("Re-uploading changed rows updates them in SQL Server"):
        changed = variant(
            "changed.csv", "clean_transactions.csv", lambda t: t.replace("100.50", "111.11", 1)
        )
        upload_page(changed, "orders_sql@v1")
        assert wait_terminal() == "completed"
        page.goto(BASE + "/tables/orders_sql@sqlsrv")
        expect(page.locator(".rows-table tbody tr")).to_have_count(5)
        expect(page.locator(".rows-table")).to_contain_text("111.11")

    with step("Query SQL Server from the SQL editor"):
        nav("SQL editor")
        page.get_by_test_id("sql-db").select_option("sqlsrv")
        expect(page.get_by_test_id("sql-tree")).to_contain_text("orders_sql")
        sql("SELECT count(*) AS n FROM orders_sql")
        expect(page.locator("[data-testid=sql-results] tbody")).to_contain_text("5")

    with step("A connection that is a destination cannot be deleted"):
        page.goto(BASE + "/connections/sqlsrv/edit")
        page.get_by_role("button", name="Delete").click()
        page.wait_for_load_state()
        expect(page.locator(".flash.error")).to_contain_text("destination of orders_sql")

    # ---- 13. runs page ------------------------------------------------------------------------------
    with step("Runs: chart, status filter, time filter and search"):
        nav("Runs")
        expect(page.locator(".chart-col")).to_have_count(14)
        page.get_by_test_id("status-filter").select_option("partial")
        page.wait_for_url("**status=partial**")
        statuses = {
            b.get_attribute("data-status")
            for b in page.locator("tbody [data-testid=status-badge]").all()
        }
        assert statuses == {"partial"}, statuses
        page.get_by_label("Time range").select_option("24h")
        page.wait_for_url("**since=24h**")
        page.get_by_role("link", name="Clear").click()
        page.wait_for_url(BASE + "/")
        page.get_by_test_id("batch-search").fill("clean_scores")
        page.get_by_test_id("filter-submit").click()
        page.wait_for_url("**q=clean_scores**")
        assert page.get_by_test_id("batch-row").count() == 1

    with step("Global search (press /) finds a run"):
        page.goto(BASE + "/contracts")
        page.keyboard.press("/")
        page.keyboard.type("twin")
        page.keyboard.press("Enter")
        page.wait_for_url("**q=twin**")
        assert page.get_by_test_id("batch-row").count() == 2

    with step("Contracts index shows health with run history"):
        nav("Contracts")
        row = page.locator("main tbody tr").filter(has_text="txn_test")
        assert row.locator(".runsq").count() >= 3

    with step("Admin settings: users, workspace settings and roles take effect"):
        page.get_by_test_id("user-menu").click()
        page.get_by_test_id("admin-settings").click()
        page.wait_for_url("**/admin/users")
        page.get_by_test_id("add-user").click()
        page.get_by_test_id("new-user-email").fill("val@example.com")
        page.get_by_test_id("new-user-name").fill("Val Viewer")
        page.get_by_test_id("new-user-role-viewer").check()
        page.get_by_test_id("new-user-submit").click()
        temp = page.locator("[data-temp-password]").inner_text().strip()
        expect(page.get_by_test_id("user-row").filter(has_text="val@example.com")).to_contain_text(
            "Invited"
        )
        viewer_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        v = viewer_ctx.new_page()
        v.goto(BASE + "/")
        v.wait_for_url("**/login**")
        v.get_by_test_id("login-email").fill("val@example.com")
        v.get_by_test_id("login-password").fill(temp)
        v.get_by_test_id("login-submit").click()
        v.wait_for_url("**/settings/profile?must=1")
        v.get_by_test_id("pw-current").fill(temp)
        v.get_by_test_id("pw-new").fill("viewer pass 2026")
        v.get_by_test_id("pw-confirm").fill("viewer pass 2026")
        v.get_by_test_id("pw-save").click()
        v.wait_for_url("**saved=password")
        v.goto(BASE + "/")
        expect(v.locator(".new-btn")).to_have_count(0)
        expect(v.locator("nav.sidebar")).not_to_contain_text("Add data")
        v.get_by_test_id("user-menu").click()
        expect(v.get_by_test_id("admin-settings")).to_have_count(0)
        assert v.goto(BASE + "/upload").status == 403
        assert v.goto(BASE + "/admin/users").status == 403
        v.goto(BASE + "/query")
        expect(v.get_by_test_id("sql-tree")).to_contain_text("batches")
        # turn the SQL editor off for viewers
        page.goto(BASE + "/admin/workspace")
        page.get_by_test_id("ws-viewers-sql").uncheck(force=True)
        page.get_by_test_id("ws-save").click()
        expect(page.get_by_test_id("workspace-saved")).to_be_visible()
        v.goto(BASE + "/")
        expect(v.locator("nav.sidebar")).not_to_contain_text("SQL editor")
        assert v.goto(BASE + "/query").status == 403
        viewer_ctx.close()
        page.goto(BASE + "/admin/audit")
        expect(page.locator("main")).to_contain_text("Changed workspace settings")
        expect(page.locator("main")).to_contain_text("Was refused access")

    with step("System status is under admin settings"):
        page.goto(BASE + "/admin/system")
        expect(page.locator("main")).to_contain_text("Reachable")
        expect(page.locator("main")).to_contain_text("0003_users_and_audit")

    with step("Unknown batch shows the error page"):
        page.goto(BASE + "/batches/does-not-exist")
        expect(page.get_by_test_id("error-state")).to_be_visible()

    # ---- 14. phone width and dark mode ------------------------------------------------------------------
    for scheme, vp in (
        ("light", {"width": 390, "height": 844}),
        ("dark", {"width": 1440, "height": 900}),
    ):
        with step(f"No horizontal scroll and no errors ({scheme}, {vp['width']}px)"):
            # signed in as the admin, so every page renders rather than the sign-in page
            c2 = browser.new_context(viewport=vp, color_scheme=scheme, storage_state=ctx.storage_state())
            p2 = c2.new_page()
            bad = []
            p2.on("pageerror", lambda e: bad.append(str(e)))
            for path in [
                "/",
                "/upload",
                "/contracts",
                "/contracts/txn_test?tab=versions",
                "/tables",
                "/tables/class_scores",
                "/query",
                "/validations",
                "/settings/profile",
                "/admin/users",
                "/admin/workspace",
                "/admin/audit",
                "/connections",
                "/connections/extdemo?table=orders_feed",
                "/rejections",
            ]:
                p2.goto(BASE + path)
                wide = p2.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                if wide:
                    bad.append(f"horizontal scroll on {path}")
                slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or "runs"
                p2.screenshot(
                    path=str(SHOTS / f"{scheme}-{vp['width']}-{slug}.png"), full_page=True
                )
            c2.close()
            assert not bad, bad

    browser.close()

# ---- report ------------------------------------------------------------------------------------
passed = sum(ok for _, ok, _ in results)
width = max(len(n) for n, _, _ in results)
for name, ok, msg in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {msg}")
print(f"\n{passed}/{len(results)} steps passed")
print("browser errors:", errors or "none")
sys.exit(0 if passed == len(results) and not errors else 1)
