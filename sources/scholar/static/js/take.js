/* Online test taking: countdown, autosave, navigator, submit confirmation, and the test's
   security rules (full screen, leaving the page, copy/paste). The browser only reports what
   happens; the server counts, decides and locks. */
(function () {
  "use strict";
  var form = document.getElementById("take-form");
  if (!form) return;
  var preview = form.hasAttribute("data-preview");
  var saveUrl = form.getAttribute("data-save-url");
  var statusUrl = form.getAttribute("data-status-url");
  var eventUrl = form.getAttribute("data-event-url");
  var wantFullscreen = form.hasAttribute("data-fullscreen");
  var blockCopy = form.hasAttribute("data-block-copy");
  var stateEl = document.getElementById("save-state");
  var timerEl = document.getElementById("timer");
  var pausedGate = document.getElementById("paused-gate");
  var fullscreenGate = document.getElementById("fullscreen-gate");
  var remaining = timerEl ? parseInt(timerEl.getAttribute("data-remaining"), 10) : null;
  var dirty = false, saving = false, submitted = false, locked = false;
  var csrf = (form.querySelector("input[name=csrf_token]") || {}).value || "";

  function setState(text) { if (stateEl) stateEl.textContent = text; }

  function formatTime(s) {
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    var mm = (h ? String(m).padStart(2, "0") : String(m)), ss = String(sec).padStart(2, "0");
    return (h ? h + ":" : "") + mm + ":" + ss;
  }

  function leaveTo(url) {
    submitted = true;
    window.onbeforeunload = null;
    window.location.href = url;
  }

  /* Server state: time left, paused, closed or signed out. */
  function applyState(resp, body) {
    if ((resp.status === 409 || resp.status === 401) && body.redirect) { leaveTo(body.redirect); return false; }
    if (typeof body.remaining === "number") remaining = body.remaining;
    setLocked(!!body.locked, body.lock_reason);
    return true;
  }

  function setLocked(on, reason) {
    if (on === locked) return;
    locked = on;
    if (pausedGate) {
      pausedGate.hidden = !on;
      var why = document.getElementById("paused-reason");
      if (why && reason) why.textContent = reason + ".";
    }
    form.classList.toggle("is-hidden", on);
    if (!on) setState("Your proctor let you continue.");
  }

  function post(url, data) {
    return fetch(url, {
      method: "POST",
      body: data,
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrf, "Accept": "application/json" }
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) { return [resp, body]; });
    });
  }

  function report(kind) {
    if (preview || submitted || !eventUrl) return;
    var data = new FormData();
    data.append("kind", kind);
    data.append("csrf_token", csrf);
    post(eventUrl, data).then(function (r) { applyState(r[0], r[1]); }).catch(function () {});
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
    if (preview || submitted || saving || locked || (!dirty && !force)) return Promise.resolve();
    saving = true;
    dirty = false;
    setState("Saving…");
    return post(saveUrl, new FormData(form)).then(function (r) {
      var resp = r[0], body = r[1];
      if (!applyState(resp, body)) return;
      if (resp.status === 423) { dirty = true; return; }
      if (!resp.ok || !body.ok) throw new Error("save failed");
      setState("All changes saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
    }).catch(function () {
      dirty = true;
      setState("Not saved: check your connection. Retrying…");
    }).finally(function () { saving = false; });
  }

  function poll() {
    if (preview || submitted || !statusUrl) return;
    fetch(statusUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (resp) { return resp.json().catch(function () { return {}; }).then(function (body) { applyState(resp, body); }); })
      .catch(function () {});
  }

  form.addEventListener("input", function () { dirty = true; updateNavigator(); setState(preview ? "Preview: answers are not saved" : "Unsaved changes"); });
  form.addEventListener("change", function () { dirty = true; updateNavigator(); save(); });
  setInterval(function () { save(); }, 15000);
  setInterval(poll, 8000);

  // flag for review
  document.querySelectorAll(".flag-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var id = btn.getAttribute("data-item");
      var link = document.querySelector('.qnav a[data-item="' + id + '"]');
      var on = !btn.classList.contains("btn-primary");
      btn.classList.toggle("btn-primary", on);
      btn.textContent = on ? "Flagged" : "Flag for review";
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
        setState("Time is up: submitting…");
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
    if (preview) { event.preventDefault(); alert("This is a preview: nothing is submitted."); return; }
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

  /* ---- leaving the test page ---- */
  var lastAway = 0;
  function away(kind) {
    var now = Date.now();
    if (now - lastAway < 1500) return; // blur and visibilitychange fire together
    lastAway = now;
    save(true);
    report(kind);
  }
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "hidden") away("focus_lost"); });
  window.addEventListener("blur", function () { if (!document.hasFocus()) away("focus_lost"); });

  /* ---- full screen ---- */
  if (wantFullscreen && !preview && fullscreenGate) {
    var fsSupported = !!(document.fullscreenEnabled && document.documentElement.requestFullscreen);
    var gate = function () {
      var outside = !document.fullscreenElement;
      fullscreenGate.hidden = !outside;
      form.classList.toggle("is-hidden", outside || locked);
    };
    if (fsSupported) {
      document.getElementById("enter-fullscreen").addEventListener("click", function () {
        document.documentElement.requestFullscreen().catch(function () {});
      });
      document.addEventListener("fullscreenchange", function () {
        if (!document.fullscreenElement && !submitted) away("fullscreen_exit");
        gate();
      });
      gate();
    } else {
      report("fullscreen_unsupported"); // e.g. iPhone Safari: recorded, not blocked
    }
  }

  /* ---- copy, paste, context menu, printing ---- */
  if (blockCopy && !preview) {
    ["copy", "cut"].forEach(function (type) {
      document.addEventListener(type, function (e) { e.preventDefault(); report("copy_blocked"); });
    });
    document.addEventListener("paste", function (e) { e.preventDefault(); report("paste_blocked"); });
    document.addEventListener("drop", function (e) { e.preventDefault(); report("paste_blocked"); });
    document.addEventListener("contextmenu", function (e) { e.preventDefault(); report("contextmenu_blocked"); });
    window.addEventListener("beforeprint", function () { report("print_blocked"); });
    document.body.classList.add("no-print-test");
  }

  updateNavigator();
})();
