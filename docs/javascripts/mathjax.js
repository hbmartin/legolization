// MathJax bootstrap for pymdownx.arithmatex in generic mode.
//
// arithmatex wraps math in `\(...\)` / `\[...\]` inside elements carrying the
// `arithmatex` class; MathJax is configured below to pick exactly those up.
// The `document$` subscription re-typesets after instant navigation swaps the
// page content without a full reload.

window.MathJax = {
  tex: {
    inlineMath: [["\\(", "\\)"]],
    displayMath: [["\\[", "\\]"]],
    processEscapes: true,
    processEnvironments: true,
  },
  options: {
    ignoreHtmlClass: ".*|",
    processHtmlClass: "arithmatex",
  },
};

document$.subscribe(() => {
  MathJax.startup.output.clearCache();
  MathJax.typesetClear();
  MathJax.texReset();
  MathJax.typesetPromise();
});
