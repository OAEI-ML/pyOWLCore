from __future__ import annotations

import os
import struct
from typing import Any, cast

import pytest

from pyowl_core import (
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    ParseLimits,
    canonical_bytes,
    encode_snapshot,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.backends.native_handoff_v2 import (
    NativeFacadeCollectionV2,
    NativeFacadeContainsRequestV2,
    NativeFacadePageRequestV2,
    NativeFacadeScopeV2,
)
from pyowl_core.backends.native_ingestion import (
    _publish_structural_closure_snapshot_v2,
    _publish_structural_snapshot_v2,
)
from pyowl_core.model import IRI, Class, Declaration, StructuralNode, constructor_spec
from tests.generated.model.fixtures import model_fixtures
from tests.native.foundation._support import NativeTestExtension, load_extension
from tests.native.publication_handoff._support_v2 import (
    fingerprint_evidence,
    fingerprint_preimages,
    publication,
)
from tests.native.publication_handoff.test_owl2_dl_v2 import _validated_fixture
from tests.unit.wire.conftest import snapshot


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    if not hasattr(selected, "_component_allocation_probe_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail("selected native test-hooks artifact lacks _component_allocation_probe_v1")
        pytest.skip("native retained-component allocation hook is unavailable")
    return selected


def _fixture_parameters() -> tuple[object, ...]:
    return tuple(
        pytest.param(value, id=f"{constructor.__name__}-tag-{constructor_spec(value).tag}")
        for constructor, value in model_fixtures().items()
    )


def _invoke(
    extension: NativeTestExtension,
    canonical: bytes,
    config: bytes,
    phase: str,
    fail_after: int | None,
) -> tuple[bytes, int]:
    return extension._component_allocation_probe_v1(
        memoryview(canonical),
        config,
        phase,
        fail_after,
    )


@pytest.mark.parametrize("fixture", _fixture_parameters())
def test_every_retained_constructor_allocation_checkpoint_fails_closed(
    extension: NativeTestExtension,
    fixture: StructuralNode,
) -> None:
    expected = canonical_bytes(fixture)
    config = native._encode_config(ParseLimits(), None, verify=True)

    for phase in ("build", "freeze", "encode"):
        output, allocations = _invoke(extension, expected, config, phase, None)
        assert output == expected
        assert allocations > 0

        for fail_after in range(allocations):
            with pytest.raises(extension._NativeError) as raised:
                _invoke(extension, expected, config, phase, fail_after)
            assert raised.value.args == (
                "NATIVE_WIRE_LIMIT",
                "injected native component allocation failure",
            )

        boundary_output, boundary_allocations = _invoke(
            extension,
            expected,
            config,
            phase,
            allocations,
        )
        assert boundary_output == expected
        assert boundary_allocations == allocations


def test_component_allocation_probe_rejects_an_unknown_phase(
    extension: NativeTestExtension,
) -> None:
    expected = canonical_bytes(next(iter(model_fixtures().values())))
    config = native._encode_config(ParseLimits(), None, verify=True)
    with pytest.raises(ValueError, match="must be build, freeze, or encode"):
        _invoke(extension, expected, config, "publish", None)


@pytest.fixture(scope="module")
def wire_extension(extension: NativeTestExtension) -> NativeTestExtension:
    if not hasattr(extension, "_wire_allocation_probe_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail("selected native test-hooks artifact lacks _wire_allocation_probe_v1")
        pytest.skip("native wire allocation hook is unavailable")
    return extension


def test_wire_validation_allocation_checkpoints_fail_closed(
    wire_extension: NativeTestExtension,
) -> None:
    encoded = bytearray(encode_snapshot(snapshot("Allocation")))
    original = bytes(encoded)
    config = native._encode_config(ParseLimits(), None, verify=True)

    receipt, allocations = wire_extension._wire_allocation_probe_v1(
        memoryview(encoded),
        config,
        None,
    )
    assert receipt[:8] == b"PYNVAL1\0"
    assert allocations > 0

    for fail_after in range(allocations):
        with pytest.raises(wire_extension._NativeError) as raised:
            wire_extension._wire_allocation_probe_v1(
                memoryview(encoded),
                config,
                fail_after,
            )
        assert raised.value.args == (
            "NATIVE_WIRE_LIMIT",
            "injected native wire allocation failure",
        )
        assert encoded == original

    boundary_receipt, boundary_allocations = wire_extension._wire_allocation_probe_v1(
        memoryview(encoded),
        config,
        allocations,
    )
    assert boundary_receipt == receipt
    assert boundary_allocations == allocations


@pytest.fixture(scope="module")
def parser_extension(extension: NativeTestExtension) -> NativeTestExtension:
    if not hasattr(extension, "_parser_allocation_probe_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail("selected native test-hooks artifact lacks _parser_allocation_probe_v1")
        pytest.skip("native parser allocation hook is unavailable")
    return extension


def _parser_request() -> bytearray:
    source = (
        b"Prefix(:=<urn:allocation:>) Ontology(<urn:ontology> "
        b"Import(<urn:import>) Annotation(:label \"hello\"@EN) "
        b"Declaration(Class(:C)) "
        b"SubClassOf(:C ObjectSomeValuesFrom(:property :D)))"
    )
    return bytearray(
        struct.pack("<8sHHQ", b"PYNFSS1\0", 1, 0, len(source)) + source
    )


def test_parser_allocation_budget_checkpoints_fail_closed(
    parser_extension: NativeTestExtension,
) -> None:
    request = _parser_request()
    original = bytes(request)
    config = native._encode_config(ParseLimits(), None, verify=True)

    output, allocations = parser_extension._parser_allocation_probe_v1(
        memoryview(request),
        config,
        None,
    )
    assert output[:8] == b"PYNFSSR1"
    assert allocations == 38

    for fail_after in range(allocations):
        with pytest.raises(parser_extension._NativeError) as raised:
            parser_extension._parser_allocation_probe_v1(
                memoryview(request),
                config,
                fail_after,
            )
        assert raised.value.args == (
            "NATIVE_WIRE_LIMIT",
            "injected native parser allocation failure",
        )
        assert request == original

    boundary_output, boundary_allocations = parser_extension._parser_allocation_probe_v1(
        memoryview(request),
        config,
        allocations,
    )
    assert boundary_output == output
    assert boundary_allocations == allocations
    assert request == original


@pytest.fixture(scope="module")
def parser_bridge_extension(extension: NativeTestExtension) -> NativeTestExtension:
    if not hasattr(extension, "_parser_bridge_allocation_probe_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_parser_bridge_allocation_probe_v1"
            )
        pytest.skip("native parser bridge allocation hook is unavailable")
    return extension


def test_parser_bridge_allocation_checkpoints_fail_before_publication(
    parser_bridge_extension: NativeTestExtension,
) -> None:
    request = _parser_request()
    original = bytes(request)
    config = bytearray(native._encode_config(ParseLimits(), None, verify=True))
    original_config = bytes(config)

    output, allocations = parser_bridge_extension._parser_bridge_allocation_probe_v1(
        memoryview(request),
        config,
        None,
    )
    assert output[:8] == b"PYNFSSR1"
    assert allocations == 13
    assert config == original_config

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native parser bridge allocation failure$",
        ):
            parser_bridge_extension._parser_bridge_allocation_probe_v1(
                memoryview(request),
                config,
                fail_after,
            )
        assert request == original
        assert config == original_config

    boundary_output, boundary_allocations = (
        parser_bridge_extension._parser_bridge_allocation_probe_v1(
            memoryview(request),
            config,
            allocations,
        )
    )
    assert boundary_output == output
    assert boundary_allocations == allocations
    assert request == original
    assert config == original_config


@pytest.fixture(scope="module")
def index_bridge_extension(extension: NativeTestExtension) -> NativeTestExtension:
    if not hasattr(extension, "_index_bridge_allocation_probe_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_index_bridge_allocation_probe_v1"
            )
        pytest.skip("native index bridge allocation hook is unavailable")
    return extension


def _index_bridge_inputs() -> tuple[bytearray, bytearray]:
    axiom = next(snapshot("IndexBridge").iter_axioms())
    encoded = canonical_bytes(axiom)
    source = bytearray(struct.pack("<8sHHQ", b"PYNIDXS1", 1, 0, 1))
    source.extend(struct.pack("<Q", len(encoded)))
    source.extend(encoded)
    request = bytearray(
        b"PYNIDXQ1" + native._encode_config(ParseLimits(), None, verify=False)
    )
    return source, request


def test_index_bridge_allocation_checkpoints_fail_before_publication(
    index_bridge_extension: NativeTestExtension,
) -> None:
    source, request = _index_bridge_inputs()
    original_source = bytes(source)
    original_request = bytes(request)

    output, allocations = index_bridge_extension._index_bridge_allocation_probe_v1(
        memoryview(source),
        memoryview(request),
        None,
    )
    assert output[:8] == b"PYNIDXR1"
    assert allocations == 13
    assert source == original_source
    assert request == original_request

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native index bridge allocation failure$",
        ):
            index_bridge_extension._index_bridge_allocation_probe_v1(
                memoryview(source),
                memoryview(request),
                fail_after,
            )
        assert source == original_source
        assert request == original_request

    boundary_output, boundary_allocations = (
        index_bridge_extension._index_bridge_allocation_probe_v1(
            memoryview(source),
            memoryview(request),
            allocations,
        )
    )
    assert boundary_output == output
    assert boundary_allocations == allocations
    assert source == original_source
    assert request == original_request


@pytest.fixture(scope="module")
def foundation_bridge_extension(extension: NativeTestExtension) -> NativeTestExtension:
    if not hasattr(extension, "_foundation_bridge_allocation_probe_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_foundation_bridge_allocation_probe_v1"
            )
        pytest.skip("native foundation bridge allocation hook is unavailable")
    return extension


@pytest.mark.parametrize(
    "operation",
    ("validate-canonical", "validate-wire", "roundtrip-wire"),
)
def test_foundation_bridge_allocation_checkpoints_fail_before_publication(
    foundation_bridge_extension: NativeTestExtension,
    operation: str,
) -> None:
    fixture = snapshot("FoundationBridge")
    if operation == "validate-canonical":
        source = bytearray(canonical_bytes(next(fixture.iter_axioms())))
    else:
        source = bytearray(encode_snapshot(fixture))
    original_source = bytes(source)
    config = bytearray(native._encode_config(ParseLimits(), None, verify=True))
    original_config = bytes(config)

    output, allocations = (
        foundation_bridge_extension._foundation_bridge_allocation_probe_v1(
            operation,
            memoryview(source),
            memoryview(config),
            None,
        )
    )
    if operation == "validate-wire":
        assert output[:8] == b"PYNVAL1\0"
    else:
        assert output == original_source
    assert allocations == 13
    assert source == original_source
    assert config == original_config

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native foundation bridge allocation failure$",
        ):
            foundation_bridge_extension._foundation_bridge_allocation_probe_v1(
                operation,
                memoryview(source),
                memoryview(config),
                fail_after,
            )
        assert source == original_source
        assert config == original_config

    boundary_output, boundary_allocations = (
        foundation_bridge_extension._foundation_bridge_allocation_probe_v1(
            operation,
            memoryview(source),
            memoryview(config),
            allocations,
        )
    )
    assert boundary_output == output
    assert boundary_allocations == allocations
    assert source == original_source
    assert config == original_config


def test_foundation_bridge_probe_rejects_an_unknown_operation(
    foundation_bridge_extension: NativeTestExtension,
) -> None:
    with pytest.raises(ValueError, match="must be validate-canonical"):
        foundation_bridge_extension._foundation_bridge_allocation_probe_v1(
            "publish",
            b"",
            b"",
            None,
        )


@pytest.fixture(scope="module")
def retained_view_layout_bridge_extension(
    extension: NativeTestExtension,
) -> NativeTestExtension:
    required = (
        "_retained_signature_index_bridge_allocation_probe_v1",
        "_retained_identity_index_bridge_allocation_probe_v1",
        "_retained_axiom_type_index_bridge_allocation_probe_v1",
        "_retained_snapshot_counters_bridge_allocation_probe_v1",
        "_retained_document_counters_bridge_allocation_probe_v1",
        "_retained_snapshot_attestation_bridge_allocation_probe_v1",
        "_retained_document_attestation_bridge_allocation_probe_v1",
        "_retained_snapshot_page_bridge_allocation_probe_v1",
        "_retained_document_page_bridge_allocation_probe_v1",
        "_retained_snapshot_contains_bridge_allocation_probe_v1",
        "_retained_document_contains_bridge_allocation_probe_v1",
        "_retained_document_handle_bridge_allocation_probe_v1",
        "_retained_signature_layout_bridge_allocation_probe_v1",
        "_retained_identity_layout_bridge_allocation_probe_v1",
        "_retained_axiom_type_layout_bridge_allocation_probe_v1",
        "_retained_axiom_type_binding_bridge_allocation_probe_v1",
        "_retained_axiom_type_sizes_bridge_allocation_probe_v1",
        "_retained_axiom_type_page_bridge_allocation_probe_v1",
    )
    missing = tuple(name for name in required if not hasattr(extension, name))
    if missing:
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks retained-view layout probes: "
                + ", ".join(missing)
            )
        pytest.skip("native retained-view layout allocation hooks are unavailable")
    return extension


