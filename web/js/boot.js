/* Runs before first paint: mark the page as animated (so the hero starts hidden instead of
   flashing in) unless the visitor asked for reduced motion. If the animation code never
   arrives (a blocked CDN), everything is shown again after a moment. */
(function () {
  var root = document.documentElement;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) return;
  root.classList.add("js-motion");
  window.setTimeout(function () {
    if (!root.classList.contains("motion-ready")) root.classList.remove("js-motion");
  }, 4000);
})();
