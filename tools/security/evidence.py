"""Validate security controls against concrete tests and render their matrix."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "reports" / "security" / "control-matrix.json"


@dataclass(frozen=True, slots=True)
class Control:
    id: str
    area: str
    threat: str
    expectation: str
    evidence: tuple[str, ...]


CONTROLS = (
    Control(
        "SEC-XML-001",
        "parsing",
        "DTD, entity expansion, and XInclude",
        "fail closed with a typed sanitized error",
        (
            "tests/unit/formats/test_security_limits.py::test_xml_dtd_entity_and_xinclude_are_rejected",
            "tests/conformance/test_corpus.py::test_negative_and_hostile_corpus_has_stable_typed_outcomes",
        ),
    ),
    Control(
        "SEC-RDF-002",
        "parsing",
        "cyclic/shared RDF collection tails",
        "bounded traversal and stable rejection",
        (
            "tests/unit/formats/test_security_limits.py::test_cyclic_and_shared_rdf_collection_tails_are_rejected",
        ),
    ),
    Control(
        "SEC-DIAGNOSTIC-003",
        "diagnostics",
        "hostile literal and control-character disclosure",
        "bounded messages omit attacker payloads",
        (
            "tests/security/test_resource_faults.py::test_hostile_payload_is_absent_from_limit_diagnostics",
        ),
    ),
    Control(
        "SEC-PATH-004",
        "filesystem",
        "percent-encoded traversal, symlink, and URL-as-path",
        "reject before opening outside the selected root",
        (
            "tests/unit/resolver/test_directory_catalog.py::test_directory_rejects_traversal_and_symlinks",
            "tests/security/test_ssrf_path.py::test_path_and_url_sources_fail_closed",
        ),
    ),
    Control(
        "SEC-SSRF-005",
        "network",
        "metadata/private DNS resolution and implicit network",
        "DNS target is checked and offline mode performs no opener call",
        (
            "tests/security/test_ssrf_path.py::test_dns_rebinding_to_metadata_address_is_denied_before_open",
            "tests/security/test_ssrf_path.py::test_default_offline_load_never_calls_http_opener",
        ),
    ),
    Control(
        "SEC-HTTP-006",
        "network",
        "compressed response amplification and corrupt cache",
        "ratio/digest limits fail before cache publication",
        (
            "tests/security/test_ssrf_path.py::test_decompression_ratio_failure_does_not_publish_cache",
            "tests/unit/resolver/test_http.py::test_http_digest_decompression_and_size_fail_closed",
        ),
    ),
    Control(
        "SEC-WIRE-007",
        "wire",
        "truncation, field corruption, and arbitrary byte input",
        "no partial snapshot; errors remain in the public wire taxonomy",
        (
            "tests/fuzz/wire/test_property_fuzz.py::test_every_compact_wire_truncation_is_rejected",
            "tests/fuzz/wire/test_property_fuzz.py::test_arbitrary_wire_bytes_never_escape_the_wire_error_boundary",
            "tests/fuzz/wire/test_mutations.py::test_deterministic_single_bit_mutation_smoke_never_returns_partial_snapshot",
        ),
    ),
    Control(
        "SEC-CACHE-008",
        "cache",
        "publication failure, corruption, and concurrent writers",
        "atomic cleanup, quarantine, and one converged artifact",
        (
            "tests/security/test_resource_faults.py::test_zero_progress_write_leaves_no_partial_artifact",
            "tests/integration/cache/test_wire_cache.py::test_concurrent_publish_converges_and_corruption_is_quarantined",
        ),
    ),
    Control(
        "SEC-LIMIT-009",
        "resources",
        "source, literal, nesting, temporary, and deadline exhaustion",
        "named limits reject without partial success",
        (
            "tests/security/test_resource_faults.py::test_public_limit_matrix_reports_exact_named_budget",
            "tests/unit/formats/test_security_limits.py::test_source_literal_nesting_and_encoding_limits",
        ),
    ),
    Control(
        "SEC-FUZZ-010",
        "fuzzing",
        "parser/model/native panic or unchecked malformed values",
        "retained Python corpus and native parser/wire libFuzzer targets",
        (
            "tests/fuzz/parser/test_property_fuzz.py::test_mutated_parser_seeds_stay_inside_public_error_boundary",
            "tests/fuzz/model/test_properties.py::test_recursive_model_values_canonical_round_trip",
            "tests/fuzz/native/fuzz_targets/functional.rs",
            "tests/fuzz/native/fuzz_targets/wire.rs",
        ),
    ),
    Control(
        "SEC-NATIVE-011",
        "native",
        "panic, malformed FFI framing, and Python/native differential",
        "panic containment and forced-native parity",
        (
            "tests/native/foundation/test_native_boundary.py",
            "tests/conformance/test_differential.py::test_functional_python_native_and_independent_wire_cross_product",
        ),
    ),
    Control(
        "SEC-SUPPLY-012",
        "supply-chain",
        "Java/runtime artifact or untracked corpus introduction",
        "repository and corpus audits remain fail closed",
        (
            "tests/foundation/test_audits.py",
            "tests/conformance/test_corpus.py::test_provenance_hashes_and_generated_locks_are_current",
        ),
    ),
)


def _functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def validate_controls() -> tuple[Control, ...]:
    ids: set[str] = set()
    for control in CONTROLS:
        if control.id in ids:
            raise ValueError(f"duplicate security control: {control.id}")
        ids.add(control.id)
        if not control.evidence:
            raise ValueError(f"{control.id} has no evidence")
        for reference in control.evidence:
            relative, separator, node = reference.partition("::")
            path = ROOT / relative
            if not path.is_file():
                raise ValueError(f"{control.id} references missing evidence: {relative}")
            if separator and node not in _functions(path):
                raise ValueError(f"{control.id} references missing test node: {reference}")
    return CONTROLS


def build_matrix() -> dict[str, object]:
    controls = validate_controls()
    return {
        "control_count": len(controls),
        "controls": [
            {
                "area": item.area,
                "evidence": list(item.evidence),
                "expectation": item.expectation,
                "id": item.id,
                "threat": item.threat,
            }
            for item in controls
        ],
        "fuzz_corpus_sha256": hashlib.sha256(
            (ROOT / "tests" / "data" / "PROVENANCE.toml").read_bytes()
        ).hexdigest(),
        "native_fuzz_targets": ["functional", "wire"],
        "schema": 1,
    }


def render_matrix() -> str:
    return json.dumps(build_matrix(), indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    rendered = render_matrix()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print(f"stale security matrix: {OUTPUT}")
            return 1
        return 0
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered, encoding="utf-8")
        return 0
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CONTROLS", "Control", "build_matrix", "main", "render_matrix", "validate_controls"]
