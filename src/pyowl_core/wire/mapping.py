"""Safe read-only mmap ownership and lazy OntologySnapshot publication."""

from __future__ import annotations

import mmap as _mmap
import os
import stat
import threading
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import TypeVar, cast

from pyowl_core._immutable import FrozenMap
from pyowl_core.cancellation import CancellationToken
from pyowl_core.config import BackendPreference, LoadOptions
from pyowl_core.diagnostics import Diagnostic
from pyowl_core.document.document import Fingerprint, OntologyDocument
from pyowl_core.document.fingerprint import StructuralContext, fingerprint_bytes
from pyowl_core.document.identity import _OntologyIdentityMetadata
from pyowl_core.document.imports import DocumentRecord, ImportManifest
from pyowl_core.document.provenance import OriginIndex
from pyowl_core.document.snapshot import (
    _ENCODED_STRUCTURAL_VIEW_FEATURE,
    AxiomScope,
    CoreCapabilities,
    LoadReport,
    OntologySnapshot,
    _encoded_view_schemas_v2,
)
from pyowl_core.exceptions import (
    ClosedSnapshotError,
    SnapshotInUseError,
    WireCorruptionError,
)
from pyowl_core.limits import ParseLimits
from pyowl_core.model import Annotation, CanonicalSet, Entity, EntityKind, StructuralNode
from pyowl_core.model.axioms import AxiomNode
from pyowl_core.model.validation import OWL2DLReport

from .codec import (
    InspectedWire,
    checked_materialize_image,
    decode_snapshot,
    encoded_structural_buffers_from_inspected_v2,
    identity_metadata_from_inspected,
    image_import_options,
    validate_bytes,
)
from .schema import HEADER_SIZE

A = TypeVar("A", bound=AxiomNode)
V = TypeVar("V")


