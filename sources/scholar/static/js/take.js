/* Online test taking: countdown, autosave, progress navigator, submit confirmation. */
(function () {
  "use strict";
  var form = document.getElementById("take-form");
  if (!form) return;
  var preview = form.hasAttribute("data-preview");
  var saveUrl = form.getAttribute("data-save-url");
  var stateEl = document.getElementById("save-state");
  var timerEl = document.getElementById("timer");
  var remaining = timerEl ? parseInt(timerEl.getAttribute("data-remaining"), 10) : null;
  var dirty = false, saving = false, submitted = false;

  function setState(text) { if (stateEl) stateEl.textContent = text; }

  function formatTime(s) {
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    var mm = (h ? String(m).padStart(2, "0") : String(m)), ss = String(sec).padStart(2, "0");
    return (h ? h + ":" : "") + mm + ":" + ss;
  }

  function updateNavigator() {
    document.querySelectorAll(".question[data-item]").forEach(function (q) {
      var id = q.getAttribute("data-item");
      var answered = false;
      q.querySelectorAll("input, textarea").forEach(function (input) {
        if (input.type === "hidden") return;
        if ((input.type === "radio" || input.type === "checkbox") ? input.checked : input.value.trim() !== "") answered = true;
      });
      var link = document.querySelector('.qnav a[data-item="' + id + '"]');
      if (link) link.classList.toggle("answered", answered);
    });
    var total = document.querySelectorAll(".question[data-item]").length;
    var done = document.querySelectorAll(".qnav a.answered").length;
    var counter = document.getElementById("answered-count");
    if (counter) counter.textContent = done + " / " + total + " answered";
    return total - done;
  }

  function save(force) {
    if (preview || submitted || saving || (!dirty && !force)) return Promise.resolve();
    saving = true;
    dirty = false;
    setState("Saving…");
    var data = new FormData(form);
    return fetch(saveUrl, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { "X-CSRFToken": data.get("csrf_token"), "Accept": "application/json" }
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        if (resp.status === 409 && body.redirect) {
          submitted = true;
          window.location.href = body.redirect;
          return;
        }
        if (!resp.ok || !body.ok) throw new Error("save failed");
        if (typeof body.remaining === "number") remaining = body.remaining;
        setState("All changes saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      });
    }).catch(function () {
      dirty = true;
      setState("⚠ Not saved — check your connection. Retrying…");
    }).finally(function () { saving = false; });
  }

  form.addEventListener("input", function () { dirty = true; updateNavigator(); setState(preview ? "Preview — answers are not saved" : "Unsaved changes"); });
  form.addEventListener("change", function () { dirty = true; updateNavigator(); save(); });
  setInterval(function () { save(); }, 15000);

  // flag for review
  document.querySelectorAll(".flag-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.getAttribute("data-item");
      var link = document.querySelector('.qnav a[data-item="' + id + '"]');
      var on = !btn.classList.contains("btn-primary");
      btn.classList.toggle("btn-primary", on);
      btn.textContent = on ? "★ Flagged" : "☆ Flag for review";
      if (link) link.classList.toggle("flagged", on);
    });
  });

  if (timerEl && !isNaN(remaining)) {
    var tick = function () {
      timerEl.textContent = formatTime(Math.max(0, remaining));
      timerEl.classList.toggle("warn", remaining <= 300 && remaining > 60);
      timerEl.classList.toggle("danger", remaining <= 60);
      if (remaining <= 0 && !preview && !submitted) {
        submitted = true;
        setState("Time is up — submitting…");
        window.onbeforeunload = null;
        HTMLFormElement.prototype.submit.call(form);
        return;
      }
      remaining -= 1;
    };
    tick();
    setInterval(tick, 1000);
  }

  form.addEventListener("submit", function (event) {
    if (preview) { event.preventDefault(); alert("This is a preview — nothing is submitted."); return; }
    var left = updateNavigator();
    var msg = left > 0 ? "You have " + left + " unanswered question(s). Submit anyway? You cannot change your answers afterwards."
                       : "Submit your answers? You cannot change them afterwards.";
    if (!window.confirm(msg)) { event.preventDefault(); return; }
    submitted = true;
    window.onbeforeunload = null;
  });

  window.onbeforeunload = function () {
    if (!preview && dirty && !submitted) { save(true); return "Your latest answers may not be saved yet."; }
  };
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") save(true); });

  updateNavigator();
})();
