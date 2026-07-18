"""Instruction booklets: cover, parts list, and step pages as HTML or PDF.

One :class:`Booklet` pagination model feeds two serializers, so the HTML and
PDF versions of the same plan always have the same page count — and so does
a booklet built without a renderer (steps get placeholder boxes instead of
images, never fewer pages). The HTML is a single self-contained file with
data-URI images; the PDF is drawn with reportlab's fixed-position canvas
API precisely because flowing layouts would let content size change the
page count.
"""

from __future__ import annotations

import base64
import html
import io
from dataclasses import dataclass
from itertools import batched
from typing import TYPE_CHECKING, Literal

from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from legolization.color import default_palette

if TYPE_CHECKING:
    from pathlib import Path

    from legolization.instructions.bom import BomEntry
    from legolization.instructions.render import StepImages
    from legolization.instructions.sequencer import BuildStep, InstructionPlan

_COVER_BOM_ROWS = 24
_COVER_WARNING_ROWS = 4
_PARTS_PAGE_ROWS = 40
_WARNING_PAGE_ROWS = 20
_PDF_MARGIN = 36.0  # ½ inch
_PDF_CALLOUT_LINES = 8

_PAGE_SIZES: dict[str, tuple[float, float]] = {"letter": letter, "a4": A4}


@dataclass(frozen=True, slots=True)
class ModelStats:
    """Cover-page summary numbers for one legolized model."""

    name: str
    brick_count: int
    mass_g: float
    step_count: int
    stable: bool
    buildable: bool
    component_count: int
    floating_count: int


@dataclass(frozen=True, slots=True)
class BookletConfig:
    """Booklet layout knobs."""

    steps_per_page: int = 2
    page_size: Literal["letter", "a4"] = "letter"
    title: str | None = None


@dataclass(frozen=True, slots=True)
class StepEntry:
    """One step's slice of the booklet: verdicts, callout parts, image."""

    step: BuildStep
    parts: tuple[BomEntry, ...]
    image_png: bytes | None


@dataclass(frozen=True, slots=True)
class Booklet:
    """A fully paginated booklet, ready for any serializer."""

    stats: ModelStats
    config: BookletConfig
    total: tuple[BomEntry, ...]
    warning_pages: tuple[tuple[str, ...], ...]
    overflow_parts_pages: tuple[tuple[BomEntry, ...], ...]
    step_pages: tuple[tuple[StepEntry, ...], ...]
    warnings: tuple[str, ...]

    @property
    def page_count(self) -> int:
        """Cover plus warning, overflow-parts, and step pages."""
        return (
            1
            + len(self.warning_pages)
            + len(self.overflow_parts_pages)
            + len(self.step_pages)
        )


def build_booklet(
    plan: InstructionPlan,
    stats: ModelStats,
    images: StepImages,
    *,
    config: BookletConfig | None = None,
) -> Booklet:
    """Paginate a plan into a booklet; image presence never changes layout."""
    config = config or BookletConfig()
    warnings = plan.warnings + images.warnings
    entries = tuple(
        StepEntry(
            step=step,
            parts=(plan.bom.per_step[index] if index < len(plan.bom.per_step) else ()),
            image_png=(images.images[index] if index < len(images.images) else None),
        )
        for index, step in enumerate(plan.steps)
    )
    overflow = plan.bom.total[_COVER_BOM_ROWS:]
    return Booklet(
        stats=stats,
        config=config,
        total=plan.bom.total,
        warning_pages=tuple(
            tuple(rows)
            for rows in batched(
                warnings[_COVER_WARNING_ROWS:],
                _WARNING_PAGE_ROWS,
            )
        ),
        overflow_parts_pages=tuple(
            tuple(rows) for rows in batched(overflow, _PARTS_PAGE_ROWS)
        ),
        step_pages=tuple(
            tuple(page) for page in batched(entries, config.steps_per_page)
        ),
        warnings=warnings,
    )


def validate_booklet_path(path: Path) -> None:
    """Reject unsupported booklet destinations before other artifacts are written."""
    if path.suffix.lower() not in {".html", ".pdf"}:
        suffix = path.suffix.lower()
        msg = f"unsupported booklet format {suffix!r} (expected .html or .pdf)"
        raise ValueError(msg)


