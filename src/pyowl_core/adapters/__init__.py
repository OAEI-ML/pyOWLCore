"""Consumer adapter negotiation and zero-reparse conformance contracts.

Importing this package performs no plugin discovery, consumer import, native
probe, parsing, resolution, wire I/O, filesystem access, or network access.
"""

from .cache import (
    CacheKeyIssue,
    CacheKeyReport,
    CacheScope,
    ConsumerCacheKey,
    compare_cache_keys,
)
from .compatibility import (
    AdapterRequirement,
    CompatibilityIssue,
    CoreContract,
    NegotiationReport,
    negotiate_capabilities,
    negotiate_view,
    require_compatible_view,
)
from .conformance import (
    ConsumerAdapter,
    ConsumerObservation,
    HandoffReport,
    OperationCounters,
    OperationCounts,
    SnapshotProviderProbe,
    UnsupportedDisposition,
    UnsupportedFeature,
    UnsupportedFeatureReport,
    ViewContract,
    capture_view_contract,
    semantic_result_digest,
    verify_consumer_handoff,
)
from .features import (
    IDENTITY_AWARE_CONSUMER_FEATURES,
    KNOWN_CORE_FEATURES,
    STRUCTURAL_CONSUMER_FEATURES,
    CoreFeature,
)
from .plugins import PLUGIN_GROUPS, PluginMetadata, discover_plugin_metadata

__all__ = [
    "IDENTITY_AWARE_CONSUMER_FEATURES",
    "KNOWN_CORE_FEATURES",
    "PLUGIN_GROUPS",
    "STRUCTURAL_CONSUMER_FEATURES",
    "AdapterRequirement",
    "CacheKeyIssue",
    "CacheKeyReport",
    "CacheScope",
    "CompatibilityIssue",
    "ConsumerAdapter",
    "ConsumerCacheKey",
    "ConsumerObservation",
    "CoreContract",
    "CoreFeature",
    "HandoffReport",
    "NegotiationReport",
    "OperationCounters",
    "OperationCounts",
    "PluginMetadata",
    "SnapshotProviderProbe",
    "UnsupportedDisposition",
    "UnsupportedFeature",
    "UnsupportedFeatureReport",
    "ViewContract",
    "capture_view_contract",
    "compare_cache_keys",
    "discover_plugin_metadata",
    "negotiate_capabilities",
    "negotiate_view",
    "require_compatible_view",
    "semantic_result_digest",
    "verify_consumer_handoff",
]
