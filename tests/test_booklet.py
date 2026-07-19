"""Booklet pagination, HTML structure, and PDF page counts."""

import base64
import math
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

import pypdf
import pytest

from legolization.catalog import default_catalog
from legolization.instructions import (
    BillOfMaterials,
    BomEntry,
    BookletConfig,
    BuildStep,
    InstructionPlan,
    ModelStats,
    RotStep,
    StepImages,
    booklet_html,
    build_booklet,
    plan_instructions,
    write_booklet,
    write_booklet_pdf,
)
from legolization.layout import Layout

# A valid 1x1 PNG so reportlab's drawImage can decode it.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

_STATS = ModelStats(
    name="testmodel",
    brick_count=42,
    mass_g=33.3,
    step_count=6,
    stable=True,
    buildable=True,
    component_count=1,
    floating_count=0,
)


def _entries(count: int) -> tuple[BomEntry, ...]:
    return tuple(
        BomEntry(
            part_key=f"brick_1x{index + 1}",
            ldraw_part=f"30{index:02d}.dat",
            colour_code=4,
            colour_name="Red",
            quantity=index + 1,
            mass_g=0.5 * (index + 1),
        )
        for index in range(count)
    )


def _plan(steps: int, bom_rows: int) -> InstructionPlan:
    return InstructionPlan(
        steps=tuple(
            BuildStep(
                index=index,
                brick_ids=(index,),
                prefix_stable=index != 2,
                prefix_max_score=0.1,
                rotstep=RotStep(yaw=90) if index == 4 else None,
            )
            for index in range(1, steps + 1)
        ),
        warnings=(),
        bom=BillOfMaterials(
            total=_entries(bom_rows),
            per_step=tuple(_entries(2) for _ in range(steps)),
        ),
    )


def _images(steps: int, data: bytes | None) -> StepImages:
    return StepImages(
        images=(data,) * steps,
        renderer=None,
        warnings=() if data is not None else ("no LDraw renderer found",),
    )


class _HtmlCounts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pages = 0
        self.steps = 0
        self.images = 0
        self.placeholders = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        classes = (dict(attrs).get("class") or "").split()
        src = dict(attrs).get("src") or ""
        if tag == "div" and "page" in classes:
            self.pages += 1
        if tag == "section" and "step" in classes:
            self.steps += 1
        if tag == "img" and src.startswith("data:image/png;base64,"):
            self.images += 1
        if tag == "div" and "placeholder" in classes:
            self.placeholders += 1


def _counts(markup: str) -> _HtmlCounts:
    parser = _HtmlCounts()
    parser.feed(markup)
    return parser


@pytest.mark.parametrize("steps_per_page", [1, 2, 3])
@pytest.mark.parametrize(("steps", "bom_rows"), [(1, 5), (6, 30), (9, 120)])
def test_page_math(steps: int, bom_rows: int, steps_per_page: int) -> None:
    plan = _plan(steps, bom_rows)
    booklet = build_booklet(
        plan,
        _STATS,
        _images(steps, _PNG),
        config=BookletConfig(steps_per_page=steps_per_page),
    )
    overflow = math.ceil(max(0, bom_rows - 24) / 40)
    assert booklet.page_count == 1 + overflow + math.ceil(steps / steps_per_page)
    assert _counts(booklet_html(booklet)).pages == booklet.page_count


def test_html_structure_with_images() -> None:
    plan = _plan(5, 10)
    booklet = build_booklet(plan, _STATS, _images(5, _PNG))
    markup = booklet_html(booklet)
    counts = _counts(markup)
    assert counts.steps == 5
    assert counts.images == 5
    assert counts.placeholders == 0
    assert "42" in markup
    assert "33.3 g" in markup
    assert "unstable — support by hand" in markup  # step 2 is prefix-unstable
    assert "rotate the model" in markup  # step 4 carries a rotstep
    assert 'id="step-3"' in markup


def test_html_without_images_keeps_page_count() -> None:
    plan = _plan(5, 10)
    with_images = build_booklet(plan, _STATS, _images(5, _PNG))
    without = build_booklet(plan, _STATS, _images(5, None))
    markup = booklet_html(without)
    counts = _counts(markup)
    assert without.page_count == with_images.page_count
    assert counts.images == 0
    assert counts.placeholders == 5
    assert "no LDraw renderer found" in markup


def test_html_escapes_part_and_colour_text() -> None:
    entry = BomEntry(
        part_key="brick<weird>",
        ldraw_part="3005.dat",
        colour_code=99_999,
        colour_name="<Fancy & Rare>",
        quantity=1,
        mass_g=0.5,
    )
    plan = InstructionPlan(
        steps=(
            BuildStep(
                index=1, brick_ids=(1,), prefix_stable=True, prefix_max_score=0.1
            ),
        ),
        warnings=("mind the <gap>",),
        bom=BillOfMaterials(total=(entry,), per_step=((entry,),)),
    )
    markup = booklet_html(build_booklet(plan, _STATS, _images(1, None)))
    assert "<Fancy" not in markup
    assert "&lt;Fancy &amp; Rare&gt;" in markup
    assert "brick&lt;weird&gt;" in markup
    assert "mind the &lt;gap&gt;" in markup


