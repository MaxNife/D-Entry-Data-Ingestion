// SQL editor: a table list, a highlighted editor with autocomplete, and a results grid.
// Queries run through POST /api/sql/run; recent queries are remembered in this browser.
(function () {
  "use strict";
  var root = document.querySelector("[data-sql-ide]");
  if (!root || !window.CodeMirror) return;

  var $ = function (sel) { return root.querySelector(sel); };
  var initial = JSON.parse($("[data-initial]").textContent);
  var RECENT_KEY = "ingest.recentQueries";
  var DRAFT_KEY = "ingest.sqlDraft";
  var schema = { dialect: "postgresql", tables: [] };
  var last = null;  // { sql, db } of the last successful query, for export

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function fmtNum(n) { return Number(n).toLocaleString("en-US"); }
  function fmtMs(ms) { return ms < 1000 ? Math.round(ms) + " ms" : (ms / 1000).toFixed(2) + " s"; }
  function readJSON(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch (e) { return fallback; } }
  function writeJSON(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {} }

  // ---- editor ------------------------------------------------------------------------
  var editor = CodeMirror.fromTextArea($("[data-editor]"), {
    mode: "text/x-pgsql",
    lineNumbers: true,
    matchBrackets: true,
    indentUnit: 2,
    tabSize: 2,
    hintOptions: { tables: {}, completeSingle: false },
    extraKeys: {
      "Ctrl-Enter": function () { run(); },
      "Cmd-Enter": function () { run(); },
      "Ctrl-Space": "autocomplete",
    },
  });
  window.sqlEditor = editor;
  editor.on("inputRead", function (cm, change) {
    if (!cm.state.completionActive && change.origin === "+input" && /^[A-Za-z_.]$/.test(change.text[0])) {
      cm.showHint({ completeSingle: false });
    }
  });
  editor.on("change", function () { writeJSON(DRAFT_KEY, editor.getValue()); });

  // ---- tables --------------------------------------------------------------------------
  function quoteIdent(name) {
    if (/^[a-z_][a-z0-9_]*$/.test(name)) return name;
    return schema.dialect === "mssql" ? "[" + name.replace(/]/g, "]]") + "]" : '"' + name.replace(/"/g, '""') + '"';
  }
  var schemaSeq = 0;  // only the latest request may draw the list (an earlier one can finish last)
  function loadSchema() {
    var tree = $("[data-tree]"), seq = ++schemaSeq;
    tree.setAttribute("aria-busy", "true");
    tree.innerHTML = '<div class="skeleton-list" aria-hidden="true">' + new Array(8).join('<span class="skeleton"></span>') + "</div>";
    return fetch("/api/sql/schema?db=" + encodeURIComponent($("[data-db]").value))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (seq !== schemaSeq) return;
        tree.setAttribute("aria-busy", "false");
        if (!data.ok) { tree.innerHTML = '<div class="tree-error">' + esc(data.error) + "</div>"; return; }
        schema = { dialect: data.dialect, tables: data.tables };
        editor.setOption("mode", data.mode);
        var hint = {};
        data.tables.forEach(function (t) { hint[t.name] = t.columns.map(function (c) { return c.name; }); });
        editor.setOption("hintOptions", { tables: hint, completeSingle: false });
        renderTree();
      })
      .catch(function () {
        if (seq !== schemaSeq) return;
        tree.setAttribute("aria-busy", "false");
        tree.innerHTML = '<div class="tree-error">Could not load the tables. Check the connection and try again.</div>';
      });
  }
  function renderTree() {
    var q = ($("[data-obj-filter]").value || "").toLowerCase();
    var items = schema.tables.filter(function (t) { return !q || t.name.toLowerCase().indexOf(q) >= 0; });
    if (!items.length) {
      $("[data-tree]").innerHTML = '<div class="tree-empty hint">' + (q ? "No tables match." : "This database has no tables yet.") + "</div>";
      return;
    }
    $("[data-tree]").innerHTML = '<ul class="obj-list">' + items.map(function (t) {
      return '<li class="obj"><details><summary class="obj-row"><span class="obj-caret" aria-hidden="true">›</span>' +
        '<span class="obj-name" data-insert="' + esc(quoteIdent(t.name)) + '" title="Insert the table name">' + esc(t.name) + "</span>" +
        '<button type="button" class="obj-act" data-top="' + esc(t.name) + '" title="Show the first 100 rows">Top 100</button></summary>' +
        '<ul class="col-list">' + t.columns.map(function (c) {
          return '<li class="col" data-insert="' + esc(quoteIdent(c.name)) + '" title="Insert the column name"><span class="col-name">' + esc(c.name) +
            '</span><span class="col-type">' + esc(c.type.toLowerCase()) + "</span></li>";
        }).join("") + "</ul></details></li>";
    }).join("") + "</ul>";
  }
  $("[data-obj-filter]").addEventListener("input", renderTree);
  $("[data-db]").addEventListener("change", loadSchema);
  $("[data-tree]").addEventListener("click", function (e) {
    var top = e.target.closest("[data-top]"), ins = e.target.closest("[data-insert]");
    if (top) {
      e.preventDefault();
      var t = quoteIdent(top.dataset.top);
      editor.setValue(schema.dialect === "mssql" ? "SELECT TOP 100 *\nFROM " + t + ";" : "SELECT *\nFROM " + t + "\nLIMIT 100;");
      run();
    } else if (ins) {
      if (ins.closest("summary")) e.preventDefault();
      editor.replaceSelection(ins.dataset.insert);
      editor.focus();
    }
  });

  // ---- run -----------------------------------------------------------------------------
  // The selection if there is one, otherwise the ";"-separated statement at the cursor.
  function currentStatement() {
    if (editor.somethingSelected()) return editor.getSelection();
    var text = editor.getValue(), at = editor.indexFromPos(editor.getCursor());
    var start = 0, q = null, parts = [];
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (q) { if (ch === q) q = null; continue; }
      if (ch === "'" || ch === '"') q = ch;
      else if (ch === ";") { parts.push([start, i]); start = i + 1; }
    }
    parts.push([start, text.length]);
    parts = parts.filter(function (p) { return text.slice(p[0], p[1]).trim(); });
    if (!parts.length) return "";
    var hit = parts.find(function (p) { return at >= p[0] && at <= p[1] + 1; }) || parts[parts.length - 1];
    return text.slice(hit[0], hit[1]);
  }
  var timer = null;
  function setRunning(on) {
    $("[data-running]").hidden = !on;
    $("[data-run]").disabled = on;
    clearInterval(timer);
    if (on) {
      var t0 = performance.now();
      timer = setInterval(function () { $("[data-run-timer]").textContent = ((performance.now() - t0) / 1000).toFixed(1) + " s"; }, 100);
    }
  }
  function run() {
    var sql = currentStatement().trim();
    if (!sql) { showError("Write a query to run."); return; }
    var db = $("[data-db]").value;
    setRunning(true);
    fetch("/api/sql/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql: sql, db: db }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        setRunning(false);
        if (!data.ok) { showError(data.error); return; }
        last = { sql: data.statement, db: db };
        remember(sql, db);
        renderGrid(data);
      })
      .catch(function () { setRunning(false); showError("The request did not complete. Check your connection and try again."); });
  }
  $("[data-run]").addEventListener("click", run);

  // ---- results ---------------------------------------------------------------------------
  function cell(v) {
    if (v === null || v === undefined) return '<td class="is-null">NULL</td>';
    if (typeof v === "number") return '<td class="num">' + esc(v) + "</td>";
    return "<td>" + esc(v) + "</td>";
  }
  function renderGrid(data) {
    if (!data.columns.length) {
      $("[data-grid]").innerHTML = '<div class="ide-empty hint">The statement returned no columns.</div>';
    } else {
      var head = '<tr><th class="rownum"></th>' + data.columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr>";
      var body = data.rows.map(function (r, i) { return '<tr><td class="rownum">' + (i + 1) + "</td>" + r.map(cell).join("") + "</tr>"; }).join("");
      if (!data.rows.length) body = '<tr><td class="rownum"></td><td colspan="' + data.columns.length + '" class="hint">The query returned no rows.</td></tr>';
      $("[data-grid]").innerHTML = '<table class="rgrid" data-testid="sql-results"><thead>' + head + "</thead><tbody>" + body + "</tbody></table>";
    }
    $("[data-row-info]").textContent = fmtNum(data.row_count) + (data.truncated ? "+" : "") + " " + (data.row_count === 1 ? "row" : "rows") +
      " · " + fmtMs(data.elapsed_ms) + (data.truncated ? " · showing the first " + fmtNum(data.limit) + "; export for all rows" : "");
  }
  function showError(text) {
    $("[data-row-info]").textContent = "";
    $("[data-grid]").innerHTML = '<div class="callout bad" role="alert" data-testid="sql-error"><strong>The query did not run.</strong><div class="callout-detail">' + esc(text) + "</div></div>";
  }

  // ---- export & recent --------------------------------------------------------------------------
  root.querySelectorAll("[data-export]").forEach(function (a) {
    a.addEventListener("click", function (e) {
      e.preventDefault();
      a.closest("details").open = false;
      if (!last) { showError("Run a query first. Export downloads all rows of the last query."); return; }
      window.location.href = "/query/export?sql=" + encodeURIComponent(last.sql) + "&db=" + encodeURIComponent(last.db) + "&format=" + a.dataset.export;
    });
  });
  function remember(sql, db) {
    var recent = readJSON(RECENT_KEY, []).filter(function (r) { return r && r.sql !== sql; });
    recent.unshift({ sql: sql, db: db });
    writeJSON(RECENT_KEY, recent.slice(0, 10));
    renderRecent();
  }
  function renderRecent() {
    var recent = readJSON(RECENT_KEY, []).filter(function (r) { return r && typeof r.sql === "string"; });
    var box = $("[data-recent]");
    if (!recent.length) return;
    box.innerHTML = "";
    recent.forEach(function (r) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "recent-q mono"; b.textContent = r.sql.replace(/\s+/g, " ").slice(0, 120);
      b.title = (r.db || "Platform database");
      b.addEventListener("click", function () {
        editor.setValue(r.sql);
        if ((r.db || "") !== $("[data-db]").value) { $("[data-db]").value = r.db || ""; loadSchema(); }
        b.closest("details").open = false; editor.focus();
      });
      box.appendChild(b);
    });
  }

  // ---- start ----------------------------------------------------------------------------------
  renderRecent();
  editor.setValue(initial.sql || readJSON(DRAFT_KEY, "") || "");
  loadSchema().then(function () { if (root.dataset.autorun === "1") run(); });
  editor.focus();
})();
