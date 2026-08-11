/*
 * Printer Nanny dashboard behaviour.
 *
 * FIRST-PARTY, NOT VENDORED. scripts/build-assets.sh regenerates tailwind.css
 * and htmx.min.js in this directory and does not touch this file -- it is
 * source, and it lives here because central/static is what the app serves.
 * Served from the image like everything else: installs sit on segmented
 * management VLANs with no outbound internet.
 *
 * It keeps the freshness strip honest and supplies progressive enhancements
 * for the responsive navigation drawer.
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

  /* The navigation drawer remains pointer-operable when this file does not
   * load. JavaScript adds keyboard activation, focus containment, Escape, and
   * background inertness. The checkbox remains the source of truth. */
  function initNavigation() {
    var control = document.getElementById("pn-nav-toggle");
    var navigation = document.getElementById("primary-navigation");
    var page = document.getElementById("pn-page");
    var mobileHeader = document.getElementById("pn-mobile-header");
    var mobileMenu = document.getElementById("pn-mobile-menu");
    if (!control) return;

    function isDesktop() {
      return window.matchMedia("(min-width: 1024px)").matches;
    }

    function sync() {
      var open = control.checked && !isDesktop();
      if (mobileMenu) mobileMenu.setAttribute("aria-expanded", open ? "true" : "false");
      document.body.classList.toggle("overflow-hidden", open);
      var backgroundNodes = [page, mobileHeader, mobileMenu];
      for (var i = 0; i < backgroundNodes.length; i++) {
        var node = backgroundNodes[i];
        if (!node) continue;
        if (open) {
          node.setAttribute("inert", "");
          node.setAttribute("aria-hidden", "true");
        } else {
          node.removeAttribute("inert");
          node.removeAttribute("aria-hidden");
        }
      }
    }

    function focusables() {
      if (!navigation) return [];
      return Array.prototype.slice.call(navigation.querySelectorAll(
        'a[href], summary, input:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter(function (node) {
        return node.getClientRects().length > 0;
      });
    }

    control.addEventListener("change", function () {
      sync();
      if (control.checked && !isDesktop()) {
        var nodes = focusables();
        var current = navigation && navigation.querySelector('[aria-current="page"]');
        if (current) current.focus();
        else if (nodes.length) nodes[0].focus();
      } else if (navigation && navigation.contains(document.activeElement)) {
        if (mobileMenu) mobileMenu.focus();
      }
    });
    var toggles = document.querySelectorAll("[data-pn-nav-toggle][tabindex]");
    for (var j = 0; j < toggles.length; j++) {
      toggles[j].addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        this.click();
      });
    }
    document.addEventListener("keydown", function (event) {
      if (!control.checked || isDesktop()) return;
      if (event.key === "Escape") {
        control.checked = false;
        sync();
        if (mobileMenu) mobileMenu.focus();
        return;
      }
      if (event.key !== "Tab") return;
      var nodes = focusables();
      if (!nodes.length) return;
      var first = nodes[0];
      var last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    window.addEventListener("resize", function () {
      if (isDesktop() && control.checked) control.checked = false;
      sync();
    });
    sync();
  }

  /* Fill manual issue time in the selected client's timezone. Docker normally
   * runs in UTC and the technician's browser may not share the client's zone;
   * using either clock silently records the wrong occurrence time. */
  function initIssueTime() {
    var printer = document.querySelector(".js-issue-printer");
    var input = document.querySelector("[data-issue-time]");
    if (!printer || !input) return;

    function two(value) { return String(value).padStart(2, "0"); }
    function nowIn(zone) {
      try {
        var parts = new Intl.DateTimeFormat("en-CA", {
          timeZone: zone || "UTC", year: "numeric", month: "2-digit",
          day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
        }).formatToParts(new Date());
        var values = {};
        for (var i = 0; i < parts.length; i++) values[parts[i].type] = parts[i].value;
        return values.year + "-" + values.month + "-" + values.day + "T" +
          values.hour + ":" + values.minute;
      } catch (_error) {
        var local = new Date();
        return local.getFullYear() + "-" + two(local.getMonth() + 1) + "-" +
          two(local.getDate()) + "T" + two(local.getHours()) + ":" +
          two(local.getMinutes());
      }
    }
    function fill() {
      if (input.dataset.autoValue !== "true") return;
      var option = printer.options[printer.selectedIndex];
      if (!option || !option.dataset.timezone) return;
      input.value = nowIn(option.dataset.timezone);
    }
    input.addEventListener("input", function () { input.dataset.autoValue = "false"; });
    printer.addEventListener("change", fill);
    fill();
  }

  refresh();
  initNavigation();
  initIssueTime();
  setInterval(tick, TICK_MS);
  /* htmx events bubble, so one listener on the document covers the strip being
   * replaced by its own poll and by the "Check now" button alike. */
  document.addEventListener("htmx:afterSwap", refresh);
})();