class _MappedState:
    __slots__ = (
        "capabilities",
        "close_callbacks",
        "closed",
        "decoded",
        "dependents",
        "fd",
        "identity",
        "index_cache",
        "inspected",
        "limits",
        "lock",
        "mapping",
        "options",
        "path",
        "pid",
        "verified",
    )

    def __init__(
        self,
        path: Path,
        fd: int,
        mapping: _mmap.mmap,
        inspected: InspectedWire,
        limits: ParseLimits,
        identity: tuple[int, int, int, int],
        verified: bool,
    ) -> None:
        self.path = path
        self.fd = fd
        self.mapping = mapping
        self.inspected = inspected
        self.limits = limits
        self.identity = identity
        self.verified = verified
        self.pid = os.getpid()
        self.lock = threading.RLock()
        self.decoded: OntologySnapshot | None = None
        self.dependents = 0
        self.closed = False
        self.close_callbacks: list[Callable[[], None]] = []
        summary = inspected.summary
        import_policy, offline = image_import_options(inspected.image)
        self.options = LoadOptions(
            imports=import_policy,
            backend=BackendPreference.PYTHON,
            limits=limits,
            offline=offline,
        )
        features = {
            "owl2-structural",
            "document-boundaries",
            "import-manifest",
            "immutable-snapshot",
            "document-scoped-anonymous",
            "structural-indexes",
            "ontology-identity-index",
            "wire-v1",
            "mmap-snapshot",
            "lazy-model",
            _ENCODED_STRUCTURAL_VIEW_FEATURE,
        }
        if verified:
            features.add("wire-verified")
        if summary.structural_context is not None:
            features.add("materialized-view")
        self.capabilities = CoreCapabilities(
            1,
            2,
            (1, 2),
            frozenset(features),
            _encoded_view_schemas_v2(),
            "python",
        )
        from pyowl_core.index.cache import create_index_cache

        self.index_cache = create_index_cache(limits)

    def ensure_open(self) -> None:
        self._after_fork()
        if self.closed:
            raise ClosedSnapshotError("mapped ontology snapshot is closed")
        try:
            current = os.fstat(self.fd)
        except OSError as error:
            raise ClosedSnapshotError(
                "mapped ontology snapshot descriptor is unavailable"
            ) from error
        identity = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        if identity != self.identity:
            raise WireCorruptionError(
                "mapped wire file changed in place after validation",
                code="WIRE_MAPPED_CHANGED",
            )

    def _after_fork(self) -> None:
        current = os.getpid()
        if current == self.pid:
            return
        # Read-only bytes and immutable decoded values are safe after fork;
        # process-local synchronization/caches are not.
        self.pid = current
        self.lock = threading.RLock()
        from pyowl_core.index.cache import create_index_cache

        self.index_cache = create_index_cache(self.limits)

    def materialize(self) -> OntologySnapshot:
        self.ensure_open()
        retained = self.decoded
        if retained is not None:
            return retained
        with self.lock:
            self.ensure_open()
            retained = self.decoded
            if retained is None:
                # Failed construction is deliberately not cached; callers may
                # retry after transient cancellation/deadline failure.
                retained = checked_materialize_image(self.inspected, limits=self.limits)
                self.decoded = retained
            return retained

    def retain(self) -> _MappedLease:
        self.ensure_open()
        with self.lock:
            self.ensure_open()
            self.dependents += 1
        return _MappedLease(self)

    def release(self) -> None:
        self._after_fork()
        with self.lock:
            if self.dependents:
                self.dependents -= 1

    def close(self) -> None:
        self._after_fork()
        with self.lock:
            if self.closed:
                return
            # Drop owner-local strong cache references before checking leases.
            # A caller-retained mmap view still keeps its lease and therefore
            # continues to fail close with the public lifecycle error.
            self.index_cache.clear()
            if self.dependents:
                raise SnapshotInUseError(
                    "mapped snapshot still has dependent ontology views",
                    code="SNAPSHOT_IN_USE",
                )
            self._close_resources()

    def _close_resources(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.inspected.image.release()
        finally:
            try:
                self.mapping.close()
            finally:
                os.close(self.fd)
        callbacks, self.close_callbacks = self.close_callbacks, []
        for callback in callbacks:
            with suppress(Exception):
                callback()

    def add_close_callback(self, callback: Callable[[], None]) -> None:
        self.ensure_open()
        with self.lock:
            self.close_callbacks.append(callback)

    def __del__(self) -> None:
        with suppress(Exception):
            self._close_resources()


class _MappedLease:
    __slots__ = ("_active", "_state")

    def __init__(self, state: _MappedState) -> None:
        self._state = state
        self._active = True

    def release(self) -> None:
        if self._active:
            self._active = False
            self._state.release()

    def __del__(self) -> None:
        self.release()


class MappedOntologySnapshot(OntologySnapshot):
    """OntologySnapshot-compatible lazy view backed by one read-only mapping."""

    __slots__ = ("_mapped_state",)
    _mapped_state: _MappedState

    def __init__(self, state: _MappedState) -> None:
        object.__setattr__(self, "_mapped_state", state)

    @property
    def root(self) -> OntologyDocument:
        return self._materialized.root

    @property
    def documents(self) -> tuple[OntologyDocument, ...]:
        return self._materialized.documents

    @property
    def import_manifest(self) -> ImportManifest:
        return self._materialized.import_manifest

    @property
    def root_document_key(self) -> str:
        self._check_open()
        return self._mapped_state.inspected.summary.root_document_key

    @property
    def load_options(self) -> LoadOptions:
        self._check_open()
        return self._mapped_state.options

    @property
    def limits(self) -> ParseLimits:
        return self.load_options.limits

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        self._check_open()
        return ()

    @property
    def timings(self) -> Mapping[str, float]:
        self._check_open()
        return FrozenMap()

    @property
    def resolution_attempts(self) -> int:
        self._check_open()
        return 0

    @property
    def acquisition_cache_hits(self) -> int:
        self._check_open()
        return 0

    @property
    def document_cache_hits(self) -> int:
        self._check_open()
        return 0

    @property
    def _index_cache(self) -> object:
        self._check_open()
        return self._mapped_state.index_cache

    @property
    def _materialized(self) -> OntologySnapshot:
        return self._mapped_state.materialize()

    def _check_open(self) -> None:
        self._mapped_state.ensure_open()

    def _retain_dependent(self) -> object:
        return self._mapped_state.retain()

    def _encoded_structural_columns_v2(
        self,
        scope: AxiomScope,
        document_key: str | None,
        limits: ParseLimits,
    ) -> tuple[Mapping[str, memoryview], object] | None:
        """Borrow validated closure columns together with a mapping lease."""

        self._check_open()
        if scope is not AxiomScope.CLOSURE or document_key is not None:
            return None
        buffers = encoded_structural_buffers_from_inspected_v2(
            self._mapped_state.inspected,
            limits=limits,
        )
        if buffers is None:
            return None
        return buffers, self._mapped_state.retain()

    def _mapped_wire_source_v1(self) -> tuple[memoryview, object] | None:
        """Borrow the exact verified wire image without model materialization."""

        self._check_open()
        if not self._mapped_state.verified:
            return None
        return self._mapped_state.inspected.image.data, self._mapped_state.retain()

    def _on_close(self, callback: Callable[[], None]) -> None:
        self._mapped_state.add_close_callback(callback)

    @property
    def closed(self) -> bool:
        self._mapped_state._after_fork()
        return self._mapped_state.closed

    def close(self) -> None:
        self._mapped_state.close()

    def __enter__(self) -> MappedOntologySnapshot:
        self._check_open()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    @property
    def capabilities(self) -> CoreCapabilities:
        self._check_open()
        return self._mapped_state.capabilities

    @property
    def is_complete(self) -> bool:
        self._check_open()
        return self._mapped_state.inspected.summary.complete

    @property
    def origin_index(self) -> OriginIndex:
        return self._materialized.origin_index

    @property
    def structural_context(self) -> StructuralContext | None:
        self._check_open()
        return self._mapped_state.inspected.summary.structural_context

    @property
    def structural_fingerprint(self) -> Fingerprint:
        self._check_open()
        return self._mapped_state.inspected.summary.structural_fingerprint

    @property
    def logical_fingerprint(self) -> Fingerprint:
        self._check_open()
        return self._mapped_state.inspected.summary.logical_fingerprint

    @property
    def signature_fingerprint(self) -> Fingerprint:
        self._check_open()
        return self._mapped_state.inspected.summary.signature_fingerprint

    @property
    def report(self) -> LoadReport:
        self._check_open()
        summary = self._mapped_state.inspected.summary
        return LoadReport(
            "python",
            (0, 2),
            2,
            summary.document_count,
            summary.total_source_bytes,
            summary.effective_axiom_count,
            0,
            0,
            0,
            {},
            (),
            summary.structural_fingerprint,
            summary.logical_fingerprint,
            summary.signature_fingerprint,
            None,
        )

    @property
    def owl2_dl_report(self) -> OWL2DLReport | None:
        self._check_open()
        return None

    def _anonymous_document_scopes(self) -> frozenset[bytes]:
        self._check_open()
        from pyowl_core.backends.native_views import (
            EncodedStructuralViewV2,
            _anonymous_document_scopes_from_encoded_view_v2,
        )

        encoded = self.view(EncodedStructuralViewV2)
        return _anonymous_document_scopes_from_encoded_view_v2(encoded)

    def _anonymous_scope_lineage(self) -> tuple[tuple[bytes, bytes, bytes], ...]:
        leaf = fingerprint_bytes(self.structural_fingerprint)
        return tuple((scope, scope, leaf) for scope in sorted(self._anonymous_document_scopes()))

    def _ontology_identity_metadata(
        self,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> _OntologyIdentityMetadata:
        self._check_open()
        return identity_metadata_from_inspected(
            self._mapped_state.inspected,
            limits=self.limits,
            cancellation_token=cancellation_token,
        )

    def document(self, document_key: str) -> OntologyDocument:
        return self._materialized.document(document_key)

    def iter_documents(self) -> Iterator[tuple[DocumentRecord, OntologyDocument]]:
        yield from self._materialized.iter_documents()

    def iter_axioms(
        self,
        axiom_type: type[A] | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[AxiomNode | A]:
        yield from self._materialized.iter_axioms(
            axiom_type, scope=scope, document_key=document_key
        )

    def iter_extensions(
        self,
        namespace: str | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> Iterator[StructuralNode]:
        yield from self._materialized.iter_extensions(
            namespace, scope=scope, document_key=document_key
        )

    def ontology_annotations(
        self,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> CanonicalSet[Annotation]:
        return self._materialized.ontology_annotations(scope=scope, document_key=document_key)

    def contains(
        self,
        axiom: AxiomNode,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
    ) -> bool:
        return self._materialized.contains(axiom, scope=scope, document_key=document_key)

    def signature(
        self,
        kind: EntityKind | None = None,
        *,
        scope: AxiomScope = AxiomScope.CLOSURE,
        document_key: str | None = None,
        include_builtins: bool = True,
    ) -> tuple[Entity, ...]:
        return self._materialized.signature(
            kind,
            scope=scope,
            document_key=document_key,
            include_builtins=include_builtins,
        )

    def view(self, view_type: type[V], /, **options: object) -> V:
        self._check_open()
        if not isinstance(view_type, type):
            raise TypeError("view_type must be a type")
        if view_type in (OntologySnapshot, MappedOntologySnapshot) or isinstance(self, view_type):
            if options:
                raise TypeError("mapped snapshot identity view accepts no options")
            return cast(V, self)
        from pyowl_core.index.cache import request_index_view

        return request_index_view(self, view_type, options)

    def materialize(self) -> OntologySnapshot:
        return self._materialized

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, OntologySnapshot):
            return NotImplemented
        other_value = other.materialize() if isinstance(other, MappedOntologySnapshot) else other
        return self._materialized == other_value

    def __hash__(self) -> int:
        value = int.from_bytes(self.structural_fingerprint.digest[:8], "big", signed=True)
        return -2 if value == -1 else value

    def __repr__(self) -> str:
        if self.closed:
            return "MappedOntologySnapshot(closed=True)"
        return (
            "MappedOntologySnapshot("
            f"path={str(self._mapped_state.path)!r}, "
            f"structural_fingerprint={self.structural_fingerprint.hex!r})"
        )


def open_snapshot(
    path: str | os.PathLike[str],
    *,
    mmap: bool = True,
    limits: ParseLimits | None = None,
    verify: bool = True,
    cancellation_token: CancellationToken | None = None,
) -> OntologySnapshot:
    """Open a validated snapshot eagerly or through a stable read-only mmap."""

    if not isinstance(mmap, bool):
        raise TypeError("mmap must be bool")
    if not isinstance(verify, bool):
        raise TypeError("verify must be bool")
    selected_limits = ParseLimits() if limits is None else limits
    if not isinstance(selected_limits, ParseLimits):
        raise TypeError("limits must be ParseLimits or None")
    selected_path = Path(os.fspath(path))
    if not mmap:
        with selected_path.open("rb") as stream:
            return decode_snapshot(
                stream,
                limits=selected_limits,
                verify=verify,
                cancellation_token=cancellation_token,
            )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(selected_path, flags)
    mapping: _mmap.mmap | None = None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise WireCorruptionError("wire mmap source must be a regular file")
        if before.st_size < HEADER_SIZE:
            raise WireCorruptionError("wire mmap source is shorter than the header")
        if before.st_size > selected_limits.max_wire_bytes:
            from pyowl_core.exceptions import WireLimitError

            raise WireLimitError("wire mmap source exceeds max_wire_bytes")
        mapping = _mmap.mmap(fd, before.st_size, access=_mmap.ACCESS_READ)
        with memoryview(mapping) as mapped_view:
            inspected = validate_bytes(
                mapped_view,
                limits=selected_limits,
                verify=verify,
                cancellation_token=cancellation_token,
                lazy_model_validation=True,
            )
        after = os.fstat(fd)
        identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != expected:
            inspected.image.release()
            raise WireCorruptionError(
                "wire mmap source changed during validation", code="WIRE_MAPPED_CHANGED"
            )
        state = _MappedState(
            selected_path.absolute(),
            fd,
            mapping,
            inspected,
            selected_limits,
            identity,
            verify,
        )
        mapping = None
        fd = -1
        return MappedOntologySnapshot(state)
    except BaseException as error:
        # Validation failures can retain mmap-backed memoryview slices in
        # traceback frame locals.  Clear those locals before closing the
        # mapping so Windows does not leave a corrupt cache file locked.
        traceback.clear_frames(error.__traceback__)
        raise
    finally:
        if mapping is not None:
            with suppress(BufferError):
                mapping.close()
        if fd >= 0:
            os.close(fd)


__all__ = ["MappedOntologySnapshot", "open_snapshot"]
