"""Capture and fail-closed validate the pinned WP20 DOID mapping failure."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from pyowl_core import (
    API_VERSION,
    MODEL_SCHEMA_VERSION,
    BackendPreference,
    ImportPolicy,
    LoadOptions,
    ParseLimits,
    RDFMappingReport,
    UnsupportedSyntaxError,
    __version__,
    parse_document,
)

from ..manifest import DEFAULT_MANIFEST, Corpus, ManifestError, load_manifest
from ..report import canonical_json_bytes, write_json

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = (
    ROOT / "reports" / "performance" / "native-redesign" / "doid-rdf-mapping-source-evidence.json"
)
SCHEMA = "pyowl-core/doid-rdf-mapping-source-evidence/v1"
CORPUS_ID = "oaei-bioml-doid-2026"
MAX_DIAGNOSTICS = 32
EXPECTED_TOTAL = 305_919
EXPECTED_CONSUMED = 305_901
EXPECTED_DROPPED = 18
EXPECTED_PREDICATES = (
    "http://purl.obolibrary.org/obo/OBI_9991118",
    "http://www.geneontology.org/formats/oboInOwl#created_by",
)
EXPECTED_EVIDENCE_SHA256 = "95977283a0092a1a8557536ade6bbf28e11b91ecdf609929fc7e7b0b2fe8ca79"

_IMPLEMENTATION_FILES = (
    "src/pyowl_core/api.py",
    "src/pyowl_core/backends/python/parser.py",
    "src/pyowl_core/config.py",
    "src/pyowl_core/document/provenance.py",
    "src/pyowl_core/exceptions.py",
    "src/pyowl_core/io/formats/rdf.py",
    "src/pyowl_core/io/formats/rdfxml.py",
)
_ROOT_FIELDS = frozenset(
    {"schema", "status", "source", "contract", "implementation", "result", "measurement"}
)
_SOURCE_FIELDS = frozenset({"corpus_id", "revision", "bytes", "sha256", "format"})
_CONTRACT_FIELDS = frozenset(
    {
        "backend",
        "imports",
        "strict_rdf_mapping",
        "max_diagnostics",
        "parse_entrypoint_calls",
    }
)
_IMPLEMENTATION_FIELDS = frozenset(
    {"pyowl_core_version", "api_version", "model_schema", "source_sha256"}
)
_RESULT_FIELDS = frozenset({"exception", "mapping_report"})
_EXCEPTION_FIELDS = frozenset(
    {
        "type",
        "code",
        "diagnostic_attached",
        "rdf_mapping_report_attached",
        "reification_evidence_count",
        "reification_issue_count",
        "reification_suppressed_count",
    }
)
_REPORT_FIELDS = frozenset(
    {
        "conformant",
        "total_triples",
        "consumed_triples",
        "dropped_triples",
        "retained_unconsumed_count",
        "suppressed_unconsumed_count",
        "rule_ids",
        "diagnostics_count",
        "predicates",
        "object_kinds",
        "evidence_sha256",
        "unconsumed",
    }
)
_EVIDENCE_FIELDS = frozenset({"subject", "predicate", "object", "object_kind"})
_MEASUREMENT_FIELDS = frozenset(
    {
        "sample_count",
        "wall_ns",
        "peak_rss_bytes",
        "wall_clock",
        "rss_semantics",
        "formal_performance_claim",
    }
)


class DoidEvidenceError(ValueError):
    """The pinned DOID evidence is missing, stale, ambiguous, or overclaims."""


def capture_strict_failure(
    source: Path,
    *,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    rss_peak_bytes: Callable[[], int] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Parse once and return the public exception/report plus bounded measurements."""

    if not isinstance(source, Path):
        raise TypeError("source must be Path")
    rss_reader = _rss_peak_bytes if rss_peak_bytes is None else rss_peak_bytes
    options = LoadOptions(
        backend=BackendPreference.PYTHON,
        imports=ImportPolicy.IGNORE,
        limits=ParseLimits(max_diagnostics=MAX_DIAGNOSTICS),
        allow_partial_rdf_mapping=False,
    )
    started = clock_ns()
    calls = 0
    failure: UnsupportedSyntaxError | None = None
    try:
        calls += 1
        parse_document(source, format="rdfxml", options=options)
    except UnsupportedSyntaxError as error:
        failure = error
        wall_ns = clock_ns() - started
        peak_rss = rss_reader()
    else:
        raise DoidEvidenceError("strict DOID mapping unexpectedly succeeded")
    if calls != 1:
        raise AssertionError("DOID evidence must invoke the parser exactly once")
    assert failure is not None
    report = failure.rdf_mapping_report
    if not isinstance(report, RDFMappingReport):
        raise DoidEvidenceError("first strict exception lacks RDFMappingReport")
    result: dict[str, object] = {
        "exception": {
            "type": type(failure).__name__,
            "code": failure.code,
            "diagnostic_attached": failure.diagnostic is not None,
            "rdf_mapping_report_attached": True,
            "reification_evidence_count": failure.reification_evidence_count,
            "reification_issue_count": failure.reification_issue_count,
            "reification_suppressed_count": failure.reification_suppressed_count,
        },
        "mapping_report": _report_row(report),
    }
    measurement = {
        "sample_count": 1,
        "wall_ns": wall_ns,
        "peak_rss_bytes": peak_rss,
        "wall_clock": "time.perf_counter_ns",
        "rss_semantics": "fresh-process resource.getrusage(RUSAGE_SELF) high-water bytes",
        "formal_performance_claim": False,
    }
    return result, measurement