def test_retained_index_and_layout_bridge_failures_publish_no_partial_result(
    retained_view_layout_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_view_layout_bridge_extension)
    selected = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:allocation:view> "
            b"Declaration(Class(<urn:allocation:view:A>)) "
            b"SubClassOf(<urn:allocation:view:A> <urn:allocation:view:B>))",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
            ),
        ),
    )
    owner = object.__getattribute__(
        selected._native_snapshot_state.owner.handle,
        "_owner_v2",
    )
    config = bytearray(native._encode_config(ParseLimits(), None, verify=False))
    original_config = bytes(config)
    construction_cases: tuple[
        tuple[str, Any, tuple[object, ...], int, str], ...
    ] = (
        (
            "signature",
            extension._retained_signature_index_bridge_allocation_probe_v1,
            (owner, "closure", None, memoryview(config)),
            2,
            "_NativeRetainedSignatureIndexV1",
        ),
        (
            "identity",
            extension._retained_identity_index_bridge_allocation_probe_v1,
            (owner,),
            1,
            "_NativeRetainedOntologyIdentityIndexV1",
        ),
        (
            "axiom-type",
            extension._retained_axiom_type_index_bridge_allocation_probe_v1,
            (owner, "closure", None, memoryview(config)),
            2,
            "_NativeRetainedAxiomTypeIndexV1",
        ),
    )
    for _name, probe, arguments, expected_allocations, expected_type in construction_cases:
        index, allocations = probe(*arguments, None)
        layout = index._layout_v1()
        assert type(index).__name__ == expected_type
        assert allocations == expected_allocations
        assert owner._publication_closed_v2() is False
        assert config == original_config

        for fail_after in range(allocations):
            with pytest.raises(
                MemoryError,
                match=r"^injected native retained-index bridge allocation failure$",
            ):
                probe(*arguments, fail_after)
            assert owner._publication_closed_v2() is False
            assert config == original_config

        boundary_index, boundary_allocations = probe(*arguments, allocations)
        assert type(boundary_index).__name__ == expected_type
        assert boundary_index._layout_v1() == layout
        assert boundary_allocations == allocations
        assert owner._publication_closed_v2() is False
        assert config == original_config

    axiom_layout, _axiom_allocations = (
        extension._retained_axiom_type_layout_bridge_allocation_probe_v1(
            owner,
            "closure",
            None,
            memoryview(config),
            None,
        )
    )
    first_tag = cast(tuple[int, ...], cast(tuple[object, ...], axiom_layout)[0])[0]
    cases: tuple[tuple[str, Any, tuple[object, ...], int, int], ...] = (
        (
            "signature",
            extension._retained_signature_layout_bridge_allocation_probe_v1,
            (owner, "closure", None, memoryview(config)),
            15,
            6,
        ),
        (
            "identity",
            extension._retained_identity_layout_bridge_allocation_probe_v1,
            (owner,),
            10,
            5,
        ),
        (
            "axiom-type",
            extension._retained_axiom_type_layout_bridge_allocation_probe_v1,
            (owner, "closure", None, memoryview(config)),
            13,
            6,
        ),
        (
            "axiom-type-binding",
            extension._retained_axiom_type_binding_bridge_allocation_probe_v1,
            (owner, "closure", None, memoryview(config)),
            2,
            2,
        ),
        (
            "axiom-type-canonical-sizes",
            extension._retained_axiom_type_sizes_bridge_allocation_probe_v1,
            (owner, "closure", None, memoryview(config)),
            1,
            2,
        ),
        (
            "axiom-type-page",
            extension._retained_axiom_type_page_bridge_allocation_probe_v1,
            (
                owner,
                "closure",
                None,
                memoryview(config),
                first_tag,
                0,
                64,
                1 << 20,
            ),
            1,
            3,
        ),
    )

    for _name, probe, arguments, expected_allocations, layout_size in cases:
        layout, allocations = probe(*arguments, None)
        assert isinstance(layout, tuple)
        assert len(layout) == layout_size
        assert allocations == expected_allocations
        assert owner._publication_closed_v2() is False
        assert config == original_config

        for fail_after in range(allocations):
            with pytest.raises(
                MemoryError,
                match=r"^injected native retained-view layout bridge allocation failure$",
            ):
                probe(*arguments, fail_after)
            assert owner._publication_closed_v2() is False
            assert config == original_config

        boundary_layout, boundary_allocations = probe(*arguments, allocations)
        assert boundary_layout == layout
        assert boundary_allocations == allocations
        assert owner._publication_closed_v2() is False
        assert config == original_config

    selected.close()
    assert selected.closed


