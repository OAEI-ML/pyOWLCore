"""Immutable canonical repair deltas and replay policy."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.exceptions import DeltaError
from pyowl_core.model import (
    Annotation,
    AnonymousIndividual,
    CanonicalSet,
    StructuralNode,
    encode_varint,
    walk,
)
from pyowl_core.model.axioms import AxiomNode

from .document import Fingerprint

_MAX_METADATA_ENTRIES = 64
_MAX_METADATA_KEY_BYTES = 128
_MAX_METADATA_VALUE_BYTES = 4_096
_MAX_METADATA_TOTAL_BYTES = 64 * 1_024


class DeltaPolicy(str, Enum):
    STRICT = "strict"
    IDEMPOTENT = "idempotent"


@dataclass(frozen=True, slots=True)
class OntologyDelta:
    """Canonical add/remove sets; base-sensitive checks occur in ``apply_delta``."""

    add_axioms: CanonicalSet[AxiomNode] = field(default_factory=CanonicalSet)
    remove_axioms: CanonicalSet[AxiomNode] = field(default_factory=CanonicalSet)
    add_ontology_annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)
    remove_ontology_annotations: CanonicalSet[Annotation] = field(default_factory=CanonicalSet)
    expected_base_fingerprint: Fingerprint | None = None
    metadata: Mapping[str, str] = field(default_factory=FrozenMap, compare=False)
    policy: DeltaPolicy = DeltaPolicy.STRICT

    def __post_init__(self) -> None:
        additions = _axiom_set(self.add_axioms, "add_axioms")
        removals = _axiom_set(self.remove_axioms, "remove_axioms")
        annotation_additions = _annotation_set(
            self.add_ontology_annotations, "add_ontology_annotations"
        )
        annotation_removals = _annotation_set(
            self.remove_ontology_annotations, "remove_ontology_annotations"
        )
        if any(item in removals for item in additions):
            raise DeltaError(
                "one axiom cannot be both added and removed",
                code="DELTA_AXIOM_CONFLICT",
            )
        if any(item in annotation_removals for item in annotation_additions):
            raise DeltaError(
                "one ontology annotation cannot be both added and removed",
                code="DELTA_ANNOTATION_CONFLICT",
            )
        if self.expected_base_fingerprint is not None and not isinstance(
            self.expected_base_fingerprint, Fingerprint
        ):
            raise TypeError("expected_base_fingerprint must be Fingerprint or None")
        if not isinstance(self.policy, DeltaPolicy):
            raise TypeError("policy must be DeltaPolicy")
        metadata = _freeze_metadata(self.metadata)
        _validate_anonymous((*additions, *removals, *annotation_additions, *annotation_removals))
        object.__setattr__(self, "add_axioms", additions)
        object.__setattr__(self, "remove_axioms", removals)
        object.__setattr__(self, "add_ontology_annotations", annotation_additions)
        object.__setattr__(self, "remove_ontology_annotations", annotation_removals)
        object.__setattr__(self, "metadata", metadata)

    @property
    def entry_count(self) -> int:
        return (
            len(self.add_axioms)
            + len(self.remove_axioms)
            + len(self.add_ontology_annotations)
            + len(self.remove_ontology_annotations)
        )

    @property
    def is_empty(self) -> bool:
        return self.entry_count == 0

    def canonical_bytes(self, *, include_provenance: bool = False) -> bytes:
        pieces = [b"pyowl-core:ontology-delta:v1\x00"]
        for collection in (
            self.add_axioms,
            self.remove_axioms,
            self.add_ontology_annotations,
            self.remove_ontology_annotations,
        ):
            pieces.append(encode_varint(len(collection)))
            for item in collection:
                encoded = item.canonical_bytes()
                pieces.append(encode_varint(len(encoded)) + encoded)
        if include_provenance:
            pieces.append(self.policy.value.encode("ascii") + b"\x00")
            expected = self.expected_base_fingerprint
            pieces.append(
                b"0"
                if expected is None
                else b"1"
                + encode_varint(len(expected.algorithm))
                + expected.algorithm.encode("ascii")
                + encode_varint(expected.schema)
                + expected.digest
            )
            pieces.append(encode_varint(len(self.metadata)))
            for key, value in self.metadata.items():
                encoded_key = key.encode("utf-8")
                encoded_value = value.encode("utf-8")
                pieces.append(encode_varint(len(encoded_key)) + encoded_key)
                pieces.append(encode_varint(len(encoded_value)) + encoded_value)
        return b"".join(pieces)

    @property
    def provenance_digest(self) -> bytes:
        return hashlib.sha256(self.canonical_bytes(include_provenance=True)).digest()


def combine_deltas(first: OntologyDelta, second: OntologyDelta) -> OntologyDelta:
    """Collapse two already-validated sequential deltas relative to one anchor."""

    if not isinstance(first, OntologyDelta) or not isinstance(second, OntologyDelta):
        raise TypeError("first and second must be OntologyDelta")
    return OntologyDelta(
        add_axioms=CanonicalSet(
            (
                *((item for item in first.add_axioms if item not in second.remove_axioms)),
                *(item for item in second.add_axioms if item not in first.remove_axioms),
            )
        ),
        remove_axioms=CanonicalSet(
            (
                *(item for item in first.remove_axioms if item not in second.add_axioms),
                *(item for item in second.remove_axioms if item not in first.add_axioms),
            )
        ),
        add_ontology_annotations=CanonicalSet(
            (
                *(
                    item
                    for item in first.add_ontology_annotations
                    if item not in second.remove_ontology_annotations
                ),
                *(
                    item
                    for item in second.add_ontology_annotations
                    if item not in first.remove_ontology_annotations
                ),
            )
        ),
        remove_ontology_annotations=CanonicalSet(
            (
                *(
                    item
                    for item in first.remove_ontology_annotations
                    if item not in second.add_ontology_annotations
                ),
                *(
                    item
                    for item in second.remove_ontology_annotations
                    if item not in first.add_ontology_annotations
                ),
            )
        ),
        policy=DeltaPolicy.STRICT,
    )


def _axiom_set(values: Iterable[AxiomNode], name: str) -> CanonicalSet[AxiomNode]:
    result = values if isinstance(values, CanonicalSet) else CanonicalSet(values)
    if not all(isinstance(item, AxiomNode) for item in result):
        raise TypeError(f"{name} must contain OWL axioms")
    return result


def _annotation_set(values: Iterable[Annotation], name: str) -> CanonicalSet[Annotation]:
    result = values if isinstance(values, CanonicalSet) else CanonicalSet(values)
    if not all(isinstance(item, Annotation) for item in result):
        raise TypeError(f"{name} must contain Annotation values")
    return result


def _freeze_metadata(values: Mapping[str, str]) -> FrozenMap[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError("metadata must be a mapping")
    if len(values) > _MAX_METADATA_ENTRIES:
        raise DeltaError("delta metadata has too many entries", code="DELTA_METADATA_LIMIT")
    retained: dict[str, str] = {}
    total = 0
    for key, value in values.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise TypeError("delta metadata must map nonempty strings to strings")
        try:
            key_size = len(key.encode("utf-8"))
            value_size = len(value.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise DeltaError(
                "delta metadata must contain valid UTF-8 text",
                code="DELTA_METADATA_ENCODING",
            ) from error
        if key_size > _MAX_METADATA_KEY_BYTES or value_size > _MAX_METADATA_VALUE_BYTES:
            raise DeltaError("delta metadata entry is too large", code="DELTA_METADATA_LIMIT")
        total += key_size + value_size
        retained[key] = value
    if total > _MAX_METADATA_TOTAL_BYTES:
        raise DeltaError("delta metadata is too large", code="DELTA_METADATA_LIMIT")
    return freeze_mapping(retained)


def _validate_anonymous(values: Iterable[StructuralNode]) -> None:
    for root in values:
        for value in walk(root):
            if isinstance(value, AnonymousIndividual) and (
                len(value.document_scope) != 32 or not value.local_key
            ):
                raise DeltaError(
                    "anonymous individual has no valid document/builder scope",
                    code="DELTA_ANONYMOUS_SCOPE",
                )


__all__ = ["DeltaPolicy", "OntologyDelta", "combine_deltas"]
