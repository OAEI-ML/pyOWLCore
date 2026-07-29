from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import os
import sys
import weakref
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import pyowl_core.model as model
from pyowl_core import BackendPreference, DocumentFormat, load_snapshot
from pyowl_core.model import structural_digest
from tools.benchmark.comparators.adapters import default_options
from tools.benchmark.comparators.common_contract import build_core_common_contract

RUNNER = Path("benchmarks/comparators/runners/py_horned_common.py")


class _WeakMapping(dict[str, object]):
    pass


class _WeakSource:
    pass


def _retention_test_frames(
    runner_module: ModuleType,
    references: dict[str, weakref.ReferenceType[object]],
) -> deque[object]:
    request = _WeakMapping({"test": "first-request"})
    request_frame = _WeakMapping(
        {
            "schema": runner_module.PERSISTENT_REQUEST_SCHEMA,
            "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
            "sequence": 0,
            "request": request,
        }
    )
    execute = _WeakMapping(
        {
            "schema": runner_module.PERSISTENT_EXECUTE_SCHEMA,
            "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
            "sequence": 0,
            "pid": os.getpid(),
        }
    )
    references.update(
        {
            "execute": weakref.ref(execute),
            "frame": weakref.ref(request_frame),
            "request": weakref.ref(request),
        }
    )
    return deque(
        (
            request_frame,
            execute,
            {
                "schema": runner_module.PERSISTENT_REQUEST_SCHEMA,
                "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                "sequence": 1,
                "request": {"test": "second-request"},
            },
            {
                "schema": runner_module.PERSISTENT_EXECUTE_SCHEMA,
                "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                "sequence": 1,
                "pid": os.getpid(),
            },
            {
                "schema": runner_module.PERSISTENT_SHUTDOWN_SCHEMA,
                "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                "sequence": 2,
            },
        )
    )


@pytest.fixture(scope="module")
def runner_module() -> Iterator[ModuleType]:
    module_name = "_pyowl_test_py_horned_common"
    previous_pyhorned = sys.modules.get("pyhornedowl")
    stub = ModuleType("pyhornedowl")
    stub.__file__ = str(RUNNER)
    sys.modules["pyhornedowl"] = stub
    spec = importlib.util.spec_from_file_location(module_name, RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)
        if previous_pyhorned is None:
            sys.modules.pop("pyhornedowl", None)
        else:
            sys.modules["pyhornedowl"] = previous_pyhorned


def test_persistent_runner_waits_for_authenticated_execute_after_preparation(
    runner_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence = 0
    request = {"test": "request"}
    frames = iter(
        (
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_REQUEST_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": sequence,
                    "request": request,
                }
            ).encode(),
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_EXECUTE_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": sequence,
                    "pid": os.getpid(),
                }
            ).encode(),
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_SHUTDOWN_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": sequence + 1,
                }
            ).encode(),
        )
    )
    written: list[dict[str, object]] = []
    monkeypatch.setattr(runner_module, "_read_frame", lambda: next(frames))
    monkeypatch.setattr(runner_module, "_write_frame", lambda value: written.append(value))
    monkeypatch.setattr(
        runner_module,
        "_validate_request",
        lambda *_args, **_kwargs: (b"Ontology()", DocumentFormat.FUNCTIONAL),
    )
    monkeypatch.setattr(
        runner_module,
        "_run_validated_request",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    runner_module._persistent_main()

    assert [value["schema"] for value in written] == [
        runner_module.PERSISTENT_HANDSHAKE_SCHEMA,
        runner_module.PERSISTENT_PREPARED_SCHEMA,
        runner_module.PERSISTENT_RESPONSE_SCHEMA,
        runner_module.PERSISTENT_SHUTDOWN_ACK_SCHEMA,
    ]
    assert written[0]["prepared_schema"] == runner_module.PERSISTENT_PREPARED_SCHEMA
    assert written[0]["execute_schema"] == runner_module.PERSISTENT_EXECUTE_SCHEMA
    assert written[1] == {
        "schema": runner_module.PERSISTENT_PREPARED_SCHEMA,
        "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
        "sequence": sequence,
        "pid": os.getpid(),
    }
    assert written[2]["result"] == {"status": "ok"}


def test_persistent_runner_rejects_a_skipped_request_sequence(
    runner_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = iter(
        (
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_REQUEST_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": 1,
                    "request": {},
                }
            ).encode(),
        )
    )
    monkeypatch.setattr(runner_module, "_read_frame", lambda: next(frames))
    monkeypatch.setattr(runner_module, "_write_frame", lambda _value: None)

    with pytest.raises(runner_module.RunnerContractError, match="sequence is nonmonotonic"):
        runner_module._persistent_main()


