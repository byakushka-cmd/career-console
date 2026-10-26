// Note fields start as one line and grow with the text, like a messenger's input box.
// Browsers that size fields to their content in CSS (field-sizing) need nothing from here.
(function () {
  if (window.CSS && CSS.supports && CSS.supports("field-sizing", "content")) return;
  function fit(el) {
    el.style.height = "auto";
    el.style.height = el.scrollHeight + 2 + "px";  // + the top and bottom border
  }
  document.addEventListener("input", function (e) {
    if (e.target.matches && e.target.matches("textarea.note-input")) fit(e.target);
  });
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("textarea.note-input").forEach(function (el) { if (el.value) fit(el); });
  });
})();

// Dictation ([data-dictate]): speak into a field instead of typing. Shown only where the browser can recognise
// speech; the field stays a normal text field, and macOS dictation (press Fn twice) works in it too.
(function () {
  var Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-dictate-box]").forEach(function (box) { box.hidden = !Rec; });
    document.querySelectorAll("select[data-dictate-lang]").forEach(function (s) {
      try { var saved = localStorage.getItem("dictate-lang"); if (saved) s.value = saved; } catch (e) {}
      s.addEventListener("change", function () { try { localStorage.setItem("dictate-lang", s.value); } catch (e) {} });
    });
  });
  if (!Rec) return;
  var active = null;
  document.addEventListener("click", function (e) {
    var button = e.target.closest && e.target.closest("[data-dictate]");
    if (!button) return;
    e.preventDefault();
    if (active) { active.stop(); return; }
    var field = document.getElementById(button.getAttribute("data-dictate"));
    var lang = document.getElementById(button.getAttribute("data-lang"));
    var label = button.querySelector("span"), was = label.textContent;
    var rec = new Rec();
    rec.lang = (lang && lang.value) || "ru-RU";
    rec.interimResults = true;
    rec.continuous = true;
    var start = field.value ? field.value.replace(/\s*$/, "\n") : "";
    rec.onresult = function (ev) {
      var said = "";
      for (var i = 0; i < ev.results.length; i++) said += ev.results[i][0].transcript;
      field.value = start + said.trim();
      field.dispatchEvent(new Event("input", { bubbles: true }));
    };
    rec.onerror = function (ev) {
      button.title = ev.error === "not-allowed" ? "Allow the microphone for this page, then try again"
                                                : "Dictation stopped: " + ev.error;
    };
    rec.onend = function () { active = null; button.setAttribute("aria-pressed", "false"); label.textContent = was; };
    active = rec;
    button.setAttribute("aria-pressed", "true");
    label.textContent = "■ Stop";
    rec.start();
  });
})();

// Copy buttons ([data-copy]): your LinkedIn link for application forms, in one click.
(function () {
  function fallback(text) {
    var t = document.createElement("textarea");
    t.value = text;
    t.setAttribute("readonly", "");
    t.className = "sr";
    document.body.appendChild(t);
    t.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    t.remove();
    return ok;
  }
  document.addEventListener("click", function (e) {
    var button = e.target.closest && e.target.closest("[data-copy]");
    if (!button) return;
    var text = button.getAttribute("data-copy"), label = button.querySelector("span"), was = label ? label.textContent : "";
    function done(ok) {
      if (!label) return;
      label.textContent = ok ? "Copied ✓" : "Copy failed";
      setTimeout(function () { label.textContent = was; }, 1600);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(fallback(text)); });
    } else {
      done(fallback(text));
    }
  });
})();
