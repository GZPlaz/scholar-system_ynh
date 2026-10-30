/* Safe Exam Browser on macOS and iOS can't send its verification headers, so the page reads the
   keys from SEB's JavaScript API (they are already hashed with this page's URL) and hands them
   to the server, which checks them against the test's settings. In any other browser nothing
   happens and the instructions stay on screen. */
(function () {
  "use strict";
  var gate = document.getElementById("seb-gate");
  if (!gate || !window.SafeExamBrowser || !window.SafeExamBrowser.security) return;
  var seb = window.SafeExamBrowser;
  var show = function (id, on) { var el = document.getElementById(id); if (el) el.hidden = !on; };
  show("seb-checking", true);
  show("seb-instructions", false);

  function send() {
    var token = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
    fetch(gate.getAttribute("data-check-url"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": token, "Accept": "application/json" },
      body: JSON.stringify({
        url: window.location.href.split("#")[0],
        config_key: seb.security.configKey || "",
        exam_key: seb.security.browserExamKey || "",
        version: seb.version || ""
      })
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (body) {
        if (resp.ok && body.redirect) { window.location.href = body.redirect; return; }
        show("seb-checking", false);
        show("seb-wrong", true);
        show("seb-instructions", true);
      });
    }).catch(function () { show("seb-checking", false); show("seb-wrong", true); });
  }

  // SEB 3.0 for macOS/iOS fills the keys asynchronously; later versions have them ready.
  if (typeof seb.security.updateKeys === "function") seb.security.updateKeys(send);
  else send();
})();