def test_retained_counter_bridge_failures_publish_no_partial_result(
    retained_view_layout_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_view_layout_bridge_extension)
    selected = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:allocation:counters> "
            b"Declaration(Class(<urn:allocation:counters:A>)))",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
            ),
        ),
    )
    snapshot = object.__getattribute__(
        selected._native_snapshot_state.owner.handle,
        "_owner_v2",
    )
    document = snapshot._publication_document_v2(0)
    expected = snapshot._publication_counters_v2()
    assert document._publication_counters_v2() == expected

    cases = (
        (
            extension._retained_snapshot_counters_bridge_allocation_probe_v1,
            snapshot,
        ),
        (
            extension._retained_document_counters_bridge_allocation_probe_v1,
            document,
        ),
    )
    for probe, owner in cases:
        counters, allocations = probe(owner, None)
        assert counters == expected
        assert allocations == 93

        for fail_after in range(allocations):
            with pytest.raises(
                MemoryError,
                match=r"^injected native retained-counters bridge allocation failure$",
            ):
                probe(owner, fail_after)
            assert snapshot._publication_counters_v2() == expected
            assert document._publication_counters_v2() == expected
            assert snapshot._publication_closed_v2() is False
            assert document._publication_closed_v2() is False

        boundary_counters, boundary_allocations = probe(owner, allocations)
        assert boundary_counters == expected
        assert boundary_allocations == allocations
        assert snapshot._publication_counters_v2() == expected
        assert document._publication_counters_v2() == expected

    document._publication_close_v2()
    selected.close()
    assert selected.closed


def test_retained_attestation_bridge_failures_publish_no_partial_result(
    retained_view_layout_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_view_layout_bridge_extension)
    v1_snapshot = extension._publication_fixture_v1()
    expected_v1 = v1_snapshot._publication_attestation_v1()
    v1_attestation, v1_allocations = (
        extension._retained_snapshot_attestation_bridge_allocation_probe_v1(
            v1_snapshot,
            None,
        )
    )
    assert v1_attestation == expected_v1
    assert v1_allocations == 33

    for fail_after in range(v1_allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native retained-attestation bridge allocation failure$",
        ):
            extension._retained_snapshot_attestation_bridge_allocation_probe_v1(
                v1_snapshot,
                fail_after,
            )
        assert v1_snapshot._publication_attestation_v1() == expected_v1
        assert v1_snapshot._publication_closed_v1() is False

    boundary_v1_attestation, boundary_v1_allocations = (
        extension._retained_snapshot_attestation_bridge_allocation_probe_v1(
            v1_snapshot,
            v1_allocations,
        )
    )
    assert boundary_v1_attestation == expected_v1
    assert boundary_v1_allocations == v1_allocations
    assert v1_snapshot._publication_attestation_v1() == expected_v1
    assert v1_snapshot._publication_closed_v1() is False

    selected = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:allocation:attestation> "
            b"Declaration(Class(<urn:allocation:attestation:A>)))",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
            ),
        ),
    )
    snapshot = object.__getattribute__(
        selected._native_snapshot_state.owner.handle,
        "_owner_v2",
    )
    document = snapshot._publication_document_v2(0)
    expected = snapshot._publication_attestation_v2()
    assert expected.owl2_dl_report_summary is None

    values, collections, summary = _validated_fixture()
    preimages = fingerprint_preimages(values)
    evidence = fingerprint_evidence(values, preimages)
    published = publication(collections, values=values, preimages=preimages)
    validated_snapshot = extension._publication_fixture_v2(
        published.handle.attestation,
        collections,
        documents=published.documents,
        report=published.report,
        root_document_key=published.root_document_key,
        load_options=published.load_options,
        capability_bits=published.capability_bits,
        fingerprint_evidence=evidence,
        fingerprint_preimages=preimages,
        facade_cardinality_summary=published.facade_cardinality_summary,
        owl2_dl_report_summary=summary,
    )
    validated_document = validated_snapshot._publication_document_v2(0)
    validated_expected = validated_snapshot._publication_attestation_v2()
    assert validated_expected.owl2_dl_report_summary == summary

    owner_sets = (
        (snapshot, document, expected, 42),
        (validated_snapshot, validated_document, validated_expected, 55),
    )
    for snapshot_owner, document_owner, expected_attestation, expected_allocations in owner_sets:
        expected_counters = snapshot_owner._publication_counters_v2()
        assert document_owner._publication_attestation_v2() == expected_attestation

        cases = (
            (
                extension._retained_snapshot_attestation_bridge_allocation_probe_v1,
                snapshot_owner,
            ),
            (
                extension._retained_document_attestation_bridge_allocation_probe_v1,
                document_owner,
            ),
        )
        for probe, owner in cases:
            attestation, allocations = probe(owner, None)
            assert attestation == expected_attestation
            assert allocations == expected_allocations

            for fail_after in range(allocations):
                with pytest.raises(
                    MemoryError,
                    match=r"^injected native retained-attestation bridge allocation failure$",
                ):
                    probe(owner, fail_after)
                assert (
                    snapshot_owner._publication_attestation_v2() == expected_attestation
                )
                assert (
                    document_owner._publication_attestation_v2() == expected_attestation
                )
                assert snapshot_owner._publication_counters_v2() == expected_counters
                assert document_owner._publication_counters_v2() == expected_counters
                assert snapshot_owner._publication_closed_v2() is False
                assert document_owner._publication_closed_v2() is False

            boundary_attestation, boundary_allocations = probe(owner, allocations)
            assert boundary_attestation == expected_attestation
            assert boundary_allocations == allocations
            assert snapshot_owner._publication_attestation_v2() == expected_attestation
            assert document_owner._publication_attestation_v2() == expected_attestation

    validated_document._publication_close_v2()
    validated_snapshot._publication_close_v2()
    document._publication_close_v2()
    v1_snapshot._publication_close_v1()
    selected.close()
    assert selected.closed


