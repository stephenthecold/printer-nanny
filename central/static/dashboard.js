/*
 * Printer Nanny dashboard behaviour.
 *
 * FIRST-PARTY, NOT VENDORED. scripts/build-assets.sh regenerates tailwind.css
 * and htmx.min.js in this directory and does not touch this file -- it is
 * source, and it lives here because central/static is what the app serves.
 * Served from the image like everything else: installs sit on segmented
 * management VLANs with no outbound internet.
 *
 * It does exactly two things, both in service of the freshness strip
 * (templates/_freshness.html):
 *
 *  1. RE-DERIVES THE RELATIVE AGE from the absolute timestamp the server sent.
 *     A server-rendered "2m ago" is true for one instant and a lie for as long
 *     as the tab stays open -- which, on a NOC screen, is days. Recomputing in
 *     the browser means that when polling stops for ANY reason (tab hidden,
 *     network gone, server down, auto-refresh switched off) the age keeps
 *     climbing rather than freezing on a comforting number. That is the honest
 *     failure mode, and it is the whole point of the exercise.
 *
 *  2. ANNOUNCES STATE TRANSITIONS, ONCE. The strip re-renders on a timer, so
 *     it is deliberately not an aria-live region: a live region on a timer is
 *     a screen reader reciting the clock every minute forever. Instead the
 *     coarse state (live / lagging / stale / none) is compared across renders
 *     and a single sentence is written into a stable live region only when it
 *     actually changed. No change, no DOM mutation, no announcement.
 *
 * Degrades cleanly: with JavaScript off, the strip still reports the true age
 * of the data at the moment the page was served, and htmx (which needs JS too)
 * is equally absent, so nothing claims to be live that isn't.
 */
(function () {
  "use strict";

  var TICK_MS = 15000;
  /* Difference between this browser's clock and the server's, measured from the
   * server's own "now" on every render. Without it a workstation whose clock is
   * ten minutes slow reports every reading as ten minutes fresher than it is --
   * the exact direction of error this feature exists to eliminate. */
  var skewMs = 0;
  var lastState = null;

  /* Mirror of humanize_age() in central/freshness.py. The two must agree, or
   * the label visibly jumps the first time this takes over from the server. */
  function pnAge(seconds) {
    var total = Math.floor(Math.max(0, seconds));
    if (total < 60) return total + "s";
    if (total < 3600) return Math.floor(total / 60) + "m";
    if (total < 86400) {
      var hours = Math.floor(total / 3600);
      var minutes = Math.floor((total % 3600) / 60);
      return minutes === 0 ? hours + "h" : hours + "h " + minutes + "m";
    }
    var days = Math.floor(total / 86400);
    var rem = Math.floor((total % 86400) / 3600);
    return rem === 0 ? days + "d" : days + "d " + rem + "h";
  }

  function strip() {
    return document.getElementById("pn-freshness");
  }

  function resync() {
    var el = strip();
    if (!el) return;
    var parsed = Date.parse(el.getAttribute("data-pn-now") || "");
    if (!isNaN(parsed)) skewMs = Date.now() - parsed;
  }

  function tick() {
    var nodes = document.querySelectorAll("[data-pn-age]");
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var at = Date.parse(el.getAttribute("data-pn-age") || "");
      if (isNaN(at)) continue;
      el.textContent = pnAge((Date.now() - skewMs - at) / 1000) + " ago";
    }
  }

  /* Announce only a change. On the first render there is nothing to announce:
   * the strip is already part of the page a screen reader is about to read. */
  function announce() {
    var el = strip();
    var live = document.getElementById("pn-freshness-live");
    if (!el || !live) return;
    var state = el.getAttribute("data-pn-state");
    if (state === lastState) return;
    var first = lastState === null;
    lastState = state;
    if (first) return;
    live.textContent = el.getAttribute("data-pn-announce") || "";
  }

  function refresh() {
    resync();
    tick();
    announce();
  }

  refresh();
  setInterval(tick, TICK_MS);
  /* htmx events bubble, so one listener on the document covers the strip being
   * replaced by its own poll and by the "Check now" button alike. */
  document.addEventListener("htmx:afterSwap", refresh);
})();
