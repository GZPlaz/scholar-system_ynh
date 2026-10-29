/* Upload answer-sheet photos one at a time, shrinking them in the browser first,
   and show what Scholar read from each as it comes back. */
(function () {
  "use strict";
  var box = document.getElementById("scan-upload");
  if (!box || !window.fetch) return;
  var url = box.getAttribute("data-upload-url");
  var queue = document.getElementById("scan-queue");
  var token = document.querySelector('meta[name="csrf-token"]').getAttribute("content");
  var pending = [], busy = false, done = 0;
  var MAX_SIDE = 2400;

  function shrink(file) {
    if (!window.createImageBitmap || !/^image\/(jpeg|png|webp)$/.test(file.type)) return Promise.resolve(file);
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bmp) {
      var scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
      if (scale === 1 && file.size < 3e6) return file;
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * scale);
      canvas.height = Math.round(bmp.height * scale);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) {
        canvas.toBlob(function (blob) { resolve(blob || file); }, "image/jpeg", 0.88);
      });
    }).catch(function () { return file; });
  }

  function row(file) {
    var li = document.createElement("li");
    li.innerHTML = '<span class="name"></span><span class="state muted">Waiting…</span>';
    li.querySelector(".name").textContent = file.name || "Photo";
    queue.prepend(li);
    return li;
  }

  function show(li, scan) {
    var state = li.querySelector(".state");
    state.className = "state";
    if (scan.status === "failed") {
      state.className = "state err";
      state.textContent = scan.error;
      return;
    }
    var who = scan.student || "Blank sheet";
    var text = who + ", page " + scan.page + " of " + scan.pages + ": ";
    text += scan.status === "ready" ? "read cleanly." : (scan.flags ? scan.flags + " answer(s) to check." : "choose the student.");
    state.textContent = text + " ";
    var a = document.createElement("a");
    a.href = scan.url;
    a.textContent = scan.status === "ready" ? "Open" : "Check now";
    state.appendChild(a);
  }

  function next() {
    if (busy || !pending.length) {
      if (!busy && done) { var r = document.getElementById("scan-reload"); if (!r) { r = document.createElement("p"); r.id = "scan-reload"; r.innerHTML = '<a class="btn btn-sm" href="">Refresh the list below</a>'; queue.after(r); } }
      return;
    }
    busy = true;
    var job = pending.shift();
    job.li.querySelector(".state").textContent = "Reading…";
    shrink(job.file).then(function (blob) {
      var data = new FormData();
      data.append("photos", blob, job.file.name || "photo.jpg");
      return fetch(url, { method: "POST", body: data, credentials: "same-origin", headers: { "X-CSRFToken": token, "Accept": "application/json" } });
    }).then(function (resp) {
      return resp.json().then(function (body) {
        if (!resp.ok || !body.scans) throw new Error(body.error || "Upload failed (" + resp.status + ")");
        show(job.li, body.scans[0]);
      });
    }).catch(function (err) {
      var s = job.li.querySelector(".state");
      s.className = "state err";
      s.textContent = err.message || "Upload failed. Check your connection and try again.";
    }).finally(function () {
      busy = false; done += 1; next();
    });
  }

  box.querySelectorAll("[data-scan-input]").forEach(function (input) {
    input.addEventListener("change", function () {
      Array.prototype.forEach.call(input.files, function (file) {
        pending.push({ file: file, li: row(file) });
      });
      input.value = "";
      next();
    });
  });
})();
