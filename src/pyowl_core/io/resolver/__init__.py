"""Explicit built-in import resolvers."""

from .base import (
    ImportRequest,
    ImportResolver,
    ResolutionAttempt,
    ResolutionKind,
    ResolutionMode,
    ResolvedDocument,
    ResolvedSource,
    ResolverOutcome,
    resolver_configuration_fingerprint,
)
from .catalog import CatalogResolver
from .composite import CompositeResolver
from .directory import DirectoryNamingStrategy, DirectoryResolver
from .http import HttpAcquisitionCache, HttpCacheEntry, HttpResolver
from .mapping import MappingResolver, MappingTarget

__all__ = [
    "CatalogResolver",
    "CompositeResolver",
    "DirectoryNamingStrategy",
    "DirectoryResolver",
    "HttpAcquisitionCache",
    "HttpCacheEntry",
    "HttpResolver",
    "ImportRequest",
    "ImportResolver",
    "MappingResolver",
    "MappingTarget",
    "ResolutionAttempt",
    "ResolutionKind",
    "ResolutionMode",
    "ResolvedDocument",
    "ResolvedSource",
    "ResolverOutcome",
    "resolver_configuration_fingerprint",
]