def test_pdf_page_count_matches(tmp_path: Path) -> None:
    plan = _plan(5, 60)
    mixed = StepImages(
        images=(_PNG, None, _PNG, None, _PNG),
        renderer=None,
        warnings=(),
    )
    booklet = build_booklet(plan, _STATS, mixed)
    path = tmp_path / "book.pdf"
    write_booklet_pdf(booklet, path)
    assert len(pypdf.PdfReader(path).pages) == booklet.page_count


def test_write_booklet_dispatches_on_suffix(tmp_path: Path) -> None:
    plan = _plan(2, 4)
    images = _images(2, _PNG)
    html_booklet = write_booklet(plan, _STATS, images, tmp_path / "book.html")
    assert (tmp_path / "book.html").read_text().startswith("<!DOCTYPE html>")
    pdf_booklet = write_booklet(plan, _STATS, images, tmp_path / "book.pdf")
    assert (tmp_path / "book.pdf").stat().st_size > 0
    assert html_booklet.page_count == pdf_booklet.page_count
    with pytest.raises(ValueError, match="unsupported booklet format"):
        write_booklet(plan, _STATS, images, tmp_path / "book.docx")


def test_warning_overflow_pages_match_html_and_pdf(tmp_path: Path) -> None:
    warnings = tuple(f"renderer failed for step {index}" for index in range(50))
    plan = replace(_plan(1, 4), warnings=warnings)
    booklet = build_booklet(plan, _STATS, _images(1, _PNG))
    markup = booklet_html(booklet)
    path = tmp_path / "warnings.pdf"

    write_booklet_pdf(booklet, path)

    assert len(booklet.warning_pages) == 3
    assert _counts(markup).pages == booklet.page_count
    reader = pypdf.PdfReader(path)
    assert len(reader.pages) == booklet.page_count
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert warnings[0] in text
    assert warnings[-1] in text


def test_real_plan_carries_per_step_callouts() -> None:
    layout = Layout(catalog=default_catalog())
    for x in range(8):
        layout.add("brick_1x1", x, 0, 0, 0, 4)
    plan = plan_instructions(layout)
    booklet = build_booklet(plan, _STATS, _images(len(plan.steps), None))
    entries = [entry for page in booklet.step_pages for entry in page]
    assert [entry.step.index for entry in entries] == list(
        range(1, len(plan.steps) + 1)
    )
    assert all(entry.parts for entry in entries)
    assert sum(part.quantity for entry in entries for part in entry.parts) == 8


def test_subassembly_badges_and_attach_callouts() -> None:
    from legolization.instructions import InstructionsConfig

    layout = Layout(catalog=default_catalog())
    for level in (0, 3, 6):
        layout.add("brick_2x2", 3, 3, level, 0, 15)  # stem
    layout.add("brick_2x2", 1, 3, 9, 0, 4)  # petal, no support below
    layout.add("brick_2x2", 3, 3, 9, 0, 4)  # hub on the stem
    layout.add("brick_2x2", 2, 3, 12, 0, 4)  # bridge petal to hub
    plan = plan_instructions(
        layout, config=InstructionsConfig(rotstep=False, subassemblies=True)
    )
    assert plan.subassemblies
    sub = plan.subassemblies[0]
    booklet = build_booklet(plan, _STATS, _images(len(plan.steps), None))
    document = booklet_html(booklet)
    assert f"subassembly {sub.name} — build on the table" in document
    assert f"attach subassembly {sub.name}" in document
    assert f"Requires subassembly {sub.name}" in document
    assert document.count('<section class="step"') == len(plan.steps)


def test_support_warnings_aggregate_consecutive_runs():
    from legolization.instructions.booklet import _aggregate_support_warnings

    warnings = (
        "step 3: prefix unstable (score 1.00); support the overhang by hand",
        "step 4: prefix unstable (score 1.00); support the overhang by hand",
        "step 5: prefix unstable (score 1.00); support the overhang by hand",
        "sequencer deadlocked; remaining steps follow band order",
        "step 9: prefix unstable (score 1.00); support the overhang by hand",
    )
    aggregated = _aggregate_support_warnings(warnings)
    assert aggregated == (
        "steps 3-5: temporary support needed while building (3 unstable prefixes)",
        "sequencer deadlocked; remaining steps follow band order",
        "step 9: temporary support needed while building (unstable prefix)",
    )