def generate_evidence(source: Path, *, manifest: Path = DEFAULT_MANIFEST) -> dict[str, object]:
    """Verify the pinned input, parse it once, and build source-backend evidence."""

    corpus = _doid_corpus(manifest)
    _verify_corpus_contract(corpus)
    _verify_source(source, corpus)
    result, measurement = capture_strict_failure(source)
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "status": "pass",
        "source": _source_row(corpus),
        "contract": {
            "backend": "python",
            "imports": "ignore",
            "strict_rdf_mapping": True,
            "max_diagnostics": MAX_DIAGNOSTICS,
            "parse_entrypoint_calls": 1,
        },
        "implementation": {
            "pyowl_core_version": __version__,
            "api_version": list(API_VERSION),
            "model_schema": MODEL_SCHEMA_VERSION,
            "source_sha256": _implementation_hashes(),
        },
        "result": result,
        "measurement": measurement,
    }
    validate_evidence(payload, manifest=manifest)
    return payload


def load_evidence(path: Path, *, manifest: Path = DEFAULT_MANIFEST) -> dict[str, object]:
    """Load canonical JSON and reject every unknown, stale, or inconsistent field."""

    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DoidEvidenceError(f"cannot load DOID evidence: {error}") from error
    if not isinstance(value, dict):
        raise DoidEvidenceError("DOID evidence root must be an object")
    payload = cast(dict[str, object], value)
    if raw != canonical_json_bytes(payload):
        raise DoidEvidenceError("DOID evidence must use canonical repository JSON encoding")
    validate_evidence(payload, manifest=manifest)
    return payload


def validate_evidence(payload: Mapping[str, object], *, manifest: Path = DEFAULT_MANIFEST) -> None:
    """Validate exact input, runtime contract, public fields, and non-claiming metrics."""

    _exact_fields(payload, _ROOT_FIELDS, "evidence")
    if payload["schema"] != SCHEMA or payload["status"] != "pass":
        raise DoidEvidenceError("DOID evidence schema/status differs")
    corpus = _doid_corpus(manifest)
    _verify_corpus_contract(corpus)

    source = _mapping(payload["source"], "source")
    _exact_fields(source, _SOURCE_FIELDS, "source")
    _positive_integer(source["bytes"], "source.bytes")
    for field in ("corpus_id", "revision", "sha256", "format"):
        _string(source[field], f"source.{field}")
    if dict(source) != _source_row(corpus):
        raise DoidEvidenceError("DOID source identity differs from the corpus lock")

    contract = _mapping(payload["contract"], "contract")
    _exact_fields(contract, _CONTRACT_FIELDS, "contract")
    _boolean(contract["strict_rdf_mapping"], "contract.strict_rdf_mapping")
    _positive_integer(contract["max_diagnostics"], "contract.max_diagnostics")
    _positive_integer(contract["parse_entrypoint_calls"], "contract.parse_entrypoint_calls")
    if dict(contract) != {
        "backend": "python",
        "imports": "ignore",
        "strict_rdf_mapping": True,
        "max_diagnostics": MAX_DIAGNOSTICS,
        "parse_entrypoint_calls": 1,
    }:
        raise DoidEvidenceError("DOID parse contract differs from the strict source lane")

    implementation = _mapping(payload["implementation"], "implementation")
    _exact_fields(implementation, _IMPLEMENTATION_FIELDS, "implementation")
    api_version = implementation["api_version"]
    if not isinstance(api_version, list) or len(api_version) != 2:
        raise DoidEvidenceError("implementation.api_version must be a two-integer array")
    for index, value in enumerate(api_version):
        _nonnegative_integer(value, f"implementation.api_version[{index}]")
    _positive_integer(implementation["model_schema"], "implementation.model_schema")
    expected_implementation = {
        "pyowl_core_version": __version__,
        "api_version": list(API_VERSION),
        "model_schema": MODEL_SCHEMA_VERSION,
        "source_sha256": _implementation_hashes(),
    }
    if dict(implementation) != expected_implementation:
        raise DoidEvidenceError("DOID implementation identity is stale")

    result = _mapping(payload["result"], "result")
    _exact_fields(result, _RESULT_FIELDS, "result")
    _validate_exception(_mapping(result["exception"], "result.exception"))
    _validate_mapping_report(_mapping(result["mapping_report"], "result.mapping_report"))
    _validate_measurement(_mapping(payload["measurement"], "measurement"), corpus)