def write_booklet(
    plan: InstructionPlan,
    stats: ModelStats,
    images: StepImages,
    path: Path,
    *,
    config: BookletConfig | None = None,
) -> Booklet:
    """Build the booklet and write it in the format the suffix picks."""
    validate_booklet_path(path)
    booklet = build_booklet(plan, stats, images, config=config)
    if path.suffix.lower() == ".html":
        path.write_text(booklet_html(booklet), encoding="utf-8")
    else:
        write_booklet_pdf(booklet, path)
    return booklet


# --- HTML ---

_CSS = """\
* { box-sizing: border-box; margin: 0; }
body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif;
       color: #1a1a1a; }
@page { size: {page_size}; margin: 12mm; }
.page { page-break-after: always; padding: 24px; max-width: 800px;
        margin: 0 auto; }
h1 { font-size: 2em; margin-bottom: 12px; }
h2 { font-size: 1.2em; margin: 16px 0 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 3px 10px 3px 0; font-size: 0.9em; }
thead th { border-bottom: 1px solid #999; }
.stats td:first-child { color: #666; }
.warnings { background: #fff4e5; border: 1px solid #f0c36d; padding: 8px;
            margin: 12px 0; font-size: 0.9em; }
.step { border: 1px solid #ddd; border-radius: 8px; padding: 12px;
        margin-bottom: 16px; }
.step h2 { margin-top: 0; }
.badge { font-size: 0.75em; padding: 2px 8px; border-radius: 8px;
         vertical-align: middle; margin-left: 8px; }
.badge.unstable { background: #fdd; color: #900; }
.badge.rotate { background: #def; color: #036; }
.step img { max-width: 100%; display: block; margin: 8px 0; }
.placeholder { border: 1px dashed #bbb; color: #888; padding: 40px;
               text-align: center; margin: 8px 0; }
.swatch { display: inline-block; width: 0.9em; height: 0.9em;
          border: 1px solid #999; margin-right: 6px;
          vertical-align: -0.1em; }
"""


def booklet_html(booklet: Booklet) -> str:
    """Serialize the booklet as one self-contained HTML document."""
    title = html.escape(booklet.config.title or booklet.stats.name)
    lines = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{title}</title>",
        "<style>",
        _CSS.replace("{page_size}", booklet.config.page_size),
        "</style>",
        "</head>",
        "<body>",
        *_html_cover(booklet, title),
    ]
    for warnings in booklet.warning_pages:
        lines.extend(_html_warnings_page(warnings))
    for rows in booklet.overflow_parts_pages:
        lines.append('<div class="page parts">')
        lines.append("<h2>Parts (continued)</h2>")
        lines.extend(_html_parts_table(rows))
        lines.append("</div>")
    for page in booklet.step_pages:
        lines.append('<div class="page steps">')
        for entry in page:
            lines.extend(_html_step(entry))
        lines.append("</div>")
    lines += ["</body>", "</html>"]
    return "\n".join(lines) + "\n"


def _html_cover(booklet: Booklet, title: str) -> list[str]:
    stats = booklet.stats
    rows = [
        ("Bricks", f"{stats.brick_count}"),
        ("Mass", f"{stats.mass_g:.1f} g"),
        ("Steps", f"{stats.step_count}"),
        ("Stable", "yes" if stats.stable else "NO"),
        ("Buildable", "yes" if stats.buildable else "NO"),
    ]
    if stats.component_count != 1 or stats.floating_count:
        rows.append(
            (
                "Connectivity",
                f"{stats.component_count} components, {stats.floating_count} floating",
            )
        )
    lines = [
        '<div class="page cover">',
        f"<h1>{title}</h1>",
        '<table class="stats"><tbody>',
        *(
            f"<tr><td>{html.escape(label)}</td><td>{html.escape(value)}</td></tr>"
            for label, value in rows
        ),
        "</tbody></table>",
    ]
    if cover_warnings := booklet.warnings[:_COVER_WARNING_ROWS]:
        lines.append('<div class="warnings"><ul>')
        lines.extend(f"<li>{html.escape(warning)}</li>" for warning in cover_warnings)
        if booklet.warning_pages:
            remaining = len(booklet.warnings) - len(cover_warnings)
            lines.append(f"<li>{remaining} more warning(s) on the next page …</li>")
        lines.append("</ul></div>")
    lines.append("<h2>Parts</h2>")
    lines.extend(_html_parts_table(booklet.total[:_COVER_BOM_ROWS]))
    if len(booklet.total) > _COVER_BOM_ROWS:
        lines.append("<p>continued on the next page …</p>")
    lines.append("</div>")
    return lines


