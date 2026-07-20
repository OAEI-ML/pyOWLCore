from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

try:
    import tomllib  # type: ignore[import-untyped, unused-ignore]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found, import-untyped, unused-ignore]

import pyowl_core
import pyowl_core.model as m
from pyowl_core.backends.native_views import (
    ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1,
    ENCODED_STRUCTURAL_DESCRIPTOR_V1,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
)
from tests.native.encoded_views._support import complete_constructor_snapshot
from tools.schema.encoded_view import check_generated_ledgers

ROOT = Path(__file__).parents[3]


def _load(name: str) -> dict[str, Any]:
    with (ROOT / "schemas" / name).open("rb") as stream:
        return cast(dict[str, Any], tomllib.load(stream))


def test_generated_schema_and_version_decision_are_current() -> None:
    assert check_generated_ledgers(ROOT) == ()
    schema = _load("encoded-view-v1.toml")
    decision = _load("version-decision-v1.toml")

    assert schema["name"] == ENCODED_STRUCTURAL_SCHEMA_NAME_V1
    assert schema["schema"] == schema["model_schema"] == 1
    assert schema["status"] == "frozen-unadvertised"
    assert schema["capability_advertised"] is False
    assert schema["descriptor_sha256"] == ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1.hex()
    assert (
        hashlib.sha256(ENCODED_STRUCTURAL_DESCRIPTOR_V1).hexdigest() == schema["descriptor_sha256"]
    )

    assert decision["status"] == "frozen-unadvertised"
    assert decision["capability_advertised"] is False
    assert decision["public_contract"] == {
        "descriptor_digest_export": "pyowl_core.ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1",
        "request_type_digest_attribute": "pyowl_core.EncodedStructuralView.DESCRIPTOR_SHA256",
        "descriptor_digest_type": "exact immutable bytes32",
        "materialization_required": False,
    }
    assert decision["amendment"] == {
        "id": "WP17-V1-anonymous-scope-map",
        "phase": "pre-advertisement",
        "decision": "retain schema version 1",
        "reason": (
            "segmented composites require explicit current-to-effective anonymous scope "
            "mappings for canonical parity"
        ),
        "field": (
            "anonymous_scope_map: sorted unique readonly 64-byte "
            "source-current/effective-target rows"
        ),
        "fingerprint": "covers exact anonymous_scope_map bytes",
    }
    assert tuple(decision["api_version"]) == pyowl_core.API_VERSION
    assert decision["adapter_protocol"] == pyowl_core.ADAPTER_PROTOCOL_VERSION
    assert decision["model_schema"] == pyowl_core.MODEL_SCHEMA_VERSION
    assert tuple(decision["wire_format"]) == pyowl_core.WIRE_FORMAT_VERSION
    assert decision["package_version"] == pyowl_core.__version__


def test_schema_constructor_and_descriptor_rows_cover_exact_registry() -> None:
    schema = _load("encoded-view-v1.toml")
    descriptor = cast(dict[str, Any], json.loads(ENCODED_STRUCTURAL_DESCRIPTOR_V1))
    expected = [
        {
            "tag": spec.tag,
            "name": spec.tag_name,
            "category": spec.category,
            "fields": list(spec.fields),
        }
        for spec in m.CONSTRUCTOR_SPECS
    ]

    assert schema["constructors"] == expected
    assert descriptor["constructors"] == expected
    assert schema["buffers"] == descriptor["buffers"]
    assert schema["component_kinds"] == descriptor["component_kinds"]
    assert schema["root_kinds"] == descriptor["root_kinds"]
    assert schema["root_validation"] == descriptor["root_tag_rules"]
    assert schema["segment_fields"] == descriptor["segment_fields"]
    assert schema["segment_roles"] == descriptor["segment_roles"]
    assert schema["segment_posting_modes"] == descriptor["segment_posting_modes"]
    assert len({row["tag"] for row in expected}) == len(m.CONSTRUCTOR_SPECS)


def test_fallback_exists_without_advertising_encoded_capability() -> None:
    snapshot = complete_constructor_snapshot()
    assert ENCODED_STRUCTURAL_SCHEMA_NAME_V1 not in snapshot.capabilities.encoded_view_schemas
    assert "encoded-structural-view" not in snapshot.capabilities.features
