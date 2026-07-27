"""Phase-separated, output-validating pyOWLCore performance harness."""

from __future__ import annotations

import argparse
import gc
import hashlib
import resource
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from pyowl_core import (
    IRI,
    BackendPreference,
    CanonicalSet,
    Class,
    Declaration,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    OntologyDocument,
    OntologySnapshot,
    OntologyView,
    OperationCancelledError,
    ParseLimits,
    ResourceLimitError,
    StructuralNode,
    apply_delta,
    canonical_bytes,
    clear_import_caches,
    coerce_snapshot,
    compose_views,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    open_snapshot,
    parse_document,
)
from pyowl_core.backends import native
from pyowl_core.cancellation import CancellationSource
from pyowl_core.index import AxiomTypeIndex
from pyowl_core.index.cache import clear_index_cache
from pyowl_core.io.resolver import MappingResolver

from .instrumentation import (
    AllocationResult,
    OperationCounters,
    arena_evidence,
    instrument_core_operations,
    measure_allocations,
)
from .manifest import (
    DEFAULT_MANIFEST,
    ROOT,
    Corpus,
    generated_bytes,
    load_manifest,
    manifest_fingerprint,
    verify_prepared,
)
from .metrics import Sample, summarize
from .report import ReportError, collect_environment, write_json
from .synthetic import adversarial_deep_functional, import_diamond

_MIB = 1024 * 1024
_INCREMENTAL_MINIMUM_BYTES = 16 * _MIB
_QUERY_LIMIT = 32
_ZERO_ALLOCATIONS = AllocationResult(0, 0)


class HarnessError(RuntimeError):
    """A benchmark input, operation, or validated output is invalid."""


@dataclass(frozen=True, slots=True)
class ScenarioOutput:
    fingerprint: str
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    kind: str
    corpus_id: str
    backend: str
    required: bool
    operation: Callable[[], object]
    validate: Callable[[object], ScenarioOutput]


def run_harness(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    cache_dir: Path | None = None,
    corpus_ids: Sequence[str] = ("generated-tiny-functional",),
    backends: Sequence[BackendPreference] = (BackendPreference.PYTHON,),
    warmups: int = 1,
    repetitions: int = 20,
    cache_state: str = "resident-bytes-warm-process",
) -> dict[str, Any]:
    """Run selected offline scenarios and return a complete comparison report."""

    _validate_run_options(warmups, repetitions, cache_state)
    manifest = load_manifest(manifest_path)
    selected = tuple(manifest.by_id(value) for value in corpus_ids)
    resolved_cache = cache_dir or ROOT / "benchmarks" / "results" / "corpora"
    payloads = {corpus.id: _payload(corpus, resolved_cache) for corpus in selected}
    environment = collect_environment(ROOT)
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="pyowl-core-benchmark-") as temporary:
        temporary_root = Path(temporary)
        for corpus in selected:
            source = payloads[corpus.id]
            for backend in backends:
                if not _backend_runnable(corpus, backend):
                    rows.append(_skipped_backend(corpus, backend))
                    continue
                scenarios = _corpus_scenarios(corpus, source, backend, temporary_root)
                rows.extend(
                    _run_scenario(scenario, warmups=warmups, repetitions=repetitions)
                    for scenario in scenarios
                )
        rows.extend(
            _run_scenario(scenario, warmups=warmups, repetitions=repetitions)
            for scenario in _global_scenarios(backends)
        )
    assertions = _acceptance_assertions(rows)
    report: dict[str, Any] = {
        "schema": "pyowl-core/performance-run/v1",
        "corpus_manifest_sha256": manifest_fingerprint(manifest_path),
        "environment": environment,
        "methodology": {
            "cache_state": cache_state,
            "warmups": warmups,
            "repetitions": repetitions,
            "safety_defaults": True,
            "network_during_timed_phases": False,
            "inputs": "hash-verified resident bytes",
            "process_isolation": False,
            "rss_semantics": (
                "absolute process high-water RSS after the timed operation; "
                "release gates require one scenario per fresh process"
            ),
            "allocation_semantics": (
                "one separate tracemalloc run; values are copied into samples as supplemental "
                "fields and never measured concurrently with wall time"
            ),
            "validation_semantics": "output validation and fingerprinting occur after timers stop",
            "profiler_attached": False,
            "gc": "full collection immediately before each timed operation",
            "minimum_release_repetitions": {"small": 20, "large": 5},
        },
        "corpora": [_corpus_metadata(corpus) for corpus in selected],
        "scenarios": rows,
        "assertions": assertions,
        "passed": all(cast(bool, item["passed"]) for item in assertions),
    }
    return report


