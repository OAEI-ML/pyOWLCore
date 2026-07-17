"""OWL 2 annotations and annotation value aliases."""

from __future__ import annotations

from dataclasses import dataclass, field

from .base import CanonicalSet, StructuralNode, canonical_set, require_node
from .primitives import (
    IRI,
    AnnotationProperty,
    AnonymousIndividual,
    Literal,
)

AnnotationSubject = IRI | AnonymousIndividual
AnnotationValue = IRI | Literal | AnonymousIndividual


@dataclass(frozen=True, slots=True, eq=False)
class Annotation(StructuralNode):
    property: AnnotationProperty
    value: AnnotationValue
    annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)

    def __post_init__(self) -> None:
        require_node(self.property, AnnotationProperty, "Annotation.property")
        require_node(
            self.value,
            (IRI, Literal, AnonymousIndividual),
            "Annotation.value",
        )
        object.__setattr__(
            self,
            "annotations",
            canonical_set(self.annotations, Annotation, "Annotation.annotations"),
        )


def normalize_annotations(values: object) -> CanonicalSet[Annotation]:
    if isinstance(values, CanonicalSet):
        iterable = values
    else:
        try:
            iterable = iter(values)  # type: ignore[call-overload]
        except TypeError as error:
            raise TypeError("annotations must be an iterable of Annotation") from error
    return canonical_set(iterable, Annotation, "annotations")


__all__ = [
    "Annotation",
    "AnnotationSubject",
    "AnnotationValue",
    "normalize_annotations",
]
