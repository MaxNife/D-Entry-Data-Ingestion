// Contract editor: renders the column/rule grids from the spec JSON, keeps the row-key
// choices in sync with the columns, and serialises everything back on save.
(function () {
  var form = document.querySelector("[data-contract-editor]");
  if (!form) return;
  var spec = JSON.parse(form.querySelector("[data-spec]").textContent);
  var types = JSON.parse(form.querySelector("[data-types]").textContent);
  var cols = form.querySelector("[data-cols]"), rules = form.querySelector("[data-rules]");
  var keyBox = form.querySelector("[data-key-cols]");
  var keyed = Array.isArray(spec.target.key) ? spec.target.key.slice() : [];

  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") e.textContent = attrs[k];
      else if (k === "checked" || k === "selected") e[k] = !!attrs[k];
      else e.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { e.appendChild(c); });
    return e;
  }
  function input(cls, name, value, extra) {
    return el("input", Object.assign({ class: "input " + cls, "data-k": name, value: value == null ? "" : value }, extra || {}));
  }
  function removeBtn(label) {
    return el("button", { type: "button", class: "icon-btn sm", "data-remove": "", "aria-label": label, title: label, text: "×" });
  }

  function colRow(c) {
    var type = el("select", { "data-k": "type", class: "input" });
    types.forEach(function (t) { type.appendChild(el("option", { value: t, text: t, selected: t === (c.type || "str") })); });
    var tr = el("tr", { "data-col": "" }, [
      el("td", {}, [input("mono", "source_name", c.source_name, { placeholder: "Header in file", "aria-label": "File header" })]),
      el("td", {}, [input("mono", "name", c.name, { placeholder: "column_name", "aria-label": "Column name" })]),
      el("td", {}, [type]),
      el("td", { class: "center" }, [el("input", { type: "checkbox", "data-k": "required", checked: c.required, "aria-label": "Required" })]),
      el("td", {}, [input("", "in_set", (c.in_set || []).join(", "), { placeholder: "—", title: "Comma-separated allowed values", "aria-label": "Allowed values" })]),
      el("td", {}, [input("mono", "pattern", c.pattern, { placeholder: "—", title: "Regular expression the value must match", "aria-label": "Pattern" })]),
      el("td", {}, [input("num", "min", c.min, { type: "number", step: "any", placeholder: "—", "aria-label": "Minimum" })]),
      el("td", {}, [input("num", "max", c.max, { type: "number", step: "any", placeholder: "—", "aria-label": "Maximum" })]),
      el("td", {}, [input("num", "max_length", c.max_length, { type: "number", min: "1", placeholder: "—", "aria-label": "Maximum length" })]),
      el("td", {}, [removeBtn("Remove column")]),
    ]);
    // Typing a header fills an empty column name with its snake_case form.
    var src = tr.querySelector('[data-k="source_name"]'), nm = tr.querySelector('[data-k="name"]');
    src.addEventListener("input", function () {
      if (nm.dataset.touched) return;
      nm.value = src.value.trim().replace(/([a-z0-9])([A-Z])/g, "$1_$2").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
      syncKeys();
    });
    nm.addEventListener("input", function () { nm.dataset.touched = "1"; syncKeys(); });
    if (c.name) nm.dataset.touched = "1";
    return tr;
  }

  function ruleRow(r) {
    var custom = r.function || r.custom || (r.check && r.check !== "exists_in" && !/^\s*(>=|<=|==|!=|>|<)/.test(r.check));
    var kind = el("select", { "data-k": "kind", class: "input" }, [
      el("option", { value: "compare", text: "Compare", selected: !custom && r.check !== "exists_in" }),
      el("option", { value: "exists_in", text: "Exists in table", selected: r.check === "exists_in" }),
    ]);
    var ref = r.reference || {};
    var expr = r.check === "exists_in" ? (ref.table ? ref.table + "." + (ref.column || "") : "") : (r.check || "");
    var column = el("select", { "data-k": "column", class: "input", "data-rule-col": r.column || "" });
    var tr = el("tr", { "data-rule": "" }, [
      el("td", {}, [input("mono", "code", r.code, { placeholder: "AMOUNT_NEGATIVE", "aria-label": "Reason code" })]),
      el("td", {}, [column]),
      el("td", {}, [kind]),
      el("td", {}, [input("mono", "expr", expr, { placeholder: ">= 0   or   table.column", "aria-label": "Expression or reference" })]),
      el("td", {}, [input("", "message", r.message, { placeholder: "Shown to whoever reads the rejection", "aria-label": "Message" })]),
      el("td", {}, [removeBtn("Remove rule")]),
    ]);
    if (custom) { tr.dataset.custom = JSON.stringify(r); tr.classList.add("is-custom"); tr.title = "Custom Python rule: edit it in the YAML tab"; tr.querySelectorAll("input,select").forEach(function (i) { i.disabled = true; }); }
    return tr;
  }

  function colNames() {
    return Array.prototype.map.call(cols.querySelectorAll('[data-k="name"]'), function (i) { return i.value.trim(); }).filter(Boolean);
  }
  function syncKeys() {
    var names = colNames();
    var checked = Array.prototype.map.call(keyBox.querySelectorAll("input:checked"), function (i) { return i.value; });
    if (keyBox.childElementCount) keyed = checked;
    keyBox.innerHTML = "";
    names.forEach(function (n) {
      keyBox.appendChild(el("label", { class: "check chip-check" }, [el("input", { type: "checkbox", value: n, checked: keyed.indexOf(n) >= 0 }), document.createTextNode(" " + n)]));
    });
    rules.querySelectorAll('[data-k="column"]').forEach(function (sel) {
      var cur = sel.value || sel.getAttribute("data-rule-col");
      sel.innerHTML = "";
      sel.appendChild(el("option", { value: "", text: "(whole row)" }));
      names.forEach(function (n) { sel.appendChild(el("option", { value: n, text: n, selected: n === cur })); });
    });
    form.querySelector("[data-col-count]").textContent = names.length + (names.length === 1 ? " column" : " columns");
    form.querySelector("[data-no-rules]").hidden = rules.childElementCount > 0;
  }

  (spec.columns || []).forEach(function (c) { cols.appendChild(colRow(c)); });
  (spec.rules || []).forEach(function (r) { rules.appendChild(ruleRow(r)); });
  syncKeys();

  form.querySelector("[data-add-col]").addEventListener("click", function () {
    var tr = colRow({ type: "str", required: true }); cols.appendChild(tr); tr.querySelector("input").focus(); syncKeys();
  });
  form.querySelector("[data-add-rule]").addEventListener("click", function () {
    var tr = ruleRow({ check: ">= 0" }); rules.appendChild(tr); syncKeys(); tr.querySelector("input").focus();
  });
  form.addEventListener("click", function (e) {
    var b = e.target.closest("[data-remove]"); if (!b) return;
    b.closest("tr").remove(); syncKeys();
  });

  // Form / YAML tabs. The active tab decides what gets saved.
  form.querySelectorAll("[data-tab]").forEach(function (t) {
    t.addEventListener("click", function (e) {
      e.preventDefault();
      var which = t.getAttribute("data-tab");
      form.querySelectorAll("[data-tab]").forEach(function (x) { x.setAttribute("aria-selected", String(x === t)); });
      form.querySelectorAll("[data-pane]").forEach(function (p) { p.hidden = p.getAttribute("data-pane") !== which; });
      form.querySelector("[data-editor-mode]").value = which;
      // The hidden form tab's required fields must not block a save from the YAML tab;
      // the server validates the YAML either way.
      form.noValidate = which === "yaml";
    });
  });

  function val(tr, k) { var i = tr.querySelector('[data-k="' + k + '"]'); return i ? (i.type === "checkbox" ? i.checked : i.value.trim()) : ""; }
  function num(v) { return v === "" ? null : Number(v); }

  form.addEventListener("submit", function () {
    if (form.querySelector("[data-editor-mode]").value === "yaml") return;
    var out = {
      contract: form.querySelector('[data-f="contract"]').value.trim(),
      description: form.querySelector('[data-f="description"]').value.trim(),
      source: {
        formats: Array.prototype.map.call(form.querySelectorAll("[data-format]:checked"), function (i) { return i.getAttribute("data-format"); }),
        encoding: form.querySelector('[data-f="source.encoding"]').value,
        delimiter: form.querySelector('[data-f="source.delimiter"]').value,
      },
      target: {
        table: form.querySelector('[data-f="target.table"]').value.trim(),
        on_conflict: form.querySelector('[data-f="target.on_conflict"]').value,
        connection: form.querySelector('[data-f="target.connection"]').value || undefined,
        key: form.querySelector('[data-keymode][value="cols"]').checked
          ? Array.prototype.map.call(keyBox.querySelectorAll("input:checked"), function (i) { return i.value; })
          : "row_hash",
      },
      columns: Array.prototype.map.call(cols.querySelectorAll("[data-col]"), function (tr) {
        return {
          name: val(tr, "name"), source_name: val(tr, "source_name") || val(tr, "name"), type: val(tr, "type"),
          required: val(tr, "required"), in_set: val(tr, "in_set"), pattern: val(tr, "pattern"),
          min: num(val(tr, "min")), max: num(val(tr, "max")), max_length: num(val(tr, "max_length")),
        };
      }),
      rules: Array.prototype.map.call(rules.querySelectorAll("[data-rule]"), function (tr) {
        if (tr.dataset.custom) return JSON.parse(tr.dataset.custom);
        var r = { code: val(tr, "code"), column: val(tr, "column") || null, message: val(tr, "message") || val(tr, "code") };
        if (val(tr, "kind") === "exists_in") {
          var parts = val(tr, "expr").split(".");
          r.check = "exists_in"; r.reference = { table: parts[0] || "", column: parts.slice(1).join(".") || "" };
        } else { r.check = val(tr, "expr"); }
        return r;
      }),
    };
    if (!out.source.formats.length) out.source.formats = ["csv"];
    if (Array.isArray(out.target.key) && !out.target.key.length) out.target.key = "row_hash";
    form.querySelector("[data-spec-json]").value = JSON.stringify(out);
  });
})();
