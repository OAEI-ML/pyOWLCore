"""Fresh-process worker for one checksum-pinned biomedical document."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import platform
import resource
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pyowl_core
from pyowl_core import (
    API_VERSION,
    AxiomScope,
    BackendPreference,
    EncodedStructuralView,
    OntologySnapshot,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.model import CONSTRUCTOR_SPECS, Declaration

from ..comparators.adapters import sanitize_failure
from ..comparators.common_contract import (
    build_core_common_contract,
    build_encoded_core_common_contract,
    validate_common_contract,
)
from ..comparators.fresh import publish_fresh_result, read_fresh_request
from ..manifest import ROOT, Corpus, generated_bytes, load_manifest
from ..native_redesign.encoded_contract import EncodedTraversalEvidence
from .contract import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    WORKER_RESULT_SCHEMA,
    BiomedicalGateError,
    corpus_identity,
    default_limits_row,
    options_for,
    options_row,
    parse_request,
    sha256_json,
    telemetry_row,
)

_DECLARATION_TAG = next(spec.tag for spec in CONSTRUCTOR_SPECS if spec.constructor is Declaration)
_ROOT_AXIOM = 2
_HASH_CHUNK_BYTES = 1024**2


def main() -> int:
    cpu_started = time.process_time_ns()
    try:
        request = parse_request(read_fresh_request(max_request_bytes=MAX_REQUEST_BYTES))
        result = run_case(request)
        measurement = cast(dict[str, object], result["measurement"])
        measurement["child_startup_to_ready_cpu_ns"] = max(1, time.process_time_ns() - cpu_started)
        peak_rss, unit = _peak_rss_bytes()
        measurement["fresh_process_peak_rss_bytes"] = max(1, peak_rss)
        measurement["rss_platform_unit"] = unit
    except Exception as error:
        sys.stderr.write(sanitize_failure(f"{type(error).__name__}: {error}") + "\n")
        return 2
    try:
        publish_fresh_result(
            result,
            max_request_bytes=MAX_REQUEST_BYTES,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
    except Exception as error:
        sys.stderr.write(sanitize_failure(f"{type(error).__name__}: {error}") + "\n")
        return 2
    return 0


def run_case(request: Mapping[str, object]) -> dict[str, object]:
    """Load exactly one locked source once and publish bounded evidence."""

    selected = parse_request(request)
    corpus = load_manifest().by_id(cast(str, selected["corpus_id"]))
    backend = BackendPreference(cast(str, selected["backend"]))
    source, source_bytes, source_sha256 = _locked_source(corpus, selected["source_path"])
    if source_bytes != corpus.counts.bytes or source_sha256 != corpus.sha256:
        raise BiomedicalGateError("source bytes differ from the corpus manifest")
    expected_native = selected["expected_native_sha256"]
    if expected_native is not None:
        observed_native = _native_artifact_identity()
        if observed_native["sha256"] != expected_native:
            raise BiomedicalGateError("loaded native extension differs from the requested digest")
    elif backend is BackendPreference.NATIVE:
        raise BiomedicalGateError("native load lacks an extension digest lock")
    if backend is BackendPreference.PYTHON and corpus.source != "generated":
        raise BiomedicalGateError("Python backend is restricted to generated harness fixtures")

    options = options_for(corpus, backend)
    option_values = options_row(options)
    options_sha256 = sha256_json(option_values)
    load_started = time.perf_counter_ns()
    snapshot = load_snapshot(
        source,
        document_iri=f"urn:pyowl-core:biomedical-gate:sha256:{source_sha256}",
        options=options,
    )
    load_wall_ns = time.perf_counter_ns() - load_started
    if not isinstance(snapshot, OntologySnapshot):
        raise BiomedicalGateError("load_snapshot did not return OntologySnapshot")

    if backend is BackendPreference.NATIVE:
        encoded = build_encoded_core_common_contract(
            snapshot,
            corpus_id=corpus.id,
            source_sha256=source_sha256,
            options_sha256=options_sha256,
        )
        common = encoded.contract
        encoded_evidence = encoded.evidence.to_metrics()
        declarations, declaration_evidence = _native_declaration_count(snapshot)
        encoded_evidence.update(declaration_evidence)
    else:
        common = build_core_common_contract(
            snapshot,
            corpus_id=corpus.id,
            source_sha256=source_sha256,
            options_sha256=options_sha256,
        )
        encoded_evidence = EncodedTraversalEvidence(0, 0, 0, 0, 0).to_metrics()
        encoded_evidence.update(
            {
                "declaration_view_count": 0,
                "declaration_root_rows_scanned": 0,
                "declaration_view_referenced_buffer_bytes": 0,
            }
        )
        declarations = sum(1 for _ in snapshot.iter_axioms(Declaration))
    validate_common_contract(common)
    inventories = cast(Mapping[str, Mapping[str, object]], common["ledger"]["inventories"])
    mapping_report = snapshot.root.rdf_mapping_report
    capabilities = snapshot.capabilities
    result: dict[str, object] = {
        "schema": WORKER_RESULT_SCHEMA,
        "status": "pass",
        "corpus": corpus_identity(corpus),
        "contract": {
            "requested_backend": backend.value,
            "selected_backend": snapshot.report.backend,
            "api_version": list(snapshot.report.api_version),
            "model_schema": snapshot.report.model_schema,
            "adapter_protocol": capabilities.adapter_protocol,
            "wire_format": list(capabilities.wire_format),
            "encoded_view_schemas": dict(capabilities.encoded_view_schemas),
            "options": option_values,
            "options_sha256": options_sha256,
            "default_parse_limits": default_limits_row(),
            "default_parse_limits_sha256": sha256_json(default_limits_row()),
            "load_entrypoint_calls": 1,
            "consumer_chunking": False,
            "document_count": snapshot.report.document_count,
        },
        "runtime": runtime_identity(),
        "output": {
            "root_document_key": snapshot.root_document_key,
            "complete_import_closure": snapshot.is_complete,
            "counts": {
                "source_bytes": snapshot.report.total_source_bytes,
                "documents": snapshot.report.document_count,
                "axioms": snapshot.report.effective_axiom_count,
                "declarations": declarations,
                "ontology_annotations": inventories["ontology_annotations"]["count"],
                "extensions": inventories["extensions"]["count"],
                "signature_entities": inventories["signature"]["count"],
                "imports": len(snapshot.import_manifest.edges),
                "diagnostics": len(snapshot.diagnostics),
                "rdf_triples": (None if mapping_report is None else mapping_report.total_triples),
            },
            "fingerprints": common["fingerprints"],
            "inventories": inventories,
            "common_contract_sha256": common["contract_sha256"],
            "anonymous_components": telemetry_row(
                snapshot.report.timings,
                required=cast(bool, selected["require_native_telemetry"]),
            ),
            "encoded_evidence": encoded_evidence,
        },
        "measurement": {
            "load_wall_ns": max(1, load_wall_ns),
            "child_startup_to_ready_cpu_ns": 0,
            "fresh_process_peak_rss_bytes": 0,
            "rss_platform_unit": "bytes",
            "sample_count": 1,
            "portable_performance_claim": False,
        },
    }
    if list(snapshot.report.api_version) != list(API_VERSION):
        raise BiomedicalGateError("loaded snapshot API version differs from package API")
    return result


def runtime_identity() -> dict[str, object]:
    """Capture exact executable, platform, package-tree, and native identities."""

    executable = Path(sys.executable).resolve()
    package_init = Path(pyowl_core.__file__).resolve()
    package_root = package_init.parent
    package_digest, package_files, package_bytes = _tree_digest(
        package_root,
        suffixes=frozenset({".py", ".pyi"}),
    )
    uname = platform.uname()
    identity: dict[str, object] = {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "compiler": platform.python_compiler(),
            "executable": str(executable),
            "executable_bytes": executable.stat().st_size,
            "executable_sha256": _file_sha256(executable),
        },
        "platform": {
            "system": uname.system,
            "release": uname.release,
            "version": uname.version,
            "machine": uname.machine,
            "processor": uname.processor,
            "python_build_platform": platform.platform(),
        },
        "cpu": {"logical_count": os.cpu_count()},
        "memory": {"physical_bytes": _physical_memory_bytes()},
        "package": {
            "version": pyowl_core.__version__,
            "api_version": list(API_VERSION),
            "source_origin": _path_label(package_init),
            "source_tree_file_count": package_files,
            "source_tree_bytes": package_bytes,
            "source_tree_sha256": package_digest,
        },
        "native": _native_artifact_identity(),
    }
    identity["identity_sha256"] = sha256_json(identity)
    return identity


def _locked_source(
    corpus: Corpus,
    source_path_value: object,
) -> tuple[bytes | Path, int, str]:
    if corpus.source == "generated":
        if source_path_value is not None:
            raise BiomedicalGateError("generated corpus request must not supply a source path")
        source = generated_bytes(corpus)
        return source, len(source), hashlib.sha256(source).hexdigest()
    if not isinstance(source_path_value, str) or not source_path_value:
        raise BiomedicalGateError("external corpus request requires a source path")
    path = Path(source_path_value).resolve()
    if not path.is_file():
        raise BiomedicalGateError("external corpus source is not a regular file")
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
            digest.update(block)
            byte_count += len(block)
    return path, byte_count, digest.hexdigest()


def _native_declaration_count(snapshot: OntologySnapshot) -> tuple[int, dict[str, int]]:
    view = snapshot.view(EncodedStructuralView, scope=AxiomScope.CLOSURE)
    root_kinds = view.buffers["root_kinds"]
    root_ids = view.buffers["root_ids"]
    node_tags = view.buffers["node_tags"]
    if len(root_ids) != len(root_kinds) * 4 or len(node_tags) % 2:
        raise BiomedicalGateError("encoded declaration-count columns are misaligned")
    declarations = 0
    root_count = len(root_kinds)
    for index in range(root_count):
        if root_kinds[index] != _ROOT_AXIOM:
            continue
        node_offset = index * 4
        node_id = int.from_bytes(root_ids[node_offset : node_offset + 4], "little")
        if node_id < 1 or node_id * 2 > len(node_tags):
            raise BiomedicalGateError("encoded declaration root ID is out of range")
        tag_offset = (node_id - 1) * 2
        tag = int.from_bytes(node_tags[tag_offset : tag_offset + 2], "little")
        if tag == _DECLARATION_TAG:
            declarations += 1
    return declarations, {
        "declaration_view_count": 1,
        "declaration_root_rows_scanned": root_count,
        "declaration_view_referenced_buffer_bytes": sum(
            len(buffer) for buffer in view.buffers.values()
        ),
    }


def _native_artifact_identity() -> dict[str, object]:
    probe = native.probe()
    spec = importlib.util.find_spec("pyowl_core._native")
    origin = None if spec is None else spec.origin
    if not probe.available:
        return {
            "available": False,
            "reason": probe.reason,
            "version": probe.version,
            "features": list(probe.features),
            "origin": origin,
            "bytes": None,
            "sha256": None,
        }
    module = importlib.import_module("pyowl_core._native")
    artifact = Path(cast(str, module.__file__)).resolve()
    return {
        "available": True,
        "reason": probe.reason,
        "version": probe.version,
        "features": list(probe.features),
        "origin": _path_label(artifact),
        "bytes": artifact.stat().st_size,
        "sha256": _file_sha256(artifact),
    }


def _tree_digest(root: Path, *, suffixes: frozenset[str]) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = 0
    byte_count = 0
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.suffix in suffixes),
        key=lambda candidate: candidate.relative_to(root).as_posix().encode("utf-8"),
    ):
        if not path.is_file():
            continue
        name = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        count += 1
        byte_count += len(data)
    return digest.hexdigest(), count, byte_count


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _path_label(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def _physical_memory_bytes() -> int | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if isinstance(pages, int) and isinstance(page_size, int) and pages > 0 and page_size > 0:
        return pages * page_size
    return None


def _peak_rss_bytes() -> tuple[int, str]:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return value, "bytes"
    return value * 1024, "kib-converted-to-bytes"


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    raise SystemExit(main())
