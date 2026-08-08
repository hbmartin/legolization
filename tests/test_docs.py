"""Guard the documentation site against silent rot.

The three reader tracks under ``docs/`` cross-link heavily, and the navigation
in ``zensical.toml`` names every page explicitly. Both break invisibly: a
renamed page still renders, it just stops being reachable, and a stale relative
link only shows up when a reader follows it.

These checks are structural only — they never build the site, hit the network,
or judge prose.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
CONFIG_PATH = REPO_ROOT / "zensical.toml"

# ``docs/README.md`` is deliberately outside the navigation: GitHub renders it
# when browsing the directory, while the site's landing page is ``index.md``.
NAV_EXEMPT: frozenset[str] = frozenset({"README.md"})

# README.md is a landing page that routes into the docs site. It regrows into a
# second manual unless something objects; the budget is that objection.
README_MAX_LINES = 200

# Matches ``[text](target)`` while skipping images (``![alt](src)``).
_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")

# Fenced code blocks hold example links that are not navigable targets.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _load_nav() -> list[Any]:
    """Return the raw ``nav`` list from the site configuration."""
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return config["project"]["nav"]


def _nav_targets(entries: list[Any]) -> list[str]:
    """Flatten the nested ``nav`` structure into its leaf page paths."""
    targets: list[str] = []
    pending: list[Any] = list(entries)
    while pending:
        match pending.pop():
            case str() as target:
                targets.append(target)
            case list() as nested:
                pending.extend(nested)
            case dict() as mapping:
                pending.extend(mapping.values())
            case other:  # pragma: no cover - configuration typo
                msg = f"unsupported nav entry: {other!r}"
                raise TypeError(msg)
    return targets


def _markdown_pages() -> list[Path]:
    """Return every Markdown page under ``docs/``, sorted for stable output."""
    return sorted(DOCS_DIR.rglob("*.md"))


def _strip_code_fences(text: str) -> str:
    """Drop fenced code blocks so example links are not treated as targets."""
    kept: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            kept.append(line)
    return "\n".join(kept)


def _relative_links(page: Path) -> list[str]:
    """Return the relative link targets a page points at, anchors removed."""
    body = _strip_code_fences(page.read_text(encoding="utf-8"))
    targets: list[str] = []
    for raw in _LINK_RE.findall(body):
        target = raw.split(" ", 1)[0].strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if (bare := target.split("#", 1)[0]) and not bare.startswith("/"):
            targets.append(bare)
    return targets


NAV_TARGETS = _nav_targets(_load_nav())
MARKDOWN_PAGES = _markdown_pages()


def test_nav_has_no_duplicate_entries() -> None:
    """A page listed twice renders twice in the sidebar."""
    seen: set[str] = set()
    duplicates = {t for t in NAV_TARGETS if t in seen or seen.add(t)}  # type: ignore[func-returns-value]
    assert not duplicates, f"duplicate nav entries: {sorted(duplicates)}"


@pytest.mark.parametrize("target", sorted(set(NAV_TARGETS)))
def test_nav_target_exists(target: str) -> None:
    """Every navigation entry resolves to a file under ``docs/``."""
    assert (DOCS_DIR / target).is_file(), f"nav names a missing page: {target}"


def test_no_orphan_pages() -> None:
    """Every page under ``docs/`` is reachable from the navigation."""
    navigated = {(DOCS_DIR / target).resolve() for target in NAV_TARGETS}
    exempt = {(DOCS_DIR / name).resolve() for name in NAV_EXEMPT}
    orphans = sorted(
        page.relative_to(DOCS_DIR).as_posix()
        for page in MARKDOWN_PAGES
        if page.resolve() not in navigated | exempt
    )
    assert not orphans, (
        f"pages unreachable from zensical.toml nav: {orphans}. "
        f"Add them to nav, or to NAV_EXEMPT if that is deliberate."
    )


@pytest.mark.parametrize(
    "page",
    MARKDOWN_PAGES,
    ids=[p.relative_to(DOCS_DIR).as_posix() for p in MARKDOWN_PAGES],
)
def test_relative_links_resolve(page: Path) -> None:
    """Relative links between pages point at files that exist."""
    broken = [
        target
        for target in _relative_links(page)
        if not (page.parent / target).resolve().exists()
    ]
    assert not broken, f"{page.relative_to(REPO_ROOT)} links to missing: {broken}"


def test_readme_stays_a_landing_page() -> None:
    """The README routes into the docs site rather than duplicating it."""
    line_count = len((REPO_ROOT / "README.md").read_text(encoding="utf-8").splitlines())
    assert line_count <= README_MAX_LINES, (
        f"README.md is {line_count} lines (budget {README_MAX_LINES}). "
        f"Deep material belongs in docs/, linked from here."
    )


def test_every_track_has_an_index() -> None:
    """Each track's section index exists, as ``navigation.indexes`` expects."""
    for track in ("basics", "guide", "theory", "project"):
        assert (DOCS_DIR / track / "index.md").is_file(), f"missing {track}/index.md"