def test_persistent_runner_rejects_a_replayed_request_sequence(
    runner_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {"test": "request"}
    frames = iter(
        (
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_REQUEST_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": 0,
                    "request": request,
                }
            ).encode(),
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_EXECUTE_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": 0,
                    "pid": os.getpid(),
                }
            ).encode(),
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_REQUEST_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": 0,
                    "request": request,
                }
            ).encode(),
        )
    )
    written: list[dict[str, object]] = []
    monkeypatch.setattr(runner_module, "_read_frame", lambda: next(frames))
    monkeypatch.setattr(runner_module, "_write_frame", lambda value: written.append(value))
    monkeypatch.setattr(
        runner_module,
        "_validate_request",
        lambda *_args, **_kwargs: (b"Ontology()", DocumentFormat.FUNCTIONAL),
    )
    monkeypatch.setattr(
        runner_module,
        "_run_validated_request",
        lambda *_args, **_kwargs: {"status": "ok"},
    )

    with pytest.raises(runner_module.RunnerContractError, match="sequence is nonmonotonic"):
        runner_module._persistent_main()

    assert [value["schema"] for value in written] == [
        runner_module.PERSISTENT_HANDSHAKE_SCHEMA,
        runner_module.PERSISTENT_PREPARED_SCHEMA,
        runner_module.PERSISTENT_RESPONSE_SCHEMA,
    ]