def _report_row(report: RDFMappingReport) -> dict[str, object]:
    examples = [
        {
            "subject": item.subject,
            "predicate": item.predicate,
            "object": item.object,
            "object_kind": item.object_kind,
        }
        for item in report.unconsumed
    ]
    return {
        "conformant": report.conformant,
        "total_triples": report.total_triples,
        "consumed_triples": report.consumed_triples,
        "dropped_triples": report.dropped_triples,
        "retained_unconsumed_count": len(examples),
        "suppressed_unconsumed_count": report.dropped_triples - len(examples),
        "rule_ids": list(report.rule_ids),
        "diagnostics_count": len(report.diagnostics),
        "predicates": sorted({item["predicate"] for item in examples}),
        "object_kinds": sorted({item["object_kind"] for item in examples}),
        "evidence_sha256": _examples_sha256(examples),
        "unconsumed": examples,
    }


def _validate_exception(value: Mapping[str, object]) -> None:
    _exact_fields(value, _EXCEPTION_FIELDS, "result.exception")
    for field in (
        "diagnostic_attached",
        "rdf_mapping_report_attached",
    ):
        _boolean(value[field], f"result.exception.{field}")
    _nonnegative_integer(
        value["reification_evidence_count"],
        "result.exception.reification_evidence_count",
    )
    expected = {
        "type": "UnsupportedSyntaxError",
        "code": "RDF_MAPPING_INCOMPLETE",
        "diagnostic_attached": False,
        "rdf_mapping_report_attached": True,
        "reification_evidence_count": 0,
        "reification_issue_count": None,
        "reification_suppressed_count": None,
    }
    if dict(value) != expected:
        raise DoidEvidenceError("first strict exception public fields differ")


def _validate_mapping_report(value: Mapping[str, object]) -> None:
    _exact_fields(value, _REPORT_FIELDS, "result.mapping_report")
    _boolean(value["conformant"], "result.mapping_report.conformant")
    for field in (
        "total_triples",
        "consumed_triples",
        "dropped_triples",
        "retained_unconsumed_count",
        "suppressed_unconsumed_count",
        "diagnostics_count",
    ):
        _nonnegative_integer(value[field], f"result.mapping_report.{field}")
    examples_value = value["unconsumed"]
    if not isinstance(examples_value, list):
        raise DoidEvidenceError("result.mapping_report.unconsumed must be an array")
    examples = tuple(
        _mapping(item, f"result.mapping_report.unconsumed[{index}]")
        for index, item in enumerate(examples_value)
    )
    for index, item in enumerate(examples):
        _exact_fields(item, _EVIDENCE_FIELDS, f"unconsumed[{index}]")
        for field in _EVIDENCE_FIELDS:
            _string(item[field], f"unconsumed[{index}].{field}")
        if item["object_kind"] not in {"iri", "blank", "literal"}:
            raise DoidEvidenceError(f"unconsumed[{index}].object_kind differs")
    digest = _examples_sha256(examples)
    if value["evidence_sha256"] != digest:
        raise DoidEvidenceError("retained evidence digest differs from its rows")
    if digest != EXPECTED_EVIDENCE_SHA256:
        raise DoidEvidenceError("retained evidence differs from the pinned DOID source run")
    expected_scalars = {
        "conformant": False,
        "total_triples": EXPECTED_TOTAL,
        "consumed_triples": EXPECTED_CONSUMED,
        "dropped_triples": EXPECTED_DROPPED,
        "retained_unconsumed_count": EXPECTED_DROPPED,
        "suppressed_unconsumed_count": 0,
        "rule_ids": ["OWL2-RDF-REVERSE"],
        "diagnostics_count": 0,
        "predicates": list(EXPECTED_PREDICATES),
        "object_kinds": ["literal"],
    }
    if any(value[field] != expected for field, expected in expected_scalars.items()):
        raise DoidEvidenceError("DOID mapping report fields differ from the pinned result")
    if len(examples) != EXPECTED_DROPPED:
        raise DoidEvidenceError("DOID retained evidence count differs")