def test_retained_page_bridge_failures_publish_no_partial_result_or_counters(
    retained_view_layout_bridge_extension: NativeTestExtension,
) -> None:
    extension = retained_view_layout_bridge_extension
    selected = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:allocation:page> "
            b"Declaration(Class(<urn:allocation:page:A>)))",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
                preserve_source_map=True,
            ),
        ),
    )
    snapshot = object.__getattribute__(
        selected._native_snapshot_state.owner.handle,
        "_owner_v2",
    )
    document = snapshot._publication_document_v2(0)
    max_row_bytes = snapshot._publication_attestation_v2().max_facade_row_bytes
    typed_request = NativeFacadePageRequestV2(
        collection=NativeFacadeCollectionV2.AXIOMS,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=0,
        max_rows=1,
        max_bytes=max_row_bytes,
        max_row_bytes=max_row_bytes,
    )
    auxiliary_request = NativeFacadePageRequestV2(
        collection=NativeFacadeCollectionV2.SOURCE_MAP_ENTRIES,
        scope=NativeFacadeScopeV2.DOCUMENT,
        document_ordinal=0,
        start=0,
        max_rows=1,
        max_bytes=max_row_bytes,
        max_row_bytes=max_row_bytes,
    )
    cases = (
        (
            extension._retained_snapshot_page_bridge_allocation_probe_v1,
            snapshot,
            typed_request,
            11,
        ),
        (
            extension._retained_document_page_bridge_allocation_probe_v1,
            document,
            typed_request,
            11,
        ),
        (
            extension._retained_snapshot_page_bridge_allocation_probe_v1,
            snapshot,
            auxiliary_request,
            13,
        ),
        (
            extension._retained_document_page_bridge_allocation_probe_v1,
            document,
            auxiliary_request,
            13,
        ),
    )
    for probe, owner, request, expected_allocations in cases:
        before = snapshot._publication_counters_v2()
        page, allocations = probe(owner, request, None)
        assert page.rows
        assert allocations == expected_allocations
        after_success = snapshot._publication_counters_v2()
        assert after_success.page_requests == before.page_requests + 1
        assert after_success.pages_returned == before.pages_returned + 1
        assert after_success.rows_emitted == before.rows_emitted + len(page.rows)
        assert document._publication_counters_v2() == after_success

        for fail_after in range(allocations):
            with pytest.raises(
                MemoryError,
                match=r"^injected native retained-page bridge allocation failure$",
            ):
                probe(owner, request, fail_after)
            assert snapshot._publication_counters_v2() == after_success
            assert document._publication_counters_v2() == after_success
            assert snapshot._publication_closed_v2() is False
            assert document._publication_closed_v2() is False

        boundary_page, boundary_allocations = probe(owner, request, allocations)
        assert boundary_page == page
        assert boundary_allocations == allocations
        after_boundary = snapshot._publication_counters_v2()
        assert after_boundary.page_requests == after_success.page_requests + 1
        assert after_boundary.pages_returned == after_success.pages_returned + 1
        assert after_boundary.rows_emitted == after_success.rows_emitted + len(page.rows)
        assert document._publication_counters_v2() == after_boundary

    document._publication_close_v2()
    selected.close()
    assert selected.closed


def test_retained_contains_bridge_failures_publish_no_partial_counters(
    retained_view_layout_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_view_layout_bridge_extension)
    selected = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:allocation:contains> "
            b"Declaration(Class(<urn:allocation:contains:A>)))",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
            ),
        ),
    )
    typed_snapshot = object.__getattribute__(
        selected._native_snapshot_state.owner.handle,
        "_owner_v2",
    )
    typed_document = typed_snapshot._publication_document_v2(0)
    typed_bound = typed_snapshot._publication_attestation_v2().max_facade_row_bytes

    values, collections, summary = _validated_fixture()
    preimages = fingerprint_preimages(values)
    evidence = fingerprint_evidence(values, preimages)
    published = publication(collections, values=values, preimages=preimages)
    retained_snapshot = extension._publication_fixture_v2(
        published.handle.attestation,
        collections,
        documents=published.documents,
        report=published.report,
        root_document_key=published.root_document_key,
        load_options=published.load_options,
        capability_bits=published.capability_bits,
        fingerprint_evidence=evidence,
        fingerprint_preimages=preimages,
        facade_cardinality_summary=published.facade_cardinality_summary,
        owl2_dl_report_summary=summary,
    )
    retained_document = retained_snapshot._publication_document_v2(0)
    retained_bound = (
        retained_snapshot._publication_attestation_v2().max_facade_row_bytes
    )

    def request(canonical: bytes, max_row_bytes: int) -> NativeFacadeContainsRequestV2:
        return NativeFacadeContainsRequestV2(
            collection=NativeFacadeCollectionV2.AXIOMS,
            scope=NativeFacadeScopeV2.DOCUMENT,
            document_ordinal=0,
            canonical=canonical,
            max_row_bytes=max_row_bytes,
        )

    typed_hit = request(
        canonical_bytes(
            Declaration(Class(IRI("urn:allocation:contains:A"))),
        ),
        typed_bound,
    )
    typed_miss = request(
        canonical_bytes(
            Declaration(Class(IRI("urn:allocation:contains:missing"))),
        ),
        typed_bound,
    )
    retained_hit = request(
        canonical_bytes(Declaration(Class(IRI("urn:handoff:Class")))),
        retained_bound,
    )
    retained_miss = request(
        canonical_bytes(Declaration(Class(IRI("urn:handoff:missing")))),
        retained_bound,
    )
    cases = (
        (
            extension._retained_snapshot_contains_bridge_allocation_probe_v1,
            typed_snapshot,
            typed_snapshot,
            typed_document,
            typed_hit,
            True,
        ),
        (
            extension._retained_document_contains_bridge_allocation_probe_v1,
            typed_document,
            typed_snapshot,
            typed_document,
            typed_miss,
            False,
        ),
        (
            extension._retained_snapshot_contains_bridge_allocation_probe_v1,
            retained_snapshot,
            retained_snapshot,
            retained_document,
            retained_hit,
            True,
        ),
        (
            extension._retained_document_contains_bridge_allocation_probe_v1,
            retained_document,
            retained_snapshot,
            retained_document,
            retained_miss,
            False,
        ),
    )
    for probe, owner, snapshot_owner, document_owner, contains_request, expected in cases:
        before = snapshot_owner._publication_counters_v2()
        found, allocations = probe(owner, contains_request, None)
        assert found is expected
        assert allocations == 2
        after_success = snapshot_owner._publication_counters_v2()
        assert after_success.contains_requests == before.contains_requests + 1
        assert after_success.contains_hits == before.contains_hits + int(expected)
        assert document_owner._publication_counters_v2() == after_success

        for fail_after in range(allocations):
            with pytest.raises(
                MemoryError,
                match=r"^injected native retained-contains bridge allocation failure$",
            ):
                probe(owner, contains_request, fail_after)
            assert snapshot_owner._publication_counters_v2() == after_success
            assert document_owner._publication_counters_v2() == after_success
            assert snapshot_owner._publication_closed_v2() is False
            assert document_owner._publication_closed_v2() is False

        boundary_found, boundary_allocations = probe(
            owner,
            contains_request,
            allocations,
        )
        assert boundary_found is expected
        assert boundary_allocations == allocations
        after_boundary = snapshot_owner._publication_counters_v2()
        assert after_boundary.contains_requests == after_success.contains_requests + 1
        assert after_boundary.contains_hits == after_success.contains_hits + int(expected)
        assert document_owner._publication_counters_v2() == after_boundary

    retained_document._publication_close_v2()
    retained_snapshot._publication_close_v2()
    typed_document._publication_close_v2()
    selected.close()
    assert selected.closed


