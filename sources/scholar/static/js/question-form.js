/* Question editor: show the fields matching the selected type and manage answer options. */
(function () {
  "use strict";
  var form = document.getElementById("question-form");
  if (!form) return;
  var typeSelect = document.getElementById("qtype");
  var choices = document.getElementById("choices");

  function currentType() { return typeSelect.value; }

  function applyType() {
    var t = currentType();
    form.querySelectorAll("[data-types]").forEach(function (el) {
      var types = el.getAttribute("data-types").split(" ");
      var visible = types.indexOf(t) !== -1;
      el.style.display = visible ? "" : "none";
      if (el.classList.contains("qsection")) {
        el.querySelectorAll("input, textarea, select").forEach(function (input) {
          if (input.type !== "hidden") input.disabled = !visible;
        });
      }
    });
  }

  function renumber() {
    choices.querySelectorAll(".choice-row").forEach(function (row, idx) {
      row.querySelector(".letter").textContent = String.fromCharCode(65 + idx);
      row.querySelector(".correct-mark").value = idx;
    });
  }

  document.getElementById("add-choice").addEventListener("click", function () {
    var rows = choices.querySelectorAll(".choice-row");
    var clone = rows[rows.length - 1].cloneNode(true);
    clone.querySelector('input[name="choice_id"]').value = "";
    clone.querySelector('input[name="choice_text"]').value = "";
    clone.querySelector(".correct-mark").checked = false;
    choices.appendChild(clone);
    renumber();
    clone.querySelector('input[name="choice_text"]').focus();
  });

  choices.addEventListener("click", function (event) {
    if (!event.target.classList.contains("remove-choice")) return;
    var rows = choices.querySelectorAll(".choice-row");
    if (rows.length <= 2) return;
    event.target.closest(".choice-row").remove();
    renumber();
  });

  // Single-answer multiple choice: ticking one option clears the others.
  choices.addEventListener("change", function (event) {
    if (!event.target.classList.contains("correct-mark") || currentType() !== "mc" || !event.target.checked) return;
    choices.querySelectorAll(".correct-mark").forEach(function (cb) {
      if (cb !== event.target) cb.checked = false;
    });
  });

  typeSelect.addEventListener("change", applyType);
  applyType();
})();
