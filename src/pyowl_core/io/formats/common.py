"""Shared syntax-parser result and bounded accounting helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pyowl_core.cancellation import CancellationToken
from pyowl_core.diagnostics import SourceSpan
from pyowl_core.document import OntologyID
from pyowl_core.document.provenance import RDFMappingReport
from pyowl_core.exceptions import ResourceLimitError
from pyowl_core.limits import ParseLimits
from pyowl_core.model import IRI, Annotation, StructuralNode
from pyowl_core.model.axioms import AxiomNode


@dataclass(frozen=True, slots=True)
class ParsedOntology:
    ontology_id: OntologyID
    imports: tuple[IRI, ...]
    annotations: tuple[Annotation, ...]
    axioms: tuple[AxiomNode, ...]
    extensions: tuple[StructuralNode, ...] = ()
    prefixes: tuple[tuple[str, str], ...] = ()
    occurrences: tuple[tuple[StructuralNode, SourceSpan | None], ...] = ()
    rdf_mapping_report: RDFMappingReport | None = None
    decoded_codepoint_length: int = 0
    source_blank_labels: tuple[str, ...] = field(default=(), compare=False)


class ParseContext:
    __slots__ = ("cancel", "limits", "started", "terms")

    def __init__(
        self,
        limits: ParseLimits,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        self.limits = limits
        self.cancel = cancellation_token
        self.started = time.monotonic()
        self.terms = 0

    def check(self, stride: int = 1) -> None:
        self.terms += stride
        self.limits.enforce("max_terms", self.terms)
        if self.cancel is not None and (
            self.terms % self.limits.cancellation_check_interval < stride
        ):
            self.cancel.check()
        deadline = self.limits.deadline_seconds
        elapsed = time.monotonic() - self.started
        if deadline is not None and elapsed >= deadline:
            raise ResourceLimitError(
                "resource limit deadline_seconds exceeded",
                limit="deadline_seconds",
                observed=elapsed,
                allowed=deadline,
            )

    def depth(self, observed: int) -> None:
        self.limits.enforce("max_nesting_depth", observed)
        self.check()


__all__ = ["ParseContext", "ParsedOntology"]