def test_persistent_runner_rejects_a_wrong_shutdown_sequence(
    runner_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = iter(
        (
            json.dumps(
                {
                    "schema": runner_module.PERSISTENT_SHUTDOWN_SCHEMA,
                    "protocol": runner_module.PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": 1,
                }
            ).encode(),
        )
    )
    monkeypatch.setattr(runner_module, "_read_frame", lambda: next(frames))
    monkeypatch.setattr(runner_module, "_write_frame", lambda _value: None)

    with pytest.raises(runner_module.RunnerContractError, match="sequence is nonmonotonic"):
        runner_module._persistent_main()


def test_persistent_runner_releases_request_state_before_the_next_request(
    runner_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references: dict[str, weakref.ReferenceType[object]] = {}
    frames = _retention_test_frames(runner_module, references)
    validate_calls = 0
    result_calls = 0
    release_checked = False

    def read_frame() -> object:
        nonlocal release_checked
        if len(frames) == 3:
            gc.collect()
            assert set(references) == {"execute", "frame", "request", "result", "source"}
            assert {
                name for name, reference in references.items() if reference() is not None
            } == set()
            release_checked = True
        return frames.popleft()

    def json_object(value: object, _label: str) -> Mapping[str, Any]:
        assert isinstance(value, Mapping)
        return value

    def validate_request(
        _request: Mapping[str, Any],
        *,
        protocol_mode: str,
    ) -> tuple[object, DocumentFormat]:
        nonlocal validate_calls
        assert protocol_mode == "persistent"
        validate_calls += 1
        source = _WeakSource()
        if validate_calls == 1:
            references["source"] = weakref.ref(source)
        return source, DocumentFormat.FUNCTIONAL

    def run_validated_request(
        _request: Mapping[str, Any],
        *,
        source: object,
        format: DocumentFormat,
    ) -> dict[str, object]:
        nonlocal result_calls
        assert isinstance(source, _WeakSource)
        assert format is DocumentFormat.FUNCTIONAL
        result_calls += 1
        result = _WeakMapping({"status": "ok"})
        if result_calls == 1:
            references["result"] = weakref.ref(result)
        return result

    def write_frame(value: Mapping[str, object]) -> None:
        assert isinstance(value.get("schema"), str)

    monkeypatch.setattr(runner_module, "_read_frame", read_frame)
    monkeypatch.setattr(runner_module, "_json_object", json_object)
    monkeypatch.setattr(runner_module, "_validate_request", validate_request)
    monkeypatch.setattr(runner_module, "_run_validated_request", run_validated_request)
    monkeypatch.setattr(runner_module, "_write_frame", write_frame)

    runner_module._persistent_main()

    assert release_checked is True
    assert validate_calls == result_calls == 2


@pytest.mark.parametrize(
    ("format", "expected"),
    (
        (DocumentFormat.FUNCTIONAL, model.RDF_PLAIN_LITERAL),
        (DocumentFormat.OWL_XML, model.XSD_STRING),
        (DocumentFormat.RDF_XML, model.XSD_STRING),
        (DocumentFormat.TURTLE, model.XSD_STRING),
    ),
)
def test_simple_literal_mapping_is_format_sensitive(
    runner_module: ModuleType,
    format: DocumentFormat,
    expected: model.Datatype,
) -> None:
    literal = _value("SimpleLiteral", literal="plain")

    mapped = runner_module._map_node(
        literal,
        runner_module._literal_datatype(format),
    )

    assert isinstance(mapped, model.Literal)
    assert mapped.datatype == expected


@pytest.mark.parametrize(
    ("axiom_type", "member_field", "members"),
    (
        (
            model.EquivalentClasses,
            "expressions",
            tuple(model.Class(model.IRI(f"urn:test:C{index}")) for index in range(4)),
        ),
        (
            model.EquivalentObjectProperties,
            "properties",
            tuple(model.ObjectProperty(model.IRI(f"urn:test:op{index}")) for index in range(4)),
        ),
        (
            model.EquivalentDataProperties,
            "properties",
            tuple(model.DataProperty(model.IRI(f"urn:test:dp{index}")) for index in range(4)),
        ),
        (
            model.SameIndividual,
            "individuals",
            tuple(model.NamedIndividual(model.IRI(f"urn:test:i{index}")) for index in range(4)),
        ),
    ),
)
def test_rdf_equivalence_mapping_coalesces_connected_components(
    runner_module: ModuleType,
    axiom_type: Any,
    member_field: str,
    members: tuple[model.StructuralNode, ...],
) -> None:
    annotations = tuple(
        model.Annotation(
            model.AnnotationProperty(model.IRI(f"urn:test:annotation{index}")),
            model.IRI(f"urn:test:value{index}"),
        )
        for index in range(3)
    )
    pairwise = [
        axiom_type(model.CanonicalSet(members[0:2]), model.CanonicalSet((annotations[0],))),
        axiom_type(model.CanonicalSet(members[2:4]), model.CanonicalSet((annotations[1],))),
        axiom_type(model.CanonicalSet(members[1:3]), model.CanonicalSet((annotations[2],))),
    ]
    unrelated = model.Declaration(model.Class(model.IRI("urn:test:unrelated")))

    mapped = runner_module._coalesce_rdf_equivalence_axioms(
        [pairwise[0], unrelated, pairwise[1], pairwise[2]]
    )

    assert len(mapped) == 2
    coalesced = mapped[0]
    assert type(coalesced) is axiom_type
    assert getattr(coalesced, member_field) == model.CanonicalSet(members)
    assert coalesced.annotations == model.CanonicalSet(annotations)
    assert mapped[1] == unrelated


def test_rdf_document_origins_exclude_ontology_annotations(
    runner_module: ModuleType,
) -> None:
    ontology_annotation = _value(
        "OntologyAnnotation",
        first=_value(
            "Annotation",
            ap=_value(
                "AnnotationProperty",
                first="http://www.w3.org/2000/01/rdf-schema#label",
            ),
            av=_value("SimpleLiteral", literal="ontology label"),
        ),
    )
    first_equivalence = _value(
        "EquivalentClasses",
        first=[
            _value("Class", first="urn:test:A"),
            _value("Class", first="urn:test:B"),
        ],
    )
    second_equivalence = _value(
        "EquivalentClasses",
        first=[
            _value("Class", first="urn:test:B"),
            _value("Class", first="urn:test:C"),
        ],
    )
    declarations = [
        _value("DeclareClass", first=_value("Class", first=f"urn:test:{name}"))
        for name in ("A", "B", "C")
    ]
    ontology = _Ontology(
        [
            _annotated(ontology_annotation),
            *(_annotated(declaration) for declaration in declarations),
            _annotated(first_equivalence),
            _annotated(second_equivalence),
        ],
        ontology_iri="urn:test:ontology",
    )
    source = b"""\
<rdf:RDF
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
  <owl:Ontology rdf:about="urn:test:ontology">
    <rdfs:label>ontology label</rdfs:label>
  </owl:Ontology>
  <owl:Class rdf:about="urn:test:A">
    <owl:equivalentClass rdf:resource="urn:test:B"/>
  </owl:Class>
  <owl:Class rdf:about="urn:test:B">
    <owl:equivalentClass rdf:resource="urn:test:C"/>
  </owl:Class>
  <owl:Class rdf:about="urn:test:C"/>
</rdf:RDF>
"""
    source_sha256 = hashlib.sha256(source).hexdigest()

    rdf_document = runner_module._map_document(
        ontology,
        source=source,
        source_sha256=source_sha256,
        document_iri="urn:test:document",
        format=DocumentFormat.RDF_XML,
    )
    functional_document = runner_module._map_document(
        ontology,
        source=b"Ontology()",
        source_sha256=hashlib.sha256(b"Ontology()").hexdigest(),
        document_iri="urn:test:functional",
        format=DocumentFormat.FUNCTIONAL,
    )

    assert len(rdf_document.ontology_annotations) == 1
    rdf_annotation = next(iter(rdf_document.ontology_annotations))
    assert isinstance(rdf_annotation.value, model.Literal)
    assert rdf_annotation.value.datatype == model.XSD_STRING
    assert len(rdf_document.axioms) == 4
    rdf_axiom = next(
        axiom for axiom in rdf_document.axioms if isinstance(axiom, model.EquivalentClasses)
    )
    assert isinstance(rdf_axiom, model.EquivalentClasses)
    assert len(rdf_axiom.expressions) == 3
    assert rdf_document.origin_index is not None
    assert structural_digest(rdf_annotation) not in rdf_document.origin_index.entries
    assert structural_digest(rdf_axiom) in rdf_document.origin_index.entries

    assert (
        sum(isinstance(axiom, model.EquivalentClasses) for axiom in functional_document.axioms) == 2
    )
    functional_annotation = next(iter(functional_document.ontology_annotations))
    assert isinstance(functional_annotation.value, model.Literal)
    assert functional_annotation.value.datatype == model.RDF_PLAIN_LITERAL
    assert functional_document.origin_index is None

    options = replace(
        default_options(DocumentFormat.RDF_XML),
        format=None,
        backend=BackendPreference.PYTHON,
    )
    snapshot = load_snapshot(rdf_document, options=options)
    contract = build_core_common_contract(
        snapshot,
        corpus_id="py-horned-rdf-origin-test",
        source_sha256=source_sha256,
        options_sha256="0" * 64,
    )
    reference = load_snapshot(
        source,
        document_iri="urn:test:document",
        options=default_options(DocumentFormat.RDF_XML),
    )
    reference_contract = build_core_common_contract(
        reference,
        corpus_id="py-horned-rdf-origin-test",
        source_sha256=source_sha256,
        options_sha256="0" * 64,
    )
    assert contract["ledger"]["inventories"]["ontology_annotations"]["count"] == 1
    assert contract["provenance"]["origin_entry_count"] == 4
    assert contract == reference_contract


def _value(name: str, **attributes: Any) -> object:
    value = type(name, (), {})()
    for attribute, item in attributes.items():
        setattr(value, attribute, item)
    return value


def _annotated(component: object) -> SimpleNamespace:
    return SimpleNamespace(component=component, ann=())


class _Ontology:
    def __init__(
        self,
        components: list[SimpleNamespace],
        *,
        ontology_iri: str | None = None,
    ) -> None:
        self._components = components
        self._ontology_iri = ontology_iri

    def get_iri(self) -> str | None:
        return self._ontology_iri

    def get_version_iri(self) -> None:
        return None

    def get_components(self) -> list[SimpleNamespace]:
        return self._components