def _corpus_scenarios(
    corpus: Corpus,
    source: bytes,
    backend: BackendPreference,
    temporary_root: Path,
) -> tuple[Scenario, ...]:
    options = _options(corpus.format, backend)

    def parse_operation() -> object:
        return parse_document(source, format=corpus.format, options=options)

    def parse_validation(value: object) -> ScenarioOutput:
        document = _document(value)
        _validate_document(document, corpus)
        return ScenarioOutput(
            document.document_fingerprint.hex,
            {
                "source_bytes": len(source),
                "axioms": len(document.axioms),
                "entities": len(document.signature(include_builtins=False)),
                "imports": len(document.direct_imports),
                "parser": document.provenance.parser,
            },
        )

    def load_operation() -> object:
        return load_snapshot(source, options=options)

    def snapshot_validation(value: object) -> ScenarioOutput:
        snapshot = _snapshot(value)
        _validate_snapshot(snapshot, corpus)
        return ScenarioOutput(
            snapshot.structural_fingerprint.hex,
            {
                "source_bytes": len(source),
                "axioms": snapshot.report.effective_axiom_count,
                "documents": snapshot.report.document_count,
                "backend": snapshot.report.backend,
            },
        )

    base = _snapshot(load_operation())
    _validate_snapshot(base, corpus)
    base_structural_bytes = sum(len(canonical_bytes(value)) for value in base.iter_axioms())
    build_base = _snapshot(load_operation())

    def index_build_operation() -> object:
        clear_index_cache(build_base)
        return build_base.view(AxiomTypeIndex)

    def index_validation(value: object) -> ScenarioOutput:
        index = _index(value)
        values = tuple(index.iter_all())
        if len(values) != base.report.effective_axiom_count:
            raise HarnessError("axiom index row count differs from snapshot")
        return ScenarioOutput(
            _node_sequence_fingerprint(values),
            {
                "rows": len(values),
                "strategy": index.report.strategy.value,
                "own_bytes": index.report.own_bytes,
                "shared_bytes": index.report.shared_bytes,
            },
        )

    index = base.view(AxiomTypeIndex)

    def index_query_operation() -> object:
        return tuple(index.iter_all(limit=_QUERY_LIMIT))

    def query_validation(value: object) -> ScenarioOutput:
        values = _axiom_tuple(value)
        return ScenarioOutput(
            _node_sequence_fingerprint(values),
            {"returned": len(values), "limit": _QUERY_LIMIT},
        )

    encoded = encode_snapshot(base)
    wire_digest = hashlib.sha256(encoded).hexdigest()

    def wire_encode_validation(value: object) -> ScenarioOutput:
        payload = _bytes(value)
        decoded = decode_snapshot(payload)
        if decoded.structural_fingerprint != base.structural_fingerprint:
            raise HarnessError("wire encode changed structural fingerprint")
        if encode_snapshot(decoded) != payload:
            raise HarnessError("wire encode is not byte-stable after decode")
        return ScenarioOutput(
            hashlib.sha256(payload).hexdigest(),
            {"wire_bytes": len(payload), "snapshot_fingerprint": base.structural_fingerprint.hex},
        )

    def wire_decode_validation(value: object) -> ScenarioOutput:
        decoded = _snapshot(value)
        if decoded.structural_fingerprint != base.structural_fingerprint:
            raise HarnessError("wire decode changed structural fingerprint")
        if encode_snapshot(decoded) != encoded:
            raise HarnessError("wire decode changed canonical wire bytes")
        return ScenarioOutput(
            decoded.structural_fingerprint.hex,
            {"wire_bytes": len(encoded), "wire_sha256": wire_digest},
        )

    wire_path = temporary_root / f"{corpus.id}-{backend.value}.pyocore"
    wire_path.write_bytes(encoded)

    def mmap_open_validation(value: object) -> ScenarioOutput:
        mapped = _mapped(value)
        try:
            fingerprint = mapped.structural_fingerprint.hex
            if fingerprint != base.structural_fingerprint.hex:
                raise HarnessError("mmap metadata fingerprint differs from source snapshot")
            return ScenarioOutput(
                fingerprint,
                {"wire_bytes": len(encoded), "metadata_only": True},
            )
        finally:
            mapped.close()

    def mmap_query_operation() -> object:
        mapped = open_snapshot(wire_path)
        values = tuple(mapped.view(AxiomTypeIndex).iter_all(limit=1))
        return mapped, values

    def mmap_query_validation(value: object) -> ScenarioOutput:
        mapped, values = _mapped_query(value)
        try:
            return ScenarioOutput(
                _node_sequence_fingerprint(values),
                {
                    "returned": len(values),
                    "snapshot_fingerprint": mapped.structural_fingerprint.hex,
                },
            )
        finally:
            mapped.close()

    addition = Declaration(Class(IRI(f"https://example.org/wp10/{corpus.id}#Added")))
    delta = OntologyDelta(add_axioms=CanonicalSet((addition,)))

    def overlay_validation(value: object) -> ScenarioOutput:
        overlay = cast(OntologyView, value)
        evidence = arena_evidence(overlay, (base,), _ZERO_ALLOCATIONS)
        if not evidence.identity_preserved:
            raise HarnessError("overlay did not preserve its base arena identity")
        return ScenarioOutput(
            overlay.structural_fingerprint.hex,
            {
                "base_structural_bytes": base_structural_bytes,
                "incremental_limit_bytes": _incremental_limit(base_structural_bytes),
                "arena_identity": evidence.identity_preserved,
            },
        )

    overlay = apply_delta(base, delta)
    overlay_index = overlay.view(AxiomTypeIndex)

    def overlay_query_operation() -> object:
        return tuple(overlay_index.iter_all(limit=_QUERY_LIMIT))

    peer = load_snapshot(
        _peer_source(corpus.id), options=_options(DocumentFormat.FUNCTIONAL, backend)
    )
    peer_structural_bytes = sum(len(canonical_bytes(value)) for value in peer.iter_axioms())

    def composite_validation(value: object) -> ScenarioOutput:
        composite = cast(OntologyView, value)
        evidence = arena_evidence(composite, (base, peer), _ZERO_ALLOCATIONS)
        if not evidence.identity_preserved:
            raise HarnessError("composite did not preserve member arena identities")
        structural_bytes = base_structural_bytes + peer_structural_bytes
        return ScenarioOutput(
            composite.structural_fingerprint.hex,
            {
                "base_structural_bytes": structural_bytes,
                "incremental_limit_bytes": _incremental_limit(structural_bytes),
                "arena_identity": evidence.identity_preserved,
                "members": 2,
            },
        )

    composite = compose_views(base, peer, roles=("source", "target"))
    composite_index = composite.view(AxiomTypeIndex)

    def composite_query_operation() -> object:
        return tuple(composite_index.iter_all(limit=_QUERY_LIMIT))

    def handoff_operation() -> object:
        with instrument_core_operations() as counters:
            selected = coerce_snapshot(base)
        return selected, counters

    def handoff_validation(value: object) -> ScenarioOutput:
        selected, counters = _handoff(value)
        counters.assert_handoff_zero()
        if selected is not base:
            raise HarnessError("coerce_snapshot did not preserve exact view identity")
        return ScenarioOutput(
            selected.structural_fingerprint.hex,
            {"identity_preserved": True, "counters": counters.as_dict()},
        )

    prefix = f"{corpus.id}/{backend.value}"
    required = backend is not BackendPreference.NATIVE
    return (
        Scenario(
            f"{prefix}/parse-document",
            "parse",
            corpus.id,
            backend.value,
            required,
            parse_operation,
            parse_validation,
        ),
        Scenario(
            f"{prefix}/load-freeze",
            "parse",
            corpus.id,
            backend.value,
            required,
            load_operation,
            snapshot_validation,
        ),
        Scenario(
            f"{prefix}/index-build",
            "index",
            corpus.id,
            backend.value,
            required,
            index_build_operation,
            index_validation,
        ),
        Scenario(
            f"{prefix}/index-warm-query",
            "query",
            corpus.id,
            backend.value,
            required,
            index_query_operation,
            query_validation,
        ),
        Scenario(
            f"{prefix}/wire-encode",
            "wire",
            corpus.id,
            backend.value,
            required,
            lambda: encode_snapshot(base),
            wire_encode_validation,
        ),
        Scenario(
            f"{prefix}/wire-decode",
            "wire",
            corpus.id,
            backend.value,
            required,
            lambda: decode_snapshot(encoded),
            wire_decode_validation,
        ),
        Scenario(
            f"{prefix}/mmap-open",
            "mmap",
            corpus.id,
            backend.value,
            required,
            lambda: open_snapshot(wire_path),
            mmap_open_validation,
        ),
        Scenario(
            f"{prefix}/mmap-first-query",
            "mmap",
            corpus.id,
            backend.value,
            required,
            mmap_query_operation,
            mmap_query_validation,
        ),
        Scenario(
            f"{prefix}/overlay-create-1",
            "overlay",
            corpus.id,
            backend.value,
            required,
            lambda: apply_delta(base, delta),
            overlay_validation,
        ),
        Scenario(
            f"{prefix}/overlay-warm-query",
            "query",
            corpus.id,
            backend.value,
            required,
            overlay_query_operation,
            query_validation,
        ),
        Scenario(
            f"{prefix}/composite-create-2",
            "composite",
            corpus.id,
            backend.value,
            required,
            lambda: compose_views(base, peer, roles=("source", "target")),
            composite_validation,
        ),
        Scenario(
            f"{prefix}/composite-warm-query",
            "query",
            corpus.id,
            backend.value,
            required,
            composite_query_operation,
            query_validation,
        ),
        Scenario(
            f"{prefix}/consumer-handoff",
            "handoff",
            corpus.id,
            backend.value,
            required,
            handoff_operation,
            handoff_validation,
        ),
    )


