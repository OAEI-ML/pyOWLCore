from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast

import pytest

from pyowl_core.document.native_storage import (
    ontology_snapshot_from_native_publication_v2,
)
from tests.native.foundation._support import load_extension
from tests.native.publication_handoff.test_rust_owner_v2 import _rust_publication

ROOT = Path(__file__).parents[3]


class _Closable(Protocol):
    def close(self) -> None: ...


def _require_rust_owner() -> None:
    extension = load_extension()
    fixture = getattr(extension, "_publication_fixture_v2", None)
    if not callable(fixture):
        pytest.skip("selected native artifact lacks the V2 publication test hook")


def test_repeated_rust_owner_release_survives_interpreter_teardown() -> None:
    _require_rust_owner()
    script = r"""
import gc

from pyowl_core.document.native_storage import ontology_snapshot_from_native_publication_v2
from tests.native.publication_handoff.test_rust_owner_v2 import _rust_publication

for _index in range(32):
    publication, owner = _rust_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(publication)
    document = snapshot.root
    assert len(tuple(snapshot.iter_axioms())) == 1
    snapshot.close()
    assert len(tuple(document.iter_axioms())) == 1
    document.close()
    del document, snapshot, publication, owner
    gc.collect()

print("RUST_OWNER_TEARDOWN_OK", flush=True)
"""
    environment = os.environ.copy()
    python_path = [str(ROOT / "src"), str(ROOT)]
    inherited = environment.get("PYTHONPATH")
    if inherited:
        python_path.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(python_path)

    process = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "RUST_OWNER_TEARDOWN_OK"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_rust_owner_resets_runtime_state_in_a_real_fork() -> None:
    _require_rust_owner()
    publication, _owner = _rust_publication()
    snapshot = ontology_snapshot_from_native_publication_v2(publication)
    assert len(tuple(snapshot.iter_axioms())) == 1
    parent_counters = publication.handle._facade_counters_v2()
    assert parent_counters.fork_reinitializations == 0

    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:  # pragma: no cover - assertions are reported through the pipe
        os.close(read_fd)
        try:
            assert len(tuple(snapshot.iter_axioms())) == 1
            counters = publication.handle._facade_counters_v2()
            assert counters.fork_reinitializations == 1
            os.write(write_fd, b"RUST_OWNER_FORK_OK")
        except BaseException as error:
            message = f"{type(error).__name__}: {error}".encode("utf-8", "replace")
            os.write(write_fd, b"ERROR:" + message[:1_024])
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    ready, _writable, _exceptional = select.select([read_fd], [], [], 10.0)
    if not ready:
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        pytest.fail("forked native owner did not make progress")
    result = os.read(read_fd, 2_048)
    os.close(read_fd)
    _pid, status = os.waitpid(child, 0)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0
    assert result == b"RUST_OWNER_FORK_OK"
    assert len(tuple(snapshot.iter_axioms())) == 1
    assert publication.handle._facade_counters_v2().fork_reinitializations == 0
    cast(_Closable, snapshot).close()
