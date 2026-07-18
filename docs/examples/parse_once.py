"""Executable Java-free parse-once and view-handoff example."""

from __future__ import annotations

from dataclasses import dataclass

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
    OntologyView,
    SnapshotProvider,
    apply_delta,
    coerce_snapshot,
    compose_views,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
)

_OPTIONS = LoadOptions(
    format=DocumentFormat.FUNCTIONAL,
    imports=ImportPolicy.IGNORE,
    backend=BackendPreference.PYTHON,
)


@dataclass(frozen=True, slots=True)
class ExactProvider(SnapshotProvider):
    """Minimal application provider: no work occurs in ``owl_snapshot``."""

    view: OntologyView

    def owl_snapshot(self) -> OntologyView:
        return self.view


@dataclass(frozen=True, slots=True)
class ConsumerSession:
    """Stand-in for a reasoner/projector compiler retaining the supplied view."""

    view: OntologyView


def _load(ontology_iri: str, class_iri: str) -> OntologyView:
    source = (
        f"Ontology(<{ontology_iri}> Declaration(Class(<{class_iri}>)))".encode()
    )
    return load_snapshot(
        source,
        document_iri=f"{ontology_iri}:document",
        options=_OPTIONS,
    )


def demonstrate() -> tuple[OntologyView, OntologyView, OntologyView]:
    """Return the original, overlay, and source/target composite views."""

    source = _load("urn:example:source", "urn:example:SourceClass")
    provider = ExactProvider(source)
    assert coerce_snapshot(source) is source
    assert coerce_snapshot(provider) is source

    # pyELK, pyHermiT, and the projector receive this same object and build
    # only their private derived IR.
    sessions = tuple(ConsumerSession(source) for _name in ("pyelk", "pyhermit", "projector"))
    assert all(session.view is source for session in sessions)

    added = Declaration(Class(IRI("urn:example:TrialClass")))
    overlay = apply_delta(source, OntologyDelta(add_axioms=CanonicalSet((added,))))
    assert overlay.base is source

    target = _load("urn:example:target", "urn:example:TargetClass")
    composite = compose_views(source, target, roles=("source", "target"))
    assert tuple(member.view for member in composite.members) == (source, target)

    # Cross-process/cache transport validates and reconstructs an equivalent
    # view; in-process consumers should keep using ``source`` by identity.
    transported = decode_snapshot(encode_snapshot(source))
    assert transported.logical_fingerprint == source.logical_fingerprint
    return source, overlay, composite


if __name__ == "__main__":
    demonstrate()

