from __future__ import annotations

import gc
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pyowl_core import (
    ClosedSnapshotError,
    DeclarationIndex,
    MappedOntologySnapshot,
    OntologyDelta,
    OntologySnapshot,
    SnapshotInUseError,
    WireCorruptionError,
    apply_delta,
    encode_snapshot,
    open_snapshot,
)

from .conftest import snapshot


def _open_mapped(path: str | Path) -> MappedOntologySnapshot:
    result = open_snapshot(path)
    assert isinstance(result, MappedOntologySnapshot)
    return result


def _fork_read(path: str, expected: str, connection) -> None:  # type: ignore[no-untyped-def]
    mapped = _open_mapped(path)
    connection.send(
        (mapped.structural_fingerprint.hex == expected, len(tuple(mapped.iter_axioms())))
    )
    mapped.close()
    connection.close()


def _fork_inherited(mapped, connection) -> None:  # type: ignore[no-untyped-def]
    connection.send((mapped.structural_fingerprint.hex, len(tuple(mapped.iter_axioms()))))
    mapped.close()
    connection.close()


def test_mmap_open_is_metadata_only_then_publishes_one_lazy_snapshot(tmp_path: Path) -> None:
    source = snapshot("A", "B")
    path = tmp_path / "snapshot.pyocore"
    path.write_bytes(encode_snapshot(source))
    opened = _open_mapped(path)
    assert isinstance(opened, OntologySnapshot)
    assert opened._mapped_state.decoded is None
    assert opened._mapped_state.inspected.materialized_model_cache is None
    assert opened.structural_fingerprint == source.structural_fingerprint
    assert opened.report.effective_axiom_count == 2
    assert opened._mapped_state.decoded is None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _index: tuple(opened.iter_axioms()), range(32)))
    assert all(result == results[0] for result in results)
    retained = opened._mapped_state.decoded
    assert retained is not None
    assert all(opened.materialize() is retained for _ in range(4))
    assert tuple(opened.view(DeclarationIndex).entities())
    opened.close()


def test_context_close_and_dependent_leases(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.pyocore"
    path.write_bytes(encode_snapshot(snapshot("A")))
    opened = _open_mapped(path)
    overlay = apply_delta(opened, OntologyDelta())
    with pytest.raises(SnapshotInUseError):
        opened.close()
    del overlay
    gc.collect()
    opened.close()
    opened.close()
    assert opened.closed
    with pytest.raises(ClosedSnapshotError):
        _fingerprint = opened.structural_fingerprint

    with _open_mapped(path) as context:
        assert context.structural_fingerprint.digest
    assert isinstance(context, MappedOntologySnapshot) and context.closed


def test_atomic_replacement_keeps_old_mapping_and_in_place_change_is_detected(
    tmp_path: Path,
) -> None:
    old = snapshot("A")
    new = snapshot("B")
    path = tmp_path / "snapshot.pyocore"
    replacement = tmp_path / "replacement.pyocore"
    path.write_bytes(encode_snapshot(old))
    replacement.write_bytes(encode_snapshot(new))
    opened = _open_mapped(path)
    os.replace(replacement, path)
    assert opened.structural_fingerprint == old.structural_fingerprint
    opened.close()
    current = _open_mapped(path)
    assert current.structural_fingerprint == new.structural_fingerprint
    current.close()

    changed = _open_mapped(path)
    with path.open("r+b") as stream:
        stream.truncate(96)
    with pytest.raises(WireCorruptionError):
        _fingerprint = changed.structural_fingerprint
    # Restore before finalization so closing never touches truncated pages.
    path.write_bytes(encode_snapshot(new))
    changed.close()


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(), reason="fork unavailable"
)
def test_fresh_and_inherited_process_local_mapping_state(tmp_path: Path) -> None:
    source = snapshot("A")
    path = tmp_path / "snapshot.pyocore"
    path.write_bytes(encode_snapshot(source))
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(False)
    process = context.Process(
        target=_fork_read,
        args=(str(path), source.structural_fingerprint.hex, child),
    )
    process.start()
    assert parent.recv() == (True, 1)
    process.join(10)
    assert process.exitcode == 0

    inherited = _open_mapped(path)
    parent, child = context.Pipe(False)
    inherited_process = context.Process(target=_fork_inherited, args=(inherited, child))
    inherited_process.start()
    assert parent.recv() == (source.structural_fingerprint.hex, 1)
    inherited_process.join(10)
    assert inherited_process.exitcode == 0
    assert len(tuple(inherited.iter_axioms())) == 1
    inherited.close()