def _html_warnings_page(warnings: tuple[str, ...]) -> list[str]:
    lines = [
        '<div class="page warning-list">',
        "<h2>Warnings (continued)</h2>",
        '<div class="warnings"><ul>',
    ]
    lines.extend(f"<li>{html.escape(warning)}</li>" for warning in warnings)
    lines.extend(["</ul></div>", "</div>"])
    return lines


def _html_parts_table(rows: tuple[BomEntry, ...]) -> list[str]:
    lines = [
        "<table>",
        "<thead><tr><th>qty</th><th>part</th><th>ldraw</th>"
        "<th>colour</th><th>mass</th></tr></thead>",
        "<tbody>",
    ]
    lines.extend(
        "<tr>"
        f"<td>{entry.quantity}</td>"
        f"<td>{html.escape(entry.part_key)}</td>"
        f"<td>{html.escape(entry.ldraw_part)}</td>"
        f"<td>{_html_swatch(entry.colour_code)}{html.escape(entry.colour_name)}</td>"
        f"<td>{entry.mass_g:.1f} g</td>"
        "</tr>"
        for entry in rows
    )
    lines += ["</tbody>", "</table>"]
    return lines


def _html_step(entry: StepEntry) -> list[str]:
    step = entry.step
    badges = ""
    if not step.prefix_stable:
        badges += '<span class="badge unstable">unstable — support by hand</span>'
    if step.rotstep is not None:
        badges += '<span class="badge rotate">rotate the model</span>'
    lines = [
        f'<section class="step" id="step-{step.index}">',
        f"<h2>Step {step.index}{badges}</h2>",
    ]
    if entry.image_png is not None:
        encoded = base64.b64encode(entry.image_png).decode("ascii")
        lines.append(
            f'<img alt="Step {step.index}" src="data:image/png;base64,{encoded}">'
        )
    else:
        lines.append(
            '<div class="placeholder">step image unavailable — '
            "no LDraw renderer found</div>"
        )
    if entry.parts:
        lines.extend(_html_parts_table(entry.parts))
    lines.append("</section>")
    return lines


def _html_swatch(colour_code: int) -> str:
    return f'<span class="swatch" style="background:{_swatch_hex(colour_code)}"></span>'


def _swatch_hex(colour_code: int) -> str:
    try:
        red, green, blue = default_palette().rgb_of(colour_code)
    except ValueError:
        return "#808080"
    return f"#{red:02x}{green:02x}{blue:02x}"


# --- PDF ---


def write_booklet_pdf(booklet: Booklet, path: Path) -> None:
    """Draw the booklet with reportlab; one canvas page per booklet page."""
    width, height = _PAGE_SIZES[booklet.config.page_size]
    canvas = Canvas(str(path), pagesize=(width, height))
    canvas.setTitle(booklet.config.title or booklet.stats.name)
    _pdf_cover(canvas, booklet, height=height)
    canvas.showPage()
    for warnings in booklet.warning_pages:
        _pdf_warnings_page(canvas, warnings, height=height)
        canvas.showPage()
    for rows in booklet.overflow_parts_pages:
        _pdf_parts_page(canvas, rows, height=height)
        canvas.showPage()
    for page in booklet.step_pages:
        _pdf_step_page(canvas, booklet, page, width=width, height=height)
        canvas.showPage()
    canvas.save()


def _pdf_cover(canvas: Canvas, booklet: Booklet, *, height: float) -> None:
    stats = booklet.stats
    cursor = height - _PDF_MARGIN - 24
    canvas.setFont("Helvetica-Bold", 22)
    canvas.drawString(_PDF_MARGIN, cursor, booklet.config.title or stats.name)
    cursor -= 30
    canvas.setFont("Helvetica", 10)
    summary = (
        f"bricks: {stats.brick_count}   mass: {stats.mass_g:.1f} g   "
        f"steps: {stats.step_count}   "
        f"stable: {'yes' if stats.stable else 'NO'}   "
        f"buildable: {'yes' if stats.buildable else 'NO'}"
    )
    canvas.drawString(_PDF_MARGIN, cursor, summary)
    cursor -= 14
    cover_warnings = booklet.warnings[:_COVER_WARNING_ROWS]
    for warning in cover_warnings:
        canvas.drawString(_PDF_MARGIN, cursor, f"warning: {warning}"[:110])
        cursor -= 12
    if booklet.warning_pages:
        remaining = len(booklet.warnings) - len(cover_warnings)
        canvas.drawString(
            _PDF_MARGIN,
            cursor,
            f"{remaining} more warning(s) on the next page ...",
        )
        cursor -= 12
    cursor -= 10
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawString(_PDF_MARGIN, cursor, "Parts")
    cursor -= 16
    _pdf_parts_rows(canvas, booklet.total[:_COVER_BOM_ROWS], cursor=cursor)
    if len(booklet.total) > _COVER_BOM_ROWS:
        canvas.setFont("Helvetica-Oblique", 9)
        canvas.drawString(_PDF_MARGIN, _PDF_MARGIN, "continued on the next page …")