def test_retained_document_handle_bridge_failures_publish_no_partial_owner(
    retained_view_layout_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_view_layout_bridge_extension)
    selected = cast(
        Any,
        load_snapshot(
            b"Ontology(<urn:allocation:document-handle> "
            b"Declaration(Class(<urn:allocation:document-handle:A>)))",
            options=LoadOptions(
                format=DocumentFormat.FUNCTIONAL,
                imports=ImportPolicy.IGNORE,
                backend=BackendPreference.NATIVE,
            ),
        ),
    )
    typed_snapshot = object.__getattribute__(
        selected._native_snapshot_state.owner.handle,
        "_owner_v2",
    )

    values, collections, summary = _validated_fixture()
    preimages = fingerprint_preimages(values)
    evidence = fingerprint_evidence(values, preimages)
    published = publication(collections, values=values, preimages=preimages)
    retained_snapshot = extension._publication_fixture_v2(
        published.handle.attestation,
        collections,
        documents=published.documents,
        report=published.report,
        root_document_key=published.root_document_key,
        load_options=published.load_options,
        capability_bits=published.capability_bits,
        fingerprint_evidence=evidence,
        fingerprint_preimages=preimages,
        facade_cardinality_summary=published.facade_cardinality_summary,
        owl2_dl_report_summary=summary,
    )

    created: list[Any] = []
    for snapshot_owner in (typed_snapshot, retained_snapshot):
        before = snapshot_owner._publication_counters_v2()
        document, allocations = (
            extension._retained_document_handle_bridge_allocation_probe_v1(
                snapshot_owner,
                0,
                None,
            )
        )
        created.append(document)
        assert allocations == 2
        assert document._publication_closed_v2() is False
        assert snapshot_owner._publication_counters_v2() == before
        assert document._publication_counters_v2() == before
        assert (
            document._publication_attestation_v2()
            == snapshot_owner._publication_attestation_v2()
        )

        for fail_after in range(allocations):
            with pytest.raises(
                MemoryError,
                match=(
                    r"^injected native retained-document-handle bridge "
                    r"allocation failure$"
                ),
            ):
                extension._retained_document_handle_bridge_allocation_probe_v1(
                    snapshot_owner,
                    0,
                    fail_after,
                )
            assert snapshot_owner._publication_counters_v2() == before
            assert snapshot_owner._publication_closed_v2() is False
            assert document._publication_counters_v2() == before
            assert document._publication_closed_v2() is False

        boundary, boundary_allocations = (
            extension._retained_document_handle_bridge_allocation_probe_v1(
                snapshot_owner,
                0,
                allocations,
            )
        )
        created.append(boundary)
        assert boundary_allocations == allocations
        assert boundary._publication_closed_v2() is False
        assert boundary._publication_counters_v2() == before
        assert snapshot_owner._publication_counters_v2() == before

    for document in created:
        document._publication_close_v2()
        assert document._publication_closed_v2()
    retained_snapshot._publication_close_v2()
    selected.close()
    assert selected.closed


@pytest.fixture(scope="module")
def functional_retained_bridge_extension(
    extension: NativeTestExtension,
) -> NativeTestExtension:
    if not hasattr(extension, "_functional_retained_bridge_allocation_probe_v2"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_functional_retained_bridge_allocation_probe_v2"
            )
        pytest.skip("native retained Functional bridge allocation hook is unavailable")
    return extension


def test_functional_retained_bridge_allocations_fail_before_publication(
    functional_retained_bridge_extension: NativeTestExtension,
) -> None:
    source = _parser_request()
    original_source = bytes(source)
    config = bytearray(native._encode_config(ParseLimits(), None, verify=False))
    original_config = bytes(config)

    output, allocations = (
        functional_retained_bridge_extension._functional_retained_bridge_allocation_probe_v2(
            memoryview(source),
            memoryview(config),
            True,
            True,
            False,
            False,
            None,
        )
    )
    assert output[:8] == b"PYNFRS2\0"
    assert allocations == 13
    assert source == original_source
    assert config == original_config

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native Functional retained bridge allocation failure$",
        ):
            functional_retained_bridge_extension._functional_retained_bridge_allocation_probe_v2(
                memoryview(source),
                memoryview(config),
                True,
                True,
                False,
                False,
                fail_after,
            )
        assert source == original_source
        assert config == original_config

    boundary_output, boundary_allocations = (
        functional_retained_bridge_extension._functional_retained_bridge_allocation_probe_v2(
            memoryview(source),
            memoryview(config),
            True,
            True,
            False,
            False,
            allocations,
        )
    )
    assert boundary_output == output
    assert boundary_allocations == allocations
    assert source == original_source
    assert config == original_config


def test_retained_preparation_bridge_allocations_fail_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    functional_retained_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, functional_retained_bridge_extension)
    request = _parser_request()
    source = bytes(request[struct.calcsize("<8sHHQ") :])
    config = bytearray(native._encode_config(ParseLimits(), None, verify=False))
    original_request = bytes(request)
    original_config = bytes(config)
    captured: dict[str, object] = {}
    prepare = extension._prepare_parsed_structural_snapshot_v2

    def capture_prepare(
        parsed: object,
        manifest: bytes,
        document_key: str,
        collect_provenance: bool,
        preserve_source_map: bool,
        cancel: object | None = None,
    ) -> bytes:
        captured.update(
            manifest=bytes(manifest),
            document_key=document_key,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
        )
        return cast(
            bytes,
            prepare(
                parsed,
                manifest,
                document_key,
                collect_provenance,
                preserve_source_map,
                cancel,
            ),
        )

    monkeypatch.setattr(extension, "_prepare_parsed_structural_snapshot_v2", capture_prepare)
    native._reset_probe_cache_for_tests()
    selected = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=True,
            preserve_source_map=True,
        ),
    )
    selected.close()
    monkeypatch.setattr(extension, "_prepare_parsed_structural_snapshot_v2", prepare)

    manifest = cast(bytes, captured["manifest"])
    document_key = cast(str, captured["document_key"])
    collect_provenance = cast(bool, captured["collect_provenance"])
    preserve_source_map = cast(bool, captured["preserve_source_map"])
    probe = extension._prepare_parsed_structural_bridge_allocation_probe_v2

    def parsed_storage() -> object:
        _summary, storage, _phases = extension._parse_functional_retained_v2(
            memoryview(request),
            memoryview(config),
            collect_provenance,
            preserve_source_map,
            False,
            False,
            None,
        )
        return storage

    output, allocations = probe(
        parsed_storage(),
        manifest,
        document_key,
        collect_provenance,
        preserve_source_map,
        None,
    )
    assert output[:8] == b"PYNFPP2\0"
    assert allocations == 2
    assert request == original_request
    assert config == original_config

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native retained preparation bridge allocation failure$",
        ):
            probe(
                parsed_storage(),
                manifest,
                document_key,
                collect_provenance,
                preserve_source_map,
                fail_after,
            )
        assert request == original_request
        assert config == original_config

    boundary_output, boundary_allocations = probe(
        parsed_storage(),
        manifest,
        document_key,
        collect_provenance,
        preserve_source_map,
        allocations,
    )
    assert boundary_output[:8] == output[:8]
    assert boundary_allocations == allocations
    assert request == original_request
    assert config == original_config


@pytest.fixture(scope="module")
def retained_finalization_bridge_extension(
    extension: NativeTestExtension,
) -> NativeTestExtension:
    if not hasattr(extension, "_finalize_parsed_structural_bridge_allocation_probe_v2"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_finalize_parsed_structural_bridge_allocation_probe_v2"
            )
        pytest.skip("native retained finalization bridge allocation hook is unavailable")
    return extension


