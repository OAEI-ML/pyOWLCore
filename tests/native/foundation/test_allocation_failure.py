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


def test_parser_allocation_budget_checkpoints_fail_closed(
    parser_extension: NativeTestExtension,
) -> None:
    source = (
        b"Prefix(:=<urn:allocation:>) Ontology(<urn:ontology> "
        b"Import(<urn:import>) Annotation(:label \"hello\"@EN) "
        b"Declaration(Class(:C)) "
        b"SubClassOf(:C ObjectSomeValuesFrom(:property :D)))"
    )
    request = bytearray(
        struct.pack("<8sHHQ", b"PYNFSS1\0", 1, 0, len(source)) + source
    )
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