def _global_scenarios(backends: Sequence[BackendPreference]) -> tuple[Scenario, ...]:
    scenarios: list[Scenario] = [_adversarial_limit_scenario(), _adversarial_cancel_scenario()]
    for backend in backends:
        if (
            backend is BackendPreference.NATIVE
            and not native.probe("parse-functional-v1").available
        ):
            continue
        scenarios.append(_import_diamond_scenario(backend))
    return tuple(scenarios)


def _import_diamond_scenario(backend: BackendPreference) -> Scenario:
    root, mapping = import_diamond()
    shared_digest = hashlib.sha256(
        next(value for key, value in mapping.items() if key.endswith("/shared"))
    ).hexdigest()

    def operation() -> object:
        clear_import_caches()
        with instrument_core_operations() as counters:
            snapshot = load_snapshot(
                root,
                options=LoadOptions(
                    format=DocumentFormat.FUNCTIONAL,
                    imports=ImportPolicy.RESOLVE_LOCAL,
                    backend=backend,
                ),
                resolver=MappingResolver(cast(Mapping[IRI | str, Any], mapping)),
            )
        return snapshot, counters

    def validate(value: object) -> ScenarioOutput:
        snapshot, counters = _instrumented_snapshot(value)
        digests = [document.provenance.source_sha256.hex() for document in snapshot.documents]
        if len(snapshot.documents) != 4 or digests.count(shared_digest) != 1:
            raise HarnessError("import diamond did not retain one shared physical document")
        backend_counts: dict[str, int] = {}
        for document in snapshot.documents:
            selected = document.provenance.backend
            backend_counts[selected] = backend_counts.get(selected, 0) + 1
        expected_document_backend = (
            "native" if backend is BackendPreference.NATIVE else "python"
        )
        if (
            snapshot.report.document_count != 4
            or snapshot.report.backend != expected_document_backend
        ):
            raise HarnessError(
                "import diamond retained-report evidence is incompatible: "
                f"backend={snapshot.report.backend!r}, "
                f"documents={snapshot.report.document_count}"
            )
        if backend_counts != {expected_document_backend: 4}:
            raise HarnessError(
                "import diamond retained incompatible parser provenance: "
                f"{dict(sorted(backend_counts.items()))!r}"
            )
        expected_python_calls = 4 if backend is BackendPreference.PYTHON else 0
        if counters.parser_calls != expected_python_calls:
            raise HarnessError(
                "import diamond crossed PythonParser.parse "
                f"{counters.parser_calls} times, expected {expected_python_calls} "
                f"for backend {backend.value}"
            )
        return ScenarioOutput(
            snapshot.structural_fingerprint.hex,
            {
                "documents": len(snapshot.documents),
                "document_backend_counts": dict(sorted(backend_counts.items())),
                "report_backend": snapshot.report.backend,
                "reported_documents": snapshot.report.document_count,
                "shared_digest": shared_digest,
                "shared_parse_count": digests.count(shared_digest),
                "counters": counters.as_dict(),
            },
        )

    return Scenario(
        f"generated-import-diamond/{backend.value}/load-closure",
        "imports",
        "generated-import-diamond",
        backend.value,
        backend is not BackendPreference.NATIVE,
        operation,
        validate,
    )


