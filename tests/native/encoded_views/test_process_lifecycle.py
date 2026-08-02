from __future__ import annotations

import gc
import mmap
import os
import select
import signal
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest

import pyowl_core
from pyowl_core import IRI, AxiomScope, Class, Declaration
from pyowl_core.backends.native_views import EncodedStructuralViewV2
from pyowl_core.exceptions import ClosedSnapshotError, SnapshotInUseError
from pyowl_core.model import canonical_bytes
from tests.native.encoded_views._independent import decode_root_canonical_bytes
from tests.native.encoded_views._support import (
    complete_constructor_snapshot,
    scalar_root_bytes,
)
from tests.native.encoded_views.test_public_native_direct import _proxy
from tests.native.foundation._support import NativeTestExtension, load_extension


@pytest.fixture(scope="module")
def extension() -> NativeTestExtension:
    selected = load_extension()
    required = (
        "_encoded_structural_fixture_v2",
        "_encoded_structural_columns_v2",
    )
    if any(not hasattr(selected, name) for name in required):
        pytest.skip("selected native artifact lacks the WP17 direct-column hooks")
    return selected


def _wait_for_child(read_fd: int, child: int) -> bytes:
    ready, _writable, _exceptional = select.select([read_fd], [], [], 10.0)
    if not ready:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        pytest.fail("forked encoded-view reader did not make progress")
    result = os.read(read_fd, 2_048)
    os.close(read_fd)
    _pid, status = os.waitpid(child, 0)
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    return result


def _child_result(write_fd: int, operation: Callable[[], object]) -> None:
    try:
        operation()
        os.write(write_fd, b"ENCODED_VIEW_FORK_OK")
    except BaseException as error:
        message = f"{type(error).__name__}: {error}".encode("utf-8", "replace")
        os.write(write_fd, b"ERROR:" + message[:1_024])
    finally:
        os.close(write_fd)
    os._exit(0)


def _publish_close_race(
    owner: Any,
    raw_owner: object,
) -> tuple[tuple[int, bytes], ...] | ClosedSnapshotError:
    barrier = Barrier(2)

    def publish() -> tuple[tuple[int, bytes], ...] | ClosedSnapshotError:
        barrier.wait()
        try:
            encoded = cast(EncodedStructuralViewV2, owner.view(EncodedStructuralViewV2))
        except ClosedSnapshotError as error:
            return error
        return cast(
            tuple[tuple[int, bytes], ...],
            decode_root_canonical_bytes(encoded.buffers),
        )

    def close() -> None:
        barrier.wait()
        cast(Any, raw_owner)._publication_close_v2()

    with ThreadPoolExecutor(max_workers=2) as executor:
        published = executor.submit(publish)
        closed = executor.submit(close)
        result = published.result(timeout=10)
        closed.result(timeout=10)
    return result


def test_direct_native_buffers_survive_concurrent_reads_and_owner_close(
    extension: NativeTestExtension,
) -> None:
    owner, raw_owner = _proxy(extension)
    encoded = owner.view(EncodedStructuralViewV2)
    expected = (
        (2, canonical_bytes(Declaration(Class(IRI("urn:encoded-view:fixture"))))),
    )

    def read(_index: int) -> tuple[tuple[int, bytes], ...]:
        counters = cast(Any, raw_owner)._publication_counters_v2()
        assert counters.encoded_view_requests >= 1
        return cast(
            tuple[tuple[int, bytes], ...],
            decode_root_canonical_bytes(encoded.buffers),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(read, range(128)))
    assert all(result == expected for result in results)
    assert owner.scalar_calls == 0

    cast(Any, raw_owner)._publication_close_v2()
    assert decode_root_canonical_bytes(encoded.buffers) == expected
    with pytest.raises(ClosedSnapshotError):
        owner.view(EncodedStructuralViewV2, scope=AxiomScope.ROOT)


def test_direct_native_publication_and_close_are_linearized(
    extension: NativeTestExtension,
) -> None:
    expected = (
        (2, canonical_bytes(Declaration(Class(IRI("urn:encoded-view:fixture"))))),
    )
    for _iteration in range(16):
        owner, raw_owner = _proxy(extension)
        result = _publish_close_race(owner, raw_owner)
        assert isinstance(result, ClosedSnapshotError) or result == expected
        assert owner.scalar_calls == 0


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_direct_native_view_and_fresh_request_are_safe_after_fork(
    extension: NativeTestExtension,
) -> None:
    owner, raw_owner = _proxy(extension)
    encoded = owner.view(EncodedStructuralViewV2)
    expected = (
        (2, canonical_bytes(Declaration(Class(IRI("urn:encoded-view:fixture"))))),
    )
    assert cast(Any, raw_owner)._publication_counters_v2().fork_reinitializations == 0

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - child failures are reported through the pipe
        os.close(read_fd)

        def child_operation() -> None:
            assert decode_root_canonical_bytes(encoded.buffers) == expected
            fresh = owner.view(EncodedStructuralViewV2, scope=AxiomScope.ROOT)
            assert decode_root_canonical_bytes(fresh.buffers) == expected
            counters = cast(Any, raw_owner)._publication_counters_v2()
            assert counters.fork_reinitializations == 1
            assert owner.scalar_calls == 0

        _child_result(write_fd, child_operation)

    os.close(write_fd)
    assert _wait_for_child(read_fd, child) == b"ENCODED_VIEW_FORK_OK"
    assert cast(Any, raw_owner)._publication_counters_v2().fork_reinitializations == 0
    assert decode_root_canonical_bytes(encoded.buffers) == expected
    cast(Any, raw_owner)._publication_close_v2()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_mapped_view_retains_inherited_mapping_across_fork(tmp_path: Path) -> None:
    source = complete_constructor_snapshot()
    expected = scalar_root_bytes(source)
    path = tmp_path / "forked-columns.pyocore"
    path.write_bytes(pyowl_core.encode_snapshot(source))
    mapped = pyowl_core.open_snapshot(path)
    assert isinstance(mapped, pyowl_core.MappedOntologySnapshot)
    encoded_holder = [mapped.view(pyowl_core.EncodedStructuralView)]
    mapped_zero_copy = (
        type(next(iter(encoded_holder[0].buffers.values())).obj) is mmap.mmap
    )

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - child failures are reported through the pipe
        os.close(read_fd)

        def child_operation() -> None:
            assert decode_root_canonical_bytes(encoded_holder[0].buffers) == expected

        _child_result(write_fd, child_operation)

    os.close(write_fd)
    assert _wait_for_child(read_fd, child) == b"ENCODED_VIEW_FORK_OK"
    assert decode_root_canonical_bytes(encoded_holder[0].buffers) == expected
    if not mapped_zero_copy:
        mapped.close()
        assert decode_root_canonical_bytes(encoded_holder[0].buffers) == expected
    else:
        with pytest.raises(SnapshotInUseError):
            mapped.close()
        encoded_holder.clear()
        gc.collect()
        mapped.close()
    assert mapped.closed
