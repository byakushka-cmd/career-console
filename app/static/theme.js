// Theme switch: every tap flips what you see (dark <-> light). Picking the same theme as the
// system clears the stored choice, so the page follows the system again. Stored only in this browser.
(function () {
  var KEY = "cc-theme", root = document.documentElement;
  var systemLight = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;
  function read() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function write(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) {} }
  function apply(v) { if (v === "dark" || v === "light") root.dataset.theme = v; else delete root.dataset.theme; }
  function system() { return systemLight && systemLight.matches ? "light" : "dark"; }
  function effective() { return root.dataset.theme || system(); }
  apply(read());  // runs in <head>, before first paint: no flash

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.getElementById("theme-toggle");
    if (!button) return;
    var NAME = { dark: "Dark", light: "Light" };
    function sync() {
      var now = effective(), other = now === "dark" ? "light" : "dark";
      button.dataset.current = now;
      button.querySelector("span").textContent = NAME[now];
      button.setAttribute("aria-label", "Theme: " + NAME[now] + (root.dataset.theme ? "" : " (system)") +
        ". Switch to " + NAME[other]);
    }
    button.hidden = false;
    sync();
    button.addEventListener("click", function () {
      var next = effective() === "dark" ? "light" : "dark";
      if (next === system()) { apply(null); write(null); } else { apply(next); write(next); }
      sync();
    });
    if (systemLight && systemLight.addEventListener) systemLight.addEventListener("change", sync);
  });
})();
