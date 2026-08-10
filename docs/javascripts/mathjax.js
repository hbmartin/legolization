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
  // The MathJax bundle loads after this file (it must - this file defines its
  // configuration), so on the very first emission `MathJax.startup` may not
  // exist yet; MathJax then typesets on its own at startup. Later emissions
  // (instant navigation) wait for startup before re-typesetting.
  if (!window.MathJax || !MathJax.startup) {
    return;
  }
  MathJax.startup.promise.then(() => {
    MathJax.startup.output.clearCache();
    MathJax.typesetClear();
    MathJax.texReset();
    MathJax.typesetPromise();
  });
});
