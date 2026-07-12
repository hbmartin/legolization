#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import pdfplumber
from PIL import Image
from pypdf import PdfReader


ROOT = Path.cwd()
OUT_ROOT = ROOT / "output" / "markdown_conversion"
TMP_ROOT = ROOT / "tmp" / "pdfs"
BUNDLED_BIN = Path("/Users/haroldmartin/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin")
BUNDLED_NODE = Path("/Users/haroldmartin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin")

CAPTION_RE = re.compile(
    r"^(?P<label>(?:Fig(?:ure)?\.?\s*\d+[A-Za-z0-9.-]*|Table\s*[A-Za-z0-9IVXLC.-]+|TABLE\s+[A-Za-z0-9IVXLC.-]+|Algorithm\s*\d+[A-Za-z0-9.-]*))\s*[:.)-]?\s*(?P<body>.*)$",
    re.IGNORECASE,
)
SECTION_RE = re.compile(
    r"^(?:"
    r"abstract|keywords?|references|acknowledg(?:e)?ments?|appendix|"
    r"\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z0-9]|"
    r"[IVXLC]+\.\s+[A-Z][A-Za-z0-9]|"
    r"[A-Z]\.\s+[A-Z][A-Za-z0-9]"
    r")"
    ,
    re.IGNORECASE,
)
MATH_CHARS = set("=<>+-*/^_{}[]()|\\abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
MATH_HINTS = set("=<>∑Σ∫√≤≥≈≠±×÷∈∉∪∩∀∃→←↔∂∇τλμσθαβγΔΦΩ")
STRONG_MATH_HINTS = set("=<>∑Σ∫√≤≥≈≠±÷∈∉∪∩∀∃→←↔∂∇τλμσθαβγΔΦΩ")
CODE_HINT_RE = re.compile(r"^\s*(?:Input|Output|Require|Ensure|procedure)\b", re.IGNORECASE)


def env() -> dict[str, str]:
    current = os.environ.copy()
    prefixes = [str(BUNDLED_BIN), str(BUNDLED_NODE), "/opt/homebrew/bin", "/usr/local/bin"]
    current["PATH"] = os.pathsep.join(prefixes + [current.get("PATH", "")])
    return current


def run(cmd: list[str], cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env(),
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    return slug[:96] or "paper"


def unique_slug(base: str, seen: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in seen:
        candidate = f"{base}-{index}"
        index += 1
    seen.add(candidate)
    return candidate


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "").strip()


def round_float(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    return value


def selected_geometry(item: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in keys:
        if key in item:
            value = item[key]
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = round_float(value)
    return out


def word_text(words: list[dict[str, Any]]) -> tuple[str, str]:
    if not words:
        return "", ""
    widths = []
    chars = 0
    for word in words:
        text = safe_text(word.get("text"))
        chars += max(len(text), 1)
        widths.append(float(word["x1"]) - float(word["x0"]))
    avg_char_width = sum(widths) / max(chars, 1)
    compact_parts = []
    layout_parts = []
    previous_x1: float | None = None
    for word in words:
        text = safe_text(word.get("text"))
        if not text:
            continue
        if previous_x1 is not None:
            gap = float(word["x0"]) - previous_x1
            spaces = max(1, min(10, int(round(gap / max(avg_char_width, 1.0)))))
            layout_parts.append(" " * spaces)
        compact_parts.append(text)
        layout_parts.append(text)
        previous_x1 = float(word["x1"])
    return " ".join(compact_parts), "".join(layout_parts)


def group_words_into_lines(page: pdfplumber.page.Page, page_number: int) -> list[dict[str, Any]]:
    words = page.extract_words(
        x_tolerance=2,
        y_tolerance=3,
        keep_blank_chars=False,
        use_text_flow=False,
    )
    sorted_words = sorted(words, key=lambda w: (float(w["top"]), float(w["x0"])))
    rows: list[list[dict[str, Any]]] = []
    row_tops: list[float] = []
    for word in sorted_words:
        top = float(word["top"])
        placed = False
        for idx, row_top in enumerate(row_tops):
            if abs(top - row_top) <= 3.2:
                rows[idx].append(word)
                row_tops[idx] = (row_top * (len(rows[idx]) - 1) + top) / len(rows[idx])
                placed = True
                break
        if not placed:
            rows.append([word])
            row_tops.append(top)

    chars = page.chars
    lines: list[dict[str, Any]] = []
    line_index = 0
    for row in rows:
        row = sorted(row, key=lambda w: float(w["x0"]))
        segments: list[list[dict[str, Any]]] = [[]]
        mid = float(page.width) / 2.0
        for i, word in enumerate(row):
            if i > 0:
                previous = row[i - 1]
                gap = float(word["x0"]) - float(previous["x1"])
                crosses_mid = float(previous["x1"]) < mid < float(word["x0"])
                if gap > max(42.0, float(page.width) * 0.08) and crosses_mid:
                    segments.append([])
            segments[-1].append(word)

        for segment in segments:
            if not segment:
                continue
            text, layout_text = word_text(segment)
            if not text:
                continue
            x0 = min(float(w["x0"]) for w in segment)
            x1 = max(float(w["x1"]) for w in segment)
            top = min(float(w["top"]) for w in segment)
            bottom = max(float(w["bottom"]) for w in segment)
            line_chars = [c for c in chars if float(c.get("top", 0)) <= bottom + 1 and float(c.get("bottom", 0)) >= top - 1 and float(c.get("x0", 0)) <= x1 + 1 and float(c.get("x1", 0)) >= x0 - 1]
            sizes = [float(c.get("size", 0)) for c in line_chars if c.get("size")]
            fonts = sorted({safe_text(c.get("fontname")) for c in line_chars if c.get("fontname")})
            center = (x0 + x1) / 2.0
            width = x1 - x0
            if x1 < mid - 16:
                column = "left"
            elif x0 > mid + 16:
                column = "right"
            elif width > float(page.width) * 0.55 or (x0 < mid - 20 and x1 > mid + 20):
                column = "full"
            else:
                column = "left" if center < mid else "right"
            line_index += 1
            lines.append(
                {
                    "id": f"p{page_number:03d}-l{line_index:04d}",
                    "page": page_number,
                    "text": text,
                    "layout_text": layout_text,
                    "x0": round(x0, 3),
                    "x1": round(x1, 3),
                    "top": round(top, 3),
                    "bottom": round(bottom, 3),
                    "avg_size": round(sum(sizes) / len(sizes), 3) if sizes else None,
                    "fontnames": fonts[:8],
                    "column": column,
                }
            )
    return sorted(lines, key=lambda line: (line["top"], line["x0"]))


def has_two_columns(lines: list[dict[str, Any]]) -> bool:
    left = [line for line in lines if line["column"] == "left" and len(line["text"]) > 18]
    right = [line for line in lines if line["column"] == "right" and len(line["text"]) > 18]
    return len(left) >= 8 and len(right) >= 8


def reading_order(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not has_two_columns(lines):
        return sorted(lines, key=lambda line: (line["top"], line["x0"]))

    ordered: list[dict[str, Any]] = []
    band: list[dict[str, Any]] = []

    def flush_band() -> None:
        nonlocal band
        if not band:
            return
        left = sorted([line for line in band if line["column"] == "left"], key=lambda line: (line["top"], line["x0"]))
        right = sorted([line for line in band if line["column"] == "right"], key=lambda line: (line["top"], line["x0"]))
        other = sorted([line for line in band if line["column"] not in {"left", "right"}], key=lambda line: (line["top"], line["x0"]))
        ordered.extend(left)
        ordered.extend(right)
        ordered.extend(other)
        band = []

    for line in sorted(lines, key=lambda item: (item["top"], item["x0"])):
        full_width_heading_or_caption = line["column"] == "full" and (
            is_heading(line["text"]) or is_caption(line["text"]) or len(line["text"]) > 20
        )
        if full_width_heading_or_caption:
            flush_band()
            ordered.append(line)
        else:
            band.append(line)
    flush_band()
    return ordered


def is_caption(text: str) -> bool:
    return CAPTION_RE.match(text.strip()) is not None


def is_heading(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) > 120:
        return False
    return SECTION_RE.match(stripped) is not None


def is_math_line(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 3:
        return False
    strong_hint = any(ch in stripped for ch in STRONG_MATH_HINTS)
    weak_hint = any(ch in stripped for ch in MATH_HINTS)
    if not weak_hint:
        return False
    alpha_words = re.findall(r"[A-Za-z]{4,}", stripped)
    prose_words = [word for word in alpha_words if word.lower() not in {"sqrt", "frac", "left", "right"}]
    if not strong_hint and len(prose_words) > 3:
        return False
    if strong_hint and len(prose_words) > 9 and not re.search(r"[=<>≤≥≈≠∑Σ]", stripped):
        return False
    mathish = sum(1 for ch in stripped if ch in MATH_CHARS or ch in MATH_HINTS)
    return mathish / max(len(stripped), 1) > 0.50 and len(prose_words) <= 9


def is_code_line(text: str) -> bool:
    stripped = text.strip()
    if CODE_HINT_RE.match(stripped):
        return True
    lower = stripped.lower()
    control = re.match(r"^(?:\d+\s*[:.)]\s*)?(for|if|else|while|return)\b", lower)
    if not control:
        return False
    if lower.startswith("if the ") or lower.startswith("if a ") or lower.startswith("if an ") or lower.startswith("if after "):
        return False
    return bool(re.search(r"\b(do|then|endif|end if|return|break|continue)\b|[:;{}]", lower))


def is_tableish_line(line: dict[str, Any]) -> bool:
    text = line.get("layout_text") or line.get("text") or ""
    tokens = text.split()
    if len(tokens) < 5:
        return False
    numeric = sum(1 for token in tokens if re.search(r"\d", token))
    multi_spaces = len(re.findall(r" {3,}", text))
    return numeric >= 3 and multi_spaces >= 2


def line_kind(line: dict[str, Any]) -> str:
    text = safe_text(line.get("text"))
    if is_caption(text):
        return "caption"
    if is_heading(text):
        return "heading"
    if is_code_line(text):
        return "code"
    if is_math_line(text):
        return "math"
    if is_tableish_line(line):
        return "table"
    return "paragraph"


def heading_level(text: str) -> int:
    stripped = text.strip()
    if re.match(r"^(abstract|keywords?|references|acknowledg(?:e)?ments?)$", stripped, re.IGNORECASE):
        return 2
    number = re.match(r"^(\d+(?:\.\d+)*)", stripped)
    if number:
        return min(2 + number.group(1).count("."), 4)
    roman = re.match(r"^[IVXLC]+\.", stripped)
    if roman:
        return 2
    letter = re.match(r"^[A-Z]\.", stripped)
    if letter:
        return 3
    return 3


def clean_inline(text: str) -> str:
    text = text.replace("\u0000", "")
    text = (
        text.replace("\ufb00", "ff")
        .replace("\ufb01", "fi")
        .replace("\ufb02", "fl")
        .replace("\ufb03", "ffi")
        .replace("\ufb04", "ffl")
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def markdown_caption(text: str) -> str:
    match = CAPTION_RE.match(text.strip())
    if not match:
        return clean_inline(text)
    label = clean_inline(match.group("label"))
    body = clean_inline(match.group("body"))
    return f"**{label}.** {body}".strip()


def normalize_for_compare(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"[#*_`>$|\\{}\[\]()<>=+:/.,;!?\"']", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def token_counter(text: str) -> Counter[str]:
    return Counter(re.findall(r"[a-z0-9][a-z0-9-]{1,}", normalize_for_compare(text)))


def counter_recall(reference: Counter[str], candidate: Counter[str]) -> float:
    total = sum(reference.values())
    if total == 0:
        return 1.0
    matched = sum(min(count, candidate.get(token, 0)) for token, count in reference.items())
    return matched / total


def infer_title(pdf_path: Path, pages: list[dict[str, Any]]) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        metadata = reader.metadata
        title = safe_text(getattr(metadata, "title", "")) if metadata else ""
        bad_titles = {"untitled", "unknown", "title", "none", "document"}
        if title and len(title) > 4 and title.strip().lower() not in bad_titles and not title.lower().endswith(".pdf"):
            return clean_inline(title)
    except Exception:
        pass

    first_lines = pages[0]["structural_lines"] if pages else []
    top_lines = [line for line in first_lines if line["top"] < 190 and len(line["text"]) > 5]
    sizes = [line["avg_size"] for line in top_lines if isinstance(line.get("avg_size"), (int, float))]
    if sizes:
        threshold = max(sizes) * 0.82
        title_lines = [line["text"] for line in top_lines if line.get("avg_size") and line["avg_size"] >= threshold and not re.search(r"doi|arxiv|copyright|journal|volume", line["text"], re.I)]
        if title_lines:
            title = clean_inline(" ".join(title_lines[:3]))
            if len(title) > 6:
                return title
    return infer_title_from_pypdf(pdf_path)


def extract_pypdf_page_texts(pdf_path: Path) -> list[str]:
    try:
        reader = PdfReader(str(pdf_path))
        return [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return []


def infer_title_from_pypdf(pdf_path: Path) -> str:
    for page_text in extract_pypdf_page_texts(pdf_path)[:2]:
        for raw_line in page_text.splitlines():
            line = clean_inline(raw_line)
            if len(line) < 8:
                continue
            if re.search(r"^(abstract|keywords?|figure|fig\.|table|doi|arxiv|copyright|published|availability|terms of use)", line, re.I):
                continue
            if len(line) <= 160:
                return line
    return pdf_path.stem


def split_embedded_headings(line: str) -> list[str]:
    text = line.rstrip()
    patterns = [
        r"\b(Abstract)\b",
        r"\b(Keywords?)\b",
        r"\b(References)\b",
        r"\b(Acknowledg(?:e)?ments?)\b",
        r"\b(\d+(?:\.\d+)*\.?\s+Introduction)\b",
    ]
    parts = [text]
    for pattern in patterns:
        next_parts: list[str] = []
        for part in parts:
            match = re.search(pattern, part, flags=re.IGNORECASE)
            if match and match.start() > 8:
                before = part[: match.start()].strip()
                heading = match.group(1).strip()
                after = part[match.end() :].strip()
                if before:
                    next_parts.append(before)
                next_parts.append(heading)
                if after:
                    next_parts.append(after)
            else:
                next_parts.append(part)
        parts = next_parts
    return [part for part in parts if part.strip()]


def write_poppler_evidence(pdf_path: Path, evidence_dir: Path, pages: int) -> tuple[str, list[str]]:
    poppler_dir = evidence_dir / "poppler_text"
    poppler_dir.mkdir(parents=True, exist_ok=True)
    all_text_path = poppler_dir / "all-layout.txt"
    result = run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf_path), str(all_text_path)], check=False)
    if result.returncode != 0:
        (poppler_dir / "pdftotext-error.txt").write_text(result.stderr, encoding="utf-8")
    page_texts: list[str] = []
    for page in range(1, pages + 1):
        page_path = poppler_dir / f"page-{page:03d}.txt"
        page_result = run(["pdftotext", "-layout", "-enc", "UTF-8", "-f", str(page), "-l", str(page), str(pdf_path), str(page_path)], check=False)
        if page_result.returncode != 0:
            page_path.write_text(page_result.stderr, encoding="utf-8")
        page_texts.append(page_path.read_text(encoding="utf-8", errors="replace") if page_path.exists() else "")
    all_text = all_text_path.read_text(encoding="utf-8", errors="replace") if all_text_path.exists() else "\n".join(page_texts)
    return all_text, page_texts


def write_pdfimages_evidence(pdf_path: Path, evidence_dir: Path) -> dict[str, Any]:
    image_dir = evidence_dir / "pdfimages"
    image_dir.mkdir(parents=True, exist_ok=True)
    listing = run(["pdfimages", "-list", str(pdf_path)], check=False)
    (image_dir / "list.txt").write_text((listing.stdout or "") + ("\nSTDERR:\n" + listing.stderr if listing.stderr else ""), encoding="utf-8")
    extract = run(["pdfimages", "-png", "-p", str(pdf_path), str(image_dir / "image")], check=False)
    if extract.stderr:
        (image_dir / "extract-stderr.txt").write_text(extract.stderr, encoding="utf-8")
    files = sorted(path.name for path in image_dir.iterdir() if path.is_file() and path.name not in {"list.txt", "extract-stderr.txt"})
    return {"listed": listing.returncode == 0, "extracted_files": files, "count": len(files)}


def render_pages(pdf_path: Path, evidence_dir: Path) -> dict[int, Path]:
    render_dir = evidence_dir / "rendered_pages"
    render_dir.mkdir(parents=True, exist_ok=True)
    prefix = render_dir / "page"
    result = run(["pdftoppm", "-r", "150", "-png", str(pdf_path), str(prefix)], check=False)
    if result.stderr:
        (render_dir / "pdftoppm-stderr.txt").write_text(result.stderr, encoding="utf-8")
    rendered: dict[int, Path] = {}
    for path in sorted(render_dir.glob("page-*.png")):
        match = re.search(r"page-(\d+)\.png$", path.name)
        if not match:
            continue
        page = int(match.group(1))
        target = render_dir / f"page-{page:03d}.png"
        if path != target:
            path.rename(target)
        rendered[page] = target
    return rendered


def extract_pdfplumber_data(pdf_path: Path, evidence_dir: Path) -> tuple[list[dict[str, Any]], str]:
    pages_data: list[dict[str, Any]] = []
    plumber_text_parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            lines = group_words_into_lines(page, page_index)
            ordered = reading_order(lines)
            plumber_text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
            plumber_text_parts.append(plumber_text)
            page_payload = {
                "page": page_index,
                "width": round(float(page.width), 3),
                "height": round(float(page.height), 3),
                "rotation": page.rotation,
                "chars": [
                    selected_geometry(ch, ["text", "fontname", "size", "x0", "x1", "top", "bottom", "doctop"])
                    for ch in page.chars
                ],
                "words": [
                    selected_geometry(word, ["text", "x0", "x1", "top", "bottom", "doctop"])
                    for word in page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
                ],
                "images": [
                    selected_geometry(image, ["name", "x0", "x1", "top", "bottom", "width", "height", "colorspace", "bits", "srcsize"])
                    for image in page.images
                ],
                "rects": [
                    selected_geometry(rect, ["x0", "x1", "top", "bottom", "width", "height", "linewidth", "stroking_color", "non_stroking_color"])
                    for rect in page.rects
                ],
                "curves": [
                    selected_geometry(curve, ["x0", "x1", "top", "bottom", "width", "height", "linewidth", "stroking_color", "non_stroking_color"])
                    for curve in page.curves
                ],
                "lines": [
                    selected_geometry(line, ["x0", "x1", "top", "bottom", "width", "height", "linewidth", "stroking_color"])
                    for line in page.lines
                ],
                "structural_lines": lines,
                "reading_order": [line["id"] for line in ordered],
            }
            pages_data.append(page_payload)

    geometry_path = evidence_dir / "pdfplumber_geometry.json"
    geometry_path.write_text(json.dumps({"pdf": pdf_path.name, "pages": pages_data}, ensure_ascii=False, indent=2), encoding="utf-8")
    structural_payload = {
        "pdf": pdf_path.name,
        "pages": [
            {
                "page": page["page"],
                "width": page["width"],
                "height": page["height"],
                "lines": page["structural_lines"],
                "reading_order": page["reading_order"],
            }
            for page in pages_data
        ],
    }
    (evidence_dir / "parser_structural_lines.json").write_text(json.dumps(structural_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return pages_data, "\n".join(plumber_text_parts)


def write_pdfinfo(pdf_path: Path, evidence_dir: Path) -> int:
    info = run(["pdfinfo", str(pdf_path)], check=False)
    (evidence_dir / "pdfinfo.txt").write_text((info.stdout or "") + ("\nSTDERR:\n" + info.stderr if info.stderr else ""), encoding="utf-8")
    pages = 0
    for line in info.stdout.splitlines():
        if line.startswith("Pages:"):
            pages = int(line.split(":", 1)[1].strip())
            break
    if pages == 0:
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = len(pdf.pages)
    return pages


def run_unpdf(pdf_path: Path, evidence_dir: Path) -> tuple[str, bool]:
    out_path = evidence_dir / "unpdf_items.json"
    script = ROOT / "scripts" / "extract_unpdf_items.mjs"
    if not script.exists():
        out_path.write_text(json.dumps({"error": "missing scripts/extract_unpdf_items.mjs"}, indent=2), encoding="utf-8")
        return "", False
    result = run(["node", str(script), str(pdf_path), str(out_path)], check=False)
    if result.returncode != 0:
        out_path.write_text(json.dumps({"error": result.stderr or result.stdout}, indent=2), encoding="utf-8")
        return "", False
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception:
        return "", False
    text_parts = []
    for page in payload.get("pages", []):
        text_parts.append(" ".join(safe_text(item.get("str")) for item in page.get("items", []) if isinstance(item, dict) and item.get("str")))
    return "\n".join(text_parts), True


def crop_from_render(
    rendered_path: Path,
    page_width: float,
    page_height: float,
    bbox: tuple[float, float, float, float],
    output_path: Path,
) -> bool:
    if not rendered_path.exists():
        return False
    with Image.open(rendered_path) as image:
        sx = image.width / page_width
        sy = image.height / page_height
        left = max(0, int(math.floor(bbox[0] * sx)))
        top = max(0, int(math.floor(bbox[1] * sy)))
        right = min(image.width, int(math.ceil(bbox[2] * sx)))
        bottom = min(image.height, int(math.ceil(bbox[3] * sy)))
        if right - left < 80 or bottom - top < 60:
            return False
        image.crop((left, top, right, bottom)).save(output_path)
    return True


def horizontal_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    overlap = max(0.0, min(a1, b1) - max(a0, b0))
    return overlap / max(min(a1 - a0, b1 - b0), 1.0)


def extract_relevant_images(
    pages_data: list[dict[str, Any]],
    rendered: dict[int, Path],
    images_dir: Path,
) -> dict[str, str]:
    images_dir.mkdir(parents=True, exist_ok=True)
    image_map: dict[str, str] = {}

    for page in pages_data:
        page_number = int(page["page"])
        page_width = float(page["width"])
        page_height = float(page["height"])
        rendered_path = rendered.get(page_number)
        if not rendered_path:
            continue
        lines_by_id = {line["id"]: line for line in page["structural_lines"]}
        ordered_lines = [lines_by_id[line_id] for line_id in page["reading_order"] if line_id in lines_by_id]
        captions = [line for line in ordered_lines if is_caption(line["text"]) and not line["text"].lower().startswith("algorithm")]
        caption_index = 0
        for caption in captions:
            caption_index += 1
            col = caption["column"]
            mid = page_width / 2.0
            if col == "left":
                x0, x1 = 24.0, mid - 8.0
            elif col == "right":
                x0, x1 = mid + 8.0, page_width - 24.0
            else:
                x0, x1 = 20.0, page_width - 20.0

            cap_top = float(caption["top"])
            cap_bottom = float(caption["bottom"])
            for other in sorted(ordered_lines, key=lambda line: line["top"]):
                if other["id"] == caption["id"] or other["page"] != page_number:
                    continue
                if other["top"] > cap_bottom and other["top"] - cap_bottom < 42 and horizontal_overlap(x0, x1, float(other["x0"]), float(other["x1"])) > 0.25:
                    if not is_heading(other["text"]) and not is_caption(other["text"]):
                        cap_bottom = max(cap_bottom, float(other["bottom"]))

            raster_candidates = []
            for image in page.get("images", []):
                ix0 = float(image.get("x0", 0.0))
                ix1 = float(image.get("x1", 0.0))
                itop = float(image.get("top", 0.0))
                ibottom = float(image.get("bottom", 0.0))
                if horizontal_overlap(x0, x1, ix0, ix1) > 0.25 and itop < cap_bottom + 24 and ibottom > max(0, cap_top - 420):
                    raster_candidates.append((ix0, itop, ix1, ibottom))

            if raster_candidates:
                rx0 = min(item[0] for item in raster_candidates)
                ry0 = min(item[1] for item in raster_candidates)
                rx1 = max(item[2] for item in raster_candidates)
                y0 = max(0.0, min(ry0, cap_top) - 12.0)
                y1 = min(page_height, max(cap_bottom, max(item[3] for item in raster_candidates)) + 12.0)
                bbox = (max(0.0, min(x0, rx0) - 8.0), y0, min(page_width, max(x1, rx1) + 8.0), y1)
            else:
                height_above = 270.0 if col == "full" else 220.0
                if cap_top < page_height * 0.34:
                    y0 = max(0.0, cap_top - (150.0 if col == "full" else 130.0))
                    y1 = min(page_height, cap_bottom + 22.0)
                else:
                    y0 = max(0.0, cap_top - height_above)
                    y1 = min(page_height, cap_bottom + 22.0)
                bbox = (x0, y0, x1, y1)

            filename = f"page-{page_number:03d}-figure-{caption_index:02d}.png"
            target = images_dir / filename
            if crop_from_render(rendered_path, page_width, page_height, bbox, target):
                image_map[caption["id"]] = f"images/{filename}"

    return image_map


def make_markdown(
    pdf_path: Path,
    doc_dir: Path,
    pages_data: list[dict[str, Any]],
    pypdf_pages: list[str],
    title: str,
    image_map: dict[str, str],
) -> str:
    md: list[str] = [
        f"# {clean_inline(title)}",
        "",
        f"Source PDF: `{pdf_path.name}`",
        "",
        "Evidence bundle: `evidence/`",
        "",
    ]
    normalized_title = normalize_for_compare(title)
    paragraph: list[str] = []
    fence: list[str] = []
    fence_kind: str | None = None
    image_slots: dict[int, list[tuple[str, str]]] = {}
    for page in pages_data:
        page_number = int(page["page"])
        slots: list[tuple[str, str]] = []
        lines_by_id = {line["id"]: line for line in page["structural_lines"]}
        ordered_lines = [lines_by_id[line_id] for line_id in page["reading_order"] if line_id in lines_by_id]
        for line in ordered_lines:
            image_path = image_map.get(line["id"])
            if image_path:
                slots.append((line["text"], image_path))
        image_slots[page_number] = slots

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        text = ""
        for part in paragraph:
            part = clean_inline(part)
            if not part:
                continue
            if text.endswith("-") and part and part[0].islower():
                text = text[:-1] + part
            elif text:
                text += " " + part
            else:
                text = part
        if text:
            md.append(text)
            md.append("")
        paragraph = []

    def flush_fence() -> None:
        nonlocal fence, fence_kind
        if not fence:
            return
        md.append("```text")
        md.extend(line.rstrip() for line in fence)
        md.append("```")
        md.append("")
        fence = []
        fence_kind = None

    def kind_for_text(text: str, previous_kind: str | None) -> str:
        pseudo_line = {"text": text, "layout_text": text}
        kind = line_kind(pseudo_line)
        if previous_kind in {"math", "code", "table"} and kind == "paragraph":
            compact = clean_inline(text)
            if len(compact) <= 42 and not is_heading(compact) and not is_caption(compact):
                return previous_kind
        return kind

    for page in pages_data:
        flush_paragraph()
        flush_fence()
        page_number = int(page["page"])
        md.append(f"<!-- Page {page_number} -->")
        md.append("")
        raw_page_text = pypdf_pages[page_number - 1] if page_number - 1 < len(pypdf_pages) else ""
        source_lines: list[str] = []
        for line in raw_page_text.splitlines():
            if line.strip():
                source_lines.extend(split_embedded_headings(line))
        if not source_lines:
            lines_by_id = {line["id"]: line for line in page["structural_lines"]}
            ordered = [lines_by_id[line_id] for line_id in page["reading_order"] if line_id in lines_by_id]
            source_lines = [line["text"] for line in ordered]
        page_image_slots = list(image_slots.get(page_number, []))
        for raw_text in source_lines:
            text = clean_inline(raw_text)
            if not text:
                continue
            if page_number == 1 and normalized_title and normalize_for_compare(text) in normalized_title and len(text) > 10:
                continue
            kind = kind_for_text(text, fence_kind)
            if kind == "caption" and page_image_slots:
                flush_paragraph()
                flush_fence()
                _, image_path = page_image_slots.pop(0)
                alt = text[:95].replace("[", "").replace("]", "")
                md.append(f"![{alt}]({image_path})")
                md.append("")
            if kind == "heading":
                flush_paragraph()
                flush_fence()
                level = heading_level(text)
                md.append(f"{'#' * level} {text}")
                md.append("")
            elif kind == "caption":
                flush_paragraph()
                flush_fence()
                md.append(markdown_caption(text))
                md.append("")
            elif kind in {"math", "code", "table"}:
                flush_paragraph()
                if fence_kind is not None and fence_kind != kind:
                    flush_fence()
                fence_kind = kind
                fence.append(raw_text)
            else:
                flush_fence()
                paragraph.append(text)
        flush_paragraph()
        flush_fence()
    return "\n".join(md).rstrip() + "\n"


def verification_report(
    pdf_path: Path,
    doc_dir: Path,
    pages_expected: int,
    markdown_text: str,
    poppler_text: str,
    plumber_text: str,
    unpdf_text: str,
    unpdf_ok: bool,
    pages_data: list[dict[str, Any]],
    image_map: dict[str, str],
    pdfimages_summary: dict[str, Any],
    rendered: dict[int, Path],
) -> dict[str, Any]:
    md_counter = token_counter(markdown_text)
    pop_counter = token_counter(poppler_text)
    plumber_counter = token_counter(plumber_text)
    unpdf_counter = token_counter(unpdf_text)
    captions = [line for page in pages_data for line in page["structural_lines"] if is_caption(line["text"])]
    equations = [line for page in pages_data for line in page["structural_lines"] if is_math_line(line["text"])]
    code_lines = [line for page in pages_data for line in page["structural_lines"] if is_code_line(line["text"])]
    page_markers = len(re.findall(r"<!-- Page \d+ -->", markdown_text))
    linked_images = len(re.findall(r"!\[[^\]]*\]\(images/", markdown_text))
    report = {
        "pdf": pdf_path.name,
        "pages_expected": pages_expected,
        "page_markers": page_markers,
        "rendered_pages": len(rendered),
        "poppler_token_recall": round(counter_recall(pop_counter, md_counter), 4),
        "pdfplumber_token_recall": round(counter_recall(plumber_counter, md_counter), 4),
        "unpdf_token_recall": round(counter_recall(unpdf_counter, md_counter), 4) if unpdf_ok else None,
        "unpdf_ok": unpdf_ok,
        "structural_lines": sum(len(page["structural_lines"]) for page in pages_data),
        "captions_detected": len(captions),
        "caption_crops_linked": linked_images,
        "math_like_lines_detected": len(equations),
        "code_like_lines_detected": len(code_lines),
        "pdfimages_extracted": pdfimages_summary.get("count", 0),
        "warnings": [],
    }
    if page_markers != pages_expected:
        report["warnings"].append(f"Markdown page markers ({page_markers}) do not match expected pages ({pages_expected}).")
    if len(rendered) != pages_expected:
        report["warnings"].append(f"Rendered page PNG count ({len(rendered)}) does not match expected pages ({pages_expected}).")
    if report["poppler_token_recall"] < 0.82:
        report["warnings"].append("Poppler token recall below 0.82; inspect Markdown against evidence.")
    if report["pdfplumber_token_recall"] < 0.82:
        report["warnings"].append("pdfplumber token recall below 0.82; inspect Markdown against evidence.")
    if unpdf_ok and report["unpdf_token_recall"] is not None and report["unpdf_token_recall"] < 0.78:
        report["warnings"].append("unpdf token recall below 0.78; inspect raw PDF.js item evidence.")

    md_lines = [
        f"# Verification: {pdf_path.name}",
        "",
        f"- Pages expected: {pages_expected}",
        f"- Markdown page markers: {page_markers}",
        f"- Rendered page PNGs: {len(rendered)}",
        f"- Poppler token recall in Markdown: {report['poppler_token_recall']:.4f}",
        f"- pdfplumber token recall in Markdown: {report['pdfplumber_token_recall']:.4f}",
        f"- unpdf token recall in Markdown: {report['unpdf_token_recall'] if report['unpdf_token_recall'] is not None else 'n/a'}",
        f"- Captions detected: {len(captions)}",
        f"- Figure/table crops linked from Markdown: {linked_images}",
        f"- Math-like structural lines detected: {len(equations)}",
        f"- Code-like structural lines detected: {len(code_lines)}",
        f"- pdfimages extracted files: {pdfimages_summary.get('count', 0)}",
        "",
        "Evidence files:",
        "",
        "- `pdfinfo.txt`",
        "- `poppler_text/all-layout.txt` and `poppler_text/page-###.txt`",
        "- `pdfplumber_geometry.json`",
        "- `unpdf_items.json`",
        "- `parser_structural_lines.json`",
        "- `pdfimages/`",
        "- `rendered_pages/page-###.png`",
        "",
    ]
    if report["warnings"]:
        md_lines.append("Warnings:")
        md_lines.append("")
        md_lines.extend(f"- {warning}" for warning in report["warnings"])
        md_lines.append("")
    else:
        md_lines.append("No automated verification warnings.")
        md_lines.append("")
    (doc_dir / "verification_report.md").write_text("\n".join(md_lines), encoding="utf-8")
    (doc_dir / "verification_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def convert_pdf(pdf_path: Path, slug: str) -> dict[str, Any]:
    doc_dir = OUT_ROOT / slug
    evidence_dir = doc_dir / "evidence"
    images_dir = doc_dir / "images"
    if doc_dir.exists():
        shutil.rmtree(doc_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    print(f"[convert] {pdf_path.name}")
    pages_expected = write_pdfinfo(pdf_path, evidence_dir)
    poppler_text, _ = write_poppler_evidence(pdf_path, evidence_dir, pages_expected)
    rendered = render_pages(pdf_path, evidence_dir)
    pdfimages_summary = write_pdfimages_evidence(pdf_path, evidence_dir)
    pages_data, plumber_text = extract_pdfplumber_data(pdf_path, evidence_dir)
    unpdf_text, unpdf_ok = run_unpdf(pdf_path, evidence_dir)
    title = infer_title(pdf_path, pages_data)
    pypdf_pages = extract_pypdf_page_texts(pdf_path)
    image_map = extract_relevant_images(pages_data, rendered, images_dir)
    markdown = make_markdown(pdf_path, doc_dir, pages_data, pypdf_pages, title, image_map)
    (doc_dir / "paper.md").write_text(markdown, encoding="utf-8")

    report = verification_report(
        pdf_path=pdf_path,
        doc_dir=doc_dir,
        pages_expected=pages_expected,
        markdown_text=markdown,
        poppler_text=poppler_text,
        plumber_text=plumber_text,
        unpdf_text=unpdf_text,
        unpdf_ok=unpdf_ok,
        pages_data=pages_data,
        image_map=image_map,
        pdfimages_summary=pdfimages_summary,
        rendered=rendered,
    )
    report["slug"] = slug
    report["markdown"] = str((doc_dir / "paper.md").relative_to(ROOT))
    print(
        "[done] {name}: pages={pages} crops={crops} poppler_recall={recall:.4f} warnings={warnings}".format(
            name=pdf_path.name,
            pages=pages_expected,
            crops=report["caption_crops_linked"],
            recall=report["poppler_token_recall"],
            warnings=len(report["warnings"]),
        )
    )
    return report


def write_root_summary(reports: list[dict[str, Any]]) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    summary_json = OUT_ROOT / "verification_summary.json"
    summary_json.write_text(json.dumps({"reports": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Markdown Conversion Verification Summary",
        "",
        f"Converted PDFs: {len(reports)}",
        "",
        "| PDF | Pages | Markdown | Crops | Poppler recall | pdfplumber recall | unpdf recall | Warnings |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in reports:
        unpdf_recall = report["unpdf_token_recall"] if report["unpdf_token_recall"] is not None else "n/a"
        lines.append(
            "| {pdf} | {pages} | `{md}` | {crops} | {pop:.4f} | {plumber:.4f} | {unpdf} | {warnings} |".format(
                pdf=report["pdf"].replace("|", "\\|"),
                pages=report["pages_expected"],
                md=report["markdown"],
                crops=report["caption_crops_linked"],
                pop=report["poppler_token_recall"],
                plumber=report["pdfplumber_token_recall"],
                unpdf=unpdf_recall,
                warnings=len(report["warnings"]),
            )
        )
    lines.append("")
    warned = [report for report in reports if report["warnings"]]
    if warned:
        lines.append("Warnings:")
        lines.append("")
        for report in warned:
            for warning in report["warnings"]:
                lines.append(f"- `{report['pdf']}`: {warning}")
    else:
        lines.append("No automated verification warnings.")
    lines.append("")
    (OUT_ROOT / "verification_summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PDFs to Markdown with evidence bundles.")
    parser.add_argument("pdfs", nargs="*", type=Path, help="PDF files to convert. Defaults to all PDFs in the current directory.")
    args = parser.parse_args()

    pdfs = args.pdfs or sorted(ROOT.glob("*.pdf")) + sorted(ROOT.glob("*.PDF"))
    if not pdfs:
        raise SystemExit("No PDFs found.")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    reports = []
    for pdf in pdfs:
        pdf_path = pdf if pdf.is_absolute() else ROOT / pdf
        slug = unique_slug(slugify(pdf_path.stem), seen)
        reports.append(convert_pdf(pdf_path, slug))
    write_root_summary(reports)
    print(f"[summary] {OUT_ROOT / 'verification_summary.md'}")


if __name__ == "__main__":
    main()