def _adversarial_limit_scenario() -> Scenario:
    source = adversarial_deep_functional(64)

    def operation() -> object:
        try:
            parse_document(
                source,
                format=DocumentFormat.FUNCTIONAL,
                options=LoadOptions(
                    backend=BackendPreference.PYTHON,
                    limits=ParseLimits(max_nesting_depth=32),
                ),
            )
        except ResourceLimitError as error:
            return error.code
        raise HarnessError("adversarial depth input unexpectedly succeeded")

    return Scenario(
        "generated-adversarial-deep/python/depth-limit",
        "adversarial",
        "generated-adversarial-deep",
        "python",
        True,
        operation,
        _error_code_validation,
    )


def _adversarial_cancel_scenario() -> Scenario:
    source = adversarial_deep_functional(64)

    def operation() -> object:
        cancellation = CancellationSource()
        cancellation.cancel("WP10 pre-cancellation")
        try:
            load_snapshot(
                source,
                options=LoadOptions(
                    format=DocumentFormat.FUNCTIONAL,
                    imports=ImportPolicy.IGNORE,
                    backend=BackendPreference.PYTHON,
                ),
                cancellation_token=cancellation.token,
            )
        except OperationCancelledError as error:
            return error.code
        raise HarnessError("pre-cancelled adversarial load unexpectedly succeeded")

    return Scenario(
        "generated-adversarial-deep/python/pre-cancel",
        "adversarial",
        "generated-adversarial-deep",
        "python",
        True,
        operation,
        _error_code_validation,
    )


