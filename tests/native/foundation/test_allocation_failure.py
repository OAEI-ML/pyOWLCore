from __future__ import annotations

import os
import struct

import pytest

from pyowl_core import ParseLimits, canonical_bytes, encode_snapshot
from pyowl_core.backends import native
from pyowl_core.model import StructuralNode, constructor_spec
from tests.generated.model.fixtures import model_fixtures
from tests.native.foundation._support import NativeTestExtension, load_extension
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
            allocations,
        )
    )
    assert boundary_output == output
    assert boundary_allocations == allocations
    assert source == original_source
    assert config == original_config
