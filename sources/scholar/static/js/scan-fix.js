/* Answer a scanned page by tapping: each question row keeps its answer in one hidden input, and
   its bubbles (on the photo) and letters (below it) are two views of that value.
   "Looks right" confirms Scholar's guess. Recording waits until every uncertain row has been
   looked at, so a guess never goes in unseen. */
(function () {
  "use strict";

  function setup(root) {
    root.querySelectorAll("[data-fix-form]").forEach(function (form) {
      if (form.getAttribute("data-fix-ready")) return;
      form.setAttribute("data-fix-ready", "1");
      var submit = form.querySelector("[data-fix-submit]");
      var help = form.querySelector("[data-fix-help]");
      var defaultHelp = help ? help.textContent : "";

      function labelsOf(row) {
        return Array.prototype.map.call(row.querySelectorAll(".fix-letter"), function (b) { return b.getAttribute("data-label"); });
      }

      function paint(row) {
        var input = row.querySelector('input[type="hidden"]');
        var value = input ? input.value : "";
        row.querySelectorAll("[data-label]").forEach(function (b) {
          var on = value.indexOf(b.getAttribute("data-label")) !== -1;
          b.classList.toggle("on", on);
          if (b.classList.contains("fix-letter")) b.setAttribute("aria-pressed", on ? "true" : "false");
        });
      }

      function refresh() {
        var open = form.querySelectorAll('.fix-row[data-flagged="1"]:not(.confirmed)').length;
        if (!submit) return;
        submit.disabled = open > 0;
        if (help) help.textContent = open ? open + " answer" + (open === 1 ? "" : "s") + " still to look at." : defaultHelp;
      }

      function confirm(row) { row.classList.add("confirmed"); row.classList.remove("flagged"); refresh(); }

      form.addEventListener("click", function (event) {
        var button = event.target.closest("button");
        if (!button || !form.contains(button)) return;
        var row = button.closest(".fix-row");
        if (!row) return;
        var input = row.querySelector('input[type="hidden"]');
        if (!input) return;
        if (button.classList.contains("fix-ok")) { confirm(row); return; }
        if (button.classList.contains("fix-none")) {
          input.value = "";
        } else if (button.hasAttribute("data-label")) {
          var label = button.getAttribute("data-label");
          if (row.getAttribute("data-multi") === "1") {
            var have = input.value.split("");
            var at = have.indexOf(label);
            if (at === -1) have.push(label); else have.splice(at, 1);
            var order = labelsOf(row);
            have.sort(function (a, b) { return order.indexOf(a) - order.indexOf(b); });
            input.value = have.join("");
          } else {
            input.value = label;
          }
        } else {
          return;
        }
        paint(row);
        if (row.getAttribute("data-flagged") === "1") confirm(row);
      });

      form.querySelectorAll("input[data-write]").forEach(function (box) {
        box.addEventListener("input", function () { var row = box.closest(".fix-row"); if (row && box.value.trim()) confirm(row); });
      });
      form.querySelectorAll(".fix-row").forEach(paint);
      refresh();
    });
  }

  window.ScholarScanFix = { setup: setup };
  setup(document);
})();
