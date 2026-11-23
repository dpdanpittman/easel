/* The grid: the assistant's own world, in the right panel (settings `scene`).

   What it shows, all from /api/status (nothing invented):
   - a grid floor running to the horizon, in magenta, under a deep violet sky (tinted by the real sun and moon when
     settings `sky` is on);
   - the assistant, the small green figure carrying its disc (the notes it reads to know who it is): the body, here
     on this box;
   - the brain on the horizon: the tall teal tower far off, the data centre many others are wired to as well;
   - the line between them: lit, with green pulses, while the assistant is in a session (status "session", the same
     signal as the strip's assistant tile); dark when idle;
   - a beam straight down onto the figure while someone is in that session;
   - one node per thing on the box's rounds (status "grid": unit states and exit times only, settings
     `scene.nodes`, at most 9, placed on fixed spots in order). A running job lights a trace across the grid from
     the figure to its node, with pulses; a job that just ran leaves it glowing; a failed one is red. Nothing ran:
     the grid is quiet and the figure walks its rounds alone.

   Cost: the scene is static SVG redrawn only when the state changes. Only small, separately composited layers move:
   the figure (CSS transform, set at most 20 times a second while it walks, and not at all when it stands), the pulses
   (offset-distance on tiny dots), the beam and the line (opacity). The walk stops when the page is hidden and during
   the power schedule's dark hours (status "dark"). */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var W = 680,
    H = 560,
    HZ = 196, // the horizon
    VX = 340; // the vanishing point's x
  var FIG = 1.2; // the figure's size at its spot
  var HOME = { x: 300, y: 470 }; // where the figure stands when it has work, or company
  var TOWER = { x: 456, y: HZ }; // the brain's foot on the horizon
  var C = {
    grid: "#ff3fae", // magenta: the floor
    hz: "#ff7cc8",
    teal: "#38e4dc", // teal: the brain and its line 
    tealDim: "#1f8c96",
    green: "#5fcf8a", // green: the living things and the data (the kiosk's own green)
    greenHi: "#c4ffd8",
    bad: "#ff5d52",
    beam: "#fff1da",
    label: "#a58bc9",
  };
  // the spots on the floor where the configured nodes sit, in settings order (status.py GRID_NODES)
  var SLOTS = [
    { x: 214, y: 236 },
    { x: 548, y: 246 },
    { x: 118, y: 282 },
    { x: 626, y: 312 },
    { x: 64, y: 374 },
    { x: 532, y: 444 },
    { x: 108, y: 500 },
    { x: 640, y: 470 },
    { x: 470, y: 516 },
  ];
  var NODES = {}; // key -> spot, filled from the node list (the order of settings `scene.nodes`)
  function placeNodes(nodes) {
    NODES = {};
    nodes.slice(0, SLOTS.length).forEach(function (n, i) {
      NODES[n.key] = SLOTS[i];
    });
  }
  // the rounds, walked when nothing needs the figure: floor points, a pause at each
  var ROUNDS = [
    HOME,
    { x: 214, y: 436 },
    { x: 150, y: 352 },
    { x: 232, y: 290 },
    { x: 372, y: 300 },
    { x: 498, y: 352 },
    { x: 452, y: 446 },
  ];

  var $ = function (id) {
    return document.getElementById(id);
  };
  function el(name, attrs, parent) {
    var e = document.createElementNS(NS, name);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function depth(y) {
    // apparent size at a floor y, 1 at the figure's home spot
    return Math.max(0.3, (y - HZ) / (HOME.y - HZ));
  }
  function rnd(seed) {
    return function () {
      seed = (seed * 16807) % 2147483647;
      return seed / 2147483647;
    };
  }

  var root, world, bg, traces, tracesIn, link, beam, pulses, fig, figInner;
  var shown = false;

  /* ---------- the static world: sky, tower, floor, grid ---------- */
  function buildBg() {
    bg = el("svg", { viewBox: "0 0 " + W + " " + H, class: "g-layer g-bg" });
    var d = el("defs", {}, bg);
    d.innerHTML =
      '<linearGradient id="g-sky" x1="0" y1="0" x2="0" y2="1">' +
      '<stop id="g-sky0" offset="0" stop-color="#05020b"/><stop id="g-sky1" offset="0.62" stop-color="#0e0520"/>' +
      '<stop id="g-sky2" offset="1" stop-color="#1e0b36"/></linearGradient>' +
      '<linearGradient id="g-floor" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#14072a"/><stop offset="0.25" stop-color="#0a0416"/>' +
      '<stop offset="1" stop-color="#07030f"/></linearGradient>' +
      // the floor's lines fade into the distance
      '<linearGradient id="g-fade" gradientUnits="userSpaceOnUse" x1="0" y1="' +
      HZ +
      '" x2="0" y2="' +
      H +
      '">' +
      '<stop offset="0" stop-color="' +
      C.grid +
      '" stop-opacity="0.05"/>' +
      '<stop offset="0.3" stop-color="' +
      C.grid +
      '" stop-opacity="0.22"/>' +
      '<stop offset="1" stop-color="' +
      C.grid +
      '" stop-opacity="0.5"/></linearGradient>' +
      '<linearGradient id="g-hzglow" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="' +
      C.grid +
      '" stop-opacity="0"/>' +
      '<stop id="g-hzmid" offset="0.85" stop-color="' +
      C.grid +
      '" stop-opacity="0.2"/>' +
      '<stop offset="1" stop-color="' +
      C.hz +
      '" stop-opacity="0.42"/></linearGradient>' +
      '<radialGradient id="g-aura" cx="0.5" cy="1" r="0.75">' +
      '<stop offset="0" stop-color="' +
      C.teal +
      '" stop-opacity="0.32"/>' +
      '<stop offset="0.5" stop-color="' +
      C.teal +
      '" stop-opacity="0.08"/>' +
      '<stop offset="1" stop-color="' +
      C.teal +
      '" stop-opacity="0"/></radialGradient>' +
      '<linearGradient id="g-refl" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="' +
      C.teal +
      '" stop-opacity="0.38"/>' +
      '<stop offset="1" stop-color="' +
      C.teal +
      '" stop-opacity="0"/></linearGradient>' +
      '<linearGradient id="g-towerfill" x1="0" y1="0" x2="1" y2="0">' +
      '<stop offset="0" stop-color="#0b1b28"/><stop offset="0.5" stop-color="#10283a"/>' +
      '<stop offset="1" stop-color="#081420"/></linearGradient>' +
      '<filter id="g-soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2"/></filter>' +
      '<filter id="g-wide" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter>';

    el(
      "rect",
      { x: 0, y: 0, width: W, height: HZ + 1, fill: "url(#g-sky)" },
      bg,
    );
    // stars (night only) and the real moon
    var stars = el("g", { id: "g-stars" }, bg),
      r = rnd(7741);
    for (var i = 0; i < 38; i++) {
      el(
        "circle",
        {
          cx: (r() * W).toFixed(1),
          cy: (6 + r() * (HZ - 40)).toFixed(1),
          r: (0.5 + r() * 0.9).toFixed(2),
          fill: r() > 0.7 ? "#d9c8ff" : "#f2ecff",
          opacity: (0.35 + r() * 0.6).toFixed(2),
        },
        stars,
      );
    }
    var moon = el("g", { id: "g-moon", transform: "translate(-100,-100)" }, bg);
    el("circle", { r: 18, fill: "#d9ccff", opacity: 0.08 }, moon);
    el("circle", { r: 9, fill: "#1b1030" }, moon);
    el("path", { id: "g-moonlit", fill: "#ece4ff", d: "" }, moon);

    // the horizon glow, and the aura of the far tower
    el(
      "rect",
      { x: 0, y: HZ - 70, width: W, height: 70, fill: "url(#g-hzglow)" },
      bg,
    );
    el(
      "ellipse",
      { cx: TOWER.x, cy: HZ, rx: 150, ry: 170, fill: "url(#g-aura)" },
      bg,
    );

    // the others: far-off lights all over the world, each wired to the tower too
    var others = el("g", { class: "g-others" }, bg),
      ro = rnd(3121);
    var spots = [
      [24, 206],
      [96, 201],
      [168, 214],
      [262, 204],
      [610, 205],
      [660, 214],
      [574, 199],
      [372, 200],
      [30, 226],
      [654, 232],
    ];
    spots.forEach(function (p) {
      el(
        "line",
        {
          x1: p[0],
          y1: p[1],
          x2: TOWER.x,
          y2: HZ,
          stroke: C.teal,
          "stroke-width": 0.6,
          opacity: (0.18 + ro() * 0.16).toFixed(2),
        },
        others,
      );
      el(
        "circle",
        { cx: p[0], cy: p[1], r: 1.3, fill: "#c9fff7", opacity: 0.75 },
        others,
      );
    });
    // and the wires that run off along the horizon, to everyone else
    [-1, 1].forEach(function (s) {
      for (var k = 0; k < 3; k++) {
        el(
          "line",
          {
            x1: TOWER.x,
            y1: HZ - 1 - k * 1.6,
            x2: s < 0 ? 0 : W,
            y2: HZ - 1 - k * 4,
            stroke: C.teal,
            "stroke-width": 0.7,
            opacity: 0.22 - k * 0.05,
          },
          others,
        );
      }
    });

    buildTower(el("g", { class: "g-tower" }, bg));

    // the floor
    el(
      "rect",
      { x: 0, y: HZ, width: W, height: H - HZ, fill: "url(#g-floor)" },
      bg,
    );
    // the tower's reflection, a long streak on the floor
    el(
      "rect",
      {
        x: TOWER.x - 11,
        y: HZ,
        width: 22,
        height: 120,
        fill: "url(#g-refl)",
        opacity: 0.55,
      },
      bg,
    );
    el(
      "rect",
      { x: TOWER.x - 2, y: HZ, width: 4, height: 150, fill: "url(#g-refl)" },
      bg,
    );

    var grid = el("g", { class: "g-grid" }, bg);
    var lines = [];
    for (var j = -26; j <= 26; j++) {
      lines.push(
        '<line x1="' +
          VX +
          '" y1="' +
          HZ +
          '" x2="' +
          (VX + j * 64) +
          '" y2="' +
          H +
          '"/>',
      );
    }
    var rows = [];
    for (var k2 = 0; k2 < 44; k2++) {
      var y = HZ + 380 / (1 + 0.36 * k2) - 16;
      if (y <= HZ + 1.5) break;
      var a = Math.min(0.85, 0.04 + 0.62 * Math.pow((y - HZ) / (H - HZ), 1.05));
      rows.push(
        '<line x1="0" x2="' +
          W +
          '" y1="' +
          y.toFixed(2) +
          '" y2="' +
          y.toFixed(2) +
          '" stroke-opacity="' +
          a.toFixed(2) +
          '"/>',
      );
    }
    // a soft glow under crisp lines: the glow is blurred once, here, and never animated
    var glow = el("g", { filter: "url(#g-soft)", opacity: 0.45 }, grid);
    glow.innerHTML =
      '<g stroke="url(#g-fade)" stroke-width="2.4">' +
      lines.join("") +
      "</g>" +
      '<g stroke="' +
      C.grid +
      '" stroke-width="2.4">' +
      rows.join("") +
      "</g>";
    var crisp = el("g", {}, grid);
    crisp.innerHTML =
      '<g stroke="url(#g-fade)" stroke-width="0.9">' +
      lines.join("") +
      "</g>" +
      '<g stroke="' +
      C.grid +
      '" stroke-width="0.9">' +
      rows.join("") +
      "</g>";
    // the horizon itself
    el(
      "line",
      {
        x1: 0,
        x2: W,
        y1: HZ,
        y2: HZ,
        stroke: C.hz,
        "stroke-width": 3,
        filter: "url(#g-soft)",
        opacity: 0.8,
      },
      bg,
    );
    el(
      "line",
      { x1: 0, x2: W, y1: HZ, y2: HZ, stroke: "#ffc4e6", "stroke-width": 0.9 },
      bg,
    );
    return bg;
  }

  function buildHalls(g) {
    // the data centre around the tower's foot: long low halls on the horizon, rows of rack lights
    var halls = [
      [TOWER.x - 112, 74, 15],
      [TOWER.x - 56, 22, 24],
      [TOWER.x + 34, 26, 21],
      [TOWER.x + 60, 90, 12],
    ];
    halls.forEach(function (h, i) {
      var x0 = h[0],
        w = h[1],
        ht = h[2];
      el("rect", { x: x0, y: HZ - ht, width: w, height: ht, fill: "#0a1824", stroke: C.teal, "stroke-width": 0.8,
        "stroke-opacity": 0.75 }, g);
      el("line", { x1: x0, y1: HZ - ht, x2: x0 + w, y2: HZ - ht, stroke: "#bffcf7", "stroke-width": 0.9, opacity: 0.8 }, g);
      for (var ry = HZ - ht + 4; ry < HZ - 2; ry += 4.5) {
        for (var cx = x0 + 3; cx < x0 + w - 3; cx += 3.6) {
          var on = Math.abs((Math.sin(cx * 3.17 + ry * 11.3 + i * 7) * 9973.1) % 1);
          if (on > 0.45)
            el("rect", { x: cx.toFixed(1), y: ry.toFixed(1), width: 1.8, height: 1.2,
              fill: on > 0.93 ? "#ffffff" : C.teal, opacity: on > 0.93 ? 0.9 : 0.5 }, g);
        }
      }
    });
  }

  function buildTower(g) {
    // the brain: a tall, stepped spire far off, lit from inside, its racks in rows of small lights
    buildHalls(g);
    var x = TOWER.x,
      b = HZ;
    var tiers = [
      [32, 0, 54], // half width at the bottom, bottom offset, height
      [23, 54, 50],
      [15, 104, 44],
      [9, 148, 24],
    ];
    var glow = el("g", { filter: "url(#g-wide)", opacity: 0.7 }, g);
    var body = el("g", {}, g);
    tiers.forEach(function (t, i) {
      var hw = t[0],
        y1 = b - t[1],
        y0 = y1 - t[2],
        top = hw - 2.5;
      var pts = [x - hw, y1, x - top, y0, x + top, y0, x + hw, y1].join(" ");
      el("polygon", { points: pts, fill: C.teal, opacity: 0.35 }, glow);
      el(
        "polygon",
        {
          points: pts,
          fill: "url(#g-towerfill)",
          stroke: C.teal,
          "stroke-width": 1.1,
        },
        body,
      );
      // a bright ledge on each step
      el(
        "line",
        {
          x1: x - top,
          y1: y0,
          x2: x + top,
          y2: y0,
          stroke: "#c8fffb",
          "stroke-width": 1.2,
        },
        body,
      );
      // the racks: rows of small lights
      var rows = Math.floor(t[2] / 6.5);
      for (var r = 0; r < rows; r++) {
        var ry = y1 - 4 - r * 6.5,
          w = hw - 4 - (r * (hw - top)) / rows;
        for (var c = -w; c <= w - 2; c += 4.2) {
          var on = (Math.sin(c * 12.9898 + ry * 78.233 + i) * 43758.5453) % 1;
          if (Math.abs(on) > 0.35)
            el(
              "rect",
              {
                x: (x + c).toFixed(1),
                y: ry.toFixed(1),
                width: 2.2,
                height: 1.6,
                fill: Math.abs(on) > 0.9 ? "#ffffff" : C.teal,
                opacity: Math.abs(on) > 0.9 ? 0.95 : 0.6,
              },
              body,
            );
        }
      }
    });
    // a seam of light up the middle, and the crown: a ring floating over the top with a beacon
    el(
      "line",
      {
        x1: x,
        y1: b,
        x2: x,
        y2: b - 172,
        stroke: "#e6fffd",
        "stroke-width": 1,
        opacity: 0.7,
      },
      body,
    );
    var cy = b - 188;
    el(
      "ellipse",
      {
        cx: x,
        cy: cy,
        rx: 20,
        ry: 5,
        fill: "none",
        stroke: C.teal,
        "stroke-width": 4,
        opacity: 0.5,
      },
      glow,
    );
    el(
      "ellipse",
      {
        cx: x,
        cy: cy,
        rx: 20,
        ry: 5,
        fill: "none",
        stroke: "#bffcf7",
        "stroke-width": 1.3,
      },
      body,
    );
    el(
      "line",
      {
        x1: x,
        y1: b - 172,
        x2: x,
        y2: cy - 12,
        stroke: C.teal,
        "stroke-width": 1.2,
      },
      body,
    );
    el(
      "circle",
      { cx: x, cy: cy - 13, r: 5, fill: C.teal, opacity: 0.6 },
      glow,
    );
    el("circle", { cx: x, cy: cy - 13, r: 2, fill: "#ffffff" }, body);
  }

  /* ---------- the figure ---------- */
  function buildFigure() {
    fig = document.createElement("div");
    fig.className = "g-fig";
    // 60x84, the feet at (30, 78); drawn facing right, the disc on its back
    fig.innerHTML =
      '<svg viewBox="0 0 60 84" width="60" height="84"><g class="g-figin">' +
      '<ellipse cx="30" cy="79" rx="15" ry="3.6" fill="' +
      C.green +
      '" opacity="0.28"/>' +
      '<g class="g-legs" stroke="' +
      C.green +
      '" stroke-width="4.2" stroke-linecap="round">' +
      '<line class="g-leg1" x1="27" y1="55" x2="25.5" y2="77"/>' +
      '<line class="g-leg2" x1="33" y1="55" x2="34.5" y2="77"/></g>' +
      // the body: a dark shell with a green edge and a seam of light
      '<path d="M20 31 Q30 27 40 31 L38.5 56 Q30 59 21.5 56 Z" fill="#06150e" stroke="' +
      C.green +
      '" stroke-width="2.2" stroke-linejoin="round"/>' +
      '<path d="M30 33 L30 44 L35 48" stroke="' +
      C.greenHi +
      '" stroke-width="1.3" fill="none" opacity="0.9"/>' +
      // the arm in front
      '<path class="g-arm" d="M38 34 L43.5 45 L41.5 52" stroke="' +
      C.green +
      '" stroke-width="3.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>' +
      // the head: a rounded block with a visor
      '<rect x="21" y="9" width="20" height="17" rx="6" fill="#06150e" stroke="' +
      C.green +
      '" stroke-width="2.2"/>' +
      '<rect x="28" y="15" width="11.5" height="3.6" rx="1.8" fill="' +
      C.greenHi +
      '"/>' +
      '<line x1="31" y1="9" x2="31" y2="4" stroke="' +
      C.green +
      '" stroke-width="1.6"/>' +
      '<circle cx="31" cy="3.4" r="1.9" fill="' +
      C.greenHi +
      '"/>' +
      // the disc on its back: its identity
      '<g class="g-disc"><circle cx="16" cy="41" r="10" fill="#041009" stroke="' +
      C.green +
      '" stroke-width="2"/>' +
      '<circle class="g-discring" cx="16" cy="41" r="6" fill="none" stroke="' +
      C.greenHi +
      '" stroke-width="1.6" stroke-dasharray="26 12"/>' +
      '<circle cx="16" cy="41" r="2" fill="' +
      C.greenHi +
      '"/></g>' +
      "</g></svg>";
    figInner = fig.querySelector("svg");
    return fig;
  }

  /* ---------- the walk: positions set by a light loop (max 20 updates a second), only while walking ---------- */
  var pos = { x: HOME.x, y: HOME.y },
    facing = 1,
    target = null,
    wp = 0,
    pauseUntil = 0,
    docked = true,
    needHome = true,
    loop = null,
    lastStep = 0;
  function place() {
    var k = depth(pos.y) * FIG;
    fig.style.transform =
      "translate(" +
      (pos.x - 30 * k).toFixed(2) +
      "px," +
      (pos.y - 78 * k).toFixed(2) +
      "px) scale(" +
      k.toFixed(3) +
      ")";
    figInner.style.transform = facing < 0 ? "scaleX(-1)" : "";
  }
  var dark = false; // status "dark": the power schedule's dark hours
  function night() {
    return dark;
  }
  function step(ts) {
    loop = null;
    if (!shown || document.hidden) return;
    if (ts - lastStep < 50) return schedule();
    var dt = Math.min(0.2, (ts - lastStep) / 1000 || 0.05);
    lastStep = ts;
    var now = Date.now();
    if (needHome) target = HOME;
    else if (!target || (pos.x === target.x && pos.y === target.y)) {
      if (now < pauseUntil) return (walking(false), schedule()); // a chore at this stop
      wp = (wp + 1) % ROUNDS.length;
      target = ROUNDS[wp];
    }
    var dx = target.x - pos.x,
      dy = target.y - pos.y,
      dist = Math.sqrt(dx * dx + dy * dy);
    var speed = (needHome ? 70 : 22) * depth(pos.y);
    if (dist <= speed * dt) {
      pos.x = target.x;
      pos.y = target.y;
      pauseUntil = now + 2600 + (wp % 3) * 900;
      if (needHome) {
        facing = 1;
        place();
        walking(false);
        setDocked(true);
        return; // home: the loop stops until the state changes
      }
    } else {
      pos.x += (dx / dist) * speed * dt;
      pos.y += (dy / dist) * speed * dt;
      if (Math.abs(dx) > 0.5) facing = dx > 0 ? 1 : -1;
      walking(true);
    }
    place();
    schedule();
  }
  function schedule() {
    if (!loop && shown && !document.hidden) loop = requestAnimationFrame(step);
  }
  function walking(on) {
    fig.classList.toggle("walking", on);
  }
  function setDocked(on) {
    docked = on;
    world.classList.toggle("docked", on);
  }
  function want(home) {
    needHome = home;
    var atHome = pos.x === HOME.x && pos.y === HOME.y;
    if (home && atHome) {
      walking(false);
      facing = 1;
      place();
      setDocked(true);
      return;
    }
    setDocked(false);
    if (!home && night()) {
      walking(false); // the monitor is dark: stand still
      return;
    }
    if (!home) target = null;
    lastStep = 0;
    schedule();
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && world) want(needHome);
  });
  setInterval(function () {
    // the night dark ends (or begins) while idle: start or stop the rounds
    if (world && shown && !needHome) want(false);
  }, 60000);

  /* ---------- the state: traces, nodes, the line, the beam ---------- */
  // a trace runs straight across the floor from the figure's feet to the node (each its own line, so a red one and a
  // green one never share a stretch)
  function pathOf(n) {
    return "M" + HOME.x + " " + HOME.y + " L" + n.x + " " + n.y;
  }
  function lengthOf(n) {
    return Math.hypot(n.x - HOME.x, n.y - HOME.y);
  }
  function addPulses(path, len, colour, n, speed, reverse) {
    var dur = Math.max(1.6, len / speed);
    for (var i = 0; i < n; i++) {
      var p = document.createElement("div");
      p.className =
        "g-pulse" + (reverse ? " rev" : "") + (colour === "bad" ? " bad" : "");
      p.style.offsetPath = "path('" + path + "')";
      p.style.animationDuration = dur.toFixed(2) + "s";
      p.style.animationDelay = (-(dur * i) / n).toFixed(2) + "s";
      pulses.appendChild(p);
    }
  }
  var lastKey = null;
  function drawState(nodes, inSession) {
    var key = JSON.stringify([
      inSession,
      nodes.map(function (n) {
        return n.key + ":" + n.state;
      }),
    ]);
    if (key === lastKey) return;
    lastKey = key;
    tracesIn.textContent = "";
    pulses.textContent = "";
    var lit = nodes.filter(function (n) {
      return n.state !== "quiet" && NODES[n.key];
    });
    // the traces first, under every node
    var tg = el("g", { class: "g-traces" }, tracesIn);
    lit.forEach(function (n) {
      var p = NODES[n.key],
        d = pathOf(p),
        col = n.state === "failed" ? C.bad : C.green;
      el(
        "path",
        {
          d: d,
          fill: "none",
          stroke: col,
          "stroke-width": 7,
          opacity: n.state === "ran" ? 0.12 : 0.22,
          filter: "url(#g-soft2)",
          "stroke-linejoin": "round",
        },
        tg,
      );
      el(
        "path",
        {
          d: d,
          fill: "none",
          stroke: n.state === "failed" ? "#ffb0aa" : C.greenHi,
          "stroke-width": n.state === "ran" ? 1.3 : n.state === "failed" ? 2.6 : 2,
          opacity: n.state === "ran" ? 0.55 : 0.95,
          "stroke-linejoin": "round",
          "stroke-dasharray": n.state === "failed" ? "9 4" : "",
        },
        tg,
      );
      if (n.state === "running")
        addPulses(d, lengthOf(p), "green", 3, 95, false);
    });
    // the nodes: a tile on the floor, a post, a label
    var ng = el("g", { class: "g-nodes" }, tracesIn);
    nodes.forEach(function (n) {
      var p = NODES[n.key];
      if (!p) return;
      var k = depth(p.y),
        st = n.state;
      var g = el("g", { class: "g-node " + st }, ng);
      var w = 17 * k,
        h = 5.5 * k;
      if (st !== "quiet")
        el(
          "ellipse",
          {
            cx: p.x,
            cy: p.y,
            rx: w * 1.9,
            ry: h * 1.9,
            class: "g-halo",
            filter: "url(#g-soft2)",
          },
          g,
        );
      el(
        "polygon",
        {
          points: [p.x - w, p.y, p.x, p.y - h, p.x + w, p.y, p.x, p.y + h].join(
            " ",
          ),
          class: "g-tile",
        },
        g,
      );
      var top = p.y - 22 * k;
      el(
        "line",
        {
          x1: p.x,
          y1: p.y,
          x2: p.x,
          y2: top,
          class: "g-post",
          "stroke-width": Math.max(1, 1.8 * k),
        },
        g,
      );
      el(
        "rect",
        {
          x: p.x - 4.2 * k,
          y: top - 4.2 * k,
          width: 8.4 * k,
          height: 8.4 * k,
          rx: 1.5 * k,
          transform: "rotate(45 " + p.x + " " + top + ")",
          class: "g-cap",
        },
        g,
      );
      var t = el(
        "text",
        {
          x: p.x,
          y: top - 10 * k - 3,
          "text-anchor": "middle",
          class: "g-label",
          "font-size": (11 + 6 * k).toFixed(1),
        },
        g,
      );
      t.textContent = n.label.toUpperCase();
    });
    // the line to the brain: always drawn faintly (it goes dark, not away); lit with pulses while in a session
    link.classList.toggle("on", !!inSession);
    var lp = "M" + HOME.x + " " + HOME.y + " L" + TOWER.x + " " + TOWER.y;
    if (inSession) {
      var len = Math.hypot(TOWER.x - HOME.x, TOWER.y - HOME.y);
      addPulses(lp, len, "green", 3, 70, false); // out to the brain
      addPulses(lp, len, "green", 2, 70, true); // and back
    }
    beam.classList.toggle("on", !!inSession);
    root.setAttribute("data-session", inSession ? "1" : "0");
    want(!!inSession || lit.length > 0);
  }

  function buildLink() {
    link = el("svg", {
      viewBox: "0 0 " + W + " " + H,
      class: "g-layer g-link",
    });
    var d = el("defs", {}, link);
    d.innerHTML =
      '<linearGradient id="g-linkgrad" gradientUnits="userSpaceOnUse" x1="' +
      HOME.x +
      '" y1="' +
      HOME.y +
      '" x2="' +
      TOWER.x +
      '" y2="' +
      TOWER.y +
      '"><stop offset="0" stop-color="' +
      C.teal +
      '" stop-opacity="1"/><stop offset="1" stop-color="' +
      C.teal +
      '" stop-opacity="0.45"/></linearGradient>' +
      '<filter id="g-soft3" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3"/></filter>';
    var a = { x1: HOME.x, y1: HOME.y, x2: TOWER.x, y2: TOWER.y };
    el(
      "line",
      Object.assign(
        {
          class: "g-linkdark",
          stroke: C.tealDim,
          "stroke-width": 1,
          "stroke-dasharray": "2 6",
        },
        a,
      ),
      link,
    );
    var on = el("g", { class: "g-linkon" }, link);
    el(
      "line",
      Object.assign(
        {
          stroke: C.teal,
          "stroke-width": 8,
          opacity: 0.35,
          filter: "url(#g-soft3)",
        },
        a,
      ),
      on,
    );
    el(
      "line",
      Object.assign({ stroke: "url(#g-linkgrad)", "stroke-width": 2.2 }, a),
      on,
    );
    return link;
  }

  /* ---------- the sky: the real sun and moon, shifted within the room's violet ---------- */
  var SKY = [
    // [sun altitude, top, middle, horizon]
    [-18, [5, 2, 11], [14, 5, 32], [30, 11, 54]],
    [-8, [8, 3, 18], [22, 7, 46], [58, 14, 72]],
    [-2, [12, 4, 26], [38, 10, 64], [124, 28, 98]],
    [6, [16, 8, 38], [36, 16, 72], [70, 28, 104]],
    [30, [20, 12, 46], [40, 22, 84], [64, 34, 116]],
  ];
  function mix(a, b, t) {
    return a.map(function (v, i) {
      return Math.round(v + (b[i] - v) * t);
    });
  }
  function skyAt(alt) {
    if (alt <= SKY[0][0]) return SKY[0].slice(1);
    for (var i = 1; i < SKY.length; i++) {
      if (alt <= SKY[i][0]) {
        var t = (alt - SKY[i - 1][0]) / (SKY[i][0] - SKY[i - 1][0]);
        return [1, 2, 3].map(function (j) {
          return mix(SKY[i - 1][j], SKY[i][j], t);
        });
      }
    }
    return SKY[SKY.length - 1].slice(1);
  }
  function moonPath(r, illum, waxing) {
    var rx = Math.abs(2 * illum - 1) * r,
      gib = illum > 0.5;
    var sweep = waxing ? [1, gib ? 1 : 0] : [0, gib ? 0 : 1];
    return (
      "M0," +
      -r +
      " A" +
      r +
      "," +
      r +
      " 0 0 " +
      sweep[0] +
      " 0," +
      r +
      " A" +
      rx +
      "," +
      r +
      " 0 0 " +
      sweep[1] +
      " 0," +
      -r +
      " Z"
    );
  }
  function sky(k) {
    if (!bg || !k) return;
    var alt = k.sun_alt,
      cols = skyAt(alt);
    ["g-sky0", "g-sky1", "g-sky2"].forEach(function (id, i) {
      $(id).setAttribute("stop-color", "rgb(" + cols[i].join(",") + ")");
    });
    $("g-stars").setAttribute(
      "opacity",
      Math.max(0, Math.min(1, (-alt - 4) / 10)).toFixed(2),
    );
    var m = k.moon;
    if (alt < -3 && m && m.illum >= 0.02) {
      $("g-moon").setAttribute("transform", "translate(104,52)");
      $("g-moonlit").setAttribute("d", moonPath(9, m.illum, m.waxing));
    } else $("g-moon").setAttribute("transform", "translate(-100,-100)");
  }

  /* ---------- the caption ---------- */
  var who = "The agent"; // status branding.assistant
  function IDLE() {
    return [
      "Idle. Walking the rounds in the dark.",
      "Nobody on the line. " + who + " checks the doors.",
      "The tower is dark. The body keeps house.",
      "Quiet grid. " + who + " walks its rounds alone.",
    ];
  }
  function SESSION() {
    return [
      "In session. The line to the far tower is lit.",
      "The thinking happens over there. The hands are here.",
      "Someone is on the line. " + who + " stands in the light.",
    ];
  }
  function list(words) {
    if (words.length < 2) return words.join("");
    return words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
  }
  function caption(s, nodes) {
    var cap = $("caption");
    cap.className = "caption";
    cap.textContent = "";
    var failed = nodes.filter(function (n) {
      return n.state === "failed";
    });
    var running = nodes.filter(function (n) {
      return n.state === "running";
    });
    var ran = nodes.filter(function (n) {
      return n.state === "ran";
    });
    var hour = new Date().getHours();
    function lead(text, problems, tone) {
      cap.classList.add(tone);
      cap.appendChild(document.createTextNode(text));
      var b = document.createElement("b");
      b.textContent = problems;
      cap.appendChild(b);
    }
    if (failed.length || s.overall === "bad") {
      var words = (s.problems || []).filter(function (p) {
        return !/^Jobs:/.test(p);
      });
      failed.forEach(function (n) {
        var named = words.some(function (w) {
          return w.toLowerCase().indexOf(n.label.toLowerCase()) === 0;
        });
        if (!named) words.push(n.label + " failed");
      });
      return lead(
        failed.length ? "A line went red. " : "The grid says look. ",
        words.slice(0, 2).join(" · "),
        "bad",
      );
    }
    if (s.overall === "warn")
      return lead(
        "Something looks off. ",
        (s.problems || []).slice(0, 2).join(" · "),
        "warn",
      );
    var busy = running.map(function (n) {
      return n.running || n.label;
    });
    var back = ran.map(function (n) {
      return n.after || n.label;
    });
    var line;
    if (s.session && busy.length)
      line = "In session. On the side, " + list(busy) + ".";
    else if (busy.length) line = "Out on the grid: " + list(busy) + ".";
    else if (s.session) line = SESSION()[hour % SESSION().length];
    else if (back.length)
      line = "Just back from " + list(back) + ". Quiet again.";
    else line = IDLE()[hour % IDLE().length];
    cap.textContent = line.charAt(0).toUpperCase() + line.slice(1);
  }

  /* ---------- the outside: screen.js calls these ---------- */
  function fit() {
    if (!root || !shown) return;
    var s = root.clientWidth / W;
    world.style.transform = "scale(" + s + ")";
    root.style.height = H * s + "px";
  }
  function init(container) {
    root = container;
    world = document.createElement("div");
    world.className = "g-world docked";
    world.appendChild(buildBg());
    // a second blur for the traces (the bg's filters are in another svg)
    traces = el("svg", {
      viewBox: "0 0 " + W + " " + H,
      class: "g-layer g-traces-layer",
    });
    el("defs", {}, traces).innerHTML =
      '<filter id="g-soft2" x="-50%" y="-50%" width="200%" height="200%">' +
      '<feGaussianBlur stdDeviation="2.6"/></filter>';
    tracesIn = el("g", {}, traces);
    world.appendChild(buildLink());
    beam = document.createElement("div");
    beam.className = "g-beam";
    beam.innerHTML = '<div class="g-beamcore"></div><div class="g-pool"></div>';
    world.appendChild(beam);
    world.appendChild(traces);
    pulses = document.createElement("div");
    pulses.className = "g-pulses";
    world.appendChild(pulses);
    world.appendChild(buildFigure());
    root.appendChild(world);
    place();
    window.addEventListener("resize", fit);
    if (window.ResizeObserver) new ResizeObserver(fit).observe(root);
  }
  function render(s) {
    if (!root) return;
    var nodes = (s && s.grid) || [];
    if (s && s.branding && s.branding.assistant) who = s.branding.assistant;
    dark = !!(s && s.dark);
    placeNodes(nodes);
    drawState(nodes, !!(s && s.session));
    caption(s || {}, nodes);
  }
  function show(on) {
    shown = !!on;
    root.hidden = !on;
    if (on) {
      fit();
      want(needHome);
    } else if (loop) {
      cancelAnimationFrame(loop);
      loop = null;
    }
  }
  window.KioskGrid = { init: init, render: render, sky: sky, show: show };
})();
