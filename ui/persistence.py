"""
Loading, validating and saving of the engine's YAML config files.

Two concerns are kept deliberately separate:

* **Fidelity** -- reading/writing the files while preserving comments, key order
  and formatting. This is handled with ``ruamel.yaml`` round-trip loading, which
  returns ``CommentedMap``/``CommentedSeq`` objects. The editor mutates *these*
  in place so untouched parts of the file (including comments) survive a save.

* **Validation** -- checking that a document is something the engine will accept,
  handled by the Pydantic models in ``ui.schema``. Because ``CommentedMap`` is a
  plain ``dict`` subclass, the raw ruamel document can be fed straight to Pydantic.

So the intended flow is: ``load_document`` (raw, editable) -> validate with
``validate_*`` whenever needed -> ``save_document`` (raw, comment-preserving).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ui import schema

# Default locations, anchored to the project root (parent of the ``ui`` package)
# so the config resolves regardless of the process's current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
STRATEGY_PATH = CONFIG_DIR / "strategy.yaml"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"


def _yaml() -> YAML:
    """A ruamel YAML configured for faithful round-tripping of our config files."""
    y = YAML()  # round-trip mode by default
    y.preserve_quotes = True
    y.width = 4096  # don't wrap long lines
    y.indent(mapping=2, sequence=4, offset=2)
    return y


# ---------------------------------------------------------------------------
# Raw (comment-preserving) document I/O
# ---------------------------------------------------------------------------


def load_document(path: str | Path) -> Any:
    """Load a YAML file as an editable, comment-preserving ruamel document."""
    with open(path, "r", encoding="utf-8") as f:
        return _yaml().load(f)


def dump_document(doc: Any) -> str:
    """Serialise a ruamel document back to a YAML string."""
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def save_document(doc: Any, path: str | Path) -> None:
    """Write a ruamel document back to disk, preserving comments/order."""
    with open(path, "w", encoding="utf-8") as f:
        _yaml().dump(doc, f)


# ---------------------------------------------------------------------------
# Validation (raw dict/document -> typed Pydantic model)
# ---------------------------------------------------------------------------


def validate_strategy(data: Any) -> schema.Strategy:
    """Validate a strategy document. Raises ``pydantic.ValidationError``."""
    return schema.Strategy.model_validate(data)


def validate_settings(data: Any) -> schema.Settings:
    """Validate a settings document. Raises ``pydantic.ValidationError``."""
    return schema.Settings.model_validate(data)


# ---------------------------------------------------------------------------
# Convenience: load + validate together
# ---------------------------------------------------------------------------


def load_strategy(path: str | Path = STRATEGY_PATH) -> tuple[Any, schema.Strategy]:
    """Return ``(raw_document, validated_model)`` for the strategy file."""
    doc = load_document(path)
    return doc, validate_strategy(doc)


def load_settings(path: str | Path = SETTINGS_PATH) -> tuple[Any, schema.Settings]:
    """Return ``(raw_document, validated_model)`` for the settings file."""
    doc = load_document(path)
    return doc, validate_settings(doc)


# ---------------------------------------------------------------------------
# Self-test: validate the live config files and check round-trip fidelity.
#   Run with:  kite-env/bin/python -m ui.persistence
# ---------------------------------------------------------------------------


def _selftest() -> int:
    from pydantic import ValidationError

    status = 0
    for path, validator in (
        (STRATEGY_PATH, validate_strategy),
        (SETTINGS_PATH, validate_settings),
    ):
        print(f"\n=== {path} ===")
        original = Path(path).read_text(encoding="utf-8")
        doc = _yaml().load(original)

        # 1) Validation
        try:
            model = validator(doc)
            print("  validation: OK")
        except ValidationError as e:
            status = 1
            print("  validation: FAILED")
            print("   ", str(e).replace("\n", "\n    "))
            continue

        # 2) Round-trip fidelity of the raw document (comments/order preserved).
        #    ruamel always terminates the file with a newline; the source files
        #    happen not to, so a lone trailing-newline delta is treated as OK.
        redumped = dump_document(doc)
        if redumped == original:
            print("  round-trip: byte-identical")
        elif redumped.rstrip("\n") == original.rstrip("\n"):
            print("  round-trip: identical (ruamel added a trailing newline at EOF)")
        else:
            status = 1
            print("  round-trip: DIFFERS (see below)")
            _print_diff(original, redumped)

        # 3) The typed model reproduces the same semantic content
        import yaml as _pyyaml  # only for a structural re-parse comparison

        model_dict = model.model_dump(by_alias=True)
        raw_dict = _pyyaml.safe_load(original)
        if _semantic_equal(model_dict, raw_dict):
            print("  model_dump: semantically matches source")
        else:
            print("  model_dump: differs semantically (defaults added is expected)")

    print("\nSelf-test", "PASSED" if status == 0 else "found issues")
    return status


def _print_diff(a: str, b: str) -> None:
    import difflib

    for line in difflib.unified_diff(
        a.splitlines(), b.splitlines(), "original", "redumped", lineterm=""
    ):
        print("   ", line)


def _semantic_equal(a: Any, b: Any) -> bool:
    """Compare two nested structures, ignoring keys that are None-only defaults."""
    if isinstance(a, dict) and isinstance(b, dict):
        a2 = {k: v for k, v in a.items() if v is not None}
        b2 = {k: v for k, v in b.items() if v is not None}
        if a2.keys() != b2.keys():
            return False
        return all(_semantic_equal(a2[k], b2[k]) for k in a2)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_semantic_equal(x, y) for x, y in zip(a, b))
    return a == b


if __name__ == "__main__":
    raise SystemExit(_selftest())
