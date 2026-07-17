from __future__ import annotations

import json
from dataclasses import replace

import pytest

import pyowl_core
from pyowl_core.adapters import (
    IDENTITY_AWARE_CONSUMER_FEATURES,
    AdapterRequirement,
    CoreContract,
    negotiate_capabilities,
    negotiate_view,
    require_compatible_view,
)


def requirement(**changes: object) -> AdapterRequirement:
    baseline = AdapterRequirement(
        consumer="fixture-consumer",
        consumer_version="2.0.0",
        consumer_api="fixture-api/1",
        required_features=IDENTITY_AWARE_CONSUMER_FEATURES,
    )
    return replace(baseline, **changes)


def test_current_snapshot_negotiates_and_preserves_identity() -> None:
    snapshot = pyowl_core.load_snapshot(
        b"Ontology(<urn:adapter> Declaration(Class(<urn:adapter#A>)))",
        options=pyowl_core.LoadOptions(backend=pyowl_core.BackendPreference.PYTHON),
    )
    report = negotiate_view(snapshot, requirement())

    assert report.compatible
    assert report.issues == ()
    assert require_compatible_view(snapshot, requirement()) is snapshot
    assert report.to_dict()["view"] == {
        "adapter_protocol": 1,
        "model_schema": 1,
        "wire_format": [1, 1],
        "features": sorted(snapshot.capabilities.features),
        "encoded_view_schemas": {},
        "backend": "python",
    }


def test_negotiation_reports_every_independent_mismatch() -> None:
    capabilities = pyowl_core.CoreCapabilities(
        adapter_protocol=2,
        model_schema=3,
        wire_format=(2, 0),
        features=frozenset({"owl2-structural"}),
        encoded_view_schemas={"bulk": 1},
        backend="foreign",
    )
    core = CoreContract("0.2.0", (0, 2), 1, 1, (1, 1))
    selected = requirement(
        minimum_wire_minor=2,
        required_features=frozenset({"owl2-structural", "import-manifest", "source-map"}),
        required_encoded_view_schemas={"bulk": 2, "columns": 1},
    )

    report = negotiate_capabilities(capabilities, selected, core=core)
    codes = [issue.code for issue in report.issues]

    assert not report.compatible
    assert codes.count("MISSING_FEATURE") == 2
    assert set(codes) == {
        "ADAPTER_PROTOCOL_MISMATCH",
        "CORE_API_MISMATCH",
        "CORE_PACKAGE_API_MISMATCH",
        "CORE_VIEW_ADAPTER_DIVERGENCE",
        "CORE_VIEW_MODEL_DIVERGENCE",
        "CORE_VIEW_WIRE_DIVERGENCE",
        "ENCODED_VIEW_SCHEMA_TOO_OLD",
        "MISSING_ENCODED_VIEW",
        "MISSING_FEATURE",
        "MODEL_SCHEMA_MISMATCH",
        "WIRE_MAJOR_MISMATCH",
        "WIRE_MINOR_TOO_OLD",
    }
    with pytest.raises(pyowl_core.AdapterCompatibilityError) as caught:
        report.raise_for_errors()
    assert caught.value.code == "ADAPTER_COMPATIBILITY"
    assert caught.value.diagnostic is not None
    details = caught.value.diagnostic.details
    assert details["issue_count"] == len(report.issues)
    assert len(json.loads(str(details["issues"]))) == len(report.issues)


def test_malformed_capabilities_fail_without_duck_typed_fallback() -> None:
    report = negotiate_capabilities(object(), requirement())

    assert [issue.code for issue in report.issues] == ["ADAPTER_CAPABILITIES_TYPE"]
    with pytest.raises(pyowl_core.AdapterCompatibilityError, match="1 issue"):
        report.raise_for_errors()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("consumer", ""),
        ("adapter_protocol", 0),
        ("minimum_wire_minor", -1),
        ("required_features", frozenset({""})),
        ("required_encoded_view_schemas", {"bulk": 0}),
    ],
)
def test_requirement_validation_fails_early(field: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(requirement(), **{field: value})
