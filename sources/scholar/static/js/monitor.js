/* Live proctoring: refresh the roster every few seconds without losing what the proctor is typing. */
(function () {
  "use strict";
  var table = document.querySelector("table.monitor[data-refresh-url]");
  if (!table) return;
  var url = table.getAttribute("data-refresh-url");

  function busy() {
    var el = document.activeElement;
    return el && table.contains(el) && (el.tagName === "INPUT" || el.tagName === "BUTTON");
  }

  function refresh() {
    if (document.hidden || busy()) return;
    fetch(url, { credentials: "same-origin" }).then(function (resp) {
      if (!resp.ok) throw new Error(resp.status);
      return resp.text();
    }).then(function (html) {
      var tpl = document.createElement("template");
      tpl.innerHTML = html.trim();
      var fresh = tpl.content.querySelector("#monitor-rows");
      var current = document.getElementById("monitor-rows");
      if (!fresh || !current || busy()) return;
      current.replaceWith(fresh);
      (fresh.getAttribute("data-tally") || "").split(";").forEach(function (pair) {
        var kv = pair.split("=");
        var b = document.querySelector('#monitor-tally b[data-key="' + kv[0] + '"]');
        if (b) {
          b.textContent = kv[1];
          b.parentNode.classList.toggle("attn", (kv[0] === "waiting" || kv[0] === "paused") && kv[1] !== "0");
        }
      });
    }).catch(function () {});
  }
  setInterval(refresh, 5000);
})();