def test_retained_finalization_bridge_failures_preserve_prepared_storage(
    monkeypatch: pytest.MonkeyPatch,
    retained_finalization_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_finalization_bridge_extension)
    request = _parser_request()
    source = bytes(request[struct.calcsize("<8sHHQ") :])
    config = bytearray(native._encode_config(ParseLimits(), None, verify=False))
    original_request = bytes(request)
    original_config = bytes(config)
    captured: dict[str, object] = {}
    prepare = extension._prepare_parsed_structural_snapshot_v2
    finalize = extension._finalize_parsed_structural_snapshot_v2

    def capture_prepare(
        parsed: object,
        manifest: bytes,
        document_key: str,
        collect_provenance: bool,
        preserve_source_map: bool,
        cancel: object | None = None,
    ) -> bytes:
        captured.update(
            manifest=bytes(manifest),
            document_key=document_key,
            collect_provenance=collect_provenance,
            preserve_source_map=preserve_source_map,
        )
        return cast(
            bytes,
            prepare(
                parsed,
                manifest,
                document_key,
                collect_provenance,
                preserve_source_map,
                cancel,
            ),
        )

    def capture_finalize(
        parsed: object,
        prepared_summary: bytes,
        attestation: object,
        cancel: object | None = None,
    ) -> object:
        captured["attestation"] = attestation
        return finalize(parsed, prepared_summary, attestation, cancel)

    monkeypatch.setattr(extension, "_prepare_parsed_structural_snapshot_v2", capture_prepare)
    monkeypatch.setattr(extension, "_finalize_parsed_structural_snapshot_v2", capture_finalize)
    native._reset_probe_cache_for_tests()
    selected = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.NATIVE,
            collect_provenance=True,
            preserve_source_map=True,
        ),
    )
    selected.close()
    monkeypatch.setattr(extension, "_prepare_parsed_structural_snapshot_v2", prepare)
    monkeypatch.setattr(extension, "_finalize_parsed_structural_snapshot_v2", finalize)

    manifest = cast(bytes, captured["manifest"])
    document_key = cast(str, captured["document_key"])
    collect_provenance = cast(bool, captured["collect_provenance"])
    preserve_source_map = cast(bool, captured["preserve_source_map"])
    attestation = captured["attestation"]
    probe = extension._finalize_parsed_structural_bridge_allocation_probe_v2

    def prepared_storage() -> tuple[object, bytes]:
        _summary, parsed, _phases = extension._parse_functional_retained_v2(
            memoryview(request),
            memoryview(config),
            collect_provenance,
            preserve_source_map,
            False,
            False,
            None,
        )
        summary = cast(
            bytes,
            prepare(
                parsed,
                manifest,
                document_key,
                collect_provenance,
                preserve_source_map,
                None,
            ),
        )
        return parsed, summary

    parsed, summary = prepared_storage()
    handle, allocations = probe(parsed, summary, attestation, None)
    assert allocations == 4
    assert handle._publication_closed_v2() is False
    handle._publication_close_v2()
    assert request == original_request
    assert config == original_config

    for fail_after in range(allocations):
        parsed, summary = prepared_storage()
        with pytest.raises(
            MemoryError,
            match=r"^injected native retained finalization bridge allocation failure$",
        ):
            probe(parsed, summary, attestation, fail_after)
        recovered = finalize(parsed, summary, attestation, None)
        assert recovered._publication_closed_v2() is False
        recovered._publication_close_v2()
        assert request == original_request
        assert config == original_config

    parsed, summary = prepared_storage()
    boundary_handle, boundary_allocations = probe(
        parsed,
        summary,
        attestation,
        allocations,
    )
    assert boundary_allocations == allocations
    assert boundary_handle._publication_closed_v2() is False
    boundary_handle._publication_close_v2()
    assert request == original_request
    assert config == original_config


@pytest.fixture(scope="module")
def retained_structural_bridge_extension(
    extension: NativeTestExtension,
) -> NativeTestExtension:
    if not hasattr(extension, "_retained_structural_bridge_allocation_probe_v2"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_retained_structural_bridge_allocation_probe_v2"
            )
        pytest.skip("native retained structural bridge allocation hook is unavailable")
    return extension


def test_retained_structural_bridge_allocations_fail_before_owner_publication(
    monkeypatch: pytest.MonkeyPatch,
    retained_structural_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_structural_bridge_extension)
    source = b"Ontology(<urn:allocation:retained> ClassAssertion(<urn:C> _:person))"
    snapshot = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
            collect_provenance=True,
        ),
    )
    object.__setattr__(snapshot.load_options, "backend", BackendPreference.NATIVE)
    object.__setattr__(snapshot.root.provenance, "backend", "native")

    captured: dict[str, object] = {}
    retain = extension._retain_structural_snapshot_v2

    def capture_retain(
        documents: object,
        origins: object,
        attestation: object,
        config: object,
        cancel: object | None = None,
        *,
        effective_documents: object | None = None,
        effective_origins: object | None = None,
        effective_document_ordinals: object | None = None,
        closure_document_ordinals: object | None = None,
    ) -> object:
        captured.update(
            documents=documents,
            origins=origins,
            attestation=attestation,
            config=bytes(cast(Any, config)),
            effective_documents=effective_documents,
            effective_origins=effective_origins,
            effective_document_ordinals=effective_document_ordinals,
            closure_document_ordinals=closure_document_ordinals,
        )
        return retain(
            documents,
            origins,
            attestation,
            config,
            cancel,
            effective_documents=effective_documents,
            effective_origins=effective_origins,
            effective_document_ordinals=effective_document_ordinals,
            closure_document_ordinals=closure_document_ordinals,
        )

    monkeypatch.setattr(extension, "_retain_structural_snapshot_v2", capture_retain)
    selected = cast(Any, _publish_structural_snapshot_v2)(
        snapshot,
        extension,
        None,
        None,
    )
    selected.close()
    monkeypatch.setattr(extension, "_retain_structural_snapshot_v2", retain)

    documents = captured["documents"]
    origins = captured["origins"]
    attestation = captured["attestation"]
    config = bytearray(cast(bytes, captured["config"]))
    effective_documents = captured["effective_documents"]
    effective_origins = captured["effective_origins"]
    effective_document_ordinals = captured["effective_document_ordinals"]
    closure_document_ordinals = captured["closure_document_ordinals"]
    assert effective_documents is not None
    assert effective_origins is not None
    assert effective_document_ordinals is None
    assert closure_document_ordinals is None
    original_documents = documents
    original_origins = origins
    original_config = bytes(config)
    probe = extension._retained_structural_bridge_allocation_probe_v2

    handle, allocations = probe(
        documents,
        origins,
        attestation,
        memoryview(config),
        None,
        effective_documents=effective_documents,
        effective_origins=effective_origins,
    )
    assert allocations == 22
    assert handle._publication_closed_v2() is False
    handle._publication_close_v2()
    assert documents == original_documents
    assert origins == original_origins
    assert config == original_config

    duplicated_documents = (*cast(tuple[object, ...], documents),) * 2
    duplicated_effective = (*cast(tuple[object, ...], effective_documents),) * 2
    duplicated_origin_tables = (cast(tuple[bytes, ...], origins),) * 2
    duplicated_effective_origin_tables = (cast(tuple[bytes, ...], effective_origins),) * 2
    with pytest.raises(
        ValueError,
        match=r"^native multi-document retention requires explicit closure topology$",
    ):
        probe(
            duplicated_documents,
            origins,
            attestation,
            memoryview(config),
            effective_documents=duplicated_effective,
            effective_origins=effective_origins,
        )
    with pytest.raises(
        ValueError,
        match=r"^native retained closure topology requires both ordinal tables$",
    ):
        probe(
            duplicated_documents,
            origins,
            attestation,
            memoryview(config),
            effective_documents=duplicated_effective,
            effective_origins=effective_origins,
            effective_document_ordinals=((0,), (1,)),
        )
    with pytest.raises(
        TypeError,
        match=r"^native retained closure ordinals must contain exact ints$",
    ):
        probe(
            duplicated_documents,
            origins,
            attestation,
            memoryview(config),
            effective_documents=duplicated_effective,
            effective_origins=effective_origins,
            effective_document_ordinals=((0,), (True,)),
            closure_document_ordinals=(0, 1),
        )
    with pytest.raises(extension._NativeError) as topology_error:
        probe(
            duplicated_documents,
            duplicated_origin_tables,
            attestation,
            memoryview(config),
            effective_documents=duplicated_effective,
            effective_origins=duplicated_effective_origin_tables,
            effective_document_ordinals=((0,), (0,)),
            closure_document_ordinals=(0, 1),
        )
    assert topology_error.value.args[0] == "NATIVE_PROTOCOL"

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native retained structural bridge allocation failure$",
        ):
            probe(
                documents,
                origins,
                attestation,
                memoryview(config),
                fail_after,
                effective_documents=effective_documents,
                effective_origins=effective_origins,
            )
        assert documents == original_documents
        assert origins == original_origins
        assert config == original_config

    boundary_handle, boundary_allocations = probe(
        documents,
        origins,
        attestation,
        memoryview(config),
        allocations,
        effective_documents=effective_documents,
        effective_origins=effective_origins,
    )
    assert boundary_allocations == allocations
    assert boundary_handle._publication_closed_v2() is False
    boundary_handle._publication_close_v2()
    assert documents == original_documents
    assert origins == original_origins
    assert config == original_config


