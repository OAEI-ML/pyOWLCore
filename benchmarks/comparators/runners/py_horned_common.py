#!/usr/bin/env python3
# mypy: ignore-errors
"""Pinned development-only py-horned common-contract comparator runner.

The parser and OWL object graph are supplied by py-horned-owl. This adapter
maps that independent graph into pyowl-core's public structural value contract;
it never invokes a pyowl-core syntax parser. The whole parse, map, freeze,
fingerprint, inventory, and validation path remains inside the measured
readiness envelope.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import resource
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT))

import pyhornedowl  # type: ignore[import-not-found]  # noqa: E402

import pyowl_core.model as core_model  # noqa: E402
import pyowl_core.model.swrl as core_swrl  # noqa: E402
from pyowl_core import IRI, BackendPreference, DocumentFormat, load_snapshot  # noqa: E402
from pyowl_core.document.document import (  # noqa: E402
    OntologyDocument,
    OntologyID,
    freeze_document_anonymous,
    provisional_anonymous,
)
from pyowl_core.document.provenance import (  # noqa: E402
    DetectionBasis,
    DigestKind,
    DocumentProvenance,
)
from tools.benchmark.comparators.adapters import (  # noqa: E402
    ADAPTER_REQUEST_SCHEMA,
    ADAPTER_RESULT_SCHEMA,
    TIMED_VALIDATION_SCHEMA,
    default_options,
    options_digest,
    options_inventory,
)
from tools.benchmark.comparators.common_contract import (  # noqa: E402
    build_core_common_contract,
    validate_common_contract,
)
from tools.benchmark.comparators.persistent import (  # noqa: E402
    PERSISTENT_HANDSHAKE_SCHEMA,
    PERSISTENT_PROTOCOL_SCHEMA,
    PERSISTENT_REQUEST_SCHEMA,
    PERSISTENT_RESPONSE_SCHEMA,
    PERSISTENT_SHUTDOWN_ACK_SCHEMA,
    PERSISTENT_SHUTDOWN_SCHEMA,
)

LANE = "py-horned-common"
IMPLEMENTATION = "py-horned-owl"
BOUNDARY = "common-contract-ready"
ENGINE_VERSION = "1.4.0"
ENGINE_REVISION = "PyPI py-horned-owl 1.4.0 (2026-02-11)"
ENGINE_ARTIFACT = "PyPI sdist py_horned_owl-1.4.0.tar.gz"
ENGINE_SHA256 = "7146d0887c5ec119e423e56c9221cc0ca7da54739be36ce3ed916503348f942d"
FEATURES = (
    "abi3-wrapper",
    "independent-common-contract-v1",
    "verified-sdist-install-v1",
)
ALLOCATOR = "Rust system allocator and CPython platform allocator"
THREAD_CEILING = 1
RUNNER_REVISION = "pyowl-core-py-horned-common-runner-v2"

MAX_REQUEST_BYTES = 512 * 1024**2
MAX_FRAME_HEADER_BYTES = 32
MAX_REASON_CHARS = 1_000
_SHA256_CHARS = frozenset("0123456789abcdef")
_XSD = "http://www.w3.org/2001/XMLSchema#"
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

_FORMAT_SERIALIZATION = {
    DocumentFormat.FUNCTIONAL: "ofn",
    DocumentFormat.OWL_XML: "owx",
    DocumentFormat.RDF_XML: "rdf",
}
_FACET_IRIS = {
    "Length": _XSD + "length",
    "MinLength": _XSD + "minLength",
    "MaxLength": _XSD + "maxLength",
    "Pattern": _XSD + "pattern",
    "MinInclusive": _XSD + "minInclusive",
    "MinExclusive": _XSD + "minExclusive",
    "MaxInclusive": _XSD + "maxInclusive",
    "MaxExclusive": _XSD + "maxExclusive",
    "TotalDigits": _XSD + "totalDigits",
    "FractionDigits": _XSD + "fractionDigits",
    "LangRange": _RDF + "langRange",
}


class RunnerContractError(ValueError):
    """The request or independent model cannot satisfy the common contract."""


def _artifact() -> dict[str, object]:
    return {
        "pin_state": "complete",
        "version": ENGINE_VERSION,
        "revision": ENGINE_REVISION,
        "artifact": ENGINE_ARTIFACT,
        "artifact_sha256": ENGINE_SHA256,
        "features": list(FEATURES),
        "allocator": ALLOCATOR,
        "thread_ceiling": THREAD_CEILING,
        "runner_revision": RUNNER_REVISION,
        "runner_sha256": _runner_sha256(),
    }


def _runner_sha256() -> str:
    digest = hashlib.sha256()
    with Path(__file__).resolve().open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_engine_install() -> None:
    """Bind the imported extension to the exact source artifact and RECORD."""

    try:
        distribution = importlib.metadata.distribution("py-horned-owl")
    except importlib.metadata.PackageNotFoundError as error:
        raise RunnerContractError("py-horned-owl distribution metadata is missing") from error
    if distribution.version != ENGINE_VERSION:
        raise RunnerContractError("py-horned-owl distribution version differs from pin")

    raw_direct_url = distribution.read_text("direct_url.json")
    if raw_direct_url is None:
        raise RunnerContractError("py-horned-owl install lacks direct artifact provenance")
    try:
        direct_url = json.loads(raw_direct_url)
    except json.JSONDecodeError as error:
        raise RunnerContractError("py-horned-owl direct artifact provenance is invalid") from error
    if not isinstance(direct_url, dict):
        raise RunnerContractError("py-horned-owl direct artifact provenance must be an object")
    archive_info = direct_url.get("archive_info")
    if not isinstance(archive_info, dict):
        raise RunnerContractError("py-horned-owl was not installed from the pinned archive")
    hashes = archive_info.get("hashes")
    if not isinstance(hashes, dict) or hashes.get("sha256") != ENGINE_SHA256:
        raise RunnerContractError("py-horned-owl source archive SHA-256 differs from pin")

    files = distribution.files
    if files is None:
        raise RunnerContractError("py-horned-owl install lacks a RECORD inventory")
    verified_files = 0
    imported_package = Path(cast(str, pyhornedowl.__file__)).resolve()
    imported_package_verified = False
    for entry in files:
        recorded_hash = entry.hash
        if recorded_hash is None:
            continue
        if recorded_hash.mode != "sha256":
            raise RunnerContractError("py-horned-owl RECORD uses a non-SHA-256 digest")
        installed_path = Path(entry.locate()).resolve(strict=True)
        digest = hashlib.sha256()
        with installed_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                digest.update(chunk)
        encoded = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
        if encoded != recorded_hash.value:
            raise RunnerContractError("py-horned-owl installed file differs from RECORD")
        verified_files += 1
        imported_package_verified |= installed_path == imported_package
    if verified_files == 0 or not imported_package_verified:
        raise RunnerContractError("py-horned-owl imported package is outside its RECORD")


def _iri(value: object) -> IRI:
    return IRI(str(value))


def _canonical_set(values: object) -> core_model.CanonicalSet[Any]:
    return core_model.CanonicalSet(_map_node(item) for item in cast(Any, values))


def _map_annotation(
    value: object,
    nested: object = (),
) -> core_model.Annotation:
    if type(value).__name__ != "Annotation":
        raise RunnerContractError("expected py-horned Annotation")
    return core_model.Annotation(
        cast(core_model.AnnotationProperty, _map_node(value.ap)),  # type: ignore[attr-defined]
        cast(core_model.AnnotationValue, _map_node(value.av)),  # type: ignore[attr-defined]
        _canonical_set(nested),
    )


def _map_node(value: object) -> core_model.StructuralNode:
    """Map one py-horned model value without invoking a core syntax parser."""

    name = type(value).__name__
    if name == "IRI":
        return _iri(value)
    entity_types = {
        "Class": core_model.Class,
        "Datatype": core_model.Datatype,
        "ObjectProperty": core_model.ObjectProperty,
        "DataProperty": core_model.DataProperty,
        "AnnotationProperty": core_model.AnnotationProperty,
        "NamedIndividual": core_model.NamedIndividual,
    }
    entity = entity_types.get(name)
    if entity is not None:
        return entity(_iri(value.first))  # type: ignore[attr-defined]
    if name == "AnonymousIndividual":
        return provisional_anonymous(value.first)  # type: ignore[attr-defined]
    if name == "SimpleLiteral":
        return core_model.Literal(value.literal, core_model.XSD_STRING)  # type: ignore[attr-defined]
    if name == "LanguageLiteral":
        return core_model.Literal(  # type: ignore[attr-defined]
            value.literal,
            core_model.RDF_PLAIN_LITERAL,
            value.lang,
        )
    if name == "DatatypeLiteral":
        return core_model.Literal(  # type: ignore[attr-defined]
            value.literal,
            core_model.Datatype(_iri(value.datatype_iri)),
        )
    if name == "Annotation":
        return _map_annotation(value)
    if name == "InverseObjectProperty":
        return core_model.ObjectInverseOf(
            cast(core_model.ObjectProperty, _map_node(value.first))  # type: ignore[attr-defined]
        )
    if name == "FacetRestriction":
        facet_name = str(value.f).removeprefix("Facet.")  # type: ignore[attr-defined]
        try:
            facet = _FACET_IRIS[facet_name]
        except KeyError as error:
            raise RunnerContractError("unsupported py-horned facet") from error
        return core_model.FacetRestriction(
            IRI(facet),
            cast(core_model.Literal, _map_node(value.l)),  # type: ignore[attr-defined]
        )

    unary_sets = {
        "DataIntersectionOf": core_model.DataIntersectionOf,
        "DataUnionOf": core_model.DataUnionOf,
        "DataOneOf": core_model.DataOneOf,
        "ObjectIntersectionOf": core_model.ObjectIntersectionOf,
        "ObjectUnionOf": core_model.ObjectUnionOf,
        "ObjectOneOf": core_model.ObjectOneOf,
    }
    unary_set = unary_sets.get(name)
    if unary_set is not None:
        return unary_set(_canonical_set(value.first))  # type: ignore[attr-defined]
    if name == "DataComplementOf":
        return core_model.DataComplementOf(_map_node(value.first))  # type: ignore[attr-defined]
    if name == "DatatypeRestriction":
        return core_model.DatatypeRestriction(  # type: ignore[attr-defined]
            cast(core_model.Datatype, _map_node(value.first)),
            _canonical_set(value.second),
        )
    if name == "ObjectComplementOf":
        return core_model.ObjectComplementOf(_map_node(value.first))  # type: ignore[attr-defined]

    object_quantifiers = {
        "ObjectSomeValuesFrom": core_model.ObjectSomeValuesFrom,
        "ObjectAllValuesFrom": core_model.ObjectAllValuesFrom,
    }
    object_quantifier = object_quantifiers.get(name)
    if object_quantifier is not None:
        return object_quantifier(
            _map_node(value.ope),  # type: ignore[attr-defined]
            _map_node(value.bce),  # type: ignore[attr-defined]
        )
    if name == "ObjectHasValue":
        return core_model.ObjectHasValue(  # type: ignore[attr-defined]
            _map_node(value.ope),
            _map_node(value.i),
        )
    if name == "ObjectHasSelf":
        return core_model.ObjectHasSelf(_map_node(value.first))  # type: ignore[attr-defined]
    object_cardinalities = {
        "ObjectMinCardinality": core_model.ObjectMinCardinality,
        "ObjectMaxCardinality": core_model.ObjectMaxCardinality,
        "ObjectExactCardinality": core_model.ObjectExactCardinality,
    }
    object_cardinality = object_cardinalities.get(name)
    if object_cardinality is not None:
        return object_cardinality(
            value.n,  # type: ignore[attr-defined]
            _map_node(value.ope),  # type: ignore[attr-defined]
            _map_node(value.bce),  # type: ignore[attr-defined]
        )

    data_quantifiers = {
        "DataSomeValuesFrom": core_model.DataSomeValuesFrom,
        "DataAllValuesFrom": core_model.DataAllValuesFrom,
    }
    data_quantifier = data_quantifiers.get(name)
    if data_quantifier is not None:
        return data_quantifier(
            (cast(core_model.DataProperty, _map_node(value.dp)),),  # type: ignore[attr-defined]
            _map_node(value.dr),  # type: ignore[attr-defined]
        )
    if name == "DataHasValue":
        return core_model.DataHasValue(  # type: ignore[attr-defined]
            cast(core_model.DataProperty, _map_node(value.dp)),
            cast(core_model.Literal, _map_node(value.l)),
        )
    data_cardinalities = {
        "DataMinCardinality": core_model.DataMinCardinality,
        "DataMaxCardinality": core_model.DataMaxCardinality,
        "DataExactCardinality": core_model.DataExactCardinality,
    }
    data_cardinality = data_cardinalities.get(name)
    if data_cardinality is not None:
        return data_cardinality(
            value.n,  # type: ignore[attr-defined]
            cast(core_model.DataProperty, _map_node(value.dp)),  # type: ignore[attr-defined]
            _map_node(value.dr),  # type: ignore[attr-defined]
        )

    if name == "Variable":
        return core_swrl.Variable(_iri(value.first))  # type: ignore[attr-defined]
    if name == "ClassAtom":
        return core_swrl.ClassAtom(  # type: ignore[attr-defined]
            _map_node(value.pred),
            _map_node(value.arg),
        )
    if name == "DataRangeAtom":
        return core_swrl.DataRangeAtom(  # type: ignore[attr-defined]
            _map_node(value.pred),
            _map_node(value.arg),
        )
    if name == "ObjectPropertyAtom":
        source, target = value.args  # type: ignore[attr-defined]
        return core_swrl.ObjectPropertyAtom(
            _map_node(value.pred),  # type: ignore[attr-defined]
            _map_node(source),
            _map_node(target),
        )
    if name == "DataPropertyAtom":
        source, target = value.args  # type: ignore[attr-defined]
        return core_swrl.DataPropertyAtom(
            cast(core_model.DataProperty, _map_node(value.pred)),  # type: ignore[attr-defined]
            _map_node(source),
            _map_node(target),
        )
    if name == "BuiltInAtom":
        return core_swrl.BuiltInAtom(  # type: ignore[attr-defined]
            _iri(value.pred),
            tuple(_map_node(item) for item in value.args),
        )
    if name in {"SameIndividualAtom", "DifferentIndividualsAtom"}:
        constructor = (
            core_swrl.SameIndividualAtom
            if name == "SameIndividualAtom"
            else core_swrl.DifferentIndividualsAtom
        )
        return constructor(_map_node(value.first), _map_node(value.second))  # type: ignore[attr-defined]
    raise RunnerContractError(f"unsupported py-horned structural value: {name}")


def _map_axiom(value: object, annotations: object) -> core_model.AxiomNode:
    name = type(value).__name__
    mapped_annotations = _canonical_set(annotations)
    declarations = {
        "DeclareClass",
        "DeclareObjectProperty",
        "DeclareAnnotationProperty",
        "DeclareDataProperty",
        "DeclareNamedIndividual",
        "DeclareDatatype",
    }
    if name in declarations:
        return core_model.Declaration(_map_node(value.first), mapped_annotations)  # type: ignore[attr-defined]
    if name == "SubClassOf":
        return core_model.SubClassOf(  # type: ignore[attr-defined]
            _map_node(value.sub),
            _map_node(value.sup),
            mapped_annotations,
        )
    class_sets = {
        "EquivalentClasses": core_model.EquivalentClasses,
        "DisjointClasses": core_model.DisjointClasses,
    }
    class_set = class_sets.get(name)
    if class_set is not None:
        return class_set(_canonical_set(value.first), mapped_annotations)  # type: ignore[attr-defined]
    if name == "DisjointUnion":
        return core_model.DisjointUnion(  # type: ignore[attr-defined]
            cast(core_model.Class, _map_node(value.first)),
            _canonical_set(value.second),
            mapped_annotations,
        )
    if name == "SubObjectPropertyOf":
        sub = value.sub  # type: ignore[attr-defined]
        mapped_sub = (
            core_model.ObjectPropertyChain(tuple(_map_node(item) for item in sub))
            if isinstance(sub, list)
            else _map_node(sub)
        )
        return core_model.SubObjectPropertyOf(
            mapped_sub,
            _map_node(value.sup),  # type: ignore[attr-defined]
            mapped_annotations,
        )
    property_sets = {
        "EquivalentObjectProperties": core_model.EquivalentObjectProperties,
        "DisjointObjectProperties": core_model.DisjointObjectProperties,
        "EquivalentDataProperties": core_model.EquivalentDataProperties,
        "DisjointDataProperties": core_model.DisjointDataProperties,
    }
    property_set = property_sets.get(name)
    if property_set is not None:
        return property_set(_canonical_set(value.first), mapped_annotations)  # type: ignore[attr-defined]
    if name == "InverseObjectProperties":
        return core_model.InverseObjectProperties(  # type: ignore[attr-defined]
            _map_node(value.first),
            _map_node(value.second),
            mapped_annotations,
        )
    if name in {"ObjectPropertyDomain", "ObjectPropertyRange"}:
        constructor = (
            core_model.ObjectPropertyDomain
            if name == "ObjectPropertyDomain"
            else core_model.ObjectPropertyRange
        )
        return constructor(
            _map_node(value.ope),  # type: ignore[attr-defined]
            _map_node(value.ce),  # type: ignore[attr-defined]
            mapped_annotations,
        )
    object_characteristics = {
        "FunctionalObjectProperty": core_model.FunctionalObjectProperty,
        "InverseFunctionalObjectProperty": core_model.InverseFunctionalObjectProperty,
        "ReflexiveObjectProperty": core_model.ReflexiveObjectProperty,
        "IrreflexiveObjectProperty": core_model.IrreflexiveObjectProperty,
        "SymmetricObjectProperty": core_model.SymmetricObjectProperty,
        "AsymmetricObjectProperty": core_model.AsymmetricObjectProperty,
        "TransitiveObjectProperty": core_model.TransitiveObjectProperty,
    }
    characteristic = object_characteristics.get(name)
    if characteristic is not None:
        return characteristic(_map_node(value.first), mapped_annotations)  # type: ignore[attr-defined]
    if name == "SubDataPropertyOf":
        return core_model.SubDataPropertyOf(  # type: ignore[attr-defined]
            cast(core_model.DataProperty, _map_node(value.sub)),
            cast(core_model.DataProperty, _map_node(value.sup)),
            mapped_annotations,
        )
    if name == "DataPropertyDomain":
        return core_model.DataPropertyDomain(  # type: ignore[attr-defined]
            cast(core_model.DataProperty, _map_node(value.dp)),
            _map_node(value.ce),
            mapped_annotations,
        )
    if name == "DataPropertyRange":
        return core_model.DataPropertyRange(  # type: ignore[attr-defined]
            cast(core_model.DataProperty, _map_node(value.dp)),
            _map_node(value.dr),
            mapped_annotations,
        )
    if name == "FunctionalDataProperty":
        return core_model.FunctionalDataProperty(  # type: ignore[attr-defined]
            cast(core_model.DataProperty, _map_node(value.first)),
            mapped_annotations,
        )
    if name == "DatatypeDefinition":
        return core_model.DatatypeDefinition(  # type: ignore[attr-defined]
            cast(core_model.Datatype, _map_node(value.kind)),
            _map_node(value.range),
            mapped_annotations,
        )
    if name == "HasKey":
        object_properties: list[core_model.StructuralNode] = []
        data_properties: list[core_model.StructuralNode] = []
        for item in value.vpe:  # type: ignore[attr-defined]
            mapped = _map_node(item)
            if isinstance(mapped, (core_model.ObjectProperty, core_model.ObjectInverseOf)):
                object_properties.append(mapped)
            elif isinstance(mapped, core_model.DataProperty):
                data_properties.append(mapped)
            else:
                raise RunnerContractError("py-horned HasKey contains a non-key property")
        return core_model.HasKey(
            _map_node(value.ce),  # type: ignore[attr-defined]
            core_model.CanonicalSet(object_properties),
            core_model.CanonicalSet(data_properties),
            mapped_annotations,
        )
    individual_sets = {
        "SameIndividual": core_model.SameIndividual,
        "DifferentIndividuals": core_model.DifferentIndividuals,
    }
    individual_set = individual_sets.get(name)
    if individual_set is not None:
        return individual_set(_canonical_set(value.first), mapped_annotations)  # type: ignore[attr-defined]
    if name == "ClassAssertion":
        return core_model.ClassAssertion(  # type: ignore[attr-defined]
            _map_node(value.ce),
            _map_node(value.i),
            mapped_annotations,
        )
    if name in {"ObjectPropertyAssertion", "NegativeObjectPropertyAssertion"}:
        constructor = (
            core_model.ObjectPropertyAssertion
            if name == "ObjectPropertyAssertion"
            else core_model.NegativeObjectPropertyAssertion
        )
        return constructor(
            _map_node(value.ope),  # type: ignore[attr-defined]
            _map_node(value.source),  # type: ignore[attr-defined]
            _map_node(value.target),  # type: ignore[attr-defined]
            mapped_annotations,
        )
    if name in {"DataPropertyAssertion", "NegativeDataPropertyAssertion"}:
        constructor = (
            core_model.DataPropertyAssertion
            if name == "DataPropertyAssertion"
            else core_model.NegativeDataPropertyAssertion
        )
        return constructor(
            cast(core_model.DataProperty, _map_node(value.dp)),  # type: ignore[attr-defined]
            _map_node(value.source),  # type: ignore[attr-defined]
            cast(core_model.Literal, _map_node(value.target)),  # type: ignore[attr-defined]
            mapped_annotations,
        )
    if name == "AnnotationAssertion":
        annotation = _map_annotation(value.ann)  # type: ignore[attr-defined]
        return core_model.AnnotationAssertion(
            annotation.property,
            _map_node(value.subject),  # type: ignore[attr-defined]
            annotation.value,
            mapped_annotations,
        )
    if name == "SubAnnotationPropertyOf":
        return core_model.SubAnnotationPropertyOf(  # type: ignore[attr-defined]
            cast(core_model.AnnotationProperty, _map_node(value.sub)),
            cast(core_model.AnnotationProperty, _map_node(value.sup)),
            mapped_annotations,
        )
    if name in {"AnnotationPropertyDomain", "AnnotationPropertyRange"}:
        constructor = (
            core_model.AnnotationPropertyDomain
            if name == "AnnotationPropertyDomain"
            else core_model.AnnotationPropertyRange
        )
        return constructor(
            cast(core_model.AnnotationProperty, _map_node(value.ap)),  # type: ignore[attr-defined]
            _iri(value.iri),  # type: ignore[attr-defined]
            mapped_annotations,
        )
    raise RunnerContractError(f"unsupported py-horned axiom: {name}")


def _map_rule(value: object, annotations: object) -> core_swrl.SWRLRule:
    return core_swrl.SWRLRule(  # type: ignore[attr-defined]
        body=_canonical_set(value.body),  # type: ignore[attr-defined]
        head=_canonical_set(value.head),  # type: ignore[attr-defined]
        annotations=_canonical_set(annotations),
    )


def _map_document(
    ontology: object,
    *,
    source: bytes,
    source_sha256: str,
    document_iri: str,
    format: DocumentFormat,
) -> OntologyDocument:
    ontology_iri = ontology.get_iri()  # type: ignore[attr-defined]
    version_iri = ontology.get_version_iri()  # type: ignore[attr-defined]
    ontology_id = OntologyID(
        None if ontology_iri is None else _iri(ontology_iri),
        None if version_iri is None else _iri(version_iri),
    )
    imports: list[IRI] = []
    ontology_annotations: list[core_model.Annotation] = []
    axioms: list[core_model.AxiomNode] = []
    extensions: list[core_model.StructuralNode] = []
    for annotated in ontology.get_components():  # type: ignore[attr-defined]
        component = annotated.component
        annotations = annotated.ann
        name = type(component).__name__
        if name in {"OntologyID", "DocIRI"}:
            continue
        if name == "Import":
            imports.append(_iri(component.first))
        elif name == "OntologyAnnotation":
            ontology_annotations.append(_map_annotation(component.first, annotations))
        elif name == "Rule":
            extensions.append(_map_rule(component, annotations))
        else:
            axioms.append(_map_axiom(component, annotations))

    frozen_imports, frozen_annotations, frozen_axioms, frozen_extensions = (
        freeze_document_anonymous(
            ontology_id,
            imports,
            ontology_annotations,
            axioms,
            extensions,
        )
    )
    decoded = source.decode("utf-8")
    provenance = DocumentProvenance(
        bytes.fromhex(source_sha256),
        DigestKind.EXACT_BYTES,
        len(source),
        len(decoded),
        IRI(document_iri),
        None,
        format,
        DetectionBasis.EXPLICIT,
        parser="pyhornedowl 1.4.0",
        backend="external-py-horned",
    )
    return OntologyDocument(
        ontology_id,
        IRI(document_iri),
        frozen_imports,
        frozen_annotations,
        frozen_axioms,
        frozen_extensions,
        provenance,
    )


@contextmanager
def _prepared_source(request: Mapping[str, Any], source: bytes) -> Iterator[Path | None]:
    if request["input_mode"] == "resident-bytes":
        yield None
        return
    with tempfile.TemporaryDirectory(prefix="pyowl-core-py-horned-") as directory:
        path = Path(directory) / "ontology-input"
        written = path.write_bytes(source)
        if written != len(source):
            raise OSError("prepared comparator source was truncated")
        yield path


def _parse_horned(
    source: bytes,
    *,
    prepared_path: Path | None,
    format: DocumentFormat,
) -> object:
    selected = source if prepared_path is None else prepared_path.read_bytes()
    text = selected.decode("utf-8")
    return pyhornedowl.open_ontology_from_string(text, _FORMAT_SERIALIZATION[format])


def _run_request(request: Mapping[str, Any], *, protocol_mode: str) -> dict[str, object]:
    source, format = _validate_request(request, protocol_mode=protocol_mode)
    if format is DocumentFormat.TURTLE:
        return _status_result(
            request,
            status="ineligible",
            reason=(
                "py-horned-owl 1.4.0 exposes RDF/XML, OWL/XML, and Functional "
                "Syntax readers, but no Turtle reader selection"
            ),
        )
    with _prepared_source(request, source) as prepared_path:
        rss_before = _rss_peak_bytes()
        cpu_start = time.process_time_ns()
        wall_start = time.perf_counter_ns()
        load_start = time.perf_counter_ns()
        ontology = _parse_horned(source, prepared_path=prepared_path, format=format)
        load_end = time.perf_counter_ns()
        common_start = load_end
        document = _map_document(
            ontology,
            source=source,
            source_sha256=cast(str, request["source_sha256"]),
            document_iri=cast(str, request["document_iri"]),
            format=format,
        )
        options = default_options(format)
        snapshot = load_snapshot(
            document,
            options=replace(options, format=None, backend=BackendPreference.PYTHON),
        )
        contract = build_core_common_contract(
            snapshot,
            corpus_id=cast(str, request["corpus_id"]),
            source_sha256=cast(str, request["source_sha256"]),
            options_sha256=cast(str, request["options_sha256"]),
        )
        validation_start = time.perf_counter_ns()
        validate_common_contract(contract)
        validation_end = time.perf_counter_ns()
        wall_end = time.perf_counter_ns()
        cpu_end = time.process_time_ns()
        rss_after = _rss_peak_bytes()
    return {
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": LANE,
        "implementation": IMPLEMENTATION,
        "boundary": BOUNDARY,
        "status": "ok",
        "reason": None,
        "corpus_id": request["corpus_id"],
        "source_sha256": request["source_sha256"],
        "options_sha256": request["options_sha256"],
        "input_mode": request["input_mode"],
        "process_mode": request["process_mode"],
        "contract": contract,
        "raw_inventory": None,
        "metrics": {
            "wall_ns": wall_end - wall_start,
            "cpu_ns": cpu_end - cpu_start,
            "load_ns": load_end - load_start,
            "common_adapter_ns": validation_end - common_start,
            "rss_peak_before_bytes": rss_before,
            "rss_peak_after_bytes": rss_after,
            "rss_peak_increment_bytes": max(0, rss_after - rss_before),
            "temporary_bytes": len(source) if prepared_path is not None else 0,
            "object_count": len(ontology.get_components()),  # type: ignore[attr-defined]
            "phase_ns": {
                "horned_load": load_end - load_start,
                "common_contract": validation_start - common_start,
                "contract_validation": validation_end - validation_start,
            },
        },
        "timed_validation": {
            "schema": TIMED_VALIDATION_SCHEMA,
            "inside_timed_envelope": True,
            "full_contract_validation": True,
            "contract_sha256": contract["contract_sha256"],
            "validation_ns": validation_end - validation_start,
        },
        "artifact": _artifact(),
    }


def _validate_request(
    request: Mapping[str, Any],
    *,
    protocol_mode: str,
) -> tuple[bytes, DocumentFormat]:
    required = {
        "schema",
        "lane",
        "implementation",
        "boundary",
        "corpus_id",
        "source_b64",
        "source_sha256",
        "document_iri",
        "format",
        "options_sha256",
        "options",
        "input_mode",
        "process_mode",
        "expected_artifact_sha256",
        "expected_features",
        "expected_allocator",
        "expected_thread_ceiling",
        "expected_runner_revision",
        "expected_runner_sha256",
    }
    if set(request) != required:
        raise RunnerContractError("adapter request fields differ from schema v2")
    expected_scalars = {
        "schema": ADAPTER_REQUEST_SCHEMA,
        "lane": LANE,
        "implementation": IMPLEMENTATION,
        "boundary": BOUNDARY,
        "expected_artifact_sha256": ENGINE_SHA256,
        "expected_allocator": ALLOCATOR,
        "expected_thread_ceiling": THREAD_CEILING,
        "expected_runner_revision": RUNNER_REVISION,
        "expected_runner_sha256": _runner_sha256(),
    }
    for name, expected in expected_scalars.items():
        if request.get(name) != expected:
            raise RunnerContractError(f"adapter request {name} differs from runner pin")
    if request.get("expected_features") != list(FEATURES):
        raise RunnerContractError("adapter request features differ from runner pin")
    if request.get("input_mode") not in {"resident-bytes", "file"}:
        raise RunnerContractError("adapter request input mode is unsupported")
    expected_process_mode = "fresh-process" if protocol_mode == "fresh" else "steady-process"
    if request.get("process_mode") != expected_process_mode:
        raise RunnerContractError("adapter request process mode differs from protocol mode")
    for name in ("corpus_id", "document_iri"):
        if not isinstance(request.get(name), str) or not request[name]:
            raise RunnerContractError(f"adapter request {name} must be nonempty")
    for name in ("source_sha256", "options_sha256"):
        if not _is_sha256(request.get(name)):
            raise RunnerContractError(f"adapter request {name} must be lowercase SHA-256")
    try:
        source = base64.b64decode(request["source_b64"], validate=True)
    except (TypeError, ValueError) as error:
        raise RunnerContractError("adapter source is not strict base64") from error
    if hashlib.sha256(source).hexdigest() != request["source_sha256"]:
        raise RunnerContractError("adapter source differs from pinned SHA-256")
    try:
        format = DocumentFormat(request["format"])
    except (TypeError, ValueError) as error:
        raise RunnerContractError("adapter format is unsupported") from error
    options = default_options(format)
    if request.get("options") != options_inventory(options):
        raise RunnerContractError("py-horned runner supports only exact comparator options")
    if request["options_sha256"] != options_digest(options):
        raise RunnerContractError("adapter options digest differs from semantic options")
    IRI(cast(str, request["document_iri"]))
    return source, format


def _error_result(request: object, error: BaseException) -> dict[str, object]:
    value = request if isinstance(request, Mapping) else {}
    return _status_result(value, status="error", reason=_safe_reason(error))


def _status_result(
    request: Mapping[str, object],
    *,
    status: str,
    reason: str,
) -> dict[str, object]:
    if status not in {"not-run", "ineligible", "error"}:
        raise RunnerContractError("adapter non-success status is invalid")
    return {
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": LANE,
        "implementation": IMPLEMENTATION,
        "boundary": BOUNDARY,
        "status": status,
        "reason": reason[:MAX_REASON_CHARS],
        "corpus_id": request.get("corpus_id", "invalid-request"),
        "source_sha256": request.get("source_sha256", "0" * 64),
        "options_sha256": request.get("options_sha256", "0" * 64),
        "input_mode": request.get("input_mode", "resident-bytes"),
        "process_mode": request.get("process_mode", "fresh-process"),
        "contract": None,
        "raw_inventory": None,
        "metrics": {},
        "timed_validation": None,
        "artifact": _artifact(),
    }


def _safe_reason(error: BaseException) -> str:
    rendered = f"{type(error).__name__}: {error}".replace(str(REPOSITORY_ROOT), "<path>")
    rendered = " ".join(rendered.replace("\x00", " ").split())
    return (rendered or "external comparator failed")[:MAX_REASON_CHARS]


def _fresh_main() -> None:
    body = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(body) > MAX_REQUEST_BYTES:
        result = _error_result({}, RunnerContractError("adapter request exceeds size limit"))
    else:
        request: object = {}
        try:
            request = _json_object(body, "adapter request")
            result = _run_request(cast(Mapping[str, Any], request), protocol_mode="fresh")
        except Exception as error:
            result = _error_result(request, error)
    sys.stdout.buffer.write(_json_bytes(result))
    sys.stdout.buffer.flush()


def _persistent_main() -> None:
    _write_frame(
        {
            "schema": PERSISTENT_HANDSHAKE_SCHEMA,
            "protocol": PERSISTENT_PROTOCOL_SCHEMA,
            "lane": LANE,
            "implementation": IMPLEMENTATION,
            "boundary": BOUNDARY,
            "pid": os.getpid(),
            "request_schema": ADAPTER_REQUEST_SCHEMA,
            "result_schema": ADAPTER_RESULT_SCHEMA,
            "fresh_ontology_per_request": True,
            "artifact": _artifact(),
        }
    )
    instance_counter = 0
    while True:
        frame = _json_object(_read_frame(), "persistent request")
        schema = frame.get("schema")
        if schema == PERSISTENT_SHUTDOWN_SCHEMA:
            if set(frame) != {"schema", "protocol", "sequence"}:
                raise RunnerContractError("persistent shutdown fields differ")
            if frame.get("protocol") != PERSISTENT_PROTOCOL_SCHEMA:
                raise RunnerContractError("persistent shutdown protocol differs")
            sequence = _u64(frame.get("sequence"), "persistent shutdown sequence")
            _write_frame(
                {
                    "schema": PERSISTENT_SHUTDOWN_ACK_SCHEMA,
                    "protocol": PERSISTENT_PROTOCOL_SCHEMA,
                    "sequence": sequence,
                    "pid": os.getpid(),
                }
            )
            return
        if set(frame) != {"schema", "protocol", "sequence", "request"}:
            raise RunnerContractError("persistent request fields differ")
        if schema != PERSISTENT_REQUEST_SCHEMA:
            raise RunnerContractError("persistent request schema differs")
        if frame.get("protocol") != PERSISTENT_PROTOCOL_SCHEMA:
            raise RunnerContractError("persistent request protocol differs")
        sequence = _u64(frame.get("sequence"), "persistent request sequence")
        request = frame.get("request")
        try:
            if not isinstance(request, Mapping):
                raise RunnerContractError("persistent adapter request must be an object")
            result = _run_request(request, protocol_mode="persistent")
        except Exception as error:
            result = _error_result(request, error)
        instance_preimage = f"{os.getpid()}:{instance_counter}:{sequence}".encode("ascii")
        _write_frame(
            {
                "schema": PERSISTENT_RESPONSE_SCHEMA,
                "protocol": PERSISTENT_PROTOCOL_SCHEMA,
                "sequence": sequence,
                "ontology_instance_id": hashlib.sha256(instance_preimage).hexdigest(),
                "result": result,
            }
        )
        instance_counter += 1


def _read_frame() -> bytes:
    header = sys.stdin.buffer.readline(MAX_FRAME_HEADER_BYTES + 1)
    if not header:
        raise EOFError("persistent runner stdin closed")
    if len(header) > MAX_FRAME_HEADER_BYTES or not header.endswith(b"\n"):
        raise RunnerContractError("persistent frame header is invalid")
    raw_length = header[:-1]
    if not raw_length or (len(raw_length) > 1 and raw_length.startswith(b"0")):
        raise RunnerContractError("persistent frame length is noncanonical")
    try:
        length = int(raw_length.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RunnerContractError("persistent frame length is invalid") from error
    if length < 1 or length > MAX_REQUEST_BYTES:
        raise RunnerContractError("persistent frame length exceeds limit")
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length or sys.stdin.buffer.read(1) != b"\n":
        raise RunnerContractError("persistent frame is truncated")
    return payload


def _write_frame(value: Mapping[str, object]) -> None:
    payload = _json_bytes(value)
    sys.stdout.buffer.write(str(len(payload)).encode("ascii") + b"\n" + payload + b"\n")
    sys.stdout.buffer.flush()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _json_object(payload: bytes, name: str) -> dict[str, Any]:
    def reject_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise RunnerContractError(f"{name} contains duplicate JSON fields")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerContractError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise RunnerContractError(f"{name} must be a JSON object")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_CHARS for character in value)
    )


def _u64(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**64:
        raise RunnerContractError(f"{name} must be an unsigned 64-bit integer")
    return value


def _rss_peak_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value) if sys.platform == "darwin" else int(value) * 1024


def _fatal(error: BaseException) -> NoReturn:
    sys.stderr.write(_safe_reason(error) + "\n")
    raise SystemExit(1)


def main() -> None:
    expected_environment = {
        "PYOWL_CORE_COMPARATOR_LANE": LANE,
        "PYOWL_CORE_COMPARATOR_IMPLEMENTATION": IMPLEMENTATION,
        "PYOWL_CORE_COMPARATOR_BOUNDARY": BOUNDARY,
    }
    for name, expected in expected_environment.items():
        if os.environ.get(name) != expected:
            raise RunnerContractError(f"runner environment {name} differs from pin")
    if os.environ.get("RAYON_NUM_THREADS") != str(THREAD_CEILING):
        raise RunnerContractError("runner thread ceiling differs from pin")
    _verify_engine_install()
    protocol_mode = os.environ.get("PYOWL_CORE_COMPARATOR_PROTOCOL_MODE")
    if protocol_mode == "fresh":
        _fresh_main()
    elif protocol_mode == "persistent":
        _persistent_main()
    else:
        raise RunnerContractError("runner protocol mode is missing or unsupported")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        _fatal(error)
