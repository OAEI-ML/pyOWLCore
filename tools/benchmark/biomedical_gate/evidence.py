"""Generate and fail-closed validate WP23 one-document biomedical evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pyowl_core import BackendPreference

from ..comparators.fresh import run_fresh_subprocess
from ..component_canonicalization.evidence import load_report as load_component_report
from ..component_canonicalization.inputs import load_input_lock
from ..manifest import DEFAULT_MANIFEST, ROOT, Corpus, load_manifest
from ..report import write_json
from .contract import (
    ANONYMOUS_TELEMETRY_NAMES,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_STDERR_BYTES,
    PRIVATE_CASE_IDS,
    PUBLIC_CASE_IDS,
    REPORT_SCHEMA,
    WORKER_REQUEST_SCHEMA,
    BiomedicalGateError,
    validate_worker_result,
)

DEFAULT_REPORT = (
    ROOT / "reports" / "performance" / "native-redesign" / ("biomedical-one-document-gate.json")
)
DEFAULT_COMPONENT_REPORT = (
    ROOT / "reports" / "performance" / "component-canonicalization-v2" / "release.json"
)
DEFAULT_FIXED_CASE = (
    ROOT
    / "reports"
    / "performance"
    / "native-redesign"
    / "biomedical-cases"
    / "generated-component-scaling-functional.json"
)

_CASE_SCHEMA = "pyowl-core/biomedical-one-document-case/v1"
_CASE_FIELDS = frozenset(
    {
        "schema",
        "id",
        "status",
        "worker",
        "transport",
        "incident_baseline",
        "rss_comparison",
        "correctness_reference",
    }
)
_TRANSPORT_FIELDS = frozenset(
    {
        "parent_startup_to_ready_wall_ns",
        "parent_cpu_ns",
        "request_bytes",
        "stdout_bytes",
        "stderr_bytes",
    }
)
_REPORT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "manifest",
        "tooling",
        "same_machine_attested",
        "public_cases",
        "private_cases",
        "claims",
    }
)
_CLAIM_FIELDS = frozenset(
    {
        "public_one_document_loads_passed",
        "source_checksums_verified",
        "default_limits_used",
        "native_backend_forced",
        "counts_and_fingerprints_present",
        "structural_component_telemetry_complete",
        "component_release_cross_pin_passed",
        "fma_counts_are_anchor_only",
        "ncit_raised_limit_alpha_equivalence_passed",
        "same_machine_rss_gate_passed",
        "private_snomed_incident_claim",
        "portable_performance_claim",
        "release_gate_passed",
    }
)

_INCIDENT_BASELINES: dict[str, dict[str, object] | None] = {
    "generated-component-scaling-functional": None,
    "oaei-bioml-fma-2026": {
        "source_bytes": 208_047_132,
        "serialization": "rdfxml",
        "load_wall_ns": 131_717_000_000,
        "end_to_end_wall_ns": 428_810_000_000,
        "peak_rss_bytes": 2_520_932 * 1024,
        "chunk_count": 300,
        "observation_kind": "consumer-chunked-single-observation",
    },
    "oaei-bioml-ncit-2026": {
        "source_bytes": 747_403_746,
        "serialization": "rdfxml",
        "load_wall_ns": 269_680_000_000,
        "end_to_end_wall_ns": 271_000_000_000,
        "peak_rss_bytes": 4_375_028 * 1024,
        "chunk_count": 343,
        "observation_kind": "consumer-chunked-single-observation",
    },
}


def run_public_case(
    corpus_id: str,
    *,
    source_path: Path | None,
    expected_native_sha256: str,
    timeout: float,
) -> dict[str, object]:
    """Run one public case in an isolated subprocess and validate its result."""

    manifest = load_manifest()
    corpus = manifest.by_id(corpus_id)
    if corpus.id not in PUBLIC_CASE_IDS:
        raise BiomedicalGateError("run-public-case accepts only the three WP23 public inputs")
    request: dict[str, object] = {
        "schema": WORKER_REQUEST_SCHEMA,
        "corpus_id": corpus.id,
        "source_path": None if source_path is None else str(source_path.resolve()),
        "backend": BackendPreference.NATIVE.value,
        "expected_native_sha256": expected_native_sha256,
        "require_native_telemetry": True,
    }
    exchange = run_fresh_subprocess(
        (sys.executable, "-m", "tools.benchmark.biomedical_gate.worker"),
        request,
        timeout=timeout,
        max_request_bytes=MAX_REQUEST_BYTES,
        max_stdout_bytes=MAX_RESPONSE_BYTES,
        max_stderr_bytes=MAX_STDERR_BYTES,
        cwd=ROOT,
        env=_worker_environment(),
    )
    worker = validate_worker_result(exchange.result, request=request, corpus=corpus)
    row: dict[str, object] = {
        "schema": _CASE_SCHEMA,
        "id": corpus.id,
        "status": "pass",
        "worker": worker,
        "transport": {
            "parent_startup_to_ready_wall_ns": max(1, exchange.parent_wall_ns),
            "parent_cpu_ns": max(1, exchange.parent_cpu_ns),
            "request_bytes": exchange.request_bytes,
            "stdout_bytes": exchange.stdout_bytes,
            "stderr_bytes": exchange.stderr_bytes,
        },
        "incident_baseline": _INCIDENT_BASELINES[corpus.id],
        "rss_comparison": _rss_comparison(corpus.id, worker),
        "correctness_reference": _correctness_reference(corpus.id),
    }
    validate_case(row, corpus=corpus, expected_native_sha256=expected_native_sha256)
    return row


def assemble_report(
    cases: Sequence[Mapping[str, object]],
    *,
    expected_native_sha256: str,
    component_report_path: Path = DEFAULT_COMPONENT_REPORT,
) -> dict[str, object]:
    """Assemble the three sequential case files into one derived release record."""

    manifest = load_manifest()
    if tuple(row.get("id") for row in cases) != PUBLIC_CASE_IDS:
        raise BiomedicalGateError("public case order/set differs from WP23")
    validated = [
        validate_case(
            row,
            corpus=manifest.by_id(corpus_id),
            expected_native_sha256=expected_native_sha256,
        )
        for row, corpus_id in zip(cases, PUBLIC_CASE_IDS, strict=True)
    ]
    runtimes = [cast(Mapping[str, object], row["worker"])["runtime"] for row in validated]
    if any(runtime != runtimes[0] for runtime in runtimes[1:]):
        raise BiomedicalGateError("public cases were not run under one exact runtime identity")
    component_cross_pin = _component_cross_pin(validated[0], component_report_path)
    tooling = _tooling_identity(expected_native_sha256, component_cross_pin)
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "status": "public-load-pass-rss-and-alpha-not-evaluable",
        "manifest": {
            "path": DEFAULT_MANIFEST.relative_to(ROOT).as_posix(),
            "sha256": _file_sha256(DEFAULT_MANIFEST),
        },
        "tooling": tooling,
        "same_machine_attested": False,
        "public_cases": validated,
        "private_cases": _private_cases(),
        "claims": _derive_claims(validated, component_cross_pin),
    }
    validate_report(report, component_report_path=component_report_path)
    return report


def load_case(
    path: Path,
    *,
    expected_native_sha256: str,
) -> dict[str, Any]:
    value = _load_json(path, "biomedical case")
    corpus_id = _string(value.get("id"), "case.id")
    corpus = load_manifest().by_id(corpus_id)
    return validate_case(value, corpus=corpus, expected_native_sha256=expected_native_sha256)


def validate_case(
    value: Mapping[str, object],
    *,
    corpus: Corpus,
    expected_native_sha256: str,
) -> dict[str, Any]:
    row = _mapping(value, "case")
    _exact_fields(row, _CASE_FIELDS, "case")
    if row["schema"] != _CASE_SCHEMA or row["status"] != "pass" or row["id"] != corpus.id:
        raise BiomedicalGateError("case identity/status differs")
    request = {
        "schema": WORKER_REQUEST_SCHEMA,
        "corpus_id": corpus.id,
        "source_path": None,
        "backend": "native",
        "expected_native_sha256": expected_native_sha256,
        "require_native_telemetry": True,
    }
    worker = validate_worker_result(row["worker"], request=request, corpus=corpus)
    transport = _mapping(row["transport"], "case.transport")
    _exact_fields(transport, _TRANSPORT_FIELDS, "case.transport")
    for name in _TRANSPORT_FIELDS - {"stderr_bytes"}:
        _positive_integer(transport[name], f"case.transport.{name}")
    _nonnegative_integer(transport["stderr_bytes"], "case.transport.stderr_bytes")
    if row["incident_baseline"] != _INCIDENT_BASELINES[corpus.id]:
        raise BiomedicalGateError("case incident baseline differs from the normative record")
    if row["rss_comparison"] != _rss_comparison(corpus.id, worker):
        raise BiomedicalGateError("case RSS comparison is not derivable")
    if row["correctness_reference"] != _correctness_reference(corpus.id):
        raise BiomedicalGateError("case correctness qualification differs")
    return cast(dict[str, Any], row)


def load_report(
    path: Path = DEFAULT_REPORT,
    *,
    component_report_path: Path = DEFAULT_COMPONENT_REPORT,
) -> dict[str, object]:
    report = _load_json(path, "biomedical report")
    validate_report(report, component_report_path=component_report_path)
    return cast(dict[str, object], report)


def validate_report(
    value: Mapping[str, object],
    *,
    component_report_path: Path = DEFAULT_COMPONENT_REPORT,
) -> None:
    row = _mapping(value, "report")
    _exact_fields(row, _REPORT_FIELDS, "report")
    if row["schema"] != REPORT_SCHEMA:
        raise BiomedicalGateError("unsupported biomedical report schema")
    if row["status"] != "public-load-pass-rss-and-alpha-not-evaluable":
        raise BiomedicalGateError("biomedical report status overclaims its open gates")
    manifest_identity = _mapping(row["manifest"], "report.manifest")
    if dict(manifest_identity) != {
        "path": DEFAULT_MANIFEST.relative_to(ROOT).as_posix(),
        "sha256": _file_sha256(DEFAULT_MANIFEST),
    }:
        raise BiomedicalGateError("biomedical report manifest identity is stale")
    if row["same_machine_attested"] is not False:
        raise BiomedicalGateError("RSS comparison must remain unattested on this evidence host")
    tooling = _mapping(row["tooling"], "report.tooling")
    expected_native = _string(tooling.get("expected_native_sha256"), "tooling native digest")
    public_raw = row["public_cases"]
    if not isinstance(public_raw, list):
        raise BiomedicalGateError("report.public_cases must be an array")
    if tuple(_mapping(item, "public case").get("id") for item in public_raw) != PUBLIC_CASE_IDS:
        raise BiomedicalGateError("report public case order/set differs")
    manifest = load_manifest()
    public = [
        validate_case(
            _mapping(item, "public case"),
            corpus=manifest.by_id(corpus_id),
            expected_native_sha256=expected_native,
        )
        for item, corpus_id in zip(public_raw, PUBLIC_CASE_IDS, strict=True)
    ]
    runtimes = [cast(Mapping[str, object], case["worker"])["runtime"] for case in public]
    if any(runtime != runtimes[0] for runtime in runtimes[1:]):
        raise BiomedicalGateError("report cases have different runtime identities")
    component_cross_pin = _component_cross_pin(public[0], component_report_path)
    if dict(tooling) != _tooling_identity(expected_native, component_cross_pin):
        raise BiomedicalGateError("biomedical report tooling identity is stale")
    if row["private_cases"] != _private_cases():
        raise BiomedicalGateError("private SNOMED disposition differs")
    claims = _mapping(row["claims"], "report.claims")
    _exact_fields(claims, _CLAIM_FIELDS, "report.claims")
    if dict(claims) != _derive_claims(public, component_cross_pin):
        raise BiomedicalGateError("report claims are not derivable from validated evidence")


def _component_cross_pin(
    fixed_case: Mapping[str, object],
    component_report_path: Path,
) -> dict[str, object]:
    component_report_path = component_report_path.resolve()
    lock = load_input_lock()
    component_report = load_component_report(component_report_path, lock)
    if component_report.get("profile") != "release" or component_report.get("status") != "pass":
        raise BiomedicalGateError("component evidence is not a passing release profile")
    component_cases = component_report.get("cases")
    if not isinstance(component_cases, list):
        raise BiomedicalGateError("component evidence cases must be an array")
    fixed = next(
        (
            _mapping(item, "component case")
            for item in component_cases
            if _mapping(item, "component case").get("id") == "fixed-50000"
        ),
        None,
    )
    if fixed is None:
        raise BiomedicalGateError("component evidence lacks fixed-50000")
    worker = _mapping(fixed_case["worker"], "fixed worker")
    corpus = _mapping(worker["corpus"], "fixed worker corpus")
    output = _mapping(worker["output"], "fixed worker output")
    counts = _mapping(output["counts"], "fixed worker counts")
    fingerprints = _mapping(output["fingerprints"], "fixed worker fingerprints")
    document = _mapping(fingerprints["document"], "fixed document fingerprint")
    component_input = _mapping(fixed["input"], "component input")
    component_output = _mapping(fixed["output"], "component output")
    if (
        corpus["bytes"] != component_input["bytes"]
        or corpus["sha256"] != component_input["sha256"]
        or counts["axioms"] != component_output["axiom_count"]
        or document["digest"] != component_output["document_fingerprint_sha256"]
    ):
        raise BiomedicalGateError(
            "native fixed-50000 result differs from Python component evidence"
        )
    telemetry = _mapping(output["anonymous_components"], "fixed anonymous telemetry")
    shape = _mapping(fixed["shape"], "component shape")
    work = _mapping(fixed["work"], "component work")
    expected = {
        "native_anonymous_component_count": shape["component_count"],
        "native_anonymous_total_labels": shape["total_labels"],
        "native_anonymous_total_arcs": shape["total_arcs"],
        "native_anonymous_largest_component_labels": shape["largest_component_labels"],
        "native_anonymous_largest_component_arcs": shape["largest_component_arcs"],
        "native_anonymous_largest_component_roots": shape["largest_component_roots"],
        "native_anonymous_maximum_root_interval_span": shape["maximum_root_interval_span"],
        "native_anonymous_maximum_open_root_intervals": shape["maximum_open_root_intervals"],
        "native_anonymous_total_setup_work": work["total_setup_work"],
        "native_anonymous_total_refinement_work": work["total_refinement_work"],
        "native_anonymous_total_candidate_order_work": work["total_candidate_order_work"],
        "native_anonymous_total_canonical_work": work["total_canonical_work"],
        "native_anonymous_largest_component_work": work["largest_component_work"],
        "native_anonymous_maximum_refinement_rounds": work["maximum_refinement_rounds"],
        "native_anonymous_total_permutations_examined": work["total_permutations_examined"],
    }
    if any(telemetry[name] != expected_value for name, expected_value in expected.items()):
        raise BiomedicalGateError("native component telemetry differs from Python evidence")
    _positive_integer(
        telemetry["native_anonymous_accounted_bytes"],
        "native_anonymous_accounted_bytes",
    )
    return {
        "path": component_report_path.relative_to(ROOT).as_posix(),
        "sha256": _file_sha256(component_report_path),
        "schema": component_report["schema"],
        "profile": component_report["profile"],
        "input_lock_sha256": _mapping(component_report["input_lock"], "input lock")["sha256"],
        "fixed_case_id": "fixed-50000",
        "source_sha256": corpus["sha256"],
        "source_bytes": corpus["bytes"],
        "axiom_count": counts["axioms"],
        "document_fingerprint_sha256": document["digest"],
        "structural_work_telemetry_compared": list(expected),
        "accounted_bytes_qualification": (
            "native Session-accounted monotonic delta; not allocator peak; "
            "RSS is measured separately"
        ),
    }


def _tooling_identity(
    expected_native_sha256: str,
    component_cross_pin: Mapping[str, object],
) -> dict[str, object]:
    paths = (
        ROOT / "tools" / "benchmark" / "biomedical_gate" / "contract.py",
        ROOT / "tools" / "benchmark" / "biomedical_gate" / "worker.py",
        ROOT / "tools" / "benchmark" / "biomedical_gate" / "evidence.py",
    )
    return {
        "expected_native_sha256": expected_native_sha256,
        "files": [
            {"path": path.relative_to(ROOT).as_posix(), "sha256": _file_sha256(path)}
            for path in paths
        ],
        "component_release_cross_pin": dict(component_cross_pin),
    }


def _rss_comparison(corpus_id: str, worker: Mapping[str, object]) -> dict[str, object]:
    measurement = _mapping(worker["measurement"], "worker measurement")
    baseline = _INCIDENT_BASELINES[corpus_id]
    return {
        "same_machine_attested": False,
        "evaluable": False,
        "observed_peak_rss_bytes": measurement["fresh_process_peak_rss_bytes"],
        "baseline_peak_rss_bytes": None if baseline is None else baseline["peak_rss_bytes"],
        "passed": None,
        "reason": (
            "generated scaling case has no incident RSS baseline"
            if baseline is None
            else "incident and candidate machine identities were not jointly attested"
        ),
    }


def _correctness_reference(corpus_id: str) -> dict[str, object]:
    if corpus_id == "oaei-bioml-ncit-2026":
        return {
            "count_qualification": "candidate-observation-no-count-parity-oracle",
            "count_anchor": None,
            "alpha_equivalence": {
                "status": "not-run",
                "baseline_model_schema": 1,
                "candidate_model_schema": 2,
                "passed": None,
                "reason": (
                    "no checksum-pinned unmodified NCIt model-schema-1 raised-limit result "
                    "is available in the workspace; an isolated v0.1.1 raised-limit run and "
                    "independent alpha-equivalence comparison remain required"
                ),
                "blocks_release_gate": True,
            },
        }
    if corpus_id == "oaei-bioml-fma-2026":
        return {
            "count_qualification": "incident-regression-anchor-not-parity-oracle",
            "count_anchor": {
                "axioms": 791_162,
                "declarations": 104_942,
                "gate": False,
                "role": "reported-composed-workaround-regression-anchor-only",
            },
            "alpha_equivalence": {
                "status": "not-required-by-WP23-FMA-anchor",
                "baseline_model_schema": None,
                "candidate_model_schema": 2,
                "passed": None,
                "reason": (
                    "FMA composed counts are regression anchors only and are not parity proof"
                ),
                "blocks_release_gate": False,
            },
        }
    return {
        "count_qualification": "generator-exact-and-python-cross-pinned",
        "count_anchor": None,
        "alpha_equivalence": {
            "status": "not-applicable-generated-model-schema-2",
            "baseline_model_schema": None,
            "candidate_model_schema": 2,
            "passed": None,
            "reason": "generated release input has no historical model-schema-1 incident baseline",
            "blocks_release_gate": False,
        },
    }


def _private_cases() -> list[dict[str, object]]:
    rows = [
        {
            "id": PRIVATE_CASE_IDS[0],
            "status": "not-run",
            "format": "rdfxml",
            "source_bytes": 921_509_982,
            "source_sha256": None,
            "reason": "licensed source and private checksum manifest were not available",
            "blocks_public_gate": False,
            "blocks_private_incident_claim": True,
        },
        {
            "id": PRIVATE_CASE_IDS[1],
            "status": "not-run",
            "format": "functional",
            "source_bytes": 211_564_833,
            "source_sha256": None,
            "reason": "licensed source and private checksum manifest were not available",
            "blocks_public_gate": False,
            "blocks_private_incident_claim": True,
        },
    ]
    return rows


def _derive_claims(
    public: Sequence[Mapping[str, object]],
    component_cross_pin: Mapping[str, object],
) -> dict[str, object]:
    del component_cross_pin
    workers = [_mapping(case["worker"], "case worker") for case in public]
    contracts = [_mapping(worker["contract"], "worker contract") for worker in workers]
    outputs = [_mapping(worker["output"], "worker output") for worker in workers]
    return {
        "public_one_document_loads_passed": all(case["status"] == "pass" for case in public),
        "source_checksums_verified": True,
        "default_limits_used": all(
            contract["load_entrypoint_calls"] == 1
            and contract["consumer_chunking"] is False
            and contract["document_count"] == 1
            for contract in contracts
        ),
        "native_backend_forced": all(
            contract["requested_backend"] == "native" and contract["selected_backend"] == "native"
            for contract in contracts
        ),
        "counts_and_fingerprints_present": all(
            _mapping(output["counts"], "counts")["axioms"] > 0
            and set(_mapping(output["fingerprints"], "fingerprints"))
            == {"document", "structural", "logical", "signature"}
            for output in outputs
        ),
        "structural_component_telemetry_complete": all(
            set(_mapping(output["anonymous_components"], "anonymous telemetry"))
            == set(ANONYMOUS_TELEMETRY_NAMES)
            for output in outputs
        ),
        "component_release_cross_pin_passed": True,
        "fma_counts_are_anchor_only": True,
        "ncit_raised_limit_alpha_equivalence_passed": None,
        "same_machine_rss_gate_passed": None,
        "private_snomed_incident_claim": False,
        "portable_performance_claim": False,
        "release_gate_passed": False,
    }


def _worker_environment() -> dict[str, str]:
    environment = dict(os.environ)
    current = environment.get("PYTHONPATH")
    prefix = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    environment["PYTHONPATH"] = prefix if not current else prefix + os.pathsep + current
    return environment


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BiomedicalGateError(f"cannot load {label}: {error}") from error
    if not isinstance(value, dict):
        raise BiomedicalGateError(f"{label} root must be an object")
    return cast(dict[str, Any], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BiomedicalGateError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BiomedicalGateError(f"{label} must be an object")
    return cast(Mapping[str, Any], value)


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        raise BiomedicalGateError(
            f"{label} fields differ: missing={sorted(expected - observed)!r}, "
            f"unknown={sorted(observed - expected)!r}"
        )


def _positive_integer(value: object, label: str) -> int:
    selected = _nonnegative_integer(value, label)
    if selected < 1:
        raise BiomedicalGateError(f"{label} must be a positive integer")
    return selected


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BiomedicalGateError(f"{label} must be a nonnegative integer")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BiomedicalGateError(f"{label} must be a nonempty string")
    return value


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run-case", help="run one public native case")
    run.add_argument("--corpus-id", required=True, choices=PUBLIC_CASE_IDS)
    run.add_argument("--source-path", type=Path)
    run.add_argument("--expected-native-sha256", required=True)
    run.add_argument("--timeout", type=float, default=3_600.0)
    run.add_argument("--output", type=Path, required=True)
    assemble = subparsers.add_parser("assemble", help="assemble three case files")
    assemble.add_argument("--fixed-case", type=Path, required=True)
    assemble.add_argument("--fma-case", type=Path, required=True)
    assemble.add_argument("--ncit-case", type=Path, required=True)
    assemble.add_argument("--expected-native-sha256", required=True)
    assemble.add_argument("--component-report", type=Path, default=DEFAULT_COMPONENT_REPORT)
    assemble.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    check = subparsers.add_parser("check", help="validate existing evidence")
    check.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    check.add_argument("--component-report", type=Path, default=DEFAULT_COMPONENT_REPORT)
    check_case = subparsers.add_parser("check-case", help="validate one retained case")
    check_case.add_argument("--case", type=Path, default=DEFAULT_FIXED_CASE)
    check_case.add_argument("--expected-native-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_args(argv)
    try:
        if arguments.command == "run-case":
            row = run_public_case(
                arguments.corpus_id,
                source_path=arguments.source_path,
                expected_native_sha256=arguments.expected_native_sha256,
                timeout=arguments.timeout,
            )
            digest = write_json(arguments.output, row)
            print(f"wrote {arguments.output} sha256={digest}")
            return 0
        if arguments.command == "assemble":
            cases = [
                load_case(path, expected_native_sha256=arguments.expected_native_sha256)
                for path in (arguments.fixed_case, arguments.fma_case, arguments.ncit_case)
            ]
            report = assemble_report(
                cases,
                expected_native_sha256=arguments.expected_native_sha256,
                component_report_path=arguments.component_report,
            )
            digest = write_json(arguments.output, report)
            print(f"wrote {arguments.output} sha256={digest}")
            return 0
        if arguments.command == "check-case":
            load_case(
                arguments.case,
                expected_native_sha256=arguments.expected_native_sha256,
            )
            print(f"validated {arguments.case}")
            return 0
        load_report(arguments.report, component_report_path=arguments.component_report)
    except (BiomedicalGateError, OSError, ValueError) as error:
        print(f"biomedical evidence failed: {error}", file=sys.stderr)
        return 2
    print(f"validated {arguments.report}")
    return 0


__all__ = [
    "DEFAULT_COMPONENT_REPORT",
    "DEFAULT_FIXED_CASE",
    "DEFAULT_REPORT",
    "assemble_report",
    "load_case",
    "load_report",
    "main",
    "run_public_case",
    "validate_case",
    "validate_report",
]
