// Mooxr website: theme picker, mobile menu, scroll reveal and the hero demo.
(function () {
  "use strict";
  var root = document.documentElement;
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function save(key, value) {
    try { localStorage.setItem(key, value); } catch (e) {}
  }

  // ---------- Theme and mode (mirrors Settings → Appearance) ----------
  function isDark() {
    var mode = root.dataset.mode;
    if (mode === "system") return !window.matchMedia("(prefers-color-scheme: light)").matches;
    return mode !== "light";
  }

  function refreshControls() {
    document.querySelectorAll("[data-theme-btn]").forEach(function (b) {
      b.setAttribute("aria-checked", String(b.dataset.themeBtn === root.dataset.theme));
    });
    document.querySelectorAll("[data-mode-btn]").forEach(function (b) {
      b.setAttribute("aria-checked", String(b.dataset.modeBtn === root.dataset.mode));
    });
    // Keep the browser's UI colour in step with the page.
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = getComputedStyle(document.body).backgroundColor;
  }

  function setTheme(key) {
    root.dataset.theme = key;
    save("mooxr-site-theme", key);
    refreshControls();
  }

  function setMode(mode) {
    root.dataset.mode = mode;
    save("mooxr-site-mode", mode);
    refreshControls();
  }

  document.querySelectorAll("[data-theme-btn]").forEach(function (b) {
    b.addEventListener("click", function () { setTheme(b.dataset.themeBtn); });
  });
  document.querySelectorAll("[data-mode-btn]").forEach(function (b) {
    b.addEventListener("click", function () { setMode(b.dataset.modeBtn); });
  });
  document.getElementById("mode-toggle").addEventListener("click", function () {
    setMode(isDark() ? "light" : "dark");
  });
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", refreshControls);
  // Wait a frame so the colour transition has a value to read.
  requestAnimationFrame(refreshControls);

  // ---------- Nav ----------
  var nav = document.querySelector(".nav");
  var links = document.getElementById("nav-links");
  var menuBtn = document.getElementById("menu-btn");

  menuBtn.addEventListener("click", function () {
    var open = links.classList.toggle("open");
    menuBtn.setAttribute("aria-expanded", String(open));
  });
  links.addEventListener("click", function (e) {
    if (e.target.closest("a")) {
      links.classList.remove("open");
      menuBtn.setAttribute("aria-expanded", "false");
    }
  });
  function onScroll() { nav.classList.toggle("scrolled", window.scrollY > 8); }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // ---------- Scroll reveal ----------
  var revealed = document.querySelectorAll(".reveal");
  if ("IntersectionObserver" in window && !reduceMotion) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    revealed.forEach(function (el, i) {
      el.style.transitionDelay = (i % 3) * 70 + "ms";
      io.observe(el);
    });
  } else {
    revealed.forEach(function (el) { el.classList.add("in"); });
  }

  var year = document.getElementById("year");
  if (year) year.textContent = new Date().getFullYear();

  // ---------- Hero demo: three rows moox one after another ----------
  var win = document.querySelector(".window");
  if (!win) return;  // pages without the demo, like the privacy policy
  var rows = Array.prototype.slice.call(document.querySelectorAll("[data-row]"));
  var label = document.getElementById("moox-label");
  var stages = [
    [0.00, "Fetching…"],
    [0.25, "Converting…"],
    [0.75, "Adding cover art…"],
    [0.92, "Tagging…"]
  ];

  function finish(row) {
    row.classList.add("done");
    row.querySelector(".bar span").style.width = "100%";
    row.querySelector(".status").textContent = "Done · in Spotify";
  }

  function reset() {
    rows.forEach(function (row) {
      row.classList.remove("done");
      row.querySelector(".bar span").style.width = "0";
      row.querySelector(".status").textContent = "Waiting";
    });
    label.textContent = "Moox all 3";
    win.classList.remove("busy");
  }

  if (reduceMotion) {
    rows.forEach(finish);
    label.textContent = "Moox";
    return;
  }

  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  function runRow(row) {
    var bar = row.querySelector(".bar span");
    var status = row.querySelector(".status");
    var start = performance.now();
    var duration = 2600 + Math.random() * 900;
    return new Promise(function (resolve) {
      function tick(now) {
        var f = Math.min((now - start) / duration, 1);
        bar.style.width = (f * 100).toFixed(1) + "%";
        for (var i = stages.length - 1; i >= 0; i--) {
          if (f >= stages[i][0]) { status.textContent = stages[i][1]; break; }
        }
        if (f < 1) requestAnimationFrame(tick);
        else { finish(row); resolve(); }
      }
      requestAnimationFrame(tick);
    });
  }

  async function loop() {
    for (;;) {
      reset();
      await wait(900);
      win.classList.add("hot");          // a file being dragged over the drop zone
      await wait(900);
      win.classList.remove("hot");
      await wait(400);
      win.classList.add("busy");
      label.textContent = "Mooxing…";
      for (var i = 0; i < rows.length; i++) await runRow(rows[i]);
      win.classList.remove("busy");
      label.textContent = "Moox";
      await wait(3200);
      win.classList.add("resetting");
      await wait(450);
      win.classList.remove("resetting");
    }
  }

  // Only animate while the hero is on screen.
  var started = false;
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting && !started) { started = true; loop(); }
    }).observe(win);
  } else {
    loop();
  }
})();