@pytest.fixture(scope="module")
def parsed_closure_bridge_extension(
    extension: NativeTestExtension,
) -> NativeTestExtension:
    if not hasattr(extension, "_merge_parsed_structural_bridge_allocation_probe_v2"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_merge_parsed_structural_bridge_allocation_probe_v2"
            )
        pytest.skip("native parsed-closure bridge allocation hook is unavailable")
    return extension


def test_parsed_closure_bridge_allocations_fail_before_owner_publication(
    monkeypatch: pytest.MonkeyPatch,
    parsed_closure_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, parsed_closure_bridge_extension)
    root = (
        b"Ontology(<urn:allocation:parsed-closure:root> "
        b"Import(<urn:allocation:parsed-closure:child>) "
        b"ClassAssertion(<urn:allocation:parsed-closure:Root> _:root))"
    )
    child = (
        b"Ontology(<urn:allocation:parsed-closure:child> "
        b"ClassAssertion(<urn:allocation:parsed-closure:Child> _:child))"
    )
    parse = extension._parse_functional_retained_v2
    merge = extension._merge_parsed_structural_snapshot_v2
    parse_specs_by_owner: dict[
        int,
        tuple[bytes, bytes, bool, bool, bool, bool, bool],
    ] = {}
    captured: dict[str, object] = {}

    def capture_parse(
        source: object,
        config: object,
        collect_provenance: bool,
        preserve_source_map: bool,
        record_unresolved: bool,
        require_empty_imports: bool,
        cancel: object | None = None,
        *,
        materialize_document: bool = False,
    ) -> tuple[bytes, object, tuple[int, int, int, int]]:
        result = cast(
            tuple[bytes, object, tuple[int, int, int, int]],
            parse(
                source,
                config,
                collect_provenance,
                preserve_source_map,
                record_unresolved,
                require_empty_imports,
                cancel,
                materialize_document=materialize_document,
            ),
        )
        parse_specs_by_owner[id(result[1])] = (
            bytes(cast(Any, source)),
            bytes(cast(Any, config)),
            collect_provenance,
            preserve_source_map,
            record_unresolved,
            require_empty_imports,
            materialize_document,
        )
        return result

    def capture_merge(
        parsed_documents: tuple[object, ...],
        origins: object,
        attestation: object,
        config: object,
        cancel: object | None = None,
        *,
        source_maps: object | None = None,
        effective_origins: object | None = None,
        effective_document_ordinals: object | None = None,
        closure_document_ordinals: object | None = None,
        anonymous_scope_targets: object | None = None,
    ) -> object:
        captured.update(
            parse_specs=tuple(parse_specs_by_owner[id(item)] for item in parsed_documents),
            origins=origins,
            attestation=attestation,
            config=bytes(cast(Any, config)),
            source_maps=source_maps,
            effective_origins=effective_origins,
            effective_document_ordinals=effective_document_ordinals,
            closure_document_ordinals=closure_document_ordinals,
            anonymous_scope_targets=anonymous_scope_targets,
        )
        return merge(
            parsed_documents,
            origins,
            attestation,
            config,
            cancel,
            source_maps=source_maps,
            effective_origins=effective_origins,
            effective_document_ordinals=effective_document_ordinals,
            closure_document_ordinals=closure_document_ordinals,
            anonymous_scope_targets=anonymous_scope_targets,
        )

    monkeypatch.setattr(extension, "_parse_functional_retained_v2", capture_parse)
    monkeypatch.setattr(extension, "_merge_parsed_structural_snapshot_v2", capture_merge)
    selected = load_snapshot(
        root,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.NATIVE,
            collect_provenance=True,
            preserve_source_map=True,
        ),
        resolver=MappingResolver({"urn:allocation:parsed-closure:child": child}),
    )
    cast(Any, selected).close()
    monkeypatch.setattr(extension, "_parse_functional_retained_v2", parse)
    monkeypatch.setattr(extension, "_merge_parsed_structural_snapshot_v2", merge)

    parse_specs = cast(
        tuple[tuple[bytes, bytes, bool, bool, bool, bool, bool], ...],
        captured["parse_specs"],
    )
    assert len(parse_specs) == 2
    origins = captured["origins"]
    attestation = captured["attestation"]
    config = cast(bytes, captured["config"])
    source_maps = captured["source_maps"]
    effective_origins = captured["effective_origins"]
    effective_document_ordinals = captured["effective_document_ordinals"]
    closure_document_ordinals = captured["closure_document_ordinals"]
    anonymous_scope_targets = captured["anonymous_scope_targets"]
    assert source_maps is not None
    assert effective_origins is not None
    assert effective_document_ordinals is not None
    assert closure_document_ordinals is not None
    assert anonymous_scope_targets is not None

    def parsed_documents() -> tuple[object, ...]:
        retained: list[object] = []
        for (
            request,
            parse_config,
            collect_provenance,
            preserve_source_map,
            record_unresolved,
            require_empty_imports,
            materialize_document,
        ) in parse_specs:
            _summary, storage, _phases = parse(
                memoryview(request),
                memoryview(parse_config),
                collect_provenance,
                preserve_source_map,
                record_unresolved,
                require_empty_imports,
                None,
                materialize_document=materialize_document,
            )
            retained.append(storage)
        return tuple(retained)

    probe = extension._merge_parsed_structural_bridge_allocation_probe_v2
    handle, allocations = probe(
        parsed_documents(),
        origins,
        attestation,
        memoryview(config),
        None,
        source_maps=source_maps,
        effective_origins=effective_origins,
        effective_document_ordinals=effective_document_ordinals,
        closure_document_ordinals=closure_document_ordinals,
        anonymous_scope_targets=anonymous_scope_targets,
    )
    assert allocations == 37
    assert handle._publication_attestation_v2() == attestation
    assert handle._publication_closed_v2() is False
    handle._publication_close_v2()

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native parsed-closure bridge allocation failure$",
        ):
            probe(
                parsed_documents(),
                origins,
                attestation,
                memoryview(config),
                fail_after,
                source_maps=source_maps,
                effective_origins=effective_origins,
                effective_document_ordinals=effective_document_ordinals,
                closure_document_ordinals=closure_document_ordinals,
                anonymous_scope_targets=anonymous_scope_targets,
            )

    boundary_handle, boundary_allocations = probe(
        parsed_documents(),
        origins,
        attestation,
        memoryview(config),
        allocations,
        source_maps=source_maps,
        effective_origins=effective_origins,
        effective_document_ordinals=effective_document_ordinals,
        closure_document_ordinals=closure_document_ordinals,
        anonymous_scope_targets=anonymous_scope_targets,
    )
    assert boundary_allocations == allocations
    assert boundary_handle._publication_attestation_v2() == attestation
    assert boundary_handle._publication_closed_v2() is False
    boundary_handle._publication_close_v2()


