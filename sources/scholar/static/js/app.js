/* Scholar System — shared behaviour (no external dependencies) */
(function () {
  "use strict";

  // Confirmation prompts for destructive POST forms
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  }, true);

  // Live filtering of long checkbox lists (e.g. standards)
  document.querySelectorAll("input.filter-list").forEach(function (input) {
    var target = document.querySelector(input.getAttribute("data-target"));
    if (!target) return;
    input.addEventListener("input", function () {
      var term = input.value.trim().toLowerCase();
      target.querySelectorAll("[data-filter]").forEach(function (el) {
        el.style.display = !term || el.getAttribute("data-filter").indexOf(term) !== -1 ? "" : "none";
      });
    });
  });

  // "Select all" checkboxes: <input type=checkbox data-check-all="name">
  document.querySelectorAll("[data-check-all]").forEach(function (box) {
    box.addEventListener("change", function () {
      var name = box.getAttribute("data-check-all");
      document.querySelectorAll('input[type=checkbox][name="' + name + '"]').forEach(function (cb) {
        if (cb.closest("tr") && cb.closest("tr").style.display === "none") return;
        cb.checked = box.checked;
      });
    });
  });

  // Print buttons
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });
})();
