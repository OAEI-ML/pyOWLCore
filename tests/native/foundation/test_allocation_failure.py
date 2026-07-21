from __future__ import annotations

import os

import pytest

from pyowl_core import ParseLimits, canonical_bytes
from pyowl_core.backends import native
from pyowl_core.model import StructuralNode, constructor_spec
from tests.generated.model.fixtures import model_fixtures
from tests.native.foundation._support import NativeTestExtension, load_extension


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    if not hasattr(selected, "_component_allocation_probe_v1"):
        if os.environ.get("PYOWL_CORE_TEST_HOOKS_REQUIRED") == "1":
            pytest.fail(
                "selected native test-hooks artifact lacks "
                "_component_allocation_probe_v1"
            )
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
