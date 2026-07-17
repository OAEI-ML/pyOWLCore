"""Deterministic benchmark inputs with exactly known structural counts."""

from __future__ import annotations

from dataclasses import dataclass

from pyowl_core import DocumentFormat

_BASE = "https://example.org/pyowl-core/benchmark"


@dataclass(frozen=True, slots=True)
class SyntheticCounts:
    """Counts implied by a generated equivalent ontology."""

    bytes: int
    triples: int
    axioms: int
    entities: int
    imports: int


def equivalent_source(format: DocumentFormat, classes: int) -> bytes:
    """Render one declaration/subclass chain equivalently in every required syntax."""

    if not isinstance(format, DocumentFormat):
        raise TypeError("format must be DocumentFormat")
    _validate_classes(classes)
    if format is DocumentFormat.FUNCTIONAL:
        return _functional(classes)
    if format is DocumentFormat.OWL_XML:
        return _owl_xml(classes)
    if format is DocumentFormat.TURTLE:
        return _turtle(classes)
    if format is DocumentFormat.RDF_XML:
        return _rdf_xml(classes)
    raise AssertionError(format)


def equivalent_counts(format: DocumentFormat, classes: int) -> SyntheticCounts:
    """Return exact byte, triple, structural-axiom, entity and import counts."""

    source = equivalent_source(format, classes)
    return SyntheticCounts(
        bytes=len(source),
        triples=1 + classes + max(0, classes - 1),
        axioms=classes + max(0, classes - 1),
        entities=classes,
        imports=0,
    )


def import_diamond() -> tuple[bytes, dict[str, bytes]]:
    """Return a local import diamond whose shared leaf has one exact source digest."""

    root = f"{_BASE}/imports/root"
    left = f"{_BASE}/imports/left"
    right = f"{_BASE}/imports/right"
    shared = f"{_BASE}/imports/shared"
    return (
        _functional_document(root, (left, right), ("Root",)),
        {
            left: _functional_document(left, (shared,), ("Left",)),
            right: _functional_document(right, (shared,), ("Right",)),
            shared: _functional_document(shared, (), ("Shared",)),
        },
    )


def adversarial_deep_functional(depth: int = 64) -> bytes:
    """Generate a bounded deeply nested expression for limit/cancellation lanes."""

    if not isinstance(depth, int) or isinstance(depth, bool):
        raise TypeError("depth must be int")
    if depth < 1:
        raise ValueError("depth must be at least one")
    expression = f"<{_BASE}#Leaf>"
    for _ in range(depth):
        expression = f"ObjectComplementOf({expression})"
    return f"Ontology(SubClassOf(<{_BASE}#Root> {expression}))\n".encode()


def annotation_list_turtle(items: int = 128) -> bytes:
    """Generate an annotation-heavy RDF-list input without network dependencies."""

    if not isinstance(items, int) or isinstance(items, bool):
        raise TypeError("items must be int")
    if items < 2:
        raise ValueError("items must be at least two")
    members = " ".join(f":C{index:06d}" for index in range(items))
    declarations = "\n".join(
        f':C{index:06d} a owl:Class ; rdfs:label "Class {index:06d}"@en .' for index in range(items)
    )
    return (
        "@prefix : <https://example.org/pyowl-core/benchmark#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        f"<{_BASE}> a owl:Ontology .\n"
        f"{declarations}\n"
        f":Union a owl:Class ; owl:equivalentClass [ a owl:Class ; owl:unionOf ({members}) ] .\n"
    ).encode()


def _validate_classes(classes: int) -> None:
    if not isinstance(classes, int) or isinstance(classes, bool):
        raise TypeError("classes must be int")
    if classes < 1:
        raise ValueError("classes must be at least one")


def _iri(index: int) -> str:
    return f"{_BASE}#C{index:06d}"


def _functional(classes: int) -> bytes:
    declarations = "\n".join(f"  Declaration(Class(<{_iri(index)}>))" for index in range(classes))
    subclasses = "\n".join(
        f"  SubClassOf(<{_iri(index)}> <{_iri(index + 1)}>)" for index in range(classes - 1)
    )
    body = declarations if not subclasses else f"{declarations}\n{subclasses}"
    return f"Ontology(<{_BASE}>\n{body}\n)\n".encode()


def _owl_xml(classes: int) -> bytes:
    declarations = "\n".join(
        f'  <Declaration><Class IRI="{_iri(index)}"/></Declaration>' for index in range(classes)
    )
    subclasses = "\n".join(
        f'  <SubClassOf><Class IRI="{_iri(index)}"/><Class IRI="{_iri(index + 1)}"/></SubClassOf>'
        for index in range(classes - 1)
    )
    body = declarations if not subclasses else f"{declarations}\n{subclasses}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="{_BASE}">\n'
        f"{body}\n</Ontology>\n"
    ).encode()


def _turtle(classes: int) -> bytes:
    rows: list[str] = [
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        f"<{_BASE}> a owl:Ontology .",
    ]
    for index in range(classes):
        suffix = " ." if index + 1 == classes else f" ; rdfs:subClassOf <{_iri(index + 1)}> ."
        rows.append(f"<{_iri(index)}> a owl:Class{suffix}")
    return ("\n".join(rows) + "\n").encode("utf-8")


def _rdf_xml(classes: int) -> bytes:
    rows = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"',
        '         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"',
        '         xmlns:owl="http://www.w3.org/2002/07/owl#">',
        f'  <owl:Ontology rdf:about="{_BASE}"/>',
    ]
    for index in range(classes):
        if index + 1 == classes:
            rows.append(f'  <owl:Class rdf:about="{_iri(index)}"/>')
        else:
            rows.extend(
                (
                    f'  <owl:Class rdf:about="{_iri(index)}">',
                    f'    <rdfs:subClassOf rdf:resource="{_iri(index + 1)}"/>',
                    "  </owl:Class>",
                )
            )
    rows.append("</rdf:RDF>")
    return ("\n".join(rows) + "\n").encode("utf-8")


def _functional_document(
    ontology_iri: str,
    imports: tuple[str, ...],
    classes: tuple[str, ...],
) -> bytes:
    import_rows = " ".join(f"Import(<{value}>)" for value in imports)
    declarations = " ".join(f"Declaration(Class(<{ontology_iri}#{value}>))" for value in classes)
    body = " ".join(item for item in (import_rows, declarations) if item)
    return f"Ontology(<{ontology_iri}> {body})\n".encode()


__all__ = [
    "SyntheticCounts",
    "adversarial_deep_functional",
    "annotation_list_turtle",
    "equivalent_counts",
    "equivalent_source",
    "import_diamond",
]
