/* The answer-key editor for a test made from a PDF: one row per question, tap the correct
   letter, or paste the whole key at once. */
(function () {
  "use strict";

  /* ---- reading a pasted key: "1.B 2.D", "1 B" on lines, "BDACC", "B D A C", "AC" for several ---- */
  function parseKey(text) {
    var out = [];
    text = String(text || "").replace(/–|—/g, "-");
    var pair = /(\d{1,3})\s*[.):=\-]?\s*([A-Za-z]+(?:\s*,\s*[A-Za-z]+)*|-?\d+(?:\.\d+)?)/g, m;
    while ((m = pair.exec(text))) out.push([parseInt(m[1], 10), m[2].replace(/\s+/g, "")]);
    var lettersOnly = text.replace(/[\s,;]+/g, "");
    if (out.length && !/^[A-Za-z]+$/.test(lettersOnly)) return out;       // numbered
    var tokens = text.match(/[A-Za-z]+/g) || [];
    out = [];
    if (tokens.length === 1) tokens = tokens[0].split("");                // BDACC
    else if (tokens.length > 1 && tokens.every(function (t) { return t.length === 1; })) tokens = tokens;  // B D A C
    tokens.forEach(function (t, i) { out.push([i + 1, t]); });            // AC BD …: one token a question
    return out;
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { parseKey: parseKey };
  if (typeof document === "undefined") return;

  var form = document.getElementById("key-form");
  if (!form) return;
  var body = document.getElementById("key-rows");
  var count = document.getElementById("key-count");
  var template = document.getElementById("key-row-template");
  var total = document.getElementById("key-total");
  var locked = form.getAttribute("data-locked") === "1";
  var MAX = 300;

  function rows() { return Array.prototype.slice.call(body.querySelectorAll(".key-row")); }
  function el(row, sel) { return row.querySelector(sel); }
  function labelsFor(type, n) {
    if (type === "tf") return ["T", "F"];
    var out = [];
    for (var i = 0; i < n; i++) out.push(String.fromCharCode(65 + i));
    return out;
  }

  function addUp() {
    var sum = 0;
    rows().forEach(function (r) { var v = parseFloat(el(r, ".key-points").value); if (v > 0) sum += v; });
    total.textContent = String(Math.round(sum * 100) / 100);
  }

  function paint(row) {
    var type = el(row, ".key-type").value, n = parseInt(el(row, ".key-choices").value, 10) || 4;
    var value = el(row, ".key-value"), letters = el(row, ".key-letters");
    row.setAttribute("data-type", type);
    var isChoice = type === "mc" || type === "ms" || type === "tf";
    value.type = (type === "numeric" || type === "short") ? "text" : "hidden";
    letters.textContent = "";
    if (isChoice) {
      labelsFor(type, n).forEach(function (label) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "fix-letter"; b.setAttribute("data-label", label); b.textContent = label;
        var on = value.value.indexOf(label) !== -1;
        b.classList.toggle("on", on); b.setAttribute("aria-pressed", on ? "true" : "false");
        letters.appendChild(b);
      });
    }
    row.classList.toggle("missing", type !== "essay" && !value.value.trim());
  }

  function renumber() {
    rows().forEach(function (row, i) {
      var n = i + 1;
      el(row, ".key-n").textContent = n;
      [["type", ".key-type"], ["choices", ".key-choices"], ["answer", ".key-value"], ["points", ".key-points"], ["tol", ".key-tol input"]].forEach(function (p) {
        var input = el(row, p[1]); if (input) input.name = p[0] + "_" + n;
      });
    });
    count.value = rows().length;
    addUp();
  }

  function fit(row) {                       // keep the answer valid for the row's type and number of choices
    var type = el(row, ".key-type").value, n = parseInt(el(row, ".key-choices").value, 10) || 4, value = el(row, ".key-value");
    if (type === "mc" || type === "ms" || type === "tf") {
      var valid = labelsFor(type, n);
      var kept = value.value.toUpperCase().split("").filter(function (c) { return valid.indexOf(c) !== -1; });
      kept = kept.filter(function (c, i) { return kept.indexOf(c) === i; });
      if (type !== "ms") kept = kept.slice(0, 1);
      value.value = kept.join("");
    } else if (type === "essay") {
      value.value = "";
    }
    paint(row);
  }

  function newRow(type, choices) {
    var row = template.content.querySelector("tr").cloneNode(true);
    el(row, ".key-type").value = type || "mc";
    el(row, ".key-choices").value = String(choices || 4);
    body.appendChild(row);
    return row;
  }

  /* ---- the answer for one row, from pasted text ---- */
  function setAnswer(row, raw, asTf) {
    var type = el(row, ".key-type"), choices = el(row, ".key-choices"), value = el(row, ".key-value");
    var a = raw.trim(), upper = a.toUpperCase();
    if (/^-?\d+(\.\d+)?$/.test(a)) { type.value = "numeric"; value.value = a; return; }
    if (/^(TRUE|T)$/.test(upper) || /^FALSE$/.test(upper) || (upper === "F" && (asTf || type.value === "tf"))) {
      type.value = "tf"; value.value = upper.charAt(0); return;
    }
    if (/^[A-H]+$/.test(upper)) {
      var letters = upper.split("").filter(function (c, i, all) { return all.indexOf(c) === i; }).sort();
      var need = letters[letters.length - 1].charCodeAt(0) - 64;
      if (!locked) {
        type.value = letters.length > 1 ? "ms" : (type.value === "ms" ? "ms" : "mc");
        if (type.value === "tf" || type.value === "numeric" || type.value === "short") type.value = letters.length > 1 ? "ms" : "mc";
        choices.value = String(Math.max(parseInt(choices.value, 10) || 4, Math.min(8, need)));
      }
      value.value = letters.join("");
      return;
    }
    if (!locked) type.value = "short";
    value.value = a;
  }

  function fill(text) {
    var pairs = parseKey(text);
    if (!pairs.length) return "Nothing that looks like an answer was found.";
    // T always means True; F is False when its neighbours are true/false answers too ("6.T 7.F"), else choice F
    var sure = pairs.map(function (p) { return /^(TRUE|FALSE|T)$/i.test(p[1]); });
    var isF = pairs.map(function (p) { return /^F$/i.test(p[1]); });
    var tf = sure.slice();
    for (var pass = 0, changed = true; changed && pass < pairs.length; pass++) {
      changed = false;
      for (var i = 0; i < pairs.length; i++) {
        if (!tf[i] && isF[i] && ((i > 0 && tf[i - 1]) || (i + 1 < pairs.length && tf[i + 1]))) { tf[i] = true; changed = true; }
      }
    }
    var made = 0, last = 0;
    pairs.forEach(function (p, index) {
      var n = p[0]; if (n < 1 || n > MAX) return;
      while (rows().length < n && !locked && rows().length < MAX) { newRow("mc", 4); made++; }
      var row = rows()[n - 1]; if (!row) return;
      setAnswer(row, p[1], tf[index]);
      fit(row);
      last = Math.max(last, n);
    });
    renumber();
    rows().forEach(paint);
    return pairs.length + " answer" + (pairs.length === 1 ? "" : "s") + " filled in" + (made ? " (" + made + " rows added)" : "") + ". Check them against the PDF, then save.";
  }

  /* ---- events ---- */
  body.addEventListener("click", function (event) {
    var b = event.target.closest(".fix-letter"); if (!b) return;
    var row = b.closest(".key-row"), type = el(row, ".key-type").value, value = el(row, ".key-value"), label = b.getAttribute("data-label");
    if (type === "ms") {
      var have = value.value.split("").filter(Boolean), at = have.indexOf(label);
      if (at === -1) have.push(label); else have.splice(at, 1);
      value.value = have.sort().join("");
    } else {
      value.value = label;
    }
    paint(row);
  });
  body.addEventListener("change", function (event) {
    var row = event.target.closest(".key-row"); if (!row) return;
    if (event.target.classList.contains("key-type")) {
      var type = event.target.value;
      if (type === "tf") el(row, ".key-choices").value = "2";
      else if (el(row, ".key-choices").value === "2" && type !== "tf") el(row, ".key-choices").value = "4";
      fit(row);
    } else if (event.target.classList.contains("key-choices")) {
      fit(row);
    } else if (event.target.classList.contains("key-points")) {
      addUp();
    }
  });
  body.addEventListener("input", function (event) {
    var row = event.target.closest(".key-row"); if (!row) return;
    if (event.target.classList.contains("key-value")) row.classList.toggle("missing", !event.target.value.trim());
    if (event.target.classList.contains("key-points")) addUp();
  });
  document.getElementById("key-fill").addEventListener("click", function () {
    document.getElementById("key-paste-msg").textContent = fill(document.getElementById("key-paste").value);
  });
  var add = document.getElementById("key-add");
  if (add) {
    add.addEventListener("click", function () {
      var last = rows()[rows().length - 1];
      var n = Math.max(1, Math.min(50, parseInt(document.getElementById("key-add-count").value, 10) || 5));
      for (var i = 0; i < n && rows().length < MAX; i++) newRow(last ? el(last, ".key-type").value : "mc", last ? parseInt(el(last, ".key-choices").value, 10) : 4);
      rows().forEach(paint); renumber();
    });
    document.getElementById("key-remove").addEventListener("click", function () {
      var all = rows(); if (all.length > 1) { body.removeChild(all[all.length - 1]); renumber(); }
    });
  }
  form.addEventListener("submit", function (event) {
    // a row still empty at the end is a spare; one empty in the middle is a mistake worth a look
    var all = rows(), lastFilled = -1;
    all.forEach(function (r, i) { if (el(r, ".key-value").value.trim() || el(r, ".key-type").value === "essay") lastFilled = i; });
    var gaps = all.slice(0, lastFilled + 1).filter(function (r) { return r.classList.contains("missing"); });
    if (gaps.length) {
      event.preventDefault();
      gaps[0].scrollIntoView({ block: "center" });
      document.getElementById("key-paste-msg").textContent = "Question " + el(gaps[0], ".key-n").textContent + " has no answer yet.";
      var d = document.querySelector(".key-paste"); if (d) d.open = true;
    }
  });

  rows().forEach(paint);
  renumber();
})();
