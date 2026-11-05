#!/usr/bin/env python3
"""Compare protected-token multisets in two manuscript files.

This is a deliberately narrow guardrail. It can catch changed or missing values,
operators, units, identifiers, and numeric citation markers. It cannot detect
tokens reassigned to different claims or prove that two passages mean the same
thing.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Callable, Pattern


NUMBER_CORE = r"(?:\d+(?:,\d{3})*(?:\.\d+)*|\.\d+)(?:[eE][+\-]?\d+)?"
QUANTITY_NUMBER = rf"""
(?:{NUMBER_CORE})
(?:
    \s*[×x]\s*10\s*\^\s*[+\-−]?\d+
    |\s*\^\s*[+\-−]?\d+
)?
"""

NUMBER_RE = re.compile(
    rf"(?<![\w.]){NUMBER_CORE}(?:\s*[%‰])?"
)

COMPARISON_RE = re.compile(
    rf"(?:\b(?:P|q|alpha)\s*)?(?:<|>|≤|≥|=|~|≈)\s*[+\-−]?{NUMBER_CORE}",
    re.IGNORECASE,
)

SIGNED_VALUE_RE = re.compile(
    rf"(?<![\d.])[+\-−]\s*{NUMBER_CORE}",
)

UNCERTAINTY_OPERATOR_RE = re.compile(
    r"(?:±|∓|\+\s*/\s*[-−]|[-−]\s*/\s*\+)"
)

UNIT_SYMBOL = r"""
(?:
    mm\s*Hg
    |(?:mEq|meq|mmHg|mmhg|kPa|kpa|bpm|Hz|hz|CFU|cfu)
    |(?:nM|[µμ]M|uM|mM|M)
    |(?:pg|ng|mcg|[µμ]g|mg|kg|g)
    |(?:nL|nl|[µμ]L|[µμ]l|uL|ul|mL|ml|dL|dl|L|l)
    |(?:nmol|[µμ]mol|umol|mmol|mol)
    |(?:IU|iu|U|u)
    |(?:nm|[µμ]m|um|mm|cm|km|m)
    |(?:°C|°c|°F|°f)
    |(?:years?|months?|weeks?|days?|hours?|minutes?|seconds?)
    |(?:min|h|s)
)
"""

UNIT_ATOM = rf"""
(?:{UNIT_SYMBOL})(?:\s*\^\s*[+\-−]?\d+|[+\-−]\d+|[23])?
"""

UNIT = rf"""
(?:{UNIT_ATOM})
(?:\s*(?:/|[·⋅*])\s*(?:{NUMBER_CORE}\s*)?(?:{UNIT_ATOM}))*
"""

MEASUREMENT_RE = re.compile(
    rf"(?<![\w.])(?P<number>{QUANTITY_NUMBER})\s*(?:-\s*)?(?P<unit>(?:/\s*)?{UNIT})(?![A-Za-z])",
    re.VERBOSE,
)

CITATION_RE = re.compile(
    r"\[\s*\d+(?:\s*(?:,|;|[-–])\s*\d+)*\s*\]"
)

IDENTIFIER_RE = re.compile(
    r"""
    \b(?:
        10\.\d{4,9}/[-._;()/:A-Z0-9]+
        |NCT\d{8}
        |ISRCTN\d{8}
        |CRD420\d{6,}
        |ACTRN\d{14,}
        |EudraCT[\s:-]*\d{4}-\d{6}-\d{2}
        |PMID[\s:]*\d+
        |PMCID[\s:]*PMC\d+
        |PMC\d{5,}
        |R\s+version\s+\d+(?:\.\d+){1,3}
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

SUPERSCRIPT_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+")
SUPERSCRIPT_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")


def _expand_superscripts(text: str) -> str:
    return SUPERSCRIPT_RE.sub(
        lambda match: "^" + match.group(0).translate(SUPERSCRIPT_MAP), text
    )


def _canonicalize_uncertainty_notation(text: str) -> str:
    text = re.sub(r"\+\s*/\s*[-−]", "±", text)
    return re.sub(r"[-−]\s*/\s*\+", "∓", text)


def _normalize_number(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("−", "-"))


def _normalize_measurement(match: re.Match[str]) -> str:
    number = _normalize_number(match.group("number"))
    unit = match.group("unit")
    unit = unit.replace("μ", "u").replace("µ", "u").replace("²", "2")
    unit = re.sub(r"[\s^]", "", unit)
    return f"{number} {unit}"


def _normalize_operator_expression(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("−", "-")).lower()


def _normalize_uncertainty_operator(value: str) -> str:
    compact = re.sub(r"\s+", "", value.replace("−", "-"))
    if compact == "+/-":
        return "±"
    if compact == "-/+":
        return "∓"
    return compact


def _normalize_citation(value: str) -> str:
    return re.sub(r"\s+", "", value.replace("–", "-"))


def _normalize_identifier(value: str) -> str:
    return re.sub(r"\s+", " ", value.rstrip(".,;)").strip()).lower()


def _extract(
    text: str,
    pattern: Pattern[str],
    normalize: Callable[[str], str],
) -> Counter[str]:
    return Counter(normalize(match.group(0)) for match in pattern.finditer(text))


def extract_signals(text: str) -> dict[str, Counter[str]]:
    """Extract signal multisets from manuscript text."""
    text = _canonicalize_uncertainty_notation(_expand_superscripts(text))
    return {
        "numbers": _extract(text, NUMBER_RE, _normalize_number),
        "comparisons": _extract(
            text, COMPARISON_RE, _normalize_operator_expression
        ),
        "signed_values": _extract(
            text, SIGNED_VALUE_RE, _normalize_operator_expression
        ),
        "uncertainty_operators": _extract(
            text, UNCERTAINTY_OPERATOR_RE, _normalize_uncertainty_operator
        ),
        "measurements": Counter(
            _normalize_measurement(match) for match in MEASUREMENT_RE.finditer(text)
        ),
        "citation_markers": _extract(text, CITATION_RE, _normalize_citation),
        "identifiers": _extract(text, IDENTIFIER_RE, _normalize_identifier),
    }


def compare_texts(
    original: str, revised: str
) -> dict[str, dict[str, Counter[str]]]:
    """Return added and removed signals by category."""
    before = extract_signals(original)
    after = extract_signals(revised)
    differences: dict[str, dict[str, Counter[str]]] = {}

    for category in before:
        removed = before[category] - after[category]
        added = after[category] - before[category]
        if removed or added:
            differences[category] = {"removed": removed, "added": added}

    return differences


def _counter_items(counter: Counter[str]) -> list[dict[str, object]]:
    return [
        {"token": token, "count": count}
        for token, count in sorted(counter.items())
    ]


def _json_ready(
    differences: dict[str, dict[str, Counter[str]]]
) -> dict[str, dict[str, list[dict[str, object]]]]:
    return {
        category: {
            "removed": _counter_items(change["removed"]),
            "added": _counter_items(change["added"]),
        }
        for category, change in differences.items()
    }


def _format_counter(counter: Counter[str]) -> str:
    if not counter:
        return "none"
    return ", ".join(
        f"{token!r}" if count == 1 else f"{token!r} × {count}"
        for token, count in sorted(counter.items())
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare global multisets of numeric values, operators, "
            "measurements, identifiers, and numeric citation markers in an "
            "original and revised manuscript."
        )
    )
    parser.add_argument("original", type=Path, help="Original UTF-8 text file")
    parser.add_argument("revised", type=Path, help="Revised UTF-8 text file")
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        original = args.original.read_text(encoding="utf-8")
        revised = args.revised.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    differences = compare_texts(original, revised)

    if args.json:
        print(json.dumps(_json_ready(differences), indent=2, ensure_ascii=False))
    elif not differences:
        print("MATCH: protected-token multisets are unchanged.")
        print(
            "This global token check cannot detect values swapped between "
            "outcomes, citations moved between claims, or semantic changes."
        )
    else:
        print("MISMATCH: protected-token multisets differ; review each change.")
        for category, change in differences.items():
            print(f"\n{category}")
            print(f"  removed: {_format_counter(change['removed'])}")
            print(f"  added:   {_format_counter(change['added'])}")

    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
