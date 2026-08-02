"""Frozen configuration values for public loading APIs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .limits import ParseLimits


class DocumentFormat(str, Enum):
    RDF_XML = "rdfxml"
    TURTLE = "turtle"
    OWL_XML = "owlxml"
    FUNCTIONAL = "functional"


class ImportPolicy(str, Enum):
    IGNORE = "ignore"
    RECORD_UNRESOLVED = "record_unresolved"
    RESOLVE_LOCAL = "resolve_local"
    RESOLVE_STRICT = "resolve_strict"


class BackendPreference(str, Enum):
    AUTO = "auto"
    PYTHON = "python"
    NATIVE = "native"


@dataclass(frozen=True, slots=True)
class LoadOptions:
    format: DocumentFormat | None = None
    imports: ImportPolicy = ImportPolicy.RESOLVE_LOCAL
    backend: BackendPreference = BackendPreference.AUTO
    limits: ParseLimits = field(default_factory=ParseLimits)
    offline: bool = True
    preserve_source_map: bool = False
    collect_provenance: bool = True
    validate_owl2_dl: bool = False
    deterministic: bool = True
    allow_partial_rdf_mapping: bool = False

    def __post_init__(self) -> None:
        if self.format is not None and not isinstance(self.format, DocumentFormat):
            raise TypeError("format must be DocumentFormat or None")
        if not isinstance(self.imports, ImportPolicy):
            raise TypeError("imports must be ImportPolicy")
        if not isinstance(self.backend, BackendPreference):
            raise TypeError("backend must be BackendPreference")
        if not isinstance(self.limits, ParseLimits):
            raise TypeError("limits must be ParseLimits")
        for name in (
            "offline",
            "preserve_source_map",
            "collect_provenance",
            "validate_owl2_dl",
            "deterministic",
            "allow_partial_rdf_mapping",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")


__all__ = [
    "BackendPreference",
    "DocumentFormat",
    "ImportPolicy",
    "LoadOptions",
]
