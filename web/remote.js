/* The easel remote. Every change is a POST to /api/control with the X-Easel-Remote header (the server refuses
   anything without it). Reads /api/remote/state every 3 s while the page is visible. */
(function () {
  "use strict";
  var $ = function (id) {
    return document.getElementById(id);
  };

  function showErr(msg) {
    var e = $("err");
    e.textContent = msg || "";
    e.hidden = !msg;
  }

  function control(action, args) {
    return fetch("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Easel-Remote": "1" },
      body: JSON.stringify({ action: action, args: args || {} }),
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.json().then(
          function (j) {
            return { ok: r.ok, j: j };
          },
          function () {
            return { ok: false, j: { error: "HTTP " + r.status } };
          },
        );
      })
      .then(function (res) {
        if (!res.ok) {
          showErr(res.j.error || "refused");
          return null;
        }
        showErr("");
        if (res.j.state) render(res.j.state);
        return res.j;
      })
      .catch(function () {
        showErr("the easel did not answer");
        return null;
      });
  }

  function render(s) {
    $("showing").textContent = s.showing || "";
    var sc = s.screen;
    $("screencard").hidden = !sc;
    $("screenline").textContent = sc
      ? "Screen " +
        (sc.output === "unknown" ? sc.want : sc.output) +
        (sc.source === "override" ? " (held from the remote)" : "") +
        " · " +
        (sc.next || "")
      : "";
    Array.prototype.forEach.call(document.querySelectorAll("[data-panel]"), function (b) {
      b.classList.toggle("on", b.getAttribute("data-panel") === s.panel);
    });
  }

  function refresh() {
    fetch("/api/remote/state", { credentials: "same-origin" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (s) {
        if (s) render(s);
      })
      .catch(function () {
        showErr("the easel did not answer");
      });
  }

  document.addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    if (b.hasAttribute("data-panel")) control("panel.set", { panel: b.getAttribute("data-panel") });
    else if (b.hasAttribute("data-act")) control(b.getAttribute("data-act"));
  });

  refresh();
  setInterval(function () {
    if (!document.hidden) refresh();
  }, 3000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
})();