def test_retained_closure_source_map_allocations_fail_before_owner_publication(
    monkeypatch: pytest.MonkeyPatch,
    retained_structural_bridge_extension: NativeTestExtension,
) -> None:
    extension = cast(Any, retained_structural_bridge_extension)
    root = (
        b"Prefix(ex:=<urn:allocation:closure:>) "
        b"Ontology(<urn:allocation:closure:root> "
        b"Import(<urn:allocation:closure:child>) Declaration(Class(ex:Root)))"
    )
    child = (
        b"Prefix(ex:=<urn:allocation:closure:>) "
        b"Ontology(<urn:allocation:closure:child> Declaration(Class(ex:Child)))"
    )
    snapshot = load_snapshot(
        root,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.PYTHON,
            collect_provenance=False,
            preserve_source_map=True,
        ),
        resolver=MappingResolver({"urn:allocation:closure:child": child}),
    )
    object.__setattr__(snapshot.load_options, "backend", BackendPreference.NATIVE)
    for document in snapshot.documents:
        object.__setattr__(document.provenance, "backend", "native")

    captured: dict[str, object] = {}
    retain = extension._retain_structural_snapshot_v2

    def capture_retain(
        documents: object,
        origins: object,
        attestation: object,
        config: object,
        cancel: object | None = None,
        *,
        source_maps: object | None = None,
        effective_documents: object | None = None,
        effective_origins: object | None = None,
        effective_document_ordinals: object | None = None,
        closure_document_ordinals: object | None = None,
    ) -> object:
        captured.update(
            documents=documents,
            origins=origins,
            attestation=attestation,
            config=bytes(cast(Any, config)),
            source_maps=source_maps,
            effective_documents=effective_documents,
            effective_origins=effective_origins,
            effective_document_ordinals=effective_document_ordinals,
            closure_document_ordinals=closure_document_ordinals,
        )
        return retain(
            documents,
            origins,
            attestation,
            config,
            cancel,
            source_maps=source_maps,
            effective_documents=effective_documents,
            effective_origins=effective_origins,
            effective_document_ordinals=effective_document_ordinals,
            closure_document_ordinals=closure_document_ordinals,
        )

    monkeypatch.setattr(extension, "_retain_structural_snapshot_v2", capture_retain)
    selected = cast(Any, _publish_structural_closure_snapshot_v2)(
        snapshot,
        extension,
        None,
    )
    selected.close()
    monkeypatch.setattr(extension, "_retain_structural_snapshot_v2", retain)

    documents = captured["documents"]
    origins = captured["origins"]
    source_maps = captured["source_maps"]
    attestation = captured["attestation"]
    config = bytearray(cast(bytes, captured["config"]))
    effective_documents = captured["effective_documents"]
    effective_origins = captured["effective_origins"]
    effective_document_ordinals = captured["effective_document_ordinals"]
    closure_document_ordinals = captured["closure_document_ordinals"]
    assert origins is None
    assert effective_documents is None
    assert effective_origins is None
    assert effective_document_ordinals == ((0,), (1,))
    assert closure_document_ordinals == (0, 1)
    assert len(cast(tuple[object, ...], source_maps)) == 2
    assert all(
        entries and prefixes
        for entries, prefixes in cast(
            tuple[tuple[tuple[bytes, ...], tuple[bytes, ...]], ...],
            source_maps,
        )
    )

    original_documents = documents
    original_source_maps = source_maps
    original_config = bytes(config)
    probe = extension._retained_structural_bridge_allocation_probe_v2
    keywords = {
        "source_maps": source_maps,
        "effective_document_ordinals": effective_document_ordinals,
        "closure_document_ordinals": closure_document_ordinals,
    }
    handle, allocations = probe(
        documents,
        origins,
        attestation,
        memoryview(config),
        None,
        **keywords,
    )
    counters = handle._publication_counters_v2()
    assert allocations > 0
    assert counters.retained_source_map_rows > 0
    assert counters.retained_source_prefix_rows > 0
    assert counters.source_map_rows_emitted == 0
    assert counters.source_prefix_rows_emitted == 0
    handle._publication_close_v2()
    assert documents == original_documents
    assert source_maps == original_source_maps
    assert config == original_config

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native retained structural bridge allocation failure$",
        ):
            probe(
                documents,
                origins,
                attestation,
                memoryview(config),
                fail_after,
                **keywords,
            )
        assert documents == original_documents
        assert source_maps == original_source_maps
        assert config == original_config

    boundary_handle, boundary_allocations = probe(
        documents,
        origins,
        attestation,
        memoryview(config),
        allocations,
        **keywords,
    )
    assert boundary_allocations == allocations
    assert boundary_handle._publication_counters_v2() == counters
    assert boundary_handle._publication_closed_v2() is False
    boundary_handle._publication_close_v2()
    assert documents == original_documents
    assert source_maps == original_source_maps
    assert config == original_config


@pytest.fixture(scope="module")
def rdfxml_bridge_extension(extension: NativeTestExtension) -> NativeTestExtension:
    if not hasattr(extension, "_rdfxml_retained_bridge_allocation_probe_v2"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_rdfxml_retained_bridge_allocation_probe_v2"
            )
        pytest.skip("native retained RDF/XML bridge allocation hook is unavailable")
    return extension


def test_rdfxml_retained_bridge_allocations_fail_before_publication(
    rdfxml_bridge_extension: NativeTestExtension,
) -> None:
    source = bytearray(
        b"<rdf:RDF "
        b"xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#' "
        b"xmlns:rdfs='http://www.w3.org/2000/01/rdf-schema#' "
        b"xmlns:owl='http://www.w3.org/2002/07/owl#'>"
        b"<owl:Ontology rdf:about='urn:allocation:rdfxml'/>"
        b"<owl:Class rdf:about='urn:allocation:C'>"
        b"<rdfs:subClassOf rdf:resource='urn:allocation:D'/>"
        b"</owl:Class><owl:Class rdf:about='urn:allocation:D'/></rdf:RDF>"
    )
    original_source = bytes(source)
    config = bytearray(native._encode_config(ParseLimits(), None, verify=False))
    original_config = bytes(config)

    output, allocations = (
        rdfxml_bridge_extension._rdfxml_retained_bridge_allocation_probe_v2(
            memoryview(source),
            "urn:allocation:document",
            memoryview(config),
            True,
            False,
            False,
            False,
            None,
        )
    )
    assert output[:8] == b"PYNRRS2\0"
    assert allocations == 9
    assert source == original_source
    assert config == original_config

    for fail_after in range(allocations):
        with pytest.raises(
            MemoryError,
            match=r"^injected native RDF/XML retained bridge allocation failure$",
        ):
            rdfxml_bridge_extension._rdfxml_retained_bridge_allocation_probe_v2(
                memoryview(source),
                "urn:allocation:document",
                memoryview(config),
                True,
                False,
                False,
                False,
                fail_after,
            )
        assert source == original_source
        assert config == original_config

    boundary_output, boundary_allocations = (
        rdfxml_bridge_extension._rdfxml_retained_bridge_allocation_probe_v2(
            memoryview(source),
            "urn:allocation:document",
            memoryview(config),
            True,
            False,
            False,
            False,
            allocations,
        )
    )
    assert boundary_output == output
    assert boundary_allocations == allocations
    assert source == original_source
    assert config == original_config