def _validate_measurement(value: Mapping[str, object], corpus: Corpus) -> None:
    _exact_fields(value, _MEASUREMENT_FIELDS, "measurement")
    sample_count = _positive_integer(value["sample_count"], "measurement.sample_count")
    wall_ns = _positive_integer(value["wall_ns"], "measurement.wall_ns")
    peak_rss = _positive_integer(value["peak_rss_bytes"], "measurement.peak_rss_bytes")
    formal_claim = _boolean(
        value["formal_performance_claim"],
        "measurement.formal_performance_claim",
    )
    if (
        sample_count != 1
        or wall_ns < 1
        or peak_rss < corpus.counts.bytes
        or value["wall_clock"] != "time.perf_counter_ns"
        or value["rss_semantics"]
        != "fresh-process resource.getrusage(RUSAGE_SELF) high-water bytes"
        or formal_claim
    ):
        raise DoidEvidenceError("DOID measurement is invalid or overclaims performance")


def _source_row(corpus: Corpus) -> dict[str, object]:
    return {
        "corpus_id": corpus.id,
        "revision": corpus.revision,
        "bytes": corpus.counts.bytes,
        "sha256": corpus.sha256,
        "format": corpus.format.value,
    }


def _doid_corpus(manifest: Path) -> Corpus:
    return load_manifest(manifest).by_id(CORPUS_ID)


def _verify_corpus_contract(corpus: Corpus) -> None:
    if (
        corpus.id != CORPUS_ID
        or corpus.format.value != "rdfxml"
        or corpus.counts.bytes != 28_385_948
        or corpus.sha256 != "611355c445537fcf4bae2c519f1b3598af5a8fea793274316e35525b7d05e945"
        or corpus.counts.triples != EXPECTED_TOTAL
    ):
        raise DoidEvidenceError("pinned DOID corpus manifest fields differ")


def _verify_source(source: Path, corpus: Corpus) -> None:
    if not isinstance(source, Path):
        raise TypeError("source must be Path")
    try:
        size = source.stat().st_size
        digest = _file_sha256(source)
    except OSError as error:
        raise DoidEvidenceError(f"cannot verify pinned DOID source: {error}") from error
    if size != corpus.counts.bytes or digest != corpus.sha256:
        raise DoidEvidenceError("DOID source bytes/SHA-256 differ from the corpus lock")


def _implementation_hashes() -> dict[str, str]:
    return {name: _file_sha256(ROOT / name) for name in _IMPLEMENTATION_FILES}


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _examples_sha256(examples: Sequence[Mapping[str, object]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(examples))).hexdigest()


def _rss_peak_bytes() -> int:
    observed = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return observed if sys.platform == "darwin" else observed * 1024


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DoidEvidenceError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _exact_fields(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        raise DoidEvidenceError(
            f"{label} fields differ: missing={sorted(expected - observed)!r}, "
            f"unknown={sorted(observed - expected)!r}"
        )


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DoidEvidenceError(f"{label} must be a nonempty string")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise DoidEvidenceError(f"{label} must be a boolean")
    return value


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DoidEvidenceError(f"{label} must be a nonnegative integer")
    return value


def _positive_integer(value: object, label: str) -> int:
    selected = _nonnegative_integer(value, label)
    if selected < 1:
        raise DoidEvidenceError(f"{label} must be a positive integer")
    return selected


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DoidEvidenceError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="parse the pinned source once")
    generate.add_argument("source", type=Path)
    generate.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    check = commands.add_parser("check", help="validate checked evidence without parsing")
    check.add_argument("report", type=Path, nargs="?", default=DEFAULT_OUTPUT)
    check.add_argument("--source", type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "generate":
            payload = generate_evidence(arguments.source, manifest=arguments.manifest)
            digest = write_json(arguments.output, payload)
            print(f"DOID mapping evidence written: sha256={digest}")
        else:
            payload = load_evidence(arguments.report, manifest=arguments.manifest)
            if arguments.source is not None:
                _verify_source(arguments.source, _doid_corpus(arguments.manifest))
            digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
            print(f"DOID mapping evidence OK: sha256={digest}")
        return 0
    except (DoidEvidenceError, ManifestError, OSError) as error:
        print(f"DOID mapping evidence error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised by module entry point
    raise SystemExit(main())


__all__ = [
    "CORPUS_ID",
    "DEFAULT_OUTPUT",
    "SCHEMA",
    "DoidEvidenceError",
    "capture_strict_failure",
    "generate_evidence",
    "load_evidence",
    "main",
    "validate_evidence",
]
