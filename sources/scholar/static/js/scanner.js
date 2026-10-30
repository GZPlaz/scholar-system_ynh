/* Scanning answer sheets with a phone or laptop camera, or from photo files.
   The camera view looks for the four black corner squares in every video frame, outlines the
   sheet, and takes the photo by itself once the sheet has been held steady. Each photo goes to
   Scholar in the background and the result (whose sheet, how many answers, anything to check)
   appears at once, so the next sheet can already be in front of the camera. */
(function () {
  "use strict";
  var box = document.getElementById("scan-upload");
  if (!box || !window.fetch) return;
  var uploadUrl = box.getAttribute("data-upload-url");
  var token = document.querySelector('meta[name="csrf-token"]').getAttribute("content");
  var MAX_SIDE = 2400;
  var anyDone = false;

  /* ---------------- remembered choice: record clean sheets by themselves ---------------- */
  var autoBoxes = Array.prototype.slice.call(document.querySelectorAll("[data-auto-record]"));
  function autoOn() { return autoBoxes.length ? autoBoxes[0].checked : false; }
  try { var saved = localStorage.getItem("scholar.scan.auto"); autoBoxes.forEach(function (b) { b.checked = saved === null ? true : saved === "1"; }); } catch (e) { autoBoxes.forEach(function (b) { b.checked = true; }); }
  autoBoxes.forEach(function (b) {
    b.addEventListener("change", function () {
      autoBoxes.forEach(function (o) { o.checked = b.checked; });
      try { localStorage.setItem("scholar.scan.auto", b.checked ? "1" : "0"); } catch (e) {}
    });
  });

  /* ---------------- results ---------------- */
  function el(tag, cls, text) { var n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }

  function resultItem(list, title) {
    var li = el("li", "scan-result");
    li.appendChild(el("span", "name", title));
    li.appendChild(el("span", "state muted", "Reading…"));
    list.prepend(li);
    return li;
  }

  function describe(scan) {
    var who = scan.student || "Blank sheet";
    var page = scan.pages > 1 ? ", page " + scan.page + " of " + scan.pages : "";
    var read = scan.answered != null ? scan.answered + " answer" + (scan.answered === 1 ? "" : "s") + " read" : "read";
    return { who: who + page, read: read };
  }

  function show(li, scan, onFix) {
    var state = li.querySelector(".state");
    state.className = "state";
    state.textContent = "";
    li.classList.remove("ok", "check", "bad");
    if (scan.status === "failed") {
      li.classList.add("bad");
      state.textContent = scan.error + " ";
      return;
    }
    var d = describe(scan);
    li.querySelector(".name").textContent = d.who;
    if (scan.duplicate) {
      li.classList.add("ok");
      state.textContent = "Already recorded, nothing changed.";
    } else if (scan.status === "recorded") {
      li.classList.add("ok");
      state.textContent = "✓ " + d.read + " and recorded.";
    } else if (scan.status === "ready") {
      li.classList.add("ok");
      state.textContent = d.read + " clearly. ";
      state.appendChild(link("Open", scan.url));
    } else {
      li.classList.add("check");
      state.textContent = (scan.flags ? scan.flags + " answer" + (scan.flags === 1 ? "" : "s") + " to check. " : "Choose the student. ");
      var b = el("button", "btn btn-sm btn-primary", "Check now");
      b.type = "button";
      b.addEventListener("click", function () { onFix ? onFix(scan, li) : (window.location.href = scan.url); });
      state.appendChild(b);
    }
    var notes = [];
    if (scan.blank && scan.blank.length) notes.push("blank: " + scan.blank.slice(0, 8).join(", ") + (scan.blank.length > 8 ? "…" : ""));
    if (scan.light) notes.push("very light pencil");
    if (scan.low_resolution) notes.push("photo taken from far away: move closer");
    if (notes.length) state.appendChild(el("span", "scan-note small muted", " (" + notes.join("; ") + ")"));
  }

  function link(text, href) { var a = el("a", "", text); a.href = href; return a; }

  /* ---------------- who is still to scan ---------------- */
  var progressBoxes = Array.prototype.slice.call(document.querySelectorAll("[data-scan-progress]"));
  function progress(p) {
    if (!p) return;
    progressBoxes.forEach(function (box) {
      box.textContent = "";
      var bar = el("div", "scan-bar");
      var fill = el("span", "fill");
      fill.style.width = p.total ? Math.round(100 * p.done / p.total) + "%" : "0%";
      bar.appendChild(fill);
      box.appendChild(el("strong", "", p.done + " of " + p.total + " students scanned"));
      box.appendChild(bar);
      if (p.missing && p.missing.length) {
        box.appendChild(el("span", "small muted", "Still to scan: " + p.missing.slice(0, 10).join(", ") + (p.missing.length > 10 ? " and " + (p.missing.length - 10) + " more" : "")));
      } else if (p.total) {
        box.appendChild(el("span", "small", "Everyone has been scanned."));
      }
    });
  }

  /* ---------------- uploading ---------------- */
  function shrink(file) {
    if (!window.createImageBitmap || !/^image\/(jpeg|png|webp)$/.test(file.type)) return Promise.resolve(file);
    return createImageBitmap(file, { imageOrientation: "from-image" }).then(function (bmp) {
      var scale = Math.min(1, MAX_SIDE / Math.max(bmp.width, bmp.height));
      if (scale === 1 && file.size < 3e6) return file;
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bmp.width * scale);
      canvas.height = Math.round(bmp.height * scale);
      canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) { canvas.toBlob(function (blob) { resolve(blob || file); }, "image/jpeg", 0.9); });
    }).catch(function () { return file; });
  }

  var running = 0, waiting = [];
  function pump() {
    while (running < 2 && waiting.length) {
      (function (job) {
        running += 1;
        job.li.querySelector(".state").textContent = "Reading…";
        Promise.resolve(job.blob).then(function (blob) {
          var data = new FormData();
          data.append("photos", blob, job.name || "photo.jpg");
          data.append("auto", autoOn() ? "1" : "0");
          return fetch(uploadUrl, { method: "POST", body: data, credentials: "same-origin", headers: { "X-CSRFToken": token, "Accept": "application/json" } });
        }).then(function (resp) {
          return resp.json().catch(function () { return {}; }).then(function (body) {
            if (!resp.ok || !body.scans) throw new Error(body.error || "Upload failed (" + resp.status + ")");
            show(job.li, body.scans[0], job.onFix);
            progress(body.progress);
            anyDone = true;
            if (job.done) job.done(body.scans[0]);
          });
        }).catch(function (err) {
          var s = job.li.querySelector(".state");
          s.className = "state";
          job.li.classList.add("bad");
          s.textContent = err.message || "Upload failed. Check your connection.";
          if (job.done) job.done(null);
        }).then(function () { running -= 1; pump(); });
      })(waiting.shift());
    }
  }
  function enqueue(blob, name, list, onFix, done) {
    var li = resultItem(list, name || "Photo");
    li.querySelector(".state").textContent = "Waiting…";
    waiting.push({ blob: blob, name: name, li: li, onFix: onFix, done: done });
    pump();
  }

  /* ---------------- photo files ---------------- */
  var pageList = document.getElementById("scan-queue");
  box.querySelectorAll("[data-scan-input]").forEach(function (input) {
    input.addEventListener("change", function () {
      Array.prototype.forEach.call(input.files, function (file) { enqueue(shrink(file), file.name, pageList, null); });
      input.value = "";
    });
  });

  /* ---------------- quick fix panel ---------------- */
  var fixBox = document.getElementById("scanner-fix");
  function openFix(scan, li) {
    if (!fixBox) { window.location.href = scan.url; return; }
    fixBox.hidden = false;
    fixBox.textContent = "Loading…";
    fetch(scan.fix_url, { credentials: "same-origin" }).then(function (r) { return r.ok ? r.text() : Promise.reject(); }).then(function (html) {
      fixBox.innerHTML = html;
      window.ScholarScanFix.setup(fixBox);
      var form = fixBox.querySelector("form");
      var close = el("button", "btn btn-sm", "Not now");
      close.type = "button";
      close.addEventListener("click", function () { fixBox.hidden = true; fixBox.textContent = ""; });
      form.querySelector(".actions").appendChild(close);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var data = new FormData(form);
        fetch(form.action, { method: "POST", body: data, credentials: "same-origin", headers: { "X-CSRFToken": token, "Accept": "application/json" } })
          .then(function (r) { return r.json().then(function (b) { return [r, b]; }); })
          .then(function (rb) {
            if (!rb[0].ok) { var h = form.querySelector("[data-fix-help]"); if (h) h.textContent = rb[1].message || "Not recorded."; return; }
            fixBox.hidden = true; fixBox.textContent = "";
            if (li) show(li, rb[1].scan, null);
            progress(rb[1].progress);
            anyDone = true;
          });
      });
      fixBox.scrollIntoView({ block: "nearest" });
    }).catch(function () { window.location.href = scan.url; });
  }

  /* ---------------- the camera ---------------- */
  var openBtn = document.getElementById("open-scanner");
  var panel = document.getElementById("scanner");
  if (!openBtn || !panel || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
  openBtn.hidden = false;
  var fallback = box.querySelector("[data-camera-fallback]");
  if (fallback) fallback.hidden = true;

  var video = panel.querySelector("video");
  var overlay = panel.querySelector("canvas.scanner-overlay");
  var octx = overlay.getContext("2d");
  var hint = document.getElementById("scanner-hint");
  var camList = document.getElementById("scanner-results");
  var captureBtn = document.getElementById("scanner-capture");
  var torchBtn = document.getElementById("scanner-torch");
  var stream = null, timer = null, track = null, torchOn = false;
  var DET_W = 480;
  var work = document.createElement("canvas");
  var wctx = work.getContext("2d", { willReadFrequently: true });
  var steady = 0, lost = 0, mode = "search", last = null, STEADY_FRAMES = 5;

  function say(text, tone) { hint.textContent = text; hint.setAttribute("data-tone", tone || ""); }

  function startCamera() {
    panel.hidden = false;
    document.body.classList.add("scanner-open");
    say("Starting the camera…");
    return navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: "environment" }, width: { ideal: 2560 }, height: { ideal: 1440 } } })
      .then(function (s) {
        stream = s;
        track = s.getVideoTracks()[0];
        video.srcObject = s;
        var caps = track.getCapabilities ? track.getCapabilities() : {};
        torchBtn.hidden = !caps.torch;
        return video.play();
      }).then(function () {
        mode = "search"; steady = 0; lost = 0; last = null;
        say("Point the camera at a sheet. All four black corner squares must be in view.");
        timer = setInterval(tick, 140);
      }).catch(function (err) {
        stopCamera();
        panel.hidden = true;
        document.body.classList.remove("scanner-open");
        alert(err && err.name === "NotAllowedError" ? "Allow camera access in your browser to scan with the camera, or use “Choose photos”." : "The camera could not be started. Use “Choose photos” instead.");
      });
  }

  function stopCamera() {
    if (timer) { clearInterval(timer); timer = null; }
    if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    video.srcObject = null;
  }

  function closeCamera() {
    stopCamera();
    panel.hidden = true;
    document.body.classList.remove("scanner-open");
    if (fixBox) { fixBox.hidden = true; fixBox.textContent = ""; }
    if (anyDone) window.location.reload();
  }

  /* find the four black corner squares in a small copy of the video frame */
  function detect() {
    var vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || !vh) return null;
    var w = DET_W, h = Math.round(DET_W * vh / vw);
    work.width = w; work.height = h;
    wctx.drawImage(video, 0, 0, w, h);
    var px = wctx.getImageData(0, 0, w, h).data, n = w * h;
    var gray = new Uint8Array(n), total = 0;
    for (var i = 0, p = 0; i < n; i++, p += 4) {
      var r = px[p], g = px[p + 1], b = px[p + 2], m = r > g ? (r > b ? r : b) : (g > b ? g : b);
      gray[i] = m; total += m;   // the brightest channel: the printed green ink drops out, pencil and black stay
    }
    var bright = total / n;
    // integral image → local mean, so a shadow across the page doesn't hide the squares
    var W1 = w + 1, integ = new Float64Array(W1 * (h + 1));
    for (var y = 0; y < h; y++) {
      var row = 0;
      for (var x = 0; x < w; x++) { row += gray[y * w + x]; integ[(y + 1) * W1 + x + 1] = integ[y * W1 + x + 1] + row; }
    }
    var R = Math.max(8, Math.round(w / 14)), dark = new Uint8Array(n);
    for (y = 0; y < h; y++) {
      var y0 = Math.max(0, y - R), y1 = Math.min(h, y + R + 1);
      for (x = 0; x < w; x++) {
        var x0 = Math.max(0, x - R), x1 = Math.min(w, x + R + 1);
        var mean = (integ[y1 * W1 + x1] - integ[y0 * W1 + x1] - integ[y1 * W1 + x0] + integ[y0 * W1 + x0]) / ((x1 - x0) * (y1 - y0));
        var v = gray[y * w + x];
        if (mean > 60 && v < 0.5 * mean && v < 115) dark[y * w + x] = 1;
      }
    }
    // connected components of dark pixels
    var seen = new Uint8Array(n), stack = new Int32Array(n), cands = [];
    var maxArea = 0.004 * n;
    for (var s = 0; s < n; s++) {
      if (!dark[s] || seen[s]) continue;
      var sp = 0, area = 0, minx = w, maxx = 0, miny = h, maxy = 0, sx = 0, sy = 0, tooBig = false;
      stack[sp++] = s; seen[s] = 1;
      while (sp) {
        var c = stack[--sp], cx = c % w, cy = (c / w) | 0;
        area++; sx += cx; sy += cy;
        if (cx < minx) minx = cx; if (cx > maxx) maxx = cx; if (cy < miny) miny = cy; if (cy > maxy) maxy = cy;
        if (area > maxArea) tooBig = true;
        if (cx > 0 && dark[c - 1] && !seen[c - 1]) { seen[c - 1] = 1; stack[sp++] = c - 1; }
        if (cx < w - 1 && dark[c + 1] && !seen[c + 1]) { seen[c + 1] = 1; stack[sp++] = c + 1; }
        if (cy > 0 && dark[c - w] && !seen[c - w]) { seen[c - w] = 1; stack[sp++] = c - w; }
        if (cy < h - 1 && dark[c + w] && !seen[c + w]) { seen[c + w] = 1; stack[sp++] = c + w; }
      }
      if (tooBig || area < 26) continue;
      var bw = maxx - minx + 1, bh = maxy - miny + 1, aspect = bw / bh;
      if (aspect < 0.62 || aspect > 1.62 || area / (bw * bh) < 0.84) continue;   // solid and square: not a filled bubble, not text
      cands.push({ x: sx / area, y: sy / area, area: area });
    }
    var out = { bright: bright, cands: cands.length, w: w, h: h, corners: null, fraction: 0 };
    if (cands.length < 4) return out;
    var tl = cands[0], tr = cands[0], br = cands[0], bl = cands[0];
    cands.forEach(function (c) {
      if (c.x + c.y < tl.x + tl.y) tl = c;
      if (c.x + c.y > br.x + br.y) br = c;
      if (c.x - c.y > tr.x - tr.y) tr = c;
      if (c.x - c.y < bl.x - bl.y) bl = c;
    });
    var four = [tl, tr, br, bl];
    for (var a = 0; a < 4; a++) for (var b2 = a + 1; b2 < 4; b2++) if (four[a] === four[b2]) return out;
    var areas = four.map(function (c) { return c.area; });
    if (Math.max.apply(null, areas) / Math.min.apply(null, areas) > 4) return out;
    var d = function (p, q) { return Math.hypot(p.x - q.x, p.y - q.y); };
    var across = (d(tl, tr) + d(bl, br)) / 2, down = (d(tl, bl) + d(tr, br)) / 2;
    var ratio = Math.max(across, down) / Math.max(1, Math.min(across, down));
    if (ratio < 1.05 || ratio > 1.95) return out;   // a sheet is about 1.4 times as tall as wide
    var shoelace = Math.abs((tl.x * tr.y - tr.x * tl.y) + (tr.x * br.y - br.x * tr.y) + (br.x * bl.y - bl.x * br.y) + (bl.x * tl.y - tl.x * bl.y)) / 2;
    out.fraction = shoelace / n;
    if (out.fraction < 0.06) return out;
    out.corners = four;
    return out;
  }

  function sizeOverlay() {
    var rect = video.getBoundingClientRect();
    overlay.width = rect.width; overlay.height = rect.height;
    overlay.style.width = rect.width + "px"; overlay.style.height = rect.height + "px";
    return rect;
  }

  function draw(res) {
    var rect = sizeOverlay(), vw = video.videoWidth, vh = video.videoHeight;
    octx.clearRect(0, 0, overlay.width, overlay.height);
    if (!res || !res.corners || !vw) return;
    var s = Math.min(rect.width / vw, rect.height / vh), ox = (rect.width - vw * s) / 2, oy = (rect.height - vh * s) / 2, k = vw / res.w * s;
    var color = mode === "cooldown" ? "#8a9486" : (steady >= 2 ? "#3b6620" : "#c99a06");
    octx.lineWidth = 4; octx.strokeStyle = color; octx.fillStyle = color;
    octx.beginPath();
    res.corners.forEach(function (c, i) { var X = ox + c.x * k, Y = oy + c.y * k; if (i) octx.lineTo(X, Y); else octx.moveTo(X, Y); });
    octx.closePath(); octx.stroke();
    res.corners.forEach(function (c) { octx.fillRect(ox + c.x * k - 7, oy + c.y * k - 7, 14, 14); });
    if (steady > 0 && mode === "search") {                    // a ring that fills while the sheet is held still
      var cx = rect.width / 2, cy = rect.height / 2;
      octx.lineWidth = 8; octx.strokeStyle = "rgba(255,255,255,.35)"; octx.beginPath(); octx.arc(cx, cy, 34, 0, 2 * Math.PI); octx.stroke();
      octx.strokeStyle = "#3b6620"; octx.beginPath(); octx.arc(cx, cy, 34, -Math.PI / 2, -Math.PI / 2 + 2 * Math.PI * Math.min(1, steady / STEADY_FRAMES)); octx.stroke();
    }
  }

  function movement(a, b) {
    var m = 0;
    for (var i = 0; i < 4; i++) m = Math.max(m, Math.hypot(a[i].x - b[i].x, a[i].y - b[i].y));
    return m;
  }

  function tick() {
    var res = detect();
    if (!res) return;
    if (res.corners) {
      lost = 0;
      if (mode === "cooldown") { draw(res); say("Got it. Slide in the next sheet.", "ok"); return; }
      var moved = last ? movement(res.corners, last) / res.w : 1;
      steady = moved < 0.012 ? steady + 1 : 0;
      last = res.corners.map(function (c) { return { x: c.x, y: c.y }; });
      if (res.fraction < 0.2) say("Move closer: fill more of the picture with the sheet.", "warn");
      else if (steady >= STEADY_FRAMES) { draw(res); capture(); return; }
      else say(steady ? "Hold still…" : "Sheet found. Hold it steady.", steady ? "ok" : "");
    } else {
      steady = 0; last = null; lost += 1;
      if (lost >= 5 && mode === "cooldown") { mode = "search"; }
      if (mode === "search") {
        if (res.bright < 55) say("Too dark: move to a brighter spot or turn on the light.", "warn");
        else if (res.cands >= 3) say("Not all four black corner squares are in view. Move the camera or hold the sheet flat.", "warn");
        else say("Point the camera at a sheet. All four black corner squares must be in view.");
      }
    }
    draw(res);
  }

  function beep() {
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)(), osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.frequency.value = 880; gain.gain.value = 0.08; osc.connect(gain); gain.connect(ctx.destination);
      osc.start(); osc.stop(ctx.currentTime + 0.09);
      setTimeout(function () { ctx.close(); }, 300);
    } catch (e) {}
    if (navigator.vibrate) navigator.vibrate(40);
  }

  function capture() {
    var vw = video.videoWidth, vh = video.videoHeight;
    if (!vw) return;
    mode = "cooldown"; steady = 0; lost = 0;
    var scale = Math.min(1, MAX_SIDE / Math.max(vw, vh));
    var canvas = document.createElement("canvas");
    canvas.width = Math.round(vw * scale); canvas.height = Math.round(vh * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    beep();
    say("Captured. Reading…", "ok");
    canvas.toBlob(function (blob) {
      enqueue(blob, "Sheet " + (new Date()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }), camList, openFix, function (scan) {
        if (scan && scan.status === "failed") { say("Couldn't read that one: " + scan.error, "warn"); mode = "search"; }
        else if (scan) { say(scan.status === "review" ? "Captured. Some answers need a look. Next sheet, or tap Check now." : "Done. Slide in the next sheet.", "ok"); }
      });
    }, "image/jpeg", 0.9);
  }

  openBtn.addEventListener("click", startCamera);
  document.getElementById("scanner-close").addEventListener("click", closeCamera);
  captureBtn.addEventListener("click", function () { if (stream) capture(); });
  torchBtn.addEventListener("click", function () {
    torchOn = !torchOn;
    track.applyConstraints({ advanced: [{ torch: torchOn }] }).catch(function () { torchOn = false; });
    torchBtn.setAttribute("aria-pressed", torchOn ? "true" : "false");
  });
  document.addEventListener("keydown", function (event) {
    if (panel.hidden) return;
    if (event.key === "Escape") closeCamera();
    else if (event.key === " " && !/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(event.target.tagName)) { event.preventDefault(); if (stream) capture(); }
  });
  window.addEventListener("pagehide", stopCamera);
})();