def _run_scenario(scenario: Scenario, *, warmups: int, repetitions: int) -> dict[str, Any]:
    expected = scenario.validate(scenario.operation())
    for _ in range(warmups):
        observed = scenario.validate(scenario.operation())
        _same_output(scenario.id, expected, observed)
    allocation_value, allocations = measure_allocations(scenario.operation)
    allocation_output = scenario.validate(allocation_value)
    _same_output(scenario.id, expected, allocation_output)
    del allocation_value
    samples: list[Sample] = []
    for _ in range(repetitions):
        gc.collect()
        cpu_started = time.process_time_ns()
        wall_started = time.perf_counter_ns()
        value = scenario.operation()
        wall_ns = time.perf_counter_ns() - wall_started
        cpu_ns = time.process_time_ns() - cpu_started
        rss = _rss_peak_bytes()
        observed = scenario.validate(value)
        _same_output(scenario.id, expected, observed)
        del value
        samples.append(
            Sample(
                wall_ns,
                cpu_ns,
                allocations.current_bytes,
                allocations.peak_bytes,
                rss,
                observed.fingerprint,
            )
        )
    metrics = {
        name: summarize(getattr(sample, name) for sample in samples).as_dict()
        for name in (
            "wall_ns",
            "cpu_ns",
            "allocated_current_bytes",
            "allocated_peak_bytes",
            "rss_peak_bytes",
        )
    }
    details = dict(expected.details)
    details["separate_allocation_run"] = asdict(allocations)
    if scenario.kind in {"parse", "wire"} and "source_bytes" in details:
        median_ns = cast(float, cast(Mapping[str, Any], metrics["wall_ns"])["median"])
        details["source_mib_per_second"] = _throughput(
            cast(int, details["source_bytes"]), median_ns
        )
    return {
        "id": scenario.id,
        "kind": scenario.kind,
        "corpus_id": scenario.corpus_id,
        "backend": scenario.backend,
        "required": scenario.required,
        "status": "ok",
        "metrics": metrics,
        "samples": [sample.as_dict() for sample in samples],
        "output": {"fingerprint": expected.fingerprint, **details},
    }


