"""Generate the checked-in constructor-to-normative-production coverage table."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "tests" / "generated" / "model" / "w3c-production-coverage.json"
OWL2 = "https://www.w3.org/TR/owl2-syntax/"
SWRL = "https://www.w3.org/Submission/SWRL/"

_SECTION_BY_CATEGORY = {
    "primitive": OWL2 + "#Entities.2C_Literals.2C_and_Anonymous_Individuals",
    "annotation": OWL2 + "#Annotations",
    "property_expression": OWL2 + "#Object_Property_Expressions",
    "sub_object_property_expression": OWL2 + "#Object_Property_Expressions",
    "facet_restriction": OWL2 + "#Data_Ranges",
    "data_range": OWL2 + "#Data_Ranges",
    "class_expression": OWL2 + "#Class_Expressions",
    "declaration_axiom": OWL2 + "#Declarations",
    "logical_axiom": OWL2 + "#Axioms",
    "annotation_axiom": OWL2 + "#Annotation_Axioms",
    "swrl_extension": SWRL + "#2.1",
}
_ENTITY_PRODUCTIONS = (
    "Class",
    "Datatype",
    "ObjectProperty",
    "DataProperty",
    "AnnotationProperty",
    "NamedIndividual",
)


def build_coverage() -> dict[str, object]:
    source = str(ROOT / "src")
    sys.path.insert(0, source)
    try:
        from pyowl_core.model.registry import CONSTRUCTOR_SPECS

        constructors = []
        for spec in CONSTRUCTOR_SPECS:
            productions = (
                _ENTITY_PRODUCTIONS if spec.constructor.__name__ == "Entity" else (spec.production,)
            )
            constructors.append(
                {
                    "category": spec.category,
                    "constructor": spec.constructor.__name__,
                    "fields": list(spec.fields),
                    "normative_source": _SECTION_BY_CATEGORY[spec.category],
                    "production": spec.production,
                    "productions": list(productions),
                    "schema_tag": spec.tag,
                    "schema_tag_name": spec.tag_name,
                }
            )
    finally:
        sys.path.remove(source)
    return {
        "constructor_count": len(constructors),
        "model_schema": 1,
        "note": "SWRL rows are extension productions, not OWL 2 axiom productions.",
        "production_count": sum(len(row["productions"]) for row in constructors),
        "rows": constructors,
    }


def render_coverage() -> str:
    return json.dumps(build_coverage(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if checked-in output is stale")
    args = parser.parse_args(argv)
    rendered = render_coverage()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"stale generated coverage table: {OUTPUT}", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_coverage", "main", "render_coverage"]
