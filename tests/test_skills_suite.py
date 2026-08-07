"""Static contract for the canonical root skills/ suite.

Fast, offline checks only: inventory, vendored-validator conformance,
agent interface metadata, icon constraints, and setup-script hygiene.
"""

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
VALIDATOR_PATH = REPO_ROOT / "tools" / "vendored" / "quick_validate.py"

EXPECTED_SKILLS = frozenset(
    {
        "legolize-model",
        "prepare-lego-input",
        "optimize-lego-build",
        "publish-lego-instructions",
        "analyze-lego-assembly",
        "repair-lego-assembly",
        "extend-lego-part-support",
        "render-ldraw",
        "inspect-instructions",
        "eval-corpus",
    }
)

EXPECTED_SCRIPTS = frozenset(
    {
        "legolize-model/scripts/setup-ldraw-library.sh",
        "legolize-model/scripts/setup-ldraw-library.ps1",
        "render-ldraw/scripts/setup-renderer.sh",
        "render-ldraw/scripts/setup-renderer.ps1",
    }
)

BRAND_COLOR = "#D92B2B"

FORBIDDEN_SUBSTRINGS = (
    "scripts/corpus.py",
    "scripts/eval_corpus.py",
    "check_instructions.py",
    ".claude/skills",
    "uv run legolization INPUT",
)

# The LEGO wordmark must not appear, but the project's own name
# (legolization, LEGOLIZATION_RENDERER, ...) legitimately contains the
# substring — scan for LEGO not followed by LIZATION, case-sensitively.
_TRADEMARK = re.compile(r"LEGO(?!LIZATION)")

SKILL_NAMES = sorted(EXPECTED_SKILLS)


def _load_validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("quick_validate", VALIDATOR_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frontmatter(skill_dir: Path) -> dict[str, object]:
    content = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---\n")
    body = content.split("---\n", 2)[1]
    loaded = yaml.safe_load(body)
    assert isinstance(loaded, dict)
    return loaded


def _skill_files() -> list[Path]:
    return sorted(path for path in SKILLS_DIR.rglob("*") if path.is_file())


def test_exact_ten_skill_inventory():
    directories = sorted(entry.name for entry in SKILLS_DIR.iterdir() if entry.is_dir())
    assert directories == SKILL_NAMES


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_vendored_validator_passes(name: str):
    validator = _load_validator()
    valid, message = validator.validate_skill(SKILLS_DIR / name)
    assert valid, message


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_frontmatter_name_and_license(name: str):
    frontmatter = _frontmatter(SKILLS_DIR / name)
    assert frontmatter["name"] == name
    assert frontmatter.get("license"), f"{name}: missing license"


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_openai_agent_interface(name: str):
    skill_dir = SKILLS_DIR / name
    payload = yaml.safe_load(
        (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
    )
    interface = payload["interface"]
    assert interface["display_name"]
    assert interface["short_description"]
    for key in ("icon_small", "icon_large"):
        icon = skill_dir / interface[key]
        assert icon.is_file(), f"{name}: {interface[key]} does not exist"
    assert interface["brand_color"] == BRAND_COLOR
    assert f"${name}" in interface["default_prompt"]
    assert payload["policy"]["allow_implicit_invocation"] is True


@pytest.mark.parametrize("name", SKILL_NAMES)
@pytest.mark.parametrize("icon", ["icon-small.svg", "icon-large.svg"])
def test_icon_svgs(name: str, icon: str):
    path = SKILLS_DIR / name / "assets" / icon
    text = path.read_text(encoding="utf-8")
    root = ET.fromstring(text)  # noqa: S314 - repository-authored icon files
    assert root.tag.endswith("svg")
    assert root.get("viewBox")
    assert BRAND_COLOR in text
    for banned in ("<text", "<image", "<script"):
        assert banned not in text, f"{path}: contains {banned}"


def test_no_forbidden_strings_anywhere_under_skills():
    for path in _skill_files():
        text = path.read_text(encoding="utf-8")
        for banned in FORBIDDEN_SUBSTRINGS:
            assert banned not in text, f"{path}: contains {banned!r}"
        match = _TRADEMARK.search(text)
        assert match is None, f"{path}: contains the LEGO wordmark"


def test_script_inventory_is_exactly_the_four_setup_scripts():
    scripts = {
        str(path.relative_to(SKILLS_DIR))
        for path in SKILLS_DIR.glob("*/scripts/*")
        if path.is_file()
    }
    assert scripts == set(EXPECTED_SCRIPTS)


@pytest.mark.parametrize(
    "relative",
    sorted(script for script in EXPECTED_SCRIPTS if script.endswith(".sh")),
)
def test_shell_scripts_are_strict(relative: str):
    text = (SKILLS_DIR / relative).read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


def test_legacy_claude_skills_directory_is_gone():
    assert not (REPO_ROOT / ".claude" / "skills").exists()
