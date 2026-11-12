/* Haqq motion: smooth scroll (Lenis), scroll reveals (GSAP + ScrollTrigger), the hero
   drawing in, and small helpers the tool uses (stamps, case-file rows, counters).
   Every helper also works without GSAP, and nothing moves under prefers-reduced-motion. */
(function () {
  "use strict";
  var root = document.documentElement;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var finePointer = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  var gsap = window.gsap;
  var ST = window.ScrollTrigger;
  var animate = !reduce && !!gsap;
  var M = (window.HaqqMotion = { reduce: reduce, animate: animate, lenis: null });

  function $$(sel, scope) { return Array.prototype.slice.call((scope || document).querySelectorAll(sel)); }
  function headerOffset() { return -(parseFloat(getComputedStyle(root).getPropertyValue("--header-h")) || 64) - 12; }

  /* Icons: team cards get real copies of the sprite symbols so each stroke can be drawn. */
  $$("svg[data-icon]").forEach(function (svg) {
    var sym = document.getElementById("i-" + svg.getAttribute("data-icon"));
    if (!sym) return;
    svg.setAttribute("viewBox", sym.getAttribute("viewBox"));
    svg.innerHTML = sym.innerHTML;
  });

  /* Headlines split into words, each revealed from behind its own mask. */
  $$("[data-split]").forEach(function (h) {
    var words = h.textContent.trim().split(/\s+/);
    h.setAttribute("aria-label", h.textContent.trim());
    h.innerHTML = words.map(function (w) {
      return '<span class="w" aria-hidden="true"><span class="wi">' + w.replace(/[&<>]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
      }) + "</span></span>";
    }).join(" ");
  });

  /* ---------------------------------------------------------------- helpers */
  M.scrollTo = function (target, opts) {
    var el = typeof target === "string" ? document.querySelector(target) : target;
    if (!el) return;
    if (M.lenis) M.lenis.scrollTo(el, Object.assign({ offset: headerOffset(), duration: 1.3 }, opts || {}));
    else {
      var y = el.getBoundingClientRect().top + window.scrollY + headerOffset();
      window.scrollTo({ top: y, behavior: reduce ? "auto" : "smooth" });
    }
  };

  M.count = function (el, to, opts) {
    opts = opts || {};
    var decimals = opts.decimals || 0, prefix = opts.prefix || "", suffix = opts.suffix || "";
    var from = opts.from != null ? opts.from : 0, dur = opts.duration != null ? opts.duration : 1.4;
    function fmt(v) {
      return prefix + v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + suffix;
    }
    if (reduce || dur === 0) { el.textContent = fmt(to); return; }
    var start = performance.now();
    function tick(now) {
      var p = Math.min(1, (now - start) / (dur * 1000));
      var e = 1 - Math.pow(1 - p, 4);
      el.textContent = fmt(from + (to - from) * e);
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  };

  M.rowIn = function (el) {
    if (!animate) return;
    gsap.fromTo(el, { x: 22, opacity: 0 }, { x: 0, opacity: 1, duration: 0.55, ease: "power3.out", clearProps: "transform" });
  };

  M.reveal = function (els, stagger) {
    if (!animate) return;
    gsap.fromTo(els, { y: 30, opacity: 0 }, { y: 0, opacity: 1, duration: 0.9, ease: "power3.out", stagger: stagger || 0.08, clearProps: "transform" });
  };

  /* A rubber stamp coming down onto paper, and the paper taking the hit. */
  M.stamp = function (el, rotate) {
    var rot = rotate != null ? rotate : -6;
    if (!animate) { el.style.opacity = ""; return; }
    var host = el.closest(".verdict, .sheet, .flag") || el.parentElement;
    gsap.timeline()
      .fromTo(el, { scale: 2.4, rotate: rot - 12, opacity: 0 }, { scale: 1, rotate: rot, opacity: 0.9, duration: 0.38, ease: "power4.in" })
      .fromTo(host, { y: 0 }, { y: 3, duration: 0.06, yoyo: true, repeat: 1, ease: "power1.out" }, ">-0.02");
  };

  M.drawIcon = function (svg, delay) {
    if (!animate || !svg) return;
    var parts = $$("path, circle, rect", svg);
    parts.forEach(function (p) {
      var len = p.getTotalLength ? p.getTotalLength() : 200;
      p.style.strokeDasharray = len;
      p.style.strokeDashoffset = len;
    });
    gsap.to(parts, { strokeDashoffset: 0, duration: 1.4, ease: "power2.inOut", stagger: 0.07, delay: delay || 0 });
  };

  M.refresh = function () { if (ST) ST.refresh(); };

  /* Anchor links glide instead of jumping. */
  document.addEventListener("click", function (e) {
    var a = e.target.closest('a[href^="#"]');
    if (!a) return;
    var id = a.getAttribute("href");
    if (id.length < 2) return;
    var target = document.querySelector(id);
    if (!target) return;
    e.preventDefault();
    M.scrollTo(id === "#top" ? document.body : target);
    if (history.replaceState) history.replaceState(null, "", id);
  });

  /* Masthead: hairline once scrolled, hides on the way down, back on the way up; progress rule. */
  var masthead = document.querySelector(".masthead");
  var bar = document.querySelector(".progress span");
  var lastY = 0;
  function onScroll(y) {
    masthead.classList.toggle("is-scrolled", y > 8);
    var goingDown = y > lastY + 2, goingUp = y < lastY - 2;
    if (goingDown && y > 520) masthead.classList.add("is-hidden");
    else if (goingUp || y < 520) masthead.classList.remove("is-hidden");
    lastY = y;
    var max = document.documentElement.scrollHeight - window.innerHeight;
    if (bar) bar.style.transform = "scaleX(" + (max > 0 ? Math.min(1, y / max) : 0) + ")";
  }
  window.addEventListener("scroll", function () { onScroll(window.scrollY); }, { passive: true });
  masthead.addEventListener("focusin", function () { masthead.classList.remove("is-hidden"); });

  /* Which § is on screen. */
  var navLinks = $$("[data-nav]");
  if ("IntersectionObserver" in window && navLinks.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        navLinks.forEach(function (l) { l.classList.toggle("is-active", l.getAttribute("data-nav") === en.target.id); });
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    ["problem", "team", "check", "honest"].forEach(function (id) { var s = document.getElementById(id); if (s) io.observe(s); });
  }

  /* The hero mark: once its font is in, the drawing's frame is fitted to the word. */
  var fontReady = document.fonts && document.fonts.load
    ? Promise.race([document.fonts.load('700 250px "Amiri"', "حق"), new Promise(function (r) { setTimeout(r, 1500); })])
    : Promise.resolve();
  function fitMark() {
    var svg = document.querySelector(".mark");
    var text = svg && svg.querySelector(".mark-fill");
    if (!text) return;
    try {
      // The ink's own bounds (Arabic fonts carry tall line metrics, so getBBox is far too loose).
      var x = parseFloat(text.getAttribute("x")), y = parseFloat(text.getAttribute("y"));
      var ctx = document.createElement("canvas").getContext("2d");
      ctx.font = '700 250px "Amiri"';
      ctx.textAlign = "center";
      ctx.direction = "rtl";
      var m = ctx.measureText(text.textContent);
      if (m.actualBoundingBoxAscent) {
        var pad = 8;
        var x0 = x - m.actualBoundingBoxLeft - pad, y0 = y - m.actualBoundingBoxAscent - pad;
        var w = m.actualBoundingBoxLeft + m.actualBoundingBoxRight + 2 * pad;
        var h = m.actualBoundingBoxAscent + m.actualBoundingBoxDescent + 2 * pad;
        svg.setAttribute("viewBox", [x0, y0, w, h].map(Math.round).join(" "));
      }
    } catch (e) { /* keep the default frame */ }
  }
  var markReady = fontReady.then(fitMark, fitMark);

  if (!animate) {
    root.classList.remove("js-motion");
    root.classList.add("motion-ready");
    return;
  }

  /* -------------------------------------------------------------- the motion */
  gsap.registerPlugin(ST);
  gsap.defaults({ ease: "power3.out" });

  if (window.Lenis) {
    var lenis = new window.Lenis({ duration: 1.15, easing: function (t) { return Math.min(1, 1.001 - Math.pow(2, -10 * t)); }, smoothWheel: true });
    M.lenis = lenis;
    root.classList.add("lenis");
    lenis.on("scroll", function (e) { ST.update(); onScroll(e.scroll); });
    gsap.ticker.add(function (time) { lenis.raf(time * 1000); });
    gsap.ticker.lagSmoothing(0);
  }

  /* Hero: the mark is traced, then inked; the headline rises line by line; the promise follows. */
  function hero() {
    gsap.set(".hero-title .line-in", { yPercent: 108, y: 0 });
    var tl = gsap.timeline({ defaults: { ease: "expo.out" } });
    tl.to(".mark-stroke", { strokeDashoffset: 0, duration: 2.2, ease: "power2.inOut" }, 0)
      .to(".hero-title .line-in", { y: 0, yPercent: 0, duration: 1.25, stagger: 0.13 }, 0.15)
      .to(".underline path", { strokeDashoffset: 0, duration: 0.95, ease: "power2.inOut" }, 0.95)
      .to("[data-hero-fade]", { opacity: 1, y: 0, duration: 1, stagger: 0.07 }, 0.6)
      .to(".mark-fill", { opacity: 1, duration: 1.1, ease: "power2.out" }, 1.75)
      .to(".mark-stroke", { opacity: 0.0, duration: 1.2, ease: "power2.out" }, 2.1)
      .add(function () {
        $$(".ledger .num[data-count]").forEach(function (el) {
          M.count(el, parseFloat(el.getAttribute("data-count")) || 0, { duration: 1.6 });
        });
      }, 0.9);
    root.classList.add("motion-ready");
  }
  markReady.then(hero, hero);

  /* Section headlines: word by word. */
  $$("[data-split]").forEach(function (h) {
    gsap.fromTo(h.querySelectorAll(".wi"), { yPercent: 110 }, {
      yPercent: 0, duration: 1.1, ease: "expo.out", stagger: 0.045,
      scrollTrigger: { trigger: h, start: "top 86%", once: true },
    });
  });

  /* Everything else marked data-reveal rises in as it enters. */
  ST.batch("[data-reveal]", {
    start: "top 88%", once: true,
    onEnter: function (batch) { gsap.to(batch, { opacity: 1, y: 0, duration: 1, stagger: 0.1, overwrite: true }); },
  });

  /* §1: the numerals count, the cap bands step up. */
  $$(".rule .big[data-count]").forEach(function (el) {
    ST.create({ trigger: el, start: "top 85%", once: true, onEnter: function () {
      M.count(el, parseFloat(el.getAttribute("data-count")), { duration: 1.8 });
    } });
  });
  gsap.to(".bands-step", { strokeDashoffset: 0, duration: 1.8, ease: "power2.inOut",
    scrollTrigger: { trigger: ".bands", start: "top 85%", once: true } });
  gsap.from(".bands-labels text", { opacity: 0, y: 6, duration: 0.6, stagger: 0.06, delay: 0.5,
    scrollTrigger: { trigger: ".bands", start: "top 85%", once: true } });

  /* §2: the team slides past while the page is held still (desktop); icons draw as each arrives. */
  var mm = gsap.matchMedia();
  mm.add("(min-width: 961px)", function () {
    var track = document.querySelector(".team-track");
    var stage = document.querySelector(".team-stage");
    var bar = document.querySelector(".team-progress i");
    function distance() { return Math.max(0, track.scrollWidth - document.documentElement.clientWidth); }
    var slide = gsap.to(track, {
      x: function () { return -distance(); }, ease: "none",
      scrollTrigger: { trigger: stage, start: "center 52%", end: function () { return "+=" + distance(); },
        pin: true, scrub: 0.7, invalidateOnRefresh: true, anticipatePin: 1,
        onUpdate: function (self) { if (bar) bar.style.transform = "scaleX(" + self.progress.toFixed(3) + ")"; } },
    });
    $$(".agent-card").forEach(function (card, i) {
      var icon = card.querySelector(".agent-icon");
      ST.create({
        trigger: card, containerAnimation: i < 3 ? null : slide, start: i < 3 ? "top 80%" : "left 88%", once: true,
        onEnter: function () { M.drawIcon(icon, i < 3 ? i * 0.15 : 0); },
      });
    });
  });
  mm.add("(max-width: 960px)", function () {
    var viewport = document.querySelector(".team-viewport");
    var bar = document.querySelector(".team-progress i");
    function onSwipe() {
      var max = viewport.scrollWidth - viewport.clientWidth;
      if (bar) bar.style.transform = "scaleX(" + (max > 0 ? viewport.scrollLeft / max : 0).toFixed(3) + ")";
    }
    viewport.addEventListener("scroll", onSwipe, { passive: true });
    ST.create({ trigger: ".team-track", start: "top 85%", once: true, onEnter: function () {
      $$(".agent-icon").forEach(function (icon, i) { M.drawIcon(icon, Math.min(i, 3) * 0.12); });
    } });
  });

  /* Big honest figures settle in. */
  $$(".p-figure").forEach(function (el) {
    gsap.from(el, { yPercent: 30, opacity: 0, duration: 1.2, ease: "expo.out", scrollTrigger: { trigger: el, start: "top 88%", once: true } });
  });

  /* The footer mark is traced as you reach the end. */
  gsap.from(".colophon-mark", { yPercent: 18, opacity: 0, duration: 1.4, ease: "expo.out",
    scrollTrigger: { trigger: ".colophon", start: "top 85%", once: true } });

  /* Buttons that lean towards the pointer and spring back. */
  if (finePointer) {
    $$(".magnetic").forEach(function (btn) {
      var xTo = gsap.quickTo(btn, "x", { duration: 0.45, ease: "power3.out" });
      var yTo = gsap.quickTo(btn, "y", { duration: 0.45, ease: "power3.out" });
      btn.addEventListener("pointermove", function (e) {
        if (btn.disabled) return;
        var r = btn.getBoundingClientRect();
        xTo((e.clientX - (r.left + r.width / 2)) * 0.16);
        yTo((e.clientY - (r.top + r.height / 2)) * 0.26);
      });
      btn.addEventListener("pointerleave", function () {
        gsap.to(btn, { x: 0, y: 0, duration: 1, ease: "elastic.out(1, 0.32)" });
      });
    });
  }

  window.addEventListener("load", function () { ST.refresh(); });
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { ST.refresh(); });
})();
