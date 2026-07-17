"""Explicit metadata-only discovery for trusted core extension points."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata

from pyowl_core.diagnostics import Diagnostic, Severity
from pyowl_core.exceptions import AdapterCompatibilityError

PLUGIN_GROUPS = frozenset(
    {
        "pyowl_core.parsers",
        "pyowl_core.resolvers",
        "pyowl_core.views",
        "pyowl_core.writers",
    }
)


@dataclass(frozen=True, slots=True, order=True)
class PluginMetadata:
    """Import-free entry-point identity.

    The record intentionally contains strings only. Discovery never calls
    ``EntryPoint.load`` and cannot instantiate or probe plugin code.
    """

    group: str
    name: str
    value: str
    module: str
    attribute: str | None
    distribution: str | None
    distribution_version: str | None

    def __post_init__(self) -> None:
        if self.group not in PLUGIN_GROUPS:
            raise ValueError("unsupported pyowl-core plugin group")
        for name in ("name", "value", "module"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a nonempty string")
        for name in ("attribute", "distribution", "distribution_version"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a nonempty string or None")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "group": self.group,
            "name": self.name,
            "value": self.value,
            "module": self.module,
            "attribute": self.attribute,
            "distribution": self.distribution,
            "distribution_version": self.distribution_version,
        }


def discover_plugin_metadata(group: str) -> tuple[PluginMetadata, ...]:
    """List one reserved group without importing or loading any plugin."""

    if group not in PLUGIN_GROUPS:
        raise ValueError(f"group must be one of {sorted(PLUGIN_GROUPS)!r}")
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        entries = tuple(discovered.select(group=group))
    else:  # pragma: no cover - retained for older importlib-metadata behavior
        entries = tuple(discovered.get(group, ()))
    records = tuple(sorted(_metadata(entry, group) for entry in entries))
    names: set[str] = set()
    duplicates: set[str] = set()
    for record in records:
        if record.name in names:
            duplicates.add(record.name)
        names.add(record.name)
    if duplicates:
        names_text = ",".join(sorted(duplicates))
        message = f"duplicate plugin names in {group}: {names_text}"
        diagnostic = Diagnostic(
            code="ADAPTER_PLUGIN_COLLISION",
            severity=Severity.ERROR,
            message=message,
            details={"group": group, "names": names_text},
        )
        raise AdapterCompatibilityError(message, diagnostic=diagnostic)
    return records


def _metadata(entry: metadata.EntryPoint, group: str) -> PluginMetadata:
    distribution = getattr(entry, "dist", None)
    distribution_name: str | None = None
    distribution_version: str | None = None
    if distribution is not None:
        raw_name = distribution.metadata.get("Name")
        raw_version = getattr(distribution, "version", None)
        distribution_name = raw_name if isinstance(raw_name, str) and raw_name else None
        distribution_version = raw_version if isinstance(raw_version, str) and raw_version else None
    module = getattr(entry, "module", None)
    attribute = getattr(entry, "attr", None)
    if not isinstance(module, str) or not module:
        module, _, parsed_attribute = entry.value.partition(":")
        attribute = parsed_attribute or None
    return PluginMetadata(
        group=group,
        name=entry.name,
        value=entry.value,
        module=module,
        attribute=attribute if isinstance(attribute, str) and attribute else None,
        distribution=distribution_name,
        distribution_version=distribution_version,
    )


__all__ = ["PLUGIN_GROUPS", "PluginMetadata", "discover_plugin_metadata"]
