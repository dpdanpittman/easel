/* The kiosk page. Polls /api/status every 15 s, the work panels every 30 s and /api/mode every second.
   No network beyond the local server: everything comes from it (the CSP forbids anything else). */
(function () {
  "use strict";
  // the configured timezone arrives with the first /api/status; until then the browser's own
  var TZ = undefined;
  var POLL_MS = 15000;
  var DWELL_MS = 30000;
  var $ = function (id) {
    return document.getElementById(id);
  };
  var svgNS = "http://www.w3.org/2000/svg";
  var last = null,
    lastOkAt = Date.now(),
    build = null,
    loadedAt = Date.now();

  /* ---------- stage scaling and a slow pixel shift (kind to an always-on panel) ---------- */
  function fit() {
    var s = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    var st = $("stage");
    st.style.transform = "scale(" + s + ")";
    st.style.left = Math.max(0, (window.innerWidth - 1920 * s) / 2) + "px";
    st.style.top = Math.max(0, (window.innerHeight - 1080 * s) / 2) + "px";
  }
  window.addEventListener("resize", fit);
  fit();
  setInterval(
    function () {
      var dx = Math.round(Math.random() * 6 - 3),
        dy = Math.round(Math.random() * 4 - 2);
      $("stage").style.translate = dx + "px " + dy + "px";
    },
    9 * 60 * 1000,
  );

  /* ---------- clock ---------- */
  var fmtTime, fmtDate, fmtDay, fmtClock;
  function setTZ(tz) {
    if (fmtTime && tz === TZ) return;
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: tz });
      TZ = tz;
    } catch (e) {
      TZ = undefined; // an unknown zone name: the browser's own
    }
    fmtTime = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true });
    fmtDate = new Intl.DateTimeFormat("en-US", {
      timeZone: TZ,
      weekday: "long",
      month: "long",
      day: "numeric",
      year: "numeric",
    });
    fmtDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });
    fmtClock = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit", hour12: true });
  }
  setTZ(undefined);
  function tick() {
    var now = new Date();
    var parts = fmtTime.formatToParts(now),
      hm = "",
      ap = "";
    parts.forEach(function (p) {
      if (p.type === "dayPeriod") ap = p.value;
      else if (p.type !== "literal" || p.value === ":") hm += p.value;
    });
    $("hm").textContent = hm;
    $("ampm").textContent = ap;
    $("date").textContent = fmtDate.format(now);
    // fresh start once a day in the small hours (keeps a long-running browser tidy)
    if (Date.now() - loadedAt > 20 * 3600 * 1000 && now.getHours() === 3)
      location.reload();
  }
  setInterval(tick, 1000);
  tick();

  /* ---------- status strip ---------- */
  function chip(c) {
    var d = document.createElement("div");
    d.className = "chip " + c.state;
    var l = document.createElement("div");
    l.className = "lab";
    l.textContent = c.label;
    var v = document.createElement("div");
    v.className = "val";
    v.textContent = c.value;
    d.appendChild(l);
    d.appendChild(v);
    return d;
  }

  function renderStatus(s) {
    var box = $("checks");
    box.textContent = "";
    var checks = (s.checks || []).slice();
    // work hours add the Linear, GitHub and Standup tiles (and leave Up out): six columns keep the strip at two rows
    box.classList.toggle("six", checks.length + 2 > 10);
    checks.forEach(function (c) {
      box.appendChild(chip(c));
    });
    var br = s.branding || {};
    box.appendChild(
      chip({
        label: br.assistant || "Agent",
        value: s.session ? "in session" : "idle",
        state: s.session ? "busy" : "info",
      }),
    );
    var words = { ok: "All green", warn: "Have a look", bad: "Needs you" };
    var v = chip({
      label: br.board || "Board",
      value: words[s.overall] || "?",
      state: s.overall,
    });
    v.classList.add("verdict");
    box.appendChild(v);
    $("strip").setAttribute("data-overall", s.overall);
  }

  function unreachable() {
    var s = {
      overall: "bad",
      problems: ["Kiosk server: not answering"],
      session: false,
      branding: last && last.branding,
      checks: [
        {
          key: "srv",
          label: "Kiosk server",
          value: "not answering since " + clockOf(lastOkAt / 1000),
          state: "bad",
        },
      ],
      sky: last && last.sky,
    };
    renderStatus(s);
    renderScene(s);
  }

  /* ---------- the right panel's scene: the grid (grid.js), when settings `scene.enabled` ---------- */
  if (window.KioskGrid) KioskGrid.init($("grid"));
  var sceneOn = null;
  function renderScene(s) {
    var on = !!(window.KioskGrid && s.grid && s.grid.length !== undefined && s.scene !== false);
    if (on !== sceneOn) {
      sceneOn = on;
      if (window.KioskGrid) KioskGrid.show(on);
    }
    if (on) KioskGrid.render(s);
  }

  function poll() {
    var x = new XMLHttpRequest();
    x.open("GET", "/api/status", true);
    x.timeout = 8000;
    x.onload = function () {
      if (x.status !== 200) return fail();
      var s;
      try {
        s = JSON.parse(x.responseText);
      } catch (e) {
        return fail();
      }
      lastOkAt = Date.now();
      if (build && s.build && s.build !== build) {
        location.reload();
        return;
      }
      build = s.build || build;
      last = s;
      if (s.branding) {
        setTZ(s.branding.tz);
        if (s.branding.title) document.title = s.branding.title;
      }
      renderStatus(s);
      renderScene(s);
      renderSky(s.sky);
      if (window.KioskGrid) KioskGrid.sky(s.sky);
    };
    x.onerror = x.ontimeout = fail;
    x.send();
  }
  function fail() {
    if (Date.now() - lastOkAt > 45000) unreachable();
  }
  poll();
  setInterval(poll, POLL_MS);

  /* ---------- the work tasks (the server sends an empty list outside work hours, unless the remote chose the Tasks
     panel) ---------- */
  var work = false,
    flightOn = false, // In flight: the server's "show" (work hours, or the remote chose it); off until it says so
    taskFails = 0;
  function renderTasks(t) {
    var items = (t && t.items) || [];
    // "blocked": the server will not show a list that lacks a Linear workspace's issues (just after a start with
    // nothing saved, or Linear down); it says so plainly instead of showing a short list
    var blocked = t && t.show && t.blocked;
    var has = !!(t && t.show && (items.length || blocked));
    var parts = blocked ? [] : has ? ["" + t.total] : ["none right now"];
    if (has && t.active) parts.push(t.active + (t.flags === "inflight" ? " running now" : " in progress"));
    if (has && t.waiting) parts.push(t.waiting + " waiting");
    $("taskcount").textContent = parts.length ? "· " + parts.join(" · ") : "";
    $("tasksrc").textContent = has ? sourceAges(t) : "";
    var ol = $("tasklist");
    ol.textContent = "";
    ol.style.gridTemplateRows =
      "repeat(" + Math.max(1, Math.ceil(items.length / 2)) + ", auto)";
    if (blocked) {
      var bl = document.createElement("li");
      bl.className = "task nostate tblocked";
      bl.textContent =
        blocked.why === "down"
          ? "Linear has not answered" + (blocked.since ? " since " + clockOf(blocked.since) : "") +
            ": the list is held back until it does, so it never shows without its Linear tickets."
          : "Loading the list from Linear…";
      ol.appendChild(bl);
    }
    items.forEach(function (it) {
      if (it.group && it.group.tickets && it.group.tickets.length > 1) {
        ol.appendChild(groupRow(it));
        return;
      }
      var li = document.createElement("li");
      var wait = !it.done && it.waiting;
      var act = !it.done && !wait && it.active;
      li.className = "task" + (it.done ? " done" : "") + (wait ? " waiting" : "") + (act ? " active" : "") +
        (it.state || wait || act ? "" : " nostate");
      var n = document.createElement("span");
      n.className = "tn";
      n.textContent = it.done ? "✓" : wait ? "⏸" : act ? "▶" : it.n + ".";
      var tl = document.createElement("div");
      tl.className = "tl";
      if (it.ids) {
        var id = document.createElement("span");
        id.className = "tid";
        id.textContent = it.ids;
        tl.appendChild(id);
      }
      if (it.prio && !it.done) {
        var pr = document.createElement("span");
        pr.className = "tprio p" + it.prio;
        pr.textContent = "P" + it.prio;
        tl.insertBefore(pr, tl.firstChild);
      }
      tl.appendChild(document.createTextNode(it.title));
      var ts = document.createElement("div");
      ts.className = "ts";
      // the state line (derived from Linear, PRs and finished agents on the server, derive.py) comes first, so a
      // flag never hides it; then the flag: "waiting on you · <W line>" (In flight W lines), "running now · <agent>"
      // (the hook's record or an R line), or the hand "waiting on <someone else>"
      var flag = wait
        ? it.waiting_text
          ? "waiting on you · " + it.waiting_text
          : "waiting on " + it.waiting
        : act
          ? (it.running ? "running now · " : "in progress · ") + it.active
          : "";
      if (it.state) {
        var st = document.createElement("span");
        st.className = "tstate";
        st.textContent = it.state;
        ts.appendChild(st);
      }
      if (flag) {
        var fl = document.createElement("span");
        fl.className = "tflag";
        fl.textContent = (it.state ? " · " : "") + flag;
        ts.appendChild(fl);
      }
      if (!it.state && !flag) ts.textContent = " ";
      li.appendChild(n);
      li.appendChild(tl);
      li.appendChild(ts);
      ol.appendChild(li);
    });
    var changed = work !== has;
    work = has;
    layout();
    requestAnimationFrame(measureScrollers);
    if (changed) pollFlight(); // the work day just started or ended: let In flight follow without waiting 30 s
  }
  /* A grouped row: one notes line naming several tickets. Its label (the line's own title and note, "1 of 3") stays; under it
     one ticket at a time, each with its own number, live title and state, a steady rotation every GROUP_STEP_MS
     (a short fade; under prefers-reduced-motion the CSS drops the fade, so it simply pages). Every step is in the row
     at once, stacked in one grid cell with only the current one visible, so the row is as tall as its tallest step
     and never changes height while it cycles (fit.js measures the row as usual). The step shown survives the
     30 s re-render (groupIdx, by the row's ticket ids). */
  var GROUP_STEP_MS = 5000;
  var groupIdx = {};
  function groupKey(tks) {
    return tks
      .map(function (t) {
        return t.id;
      })
      .join("/");
  }
  function groupRow(it) {
    var g = it.group,
      tks = g.tickets,
      key = groupKey(tks);
    var idx = (groupIdx[key] || 0) % tks.length;
    groupIdx[key] = idx;
    var li = document.createElement("li");
    li.className = "task tgroup";
    li.setAttribute("data-key", key);
    var lab = document.createElement("div");
    lab.className = "tglabel";
    if (it.prio) {
      var pr = document.createElement("span");
      pr.className = "tprio p" + it.prio;
      pr.textContent = "P" + it.prio;
      lab.appendChild(pr);
    }
    var lt = document.createElement("span");
    lt.className = "tglt";
    lt.textContent = [g.title, g.note].filter(Boolean).join(" · ");
    lab.appendChild(lt);
    var pos = document.createElement("span");
    pos.className = "tgpos";
    pos.textContent = idx + 1 + " of " + tks.length;
    lab.appendChild(pos);
    var steps = document.createElement("div");
    steps.className = "tgsteps";
    // the hand "WAITING ON: <someone else>" belongs to the whole line: every step says it
    var handWait =
      it.waiting && !it.waiting_text ? "waiting on " + it.waiting : "";
    tks.forEach(function (t, i) {
      var st = document.createElement("div");
      var wait = t.waiting_text
        ? "waiting on you · " + t.waiting_text
        : handWait;
      var run =
        !wait && t.running_text ? "running now · " + t.running_text : "";
      st.className =
        "tgstep" +
        (i === idx ? " on" : "") +
        (wait ? " waiting" : "") +
        (run ? " active" : "");
      var n = document.createElement("span");
      n.className = "tn";
      n.textContent = wait ? "⏸" : run ? "▶" : t.n + ".";
      var tl = document.createElement("div");
      tl.className = "tl";
      var id = document.createElement("span");
      id.className = "tid";
      id.textContent = t.id;
      tl.appendChild(id);
      tl.appendChild(document.createTextNode(t.title || ""));
      var ts = document.createElement("div");
      ts.className = "ts";
      if (t.state) {
        var s = document.createElement("span");
        s.className = "tstate";
        s.textContent = t.state;
        ts.appendChild(s);
      }
      var flag = wait || run;
      if (flag) {
        var fl = document.createElement("span");
        fl.className = "tflag";
        fl.textContent = (t.state ? " · " : "") + flag;
        ts.appendChild(fl);
      }
      if (!t.state && !flag) ts.textContent = " "; // keeps every step two lines tall
      st.appendChild(n);
      st.appendChild(tl);
      st.appendChild(ts);
      steps.appendChild(st);
    });
    li.appendChild(lab);
    li.appendChild(steps);
    return li;
  }
  function stepGroups() {
    Array.prototype.forEach.call(
      document.querySelectorAll("#tasklist .tgroup"),
      function (li) {
        var key = li.getAttribute("data-key"),
          steps = li.querySelectorAll(".tgstep");
        if (steps.length < 2) return;
        var idx = ((groupIdx[key] || 0) + 1) % steps.length;
        groupIdx[key] = idx;
        Array.prototype.forEach.call(steps, function (s, i) {
          s.classList.toggle("on", i === idx);
        });
        li.querySelector(".tgpos").textContent =
          idx + 1 + " of " + steps.length;
      },
    );
  }
  setInterval(stepGroups, GROUP_STEP_MS);
  // a source that stopped answering shows its age instead of looking current: Linear and GitHub
  // are fetched every 5 minutes, so "stale" means a fetch has failed and what is shown is the last good one
  function sourceAges(t) {
    var out = [];
    if (t.linear === "stale" && t.linear_at) out.push("Linear as of " + clockOf(t.linear_at));
    else if (t.linear === "off") out.push("Linear not answering");
    if (t.github === "stale" && t.github_at) out.push("PRs as of " + clockOf(t.github_at));
    else if (t.github === "off") out.push("PRs not answering");
    if (taskFails && lastTasksAt) out.push("kiosk server not answering since " + clockOf(lastTasksAt / 1000));
    return out.join(" · ");
  }
  var lastTasks = null,
    lastTasksAt = 0;
  function pollTasks() {
    var x = new XMLHttpRequest();
    x.open("GET", "/api/tasks", true);
    x.timeout = 8000;
    x.onload = function () {
      var t = null;
      if (x.status === 200) {
        try {
          t = JSON.parse(x.responseText);
        } catch (e) {
          t = null;
        }
      }
      taskFails = 0;
      lastTasks = t;
      lastTasksAt = Date.now();
      renderTasks(t);
    };
    // a failed poll keeps the last list on screen, marked with how long the server has been silent (never an
    // empty list that looks like "nothing to do"); with no list yet there is nothing to keep
    x.onerror = x.ontimeout = function () {
      taskFails++;
      if (lastTasks) renderTasks(lastTasks);
      else if (taskFails >= 3) renderTasks(null);
    };
    x.send();
  }

  /* ---------- the left panel: chosen by the remote, decided on the server (/api/mode, every second) ---------- */
  var panel = "auto",
    mode = null;
  function layout() {
    var top = panel === "auto" && work;
    var b = document.body.classList;
    b.toggle("work", top);
    b.toggle("mode-tasks", panel === "tasks");
    $("taskpanel").hidden = !(top || panel === "tasks");
    // In flight sits under the task list in work hours only; outside them the server hides it and the off-hours view
    // takes the spot. The server decides (flightOn), never this page's clock.
    var showFlight = flightOn && (panel === "auto" || panel === "inflight");
    $("flightpanel").hidden = !showFlight;
    $("offpanel").hidden = !(panel === "auto" && !showFlight && !work);
    requestAnimationFrame(measureScrollers);
  }

  function applyMode(m) {
    mode = m;
    var p = m.panel || "auto";
    if (p !== panel) {
      var wasTasks = panel === "tasks",
        wasFlight = panel === "inflight";
      panel = p;
      layout();
      if (p === "tasks" || wasTasks) pollTasks();
      if (p === "inflight" || wasFlight || p === "auto") pollFlight();
    }
  }

  function pollMode() {
    var x = new XMLHttpRequest();
    x.open("GET", "/api/mode", true);
    x.timeout = 3000;
    x.onload = function () {
      if (x.status !== 200) return;
      try {
        applyMode(JSON.parse(x.responseText));
      } catch (e) {}
    };
    x.send();
  }

  /* ---------- sunrise, sunset, daylight and the moon under the scene (settings `sky`) ---------- */
  function shortDate(iso) {
    var p = iso.split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
  }

  function renderSky(k) {
    $("skyfacts").hidden = !k;
    if (!k) {
      $("skynote").textContent = "";
      return;
    }
    var m = k.moon;
    $("f-rise").textContent = k.sunrise || "-";
    $("f-set").textContent = k.sunset || "-";
    var dd = k.daylight_delta_min,
      ds = dd === 0 ? "" : " " + (dd > 0 ? "+" : "−") + Math.abs(dd) + " min";
    $("f-day").textContent = "";
    $("f-day").appendChild(document.createTextNode(k.daylight));
    if (ds) {
      var sm = document.createElement("small");
      sm.textContent = ds;
      $("f-day").appendChild(sm);
    }
    $("f-moon").textContent = "";
    if (m) {
      $("f-moon").appendChild(document.createTextNode(Math.round(m.illum * 100) + "% "));
      var ms = document.createElement("small");
      ms.textContent = m.phase.toLowerCase();
      $("f-moon").appendChild(ms);
    }
    var notes = [];
    if (m) notes.push(m.illum > 0.97 ? "Full moon tonight" : "Full moon " + shortDate(m.next_full));
    if (k.solstice) {
      notes.push(
        k.solstice.days === 0
          ? "Solstice today"
          : (k.solstice.name === "winter" ? "the days start growing again in " : "the longest day comes in ") +
              k.solstice.days +
              (k.solstice.days === 1 ? " day" : " days"),
      );
    }
    $("skynote").textContent = notes.join(" · ");
  }

  /* ---------- Sizing and auto-scroll: nothing is hidden silently, nothing moves for a few pixels. Layout first,
     motion last (rules in fit.js):
     - In work hours the task list comes first in the left column (KioskFit.allocate): it shows all
       its rows while In flight keeps its minimum (3 waiting rows); In flight takes the rest and each of its groups
       (Waiting on you, Running now, Just finished) moves through its rows when they do not all fit. The task list
       moves only as the last resort, when even that minimum leaves it too little room.
     - Every box ends on a row boundary. A list moves only when at least ONE WHOLE ROW cannot be shown; less than a
       row is absorbed (the box gets the pixels).
     - When a list does move, it steps one row at a time (a 0.7 s glide, then 3.5 s still), holds 6 s at the top and
       at the bottom, then glides back to the top: calmer than a continuous crawl, the text is still while it is
       read. With prefers-reduced-motion it jumps a page at a time (every 8 s). No fonts are shrunk.
     Measured after every data refresh, layout change and resize, and every 5 s. */
  var Fit = window.KioskFit;
  var ROW_HOLD_MS = 3500,
    STEP_MS = 700,
    END_HOLD_MS = 6000,
    RETURN_MS = 1200,
    PAGE_MS = 8000;
  var reduceMQ = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  var scrollers = [];
  function makeScroller(view, list, group) {
    var sc = { view: view, list: list, group: group, stops: [], idx: 0, nextAt: 0 };
    scrollers.push(sc);
    return sc;
  }
  function reduced() {
    return !!(reduceMQ && reduceMQ.matches);
  }
  function rowsOf(list) {
    // each row's top and bottom inside its box (the box is position: relative; the list's transform does not count)
    var tops = [],
      bottoms = [];
    Array.prototype.forEach.call(list.children, function (c) {
      if (!c.offsetHeight) return;
      tops.push(c.offsetTop);
      bottoms.push(c.offsetTop + c.offsetHeight);
    });
    function uniq(a) {
      a.sort(function (x, y) {
        return x - y;
      });
      return a.filter(function (v, i) {
        return i === 0 || v - a[i - 1] > 1;
      });
    }
    return { tops: uniq(tops), bottoms: uniq(bottoms) };
  }
  function place(sc, ms) {
    var y = sc.stops.length ? sc.stops[sc.idx] || 0 : 0;
    sc.list.style.transition = ms ? "transform " + ms + "ms ease-in-out" : "none";
    sc.list.style.transform = y ? "translateY(" + -y + "px)" : "";
  }
  function setCount(sc, shown) {
    // just finished in work hours: the newest rows that fit, the heading says how many of how many
    var g = sc.group,
      total = +(g.getAttribute("data-total") || 0),
      items = +(g.getAttribute("data-items") || 0);
    if (!items) return;
    var n = Math.max(total, items);
    g.querySelector(".fgn").textContent = "· " + (shown < n ? shown + " of " + n : n);
  }
  function allocWork(byId) {
    // the work-hours left column: task list on top, In flight under it (waiting | running over just finished)
    var body = $("body"),
      cs = getComputedStyle(body);
    var tv = $("taskview"),
      wv = byId["g-wait"].view,
      rv = byId["g-run"].view,
      dv = byId["g-done"].view;
    var tl = $("tasklist");
    tl.style.rowGap = "";
    var tGap = parseFloat(getComputedStyle(tl).rowGap) || 0;
    tv.style.height = "0px"; // measure the chrome with the task list collapsed: In flight then has its full row
    var total = body.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    var gap = parseFloat(cs.rowGap) || 0;
    var taskChrome = $("taskpanel").offsetHeight;
    var flightChrome = $("flightpanel").offsetHeight - $("g-wait").offsetHeight;
    var o = {
      budget: total - gap - taskChrome - flightChrome,
      t: rowsOf($("tasklist")).bottoms,
      w: rowsOf(byId["g-wait"].list).bottoms,
      r: rowsOf(byId["g-run"].list).bottoms,
      d: rowsOf(byId["g-done"].list).bottoms,
      labW: wv.offsetTop,
      labR: rv.offsetTop,
      labD: dv.offsetTop,
      colGap: $("g-done").offsetTop - ($("g-run").offsetTop + $("g-run").offsetHeight),
      tGapSlack: Math.floor(tGap / 2), // up to half of each task-row gap may go before a row is taken away
    };
    var k = Fit.allocate(o);
    if (k.gapCut) tl.style.rowGap = tGap - k.gapCut + "px";
    tv.style.height = k.tHeight + "px";
    wv.style.height = Fit.heightOf(o.w, k.w) + "px";
    rv.style.height = Fit.heightOf(o.r, k.r) + "px";
    dv.style.height = Fit.heightOf(o.d, k.d) + "px";
    // just finished scrolls like the others: its heading is the plain count
    setCount(byId["g-done"], +(byId["g-done"].group.getAttribute("data-items") || 0));
  }
  function measureScrollers() {
    var now = performance.now();
    var byId = {};
    scrollers.forEach(function (sc) {
      byId[sc.group.id] = sc;
      sc.view.style.height = "";
    });
    $("tasklist").style.rowGap = "";
    var alloc = document.body.classList.contains("work") && !$("flightpanel").hidden && !$("taskpanel").hidden;
    document.body.classList.toggle("alloc", alloc);
    if (alloc) allocWork(byId);
    scrollers.forEach(function (sc) {
      var shown = sc.view.offsetParent !== null && sc.view.clientHeight > 0;
      var rows = shown ? rowsOf(sc.list) : { tops: [], bottoms: [] };
      if (shown && !alloc) {
        // any other layout: the box the CSS gives, snapped to whole rows (or given the pixels when under a row)
        var box = Fit.fitBox(rows.bottoms, sc.view.clientHeight);
        if (box.mode !== "fits") sc.view.style.height = box.height + "px";
      }
      var max = shown ? sc.list.offsetHeight - sc.view.clientHeight : 0;
      var k = Fit.rowsThatFit(rows.bottoms, sc.view.clientHeight);
      var rowPx = k < rows.bottoms.length ? rows.bottoms[k] - Fit.heightOf(rows.bottoms, k) : 0;
      if (Fit.decide(max, rowPx) !== "scroll") {
        // it fits, or less than a whole row is hidden (absorbed above): no motion
        sc.stops = [];
        sc.idx = 0;
        place(sc, 0);
        return;
      }
      var stops = reduced() ? pageStops(rows, sc.view.clientHeight, max) : Fit.rowStops(rows.tops, max);
      var was = sc.stops.length;
      if (stops.join() !== sc.stops.join()) {
        sc.stops = stops;
        sc.idx = Math.min(sc.idx, stops.length - 1);
        place(sc, 0);
      }
      if (!was) {
        sc.idx = 0;
        sc.nextAt = now + END_HOLD_MS; // newly too long: read the top first
        place(sc, 0);
      }
    });
    runScrollers();
  }
  function pageStops(rows, h, max) {
    // reduced motion: page by page, each page starting on a row
    var stops = [0];
    for (var i = 0; i < rows.tops.length; i++) {
      if (rows.bottoms[i] - stops[stops.length - 1] > h + 1 && rows.tops[i] > stops[stops.length - 1]) {
        stops.push(Math.min(rows.tops[i], max));
      }
    }
    if (stops[stops.length - 1] < max - 1) stops.push(max);
    return stops;
  }
  var scrollTimer = 0;
  function stepScrollers() {
    var now = performance.now(),
      any = false;
    scrollers.forEach(function (sc) {
      if (sc.stops.length < 2) return;
      any = true;
      if (now < sc.nextAt) return;
      var last = sc.stops.length - 1;
      if (reduced()) {
        sc.idx = sc.idx >= last ? 0 : sc.idx + 1;
        place(sc, 0);
        sc.nextAt = now + PAGE_MS;
      } else if (sc.idx < last) {
        sc.idx++;
        place(sc, STEP_MS);
        sc.nextAt = now + STEP_MS + (sc.idx === last ? END_HOLD_MS : ROW_HOLD_MS);
      } else {
        sc.idx = 0;
        place(sc, RETURN_MS);
        sc.nextAt = now + RETURN_MS + END_HOLD_MS;
      }
    });
    return any;
  }
  function runScrollers() {
    var any = scrollers.some(function (sc) {
      return sc.stops.length > 1;
    });
    if (!any) {
      clearInterval(scrollTimer);
      scrollTimer = 0;
    } else if (!scrollTimer) {
      scrollTimer = setInterval(function () {
        if (!stepScrollers()) runScrollers();
      }, 250);
    }
  }
  ["g-wait", "g-run", "g-done", "g-prs"].forEach(function (id) {
    var g = $(id);
    makeScroller(g.querySelector(".fview"), g.querySelector(".flist"), g);
  });
  makeScroller($("taskview"), $("tasklist"), $("taskpanel"));
  window.addEventListener("resize", function () {
    requestAnimationFrame(measureScrollers);
  });
  if (reduceMQ && reduceMQ.addEventListener) reduceMQ.addEventListener("change", measureScrollers);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(measureScrollers);
  setInterval(measureScrollers, 5000);

  /* ---------- In flight: waiting on you, running now, just finished, open PRs (/api/inflight) ---------- */
  var flight = null,
    flightFails = 0,
    lastFlight = null,
    lastFlightAt = 0;
  function clockOf(ts) {
    return fmtClock.format(new Date(ts * 1000)).replace(/\s?([AP])M$/, function (_, a) {
      return " " + a.toLowerCase() + "m";
    });
  }
  function age(ts) {
    var m = Math.max(0, Math.floor((Date.now() / 1000 - ts) / 60));
    if (m < 1) return "<1m";
    if (m < 60) return m + "m";
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }
  function pill(text, tone) {
    var x = document.createElement("span");
    x.className = "fx" + (tone ? " " + tone : "");
    x.textContent = text;
    return x;
  }
  function ciPill(ci) {
    if (ci === "passing") return pill("checks green", "green");
    if (ci === "failing") return pill("checks failing", "red");
    if (ci === "running") return pill("checks running", "amber");
    if (ci === "none") return pill("no checks", "");
    return null;
  }
  function row(mark, text, right, extras) {
    var li = document.createElement("li");
    li.className = "fi";
    var m = document.createElement("span");
    m.className = "fm";
    m.textContent = mark;
    var t = document.createElement("span");
    t.className = "ft";
    if (typeof text === "string") t.textContent = text;
    else t.appendChild(text);
    (extras || []).forEach(function (e) {
      if (e) t.appendChild(e);
    });
    var a = document.createElement("span");
    a.className = "fa";
    a.textContent = right || "";
    li.appendChild(m);
    li.appendChild(t);
    li.appendChild(a);
    return li;
  }
  function fill(id, items, quiet, make, total) {
    var g = $(id),
      ul = g.querySelector(".flist");
    ul.textContent = "";
    // the real count, always; "12 of 15" only when the server
    // capped the group (ITEMS_MAX), never because of the space: rows that do not fit scroll instead
    var n = Math.max(total || 0, items.length);
    g.setAttribute("data-total", n);
    g.setAttribute("data-items", items.length);
    g.querySelector(".fgn").textContent = "· " + (n > items.length ? items.length + " of " + n : n);
    if (!items.length) {
      var q = document.createElement("li");
      q.className = "fquiet";
      q.textContent = quiet;
      ul.appendChild(q);
      return;
    }
    items.forEach(function (it) {
      ul.appendChild(make(it));
    });
  }
  function renderFlight(d) {
    flight = d;
    var on = !!(d && d.show);
    if (on !== flightOn) {
      flightOn = on;
      layout();
    }
    if (!on) return; // hidden (outside work hours): nothing to draw, the off-hours view has the spot
    d = d || { waiting: [], running: [], finished: [], prs: [], notes: "missing", github: "off" };
    var w = d.waiting || [],
      r = d.running || [],
      f = d.finished || [],
      p = d.prs || [];
    var c = d.counts || {};
    var none = !w.length && !r.length && !f.length;
    // all three empty: say so plainly instead of three quiet lines
    $("flightempty").hidden = !none;
    $("g-wait").hidden = none;
    $("flightpanel").querySelector(".fcols").style.display = none ? "none" : "";
    fill("g-wait", w, "nothing waiting on you", function (it) {
      var ex = [];
      if (it.ci) ex.push(ciPill(it.ci));
      if (it.review) ex.push(pill(it.review, it.review === "approved" ? "green" : ""));
      // a computed line (inflight.auto_waiting: In Review, every PR merged) gets a hollow mark; hand lines unchanged
      return row(it.source === "auto" ? "○" : "●", it.text, "", ex);
    }, c.waiting);
    fill("g-run", r, "nothing running", function (it) {
      // the hook's record says running, but the agent has not written for a long while: say so, with when
      // (agents.py QUIET_S), instead of an age that keeps counting
      if (it.quiet_since) {
        var q = row("?", it.text, "no word since " + clockOf(it.quiet_since));
        q.classList.add("quiet");
        return q;
      }
      var li = row("▶", it.text, it.ts ? age(it.ts) : "");
      if (it.ts) li.querySelector(".fa").setAttribute("data-since", it.ts);
      return li;
    }, c.running);
    fill("g-done", f, "nothing just finished", function (it) {
      return row("✓", it.text, it.ts ? clockOf(it.ts).replace(/\s?[ap]m$/, "") : it.clock || "");
    }, c.finished);
    $("g-prs").hidden = !p.length;
    fill("g-prs", p, "", function (it) {
      var frag = document.createDocumentFragment();
      var ref = document.createElement("span");
      ref.className = "fref";
      ref.textContent = it.repo + "#" + it.number;
      frag.appendChild(ref);
      frag.appendChild(document.createTextNode(it.title));
      return row("·", frag, "", [ciPill(it.ci), it.review ? pill(it.review, "") : null]);
    }, c.prs);
    var sub = "";
    // when the block was last written, small, so a stale panel shows
    if (d.notes === "ok" && d.block_at) sub = "updated " + clockOf(d.block_at);
    else if (d.notes === "stale")
      sub = "not current: last updated " + (d.block_at ? fmtDay.format(new Date(d.block_at * 1000)) + ", " +
        clockOf(d.block_at) : "over 18 hours ago");
    else sub = "no In flight block yet";
    if (flightFails && lastFlightAt) sub += " · kiosk server not answering since " + clockOf(lastFlightAt / 1000);
    $("flightsub").textContent = sub;
    var gh = $("flightgh");
    gh.className = "flightgh";
    if (d.github === "ok") gh.textContent = "GitHub ✓";
    else if (d.github === "stale" && d.github_at) {
      // the last good result (a fetch failed, or the server just started from its saved copy): its age, in amber
      gh.textContent = "GitHub as of " + clockOf(d.github_at);
      gh.classList.add("off");
    } else if (d.github === "off") {
      gh.textContent = "GitHub unavailable";
      gh.classList.add("off");
    } else gh.textContent = "";
    requestAnimationFrame(measureScrollers);
  }
  function tickAges() {
    Array.prototype.forEach.call(document.querySelectorAll("#g-run .fa[data-since]"), function (el) {
      el.textContent = age(+el.getAttribute("data-since"));
    });
  }
  function pollFlight() {
    // only while the panel could be on screen; outside work hours the server answers "hidden" without reading the
    // notes or GitHub, so this poll is what brings In flight back at 08:00 without a reload
    if (panel !== "auto" && panel !== "inflight") return;
    var x = new XMLHttpRequest();
    x.open("GET", "/api/inflight", true);
    x.timeout = 8000;
    x.onload = function () {
      var d = null;
      if (x.status === 200) {
        try {
          d = JSON.parse(x.responseText);
        } catch (e) {
          d = null;
        }
      }
      flightFails = 0;
      if (d) {
        lastFlight = d;
        lastFlightAt = Date.now();
        renderFlight(d);
      }
    };
    // a failed poll keeps the last panel, marked with how long the server has been silent
    x.onerror = x.ontimeout = function () {
      flightFails++;
      if (lastFlight) renderFlight(lastFlight);
      else if (flightFails >= 3) renderFlight(null);
    };
    x.send();
  }

  /* ---------- Done this week (/api/done, work hours only): the reserved space under the daylight facts ---------- */
  var lastDone = null,
    lastDoneAt = 0,
    doneFails = 0;
  function doneAge(ts, nowTs) {
    var s = Math.max(0, nowTs - ts);
    if (s < 3600) return Math.max(1, Math.floor(s / 60)) + "m";
    if (s < 86400) return Math.floor(s / 3600) + "h";
    return Math.floor(s / 86400) + "d";
  }
  function renderDone(d) {
    var list = $("donelist"),
      foot = $("donefoot");
    list.textContent = "";
    foot.textContent = "";
    $("donenote").textContent = "";
    $("donecount").textContent = "";
    if (!d || !d.show) return;
    var nowTs = (d.now || Date.now() / 1000) + (Date.now() - lastDoneAt) / 1000;
    $("donecount").textContent = "· " + (d.count || 0);
    var notes = (d.notes || []).slice();
    if (doneFails && lastDoneAt) notes.push("kiosk server not answering since " + clockOf(lastDoneAt / 1000));
    $("donenote").textContent = notes.join(" · ");
    (d.items || []).forEach(function (it) {
      var li = document.createElement("li");
      li.className = "done";
      var a = document.createElement("span");
      a.className = "dage";
      a.textContent = doneAge(it.ts, nowTs);
      var r = document.createElement("span");
      r.className = "dref";
      r.textContent = it.ref + (it.pr ? " " + it.pr : "");
      var t = document.createElement("span");
      t.className = "dtitle";
      t.textContent = it.title || "";
      li.appendChild(a);
      if (it.ref) li.appendChild(r);
      li.appendChild(t);
      list.appendChild(li);
    });
    if (!(d.items || []).length) {
      var none = document.createElement("li");
      none.className = "done dnone";
      none.textContent = notes.length ? "nothing known yet" : "nothing in the last 7 days";
      list.appendChild(none);
    }
    var more = document.createElement("span");
    more.className = "dmore";
    var close = document.createElement("span");
    close.className = "dclose";
    var cc = d.can_close || [];
    close.textContent = "can close: " + (cc.length ? cc.join(", ") : "none");
    if (cc.length) close.classList.add("some");
    foot.appendChild(more);
    foot.appendChild(close);
    // whole rows only: a row that does not fit is taken off and counted in "+N more" (never dropped silently)
    var extra = d.more || 0;
    var box = $("shopspare");
    function setMore() {
      more.textContent = extra ? "+" + extra + " more" : "";
    }
    setMore();
    while (list.children.length > 1 && box.scrollHeight > box.clientHeight + 1 &&
      !list.lastChild.classList.contains("dnone")) {
      list.removeChild(list.lastChild);
      extra++;
      setMore();
    }
  }
  function pollDone() {
    var x = new XMLHttpRequest();
    x.open("GET", "/api/done", true);
    x.timeout = 8000;
    x.onload = function () {
      var d = null;
      if (x.status === 200) {
        try {
          d = JSON.parse(x.responseText);
        } catch (e) {
          d = null;
        }
      }
      doneFails = 0;
      lastDone = d;
      lastDoneAt = Date.now();
      renderDone(d);
    };
    x.onerror = x.ontimeout = function () {
      doneFails++;
      renderDone(lastDone);
    };
    x.send();
  }

  // start the polls last, so every variable above is set
  pollFlight();
  setInterval(pollFlight, 30000);
  setInterval(tickAges, 15000);
  pollTasks();
  setInterval(pollTasks, 30000);
  pollMode();
  setInterval(pollMode, 1000);
  pollDone();
  setInterval(pollDone, 60000);
  // drawn again every 5 s from the last answer: the ages move on and the rows are fitted to the box's current size
  // (the caption above it can change height)
  setInterval(function () {
    if (lastDone) renderDone(lastDone);
  }, 5000);
  // and at once when the Done panel changes size (it takes what the workshop above leaves, which changes with the
  // scene or the caption); its size does not depend on its rows, so no loop
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      if (lastDone) renderDone(lastDone);
    }).observe($("shopspare"));
  }
})();
