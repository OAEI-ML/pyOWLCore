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
    ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2,
    ENCODED_STRUCTURAL_DESCRIPTOR_V1,
    ENCODED_STRUCTURAL_DESCRIPTOR_V2,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V1,
    ENCODED_STRUCTURAL_SCHEMA_NAME_V2,
)
from tests.native.encoded_views._support import complete_constructor_snapshot
from tools.schema.encoded_view import check_generated_ledgers

ROOT = Path(__file__).parents[3]


def _load(name: str) -> dict[str, Any]:
    with (ROOT / "schemas" / name).open("rb") as stream:
        return cast(dict[str, Any], tomllib.load(stream))


def test_generated_schema_and_version_decisions_are_current() -> None:
    assert check_generated_ledgers(ROOT) == ()
    schema_v1 = _load("encoded-view-v1.toml")
    schema_v2 = _load("encoded-view-v2.toml")
    decision_v1 = _load("version-decision-v1.toml")
    decision_v2 = _load("version-decision-v2.toml")

    assert schema_v1["name"] == ENCODED_STRUCTURAL_SCHEMA_NAME_V1
    assert schema_v1["schema"] == schema_v1["model_schema"] == 1
    assert schema_v1["status"] == "frozen-advertised"
    assert schema_v1["capability_advertised"] is True
    assert schema_v1["descriptor_sha256"] == ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1.hex()
    assert (
        hashlib.sha256(ENCODED_STRUCTURAL_DESCRIPTOR_V1).hexdigest()
        == schema_v1["descriptor_sha256"]
    )

    assert schema_v2["name"] == ENCODED_STRUCTURAL_SCHEMA_NAME_V2
    assert schema_v2["schema"] == schema_v2["model_schema"] == 2
    assert schema_v2["status"] == "frozen-advertised"
    assert schema_v2["capability_advertised"] is True
    assert schema_v2["descriptor_sha256"] == ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2.hex()
    assert (
        hashlib.sha256(ENCODED_STRUCTURAL_DESCRIPTOR_V2).hexdigest()
        == schema_v2["descriptor_sha256"]
    )

    assert decision_v1["status"] == "frozen-advertised"
    assert decision_v1["capability_advertised"] is True
    assert decision_v1["public_contract"] == {
        "descriptor_digest_export": "pyowl_core.ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V1",
        "request_type_digest_attribute": "pyowl_core.EncodedStructuralView.DESCRIPTOR_SHA256",
        "descriptor_digest_type": "exact immutable bytes32",
        "materialization_required": False,
    }
    assert decision_v1["amendment"] == {
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
    # V1 remains byte-for-byte historical evidence and is never reinterpreted.
    assert tuple(decision_v1["api_version"]) == (0, 1)
    assert decision_v1["model_schema"] == 1
    assert tuple(decision_v1["wire_format"]) == (1, 1)
    assert decision_v1["package_version"] == "0.1.0"

    assert decision_v2["public_contract"]["descriptor_digest_export"] == (
        "pyowl_core.ENCODED_STRUCTURAL_DESCRIPTOR_SHA256_V2"
    )
    assert decision_v2["public_contract"]["generic_request_type"] == (
        "pyowl_core.EncodedStructuralViewV2"
    )
    assert tuple(decision_v2["api_version"]) == pyowl_core.API_VERSION
    assert decision_v2["adapter_protocol"] == pyowl_core.ADAPTER_PROTOCOL_VERSION
    assert decision_v2["model_schema"] == pyowl_core.MODEL_SCHEMA_VERSION
    assert tuple(decision_v2["wire_format"]) == pyowl_core.WIRE_FORMAT_VERSION
    assert decision_v2["package_version"] == "0.2.0"


def test_schema_constructor_and_descriptor_rows_cover_exact_registry() -> None:
    expected = [
        {
            "tag": spec.tag,
            "name": spec.tag_name,
            "category": spec.category,
            "fields": list(spec.fields),
        }
        for spec in m.CONSTRUCTOR_SPECS
    ]

    for version, descriptor_bytes in (
        (1, ENCODED_STRUCTURAL_DESCRIPTOR_V1),
        (2, ENCODED_STRUCTURAL_DESCRIPTOR_V2),
    ):
        schema = _load(f"encoded-view-v{version}.toml")
        descriptor = cast(dict[str, Any], json.loads(descriptor_bytes))
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


def test_model_v2_preserves_every_model_v1_tag_assignment() -> None:
    model_v1 = _load("model-v1.toml")
    model_v2 = _load("model-v2.toml")

    assert model_v1["schema"] == 1
    assert model_v2["schema"] == 2
    assert model_v2["namespace"] == model_v1["namespace"] == "model"
    assert model_v2["tag"] == model_v1["tag"]
    assert model_v2["canonical_domains"] == {
        "framing": "ASCII domain bytes, one trailing NUL byte, then framed payload",
        "anonymous_key": "pyowl-core:anonymous-key:v2",
        "blank_color": "pyowl-core:blank-color:v2",
        "blank_component_class": "pyowl-core:blank-component-class:v2",
        "blank_component_manifest": "pyowl-core:blank-component-manifest:v2",
        "blank_graph": "pyowl-core:blank-graph:v2",
        "composite_structural": "pyowl-core:composite-structural:v2",
        "datatype_policy": "datatype-policy:owl2-v1",
        "document_fingerprint": "pyowl-core:document-fingerprint:v2",
        "document_key": "pyowl-core:document-key:v2",
        "document_scope": "pyowl-core:document-scope:v2",
        "materialized_document_key": "pyowl-core:materialized-document-key:v2",
        "overlay_structural": "pyowl-core:overlay-structural:v2",
        "parser_blank_label": "pyowl-core:parser-blank-label:v2",
        "provisional_document_scope": "pyowl-core:provisional-document-scope:v2",
        "snapshot_document_scope": "pyowl-core:snapshot-document-scope:v2",
        "snapshot_logical": "pyowl-core:snapshot-logical:v2",
        "snapshot_signature": "pyowl-core:snapshot-signature:v2",
        "snapshot_structural": "pyowl-core:snapshot-structural:v2",
        "view_structure_context": "pyowl-core:view-structure-context:v2",
    }


def test_scalar_fallback_advertises_the_frozen_encoded_capability() -> None:
    snapshot = complete_constructor_snapshot()
    assert snapshot.capabilities.encoded_view_schemas == {
        ENCODED_STRUCTURAL_SCHEMA_NAME_V2: 2,
    }
    assert "encoded-structural-view" in snapshot.capabilities.features