def _pdf_warnings_page(
    canvas: Canvas,
    warnings: tuple[str, ...],
    *,
    height: float,
) -> None:
    canvas.setFont("Helvetica-Bold", 12)
    cursor = height - _PDF_MARGIN - 12
    canvas.drawString(_PDF_MARGIN, cursor, "Warnings (continued)")
    cursor -= 18
    canvas.setFont("Helvetica", 9)
    for warning in warnings:
        canvas.drawString(_PDF_MARGIN, cursor, f"warning: {warning}"[:110])
        cursor -= 12


def _pdf_parts_page(
    canvas: Canvas,
    rows: tuple[BomEntry, ...],
    *,
    height: float,
) -> None:
    canvas.setFont("Helvetica-Bold", 12)
    cursor = height - _PDF_MARGIN - 12
    canvas.drawString(_PDF_MARGIN, cursor, "Parts (continued)")
    _pdf_parts_rows(canvas, rows, cursor=cursor - 16)


def _pdf_parts_rows(
    canvas: Canvas,
    rows: tuple[BomEntry, ...],
    *,
    cursor: float,
) -> None:
    canvas.setFont("Helvetica", 9)
    for entry in rows:
        canvas.drawString(_PDF_MARGIN, cursor, _part_line(entry))
        cursor -= 12


def _pdf_step_page(
    canvas: Canvas,
    booklet: Booklet,
    page: tuple[StepEntry, ...],
    *,
    width: float,
    height: float,
) -> None:
    slots = booklet.config.steps_per_page
    slot_height = (height - 2 * _PDF_MARGIN) / slots
    for position, entry in enumerate(page):
        top = height - _PDF_MARGIN - position * slot_height
        _pdf_step_slot(
            canvas,
            entry,
            top=top,
            slot_height=slot_height,
            width=width,
        )


def _pdf_step_slot(
    canvas: Canvas,
    entry: StepEntry,
    *,
    top: float,
    slot_height: float,
    width: float,
) -> None:
    step = entry.step
    label = f"Step {step.index}"
    if not step.prefix_stable:
        label += "   [unstable — support by hand]"
    if step.rotstep is not None:
        label += "   [rotate the model]"
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawString(_PDF_MARGIN, top - 16, label)

    image_top = top - 28
    image_height = slot_height - 40
    image_width = (width - 2 * _PDF_MARGIN) * 0.62
    if entry.image_png is not None:
        canvas.drawImage(
            ImageReader(io.BytesIO(entry.image_png)),
            _PDF_MARGIN,
            image_top - image_height,
            width=image_width,
            height=image_height,
            preserveAspectRatio=True,
            anchor="c",
        )
    else:
        canvas.rect(
            _PDF_MARGIN,
            image_top - image_height,
            image_width,
            image_height,
        )
        canvas.setFont("Helvetica-Oblique", 9)
        canvas.drawCentredString(
            _PDF_MARGIN + image_width / 2,
            image_top - image_height / 2,
            "step image unavailable",
        )

    callout_x = _PDF_MARGIN + image_width + 16
    cursor = image_top - 10
    canvas.setFont("Helvetica", 9)
    for part in entry.parts[:_PDF_CALLOUT_LINES]:
        canvas.drawString(
            callout_x, cursor, f"{part.quantity} x {part.part_key} {part.colour_name}"
        )
        cursor -= 12
    if len(entry.parts) > _PDF_CALLOUT_LINES:
        remaining = len(entry.parts) - _PDF_CALLOUT_LINES
        canvas.drawString(callout_x, cursor, f"+ {remaining} more part lines")


def _part_line(entry: BomEntry) -> str:
    return (
        f"{entry.quantity:>4}  {entry.part_key:<14} {entry.ldraw_part:<10} "
        f"{entry.colour_name}  {entry.mass_g:.1f} g"
    )
