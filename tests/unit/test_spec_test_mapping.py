"""Enforce the bidirectional mapping between `specs/<slug>.md` and `tests/test_<slug>_generated.py`.

Convention:

- Spec files live at `specs/<slug>.md` where `<slug>` is kebab-case.
- Generated test files live at `tests/test_<slug_underscored>_generated.py`.
- Every spec's FIRST line must be: `<!-- test: tests/test_<slug_underscored>_generated.py -->`
- Every generated test's FIRST line must be: `# spec: specs/<slug>.md`
- The slug on each side must match (after kebab<->snake conversion).

Specs without matching tests are allowed (the planner may produce a spec before
the generator has run). Generated tests without matching specs are NOT allowed —
a test claiming to be generated must link back to its source spec.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SPECS_DIR = REPO_DIR / "specs"
TESTS_DIR = REPO_DIR / "tests"

SPEC_HEADER_RE = re.compile(r"^<!--\s*test:\s*(?P<path>\S+?)\s*-->\s*$")
TEST_HEADER_RE = re.compile(r"^#\s*spec:\s*(?P<path>\S+?)\s*$")

TEST_FILENAME_RE = re.compile(r"^test_(?P<slug>.+)_generated\.py$")


def _kebab(slug_snake: str) -> str:
    return slug_snake.replace("_", "-")


def _snake(slug_kebab: str) -> str:
    return slug_kebab.replace("-", "_")


def _spec_files() -> list[Path]:
    if not SPECS_DIR.exists():
        return []
    return sorted(SPECS_DIR.glob("*.md"))


def _generated_test_files() -> list[Path]:
    return sorted(TESTS_DIR.glob("test_*_generated.py"))


def _spec_ids(path: Path) -> str:
    return path.name


def _test_ids(path: Path) -> str:
    return path.name


@pytest.mark.parametrize("spec", _spec_files(), ids=_spec_ids)
def test_spec_declares_matching_test_path(spec: Path) -> None:
    first_line = spec.read_text().splitlines()[0] if spec.read_text().strip() else ""
    match = SPEC_HEADER_RE.match(first_line)
    assert match, (
        f"{spec.relative_to(REPO_DIR)} must start with:\n"
        f"  <!-- test: tests/test_<slug>_generated.py -->\n"
        f"Got: {first_line!r}"
    )
    declared = match.group("path")
    expected = f"tests/test_{_snake(spec.stem)}_generated.py"
    assert declared == expected, (
        f"{spec.relative_to(REPO_DIR)}: declared test path {declared!r} does not match convention {expected!r}"
    )


@pytest.mark.parametrize("test_file", _generated_test_files(), ids=_test_ids)
def test_generated_test_declares_matching_spec_path(test_file: Path) -> None:
    first_line = test_file.read_text().splitlines()[0] if test_file.read_text().strip() else ""
    match = TEST_HEADER_RE.match(first_line)
    assert match, (
        f"{test_file.relative_to(REPO_DIR)} must start with:\n"
        f"  # spec: specs/<slug>.md\n"
        f"Got: {first_line!r}"
    )
    declared = match.group("path")
    name_match = TEST_FILENAME_RE.match(test_file.name)
    assert name_match, f"unexpected test filename: {test_file.name}"
    expected = f"specs/{_kebab(name_match.group('slug'))}.md"
    assert declared == expected, (
        f"{test_file.relative_to(REPO_DIR)}: declared spec path {declared!r} does not match convention {expected!r}"
    )


@pytest.mark.parametrize("test_file", _generated_test_files(), ids=_test_ids)
def test_generated_test_has_existing_spec(test_file: Path) -> None:
    name_match = TEST_FILENAME_RE.match(test_file.name)
    assert name_match, f"unexpected test filename: {test_file.name}"
    spec_path = SPECS_DIR / f"{_kebab(name_match.group('slug'))}.md"
    assert spec_path.exists(), (
        f"{test_file.relative_to(REPO_DIR)} is a generated test but spec {spec_path.relative_to(REPO_DIR)} is missing."
    )
