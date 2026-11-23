/* The kiosk: the pure sizing rules for the kiosk's lists (no DOM here, so node can test them:
   tests/test_fit.py). Loaded before screen.js; exposes window.KioskFit (module.exports under node).

   A list that moves for a few pixels is worse than one that does not move at all. So: layout first, motion last.
   - Boxes are sized from their content and always end on a row boundary (never a half-cut row).
   - THE THRESHOLD: a list may move only when at least ONE WHOLE ROW of it cannot be shown after the layout has
     done its best. A few pixels, part of the last row, or padding never starts motion: the box is given those
     pixels instead ("absorb"). */
(function (root) {
  "use strict";
  var TOL = 1; // px of rounding slack

  /* hidden = list height minus box height; rowPx = the height of the first row that does not fit.
     "fits": nothing hidden. "absorb": less than one whole row hidden -> give the box the pixels, no motion.
     "scroll": at least one whole row hidden -> the box shows whole rows and the list moves. */
  function decide(hidden, rowPx) {
    if (!(hidden > TOL)) return "fits";
    if (hidden < rowPx - TOL) return "absorb";
    return "scroll";
  }

  /* how many whole rows fit in `avail` px; bottoms = each row's bottom edge from the top of the list, ascending */
  function rowsThatFit(bottoms, avail) {
    var k = 0;
    while (k < bottoms.length && bottoms[k] <= avail + TOL) k++;
    return k;
  }

  function heightOf(bottoms, k) {
    return k > 0 ? bottoms[Math.min(k, bottoms.length) - 1] : 0;
  }

  /* One box: the list's row bottoms and the space the layout offers -> {mode, height} with height on a row
     boundary ("absorb" gives the whole list its height, a little past `avail`). */
  function fitBox(bottoms, avail) {
    var natural = heightOf(bottoms, bottoms.length);
    var k = rowsThatFit(bottoms, avail);
    var rowPx = k < bottoms.length ? bottoms[k] - heightOf(bottoms, k) : 0;
    var mode = decide(natural - avail, rowPx);
    if (mode !== "scroll")
      return { mode: mode, height: natural, rows: bottoms.length };
    k = Math.max(1, k);
    return { mode: mode, height: heightOf(bottoms, k), rows: k };
  }

  /* The work-hours left column: the task list on top, In flight under it (waiting on the left; running and just
     finished stacked on the right).
     The task list comes first (In flight scrolls, and nothing is missed as long as its rows keep coming into view): it gets its whole height as long as In flight
     keeps its minimum (FLIGHT_MIN_ROWS waiting rows beside one running and one just finished row); a few pixels
     short is absorbed by tightening the gaps between task rows (at most o.tGapSlack px each). Only when the tasks
     still cannot all be shown does the task list scroll (the last resort), in whole rows.
     In flight takes everything under the task list. Each of its groups gets whole rows and scrolls when rows of it
     do not fit (waiting in its column; running takes its rows first but leaves at least one row to just finished,
     which takes the rest). Nothing is cut: a list that does not fit moves.
     o = {budget, t, w, r, d (row bottoms), labW, labR, labD, colGap, tGapSlack} ->
     {t, w, r, d (rows shown), tHeight, gapCut, used, fits (every row of every list shown, nothing moves), flightMin}. */
  var FLIGHT_MIN_ROWS = 3;
  function allocate(o) {
    o = Object.assign({}, o);
    function col2(r, d) {
      return o.labR + heightOf(o.r, r) + o.colGap + o.labD + heightOf(o.d, d);
    }
    var flightMin = Math.max(o.labW + heightOf(o.w, Math.min(FLIGHT_MIN_ROWS, o.w.length)),
                             col2(Math.min(1, o.r.length), Math.min(1, o.d.length)));
    var tAvail = o.budget - flightMin;
    var n = o.t.length,
      gapCut = 0,
      short = heightOf(o.t, n) - tAvail;
    if (short > TOL && n > 1 && o.tGapSlack > 0 && short <= (n - 1) * o.tGapSlack + TOL) {
      gapCut = Math.min(o.tGapSlack, Math.ceil((short - TOL) / (n - 1)));
      o.t = o.t.map(function (b, i) {
        return b - i * gapCut;
      });
    }
    var k = { t: n ? Math.max(1, rowsThatFit(o.t, tAvail)) : 0 };
    var row2 = o.budget - heightOf(o.t, k.t);
    k.w = o.w.length ? Math.max(1, rowsThatFit(o.w, row2 - o.labW)) : 0;
    // running: its rows first, but one row of just finished stays in view under it
    var dMin = Math.min(1, o.d.length);
    k.r = o.r.length ? Math.max(1, rowsThatFit(o.r, row2 - col2(0, dMin))) : 0;
    k.d = o.d.length ? Math.max(1, rowsThatFit(o.d, row2 - col2(k.r, 0))) : 0;
    var used = heightOf(o.t, k.t) + Math.max(o.labW + heightOf(o.w, k.w), col2(k.r, k.d));
    var all = k.t === n && k.w === o.w.length && k.r === o.r.length && k.d === o.d.length;
    return { t: k.t, w: k.w, r: k.r, d: k.d, used: used, fits: all && used <= o.budget + TOL, gapCut: gapCut,
             tHeight: heightOf(o.t, k.t), flightMin: flightMin };
  }

  /* Where a moving list stops: the top of each row it can show at the top of the box, then the bottom. */
  function rowStops(tops, max) {
    var stops = [0];
    for (var i = 0; i < tops.length; i++)
      if (tops[i] > stops[stops.length - 1] + TOL && tops[i] < max - TOL)
        stops.push(tops[i]);
    if (max > TOL) stops.push(max);
    return stops;
  }

  var api = {
    decide: decide,
    rowsThatFit: rowsThatFit,
    heightOf: heightOf,
    fitBox: fitBox,
    allocate: allocate,
    rowStops: rowStops,
  };
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.KioskFit = api;
})(this);