def _acceptance_assertions(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    required_ok = all(row.get("status") == "ok" for row in rows if row.get("required") is True)
    assertions.append({"name": "all-required-scenarios-runnable", "passed": required_ok})
    parity_groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in rows:
        if row.get("status") != "ok":
            continue
        output = cast(Mapping[str, Any], row["output"])
        row_id = cast(str, row["id"])
        phase = row_id.rsplit("/", 1)[-1]
        parity_groups.setdefault((cast(str, row["corpus_id"]), phase), []).append(
            (cast(str, row["backend"]), cast(str, output["fingerprint"]))
        )
        if row.get("kind") in {"overlay", "composite"}:
            allocation = cast(Mapping[str, Any], output["separate_allocation_run"])
            observed = cast(int, allocation["peak_bytes"])
            limit = cast(int, output["incremental_limit_bytes"])
            assertions.append(
                {
                    "name": f"{row_id}-incremental-allocation",
                    "passed": observed <= limit and output.get("arena_identity") is True,
                    "observed_bytes": observed,
                    "limit_bytes": limit,
                }
            )
        if row.get("kind") == "handoff":
            allocation = cast(Mapping[str, Any], output["separate_allocation_run"])
            assertions.append(
                {
                    "name": f"{row_id}-identity-and-zero-work",
                    "passed": output.get("identity_preserved") is True
                    and not any(cast(Mapping[str, int], output["counters"]).values())
                    and cast(int, allocation["peak_bytes"]) < _INCREMENTAL_MINIMUM_BYTES,
                    "peak_bytes": allocation["peak_bytes"],
                }
            )
        if row.get("kind") == "imports":
            assertions.append(
                {
                    "name": f"{row_id}-shared-source-parsed-once",
                    "passed": output.get("shared_parse_count") == 1,
                }
            )
    for (corpus_id, phase), values in sorted(parity_groups.items()):
        if len(values) < 2:
            continue
        fingerprints = {value for _backend, value in values}
        assertions.append(
            {
                "name": f"{corpus_id}/{phase}-backend-result-parity",
                "passed": len(fingerprints) == 1,
                "backends": [backend for backend, _fingerprint in values],
                "fingerprints": sorted(fingerprints),
            }
        )
    return assertions


def _payload(corpus: Corpus, cache_dir: Path) -> bytes:
    if corpus.source == "generated":
        return generated_bytes(corpus)
    path = cache_dir / corpus.filename
    if not path.is_file():
        raise HarnessError(
            f"prepared corpus is absent: {corpus.id}; run tools.benchmark.manifest --prepare"
        )
    verify_prepared(corpus, path)
    try:
        return path.read_bytes()
    except OSError as error:
        raise HarnessError(f"cannot read prepared corpus {corpus.id}: {error}") from error


def _backend_runnable(corpus: Corpus, backend: BackendPreference) -> bool:
    if backend is not BackendPreference.NATIVE:
        return True
    return (
        corpus.format is DocumentFormat.FUNCTIONAL and native.probe("parse-functional-v1").available
    )


def _skipped_backend(corpus: Corpus, backend: BackendPreference) -> dict[str, Any]:
    probe = native.probe("parse-functional-v1")
    reason = (
        f"native parser does not advertise {corpus.format.value}"
        if corpus.format is not DocumentFormat.FUNCTIONAL
        else probe.reason or "native parser unavailable"
    )
    return {
        "id": f"{corpus.id}/{backend.value}/backend-availability",
        "kind": "availability",
        "corpus_id": corpus.id,
        "backend": backend.value,
        "required": False,
        "status": "skipped",
        "reason": reason,
    }


def _options(format: DocumentFormat, backend: BackendPreference) -> LoadOptions:
    return LoadOptions(format=format, imports=ImportPolicy.IGNORE, backend=backend)


def _validate_document(document: OntologyDocument, corpus: Corpus) -> None:
    if len(document.axioms) != corpus.counts.axioms:
        raise HarnessError(
            f"{corpus.id}: expected {corpus.counts.axioms} axioms, got {len(document.axioms)}"
        )
    if len(document.direct_imports) != corpus.counts.imports:
        raise HarnessError(
            f"{corpus.id}: expected {corpus.counts.imports} imports, "
            f"got {len(document.direct_imports)}"
        )
    report = document.rdf_mapping_report
    if report is not None and report.total_triples != corpus.counts.triples:
        raise HarnessError(
            f"{corpus.id}: expected {corpus.counts.triples} triples, got {report.total_triples}"
        )
    if corpus.source == "generated" and corpus.generator == "equivalent-chain":
        entities = len(document.signature(include_builtins=False))
        if entities != corpus.counts.entities:
            raise HarnessError(
                f"{corpus.id}: expected {corpus.counts.entities} entities, got {entities}"
            )


def _validate_snapshot(snapshot: OntologySnapshot, corpus: Corpus) -> None:
    if snapshot.report.effective_axiom_count != corpus.counts.axioms:
        raise HarnessError(
            f"{corpus.id}: snapshot axiom count differs from manifest: "
            f"{snapshot.report.effective_axiom_count}"
        )


def _same_output(identifier: str, expected: ScenarioOutput, observed: ScenarioOutput) -> None:
    if observed.fingerprint != expected.fingerprint:
        raise HarnessError(
            f"{identifier}: output fingerprint drifted from {expected.fingerprint} "
            f"to {observed.fingerprint}"
        )


def _node_sequence_fingerprint(values: Sequence[object]) -> str:
    digest = hashlib.sha256(b"pyowl-core:benchmark-output:v1\0")
    for value in values:
        encoded = canonical_bytes(cast(StructuralNode, value))
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _incremental_limit(base_structural_bytes: int) -> int:
    return max(_INCREMENTAL_MINIMUM_BYTES, base_structural_bytes // 200)


def _throughput(source_bytes: int, median_ns: float) -> float:
    if median_ns == 0:
        return float("inf")
    return source_bytes / _MIB / (median_ns / 1_000_000_000)


def _rss_peak_bytes() -> int:
    observed = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return observed if sys.platform == "darwin" else observed * 1024


def _peer_source(corpus_id: str) -> bytes:
    return (
        "Ontology(<https://example.org/pyowl-core/benchmark/peer/"
        f"{corpus_id}> Declaration(Class(<https://example.org/pyowl-core/benchmark/peer#C>)))"
    ).encode()


def _corpus_metadata(corpus: Corpus) -> dict[str, Any]:
    return {
        "id": corpus.id,
        "tier": corpus.tier,
        "families": list(corpus.families),
        "format": corpus.format.value,
        "revision": corpus.revision,
        "sha256": corpus.sha256,
        "bytes": corpus.counts.bytes,
        "triples": corpus.counts.triples,
        "axioms": corpus.counts.axioms,
        "entities": corpus.counts.entities,
        "imports": corpus.counts.imports,
        "license": corpus.license,
        "redistribution": corpus.redistribution,
    }


def _validate_run_options(warmups: int, repetitions: int, cache_state: str) -> None:
    if warmups < 0:
        raise HarnessError("warmups must be non-negative")
    if repetitions < 1:
        raise HarnessError("repetitions must be positive")
    if cache_state not in {"resident-bytes-warm-process", "resident-bytes-fresh-process"}:
        raise HarnessError("cache_state must describe a supported resident-byte mode")


def _document(value: object) -> OntologyDocument:
    if not isinstance(value, OntologyDocument):
        raise HarnessError("scenario returned a non-document")
    return value


def _snapshot(value: object) -> OntologySnapshot:
    if not isinstance(value, OntologySnapshot):
        raise HarnessError("scenario returned a non-snapshot")
    return value


def _index(value: object) -> AxiomTypeIndex:
    if not isinstance(value, AxiomTypeIndex):
        raise HarnessError("scenario returned a non-index")
    return value


def _bytes(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise HarnessError("scenario returned non-bytes")
    return value


def _axiom_tuple(value: object) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise HarnessError("query scenario returned a non-tuple")
    return value


def _mapped(value: object) -> Any:
    if not isinstance(value, OntologySnapshot) or not hasattr(value, "close"):
        raise HarnessError("mmap scenario returned a non-mapped snapshot")
    return value


def _mapped_query(value: object) -> tuple[Any, tuple[object, ...]]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise HarnessError("mmap query returned invalid framing")
    return _mapped(value[0]), _axiom_tuple(value[1])


def _handoff(value: object) -> tuple[OntologyView, OperationCounters]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise HarnessError("handoff scenario returned invalid framing")
    view, counters = value
    if not isinstance(view, OntologyView) or not isinstance(counters, OperationCounters):
        raise HarnessError("handoff scenario returned invalid values")
    return view, counters


def _instrumented_snapshot(value: object) -> tuple[OntologySnapshot, OperationCounters]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise HarnessError("instrumented load returned invalid framing")
    snapshot, counters = value
    return _snapshot(snapshot), cast(OperationCounters, counters)


def _error_code_validation(value: object) -> ScenarioOutput:
    if not isinstance(value, str) or not value:
        raise HarnessError("bounded failure scenario returned no error code")
    return ScenarioOutput(hashlib.sha256(value.encode()).hexdigest(), {"error_code": value})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results" / "corpora",
    )
    parser.add_argument("--corpus", action="append", dest="corpora")
    parser.add_argument(
        "--backend",
        action="append",
        choices=tuple(value.value for value in BackendPreference),
        dest="backends",
    )
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument(
        "--cache-state",
        choices=("resident-bytes-warm-process", "resident-bytes-fresh-process"),
        default="resident-bytes-warm-process",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "benchmarks" / "results" / "performance-run.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_harness(
            manifest_path=args.manifest,
            cache_dir=args.cache_dir,
            corpus_ids=tuple(args.corpora or ("generated-tiny-functional",)),
            backends=tuple(BackendPreference(value) for value in (args.backends or ("python",))),
            warmups=args.warmups,
            repetitions=args.repetitions,
            cache_state=args.cache_state,
        )
        digest = write_json(args.output, report)
        print(f"performance report: {args.output} sha256={digest}")
        return 0 if report["passed"] is True else 1
    except (HarnessError, ReportError) as error:
        print(f"benchmark error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["HarnessError", "Scenario", "ScenarioOutput", "run_harness"]
