from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyowl_core import BackendPreference, ImportPolicy, LoadOptions, load_snapshot
from pyowl_core.backends import native_views
from pyowl_core.backends.native_views import (
    EncodedStructuralViewV1,
    produce_encoded_structural_view_v1,
    validate_encoded_structural_view_v1,
)
from pyowl_core.document.document import Fingerprint
from pyowl_core.document.snapshot import AxiomScope, OntologyView
from pyowl_core.exceptions import BackendProtocolError, ResourceLimitError
from pyowl_core.limits import ParseLimits
from tests.native.encoded_views._support import complete_constructor_snapshot


def _publication(view: EncodedStructuralViewV1, **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "schema_name": view.schema_name,
        "schema_version": view.schema_version,
        "model_schema": view.model_schema,
        "owner": view.owner,
        "buffers": dict(view.buffers),
        "descriptor": view.descriptor,
        "structural_fingerprint": view.structural_fingerprint,
        "segments": view.segments,
        "scope": view.scope,
        "document_key": view.document_key,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _segment(view: EncodedStructuralViewV1, **changes: object) -> SimpleNamespace:
    direct = view.segments[0]
    values: dict[str, object] = {
        "role": direct.role,
        "owner": direct.owner,
        "source": direct.source,
        "posting_mode": direct.posting_mode,
        "root_ids": direct.root_ids,
        "anonymous_scope_map": direct.anonymous_scope_map,
        "member_token": direct.member_token,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _validate(candidate: object, view: EncodedStructuralViewV1) -> EncodedStructuralViewV1:
    return validate_encoded_structural_view_v1(
        candidate,
        expected_owner=view.owner,
        expected_scope=AxiomScope.CLOSURE,
        expected_document_key=None,
    )


def test_validator_freezes_exact_owner_and_fresh_buffer_views() -> None:
    snapshot = complete_constructor_snapshot()
    produced = produce_encoded_structural_view_v1(snapshot)
    publication = _publication(produced)
    frozen = _validate(publication, produced)

    assert frozen.owner is snapshot
    assert id(frozen) != id(publication)
    assert frozen.buffers is not publication.buffers
    assert all(frozen.buffers[name] is not publication.buffers[name] for name in frozen.buffers)
    assert frozen.structural_fingerprint == produced.structural_fingerprint


def test_readonly_view_of_mutable_exporter_is_copied_before_publication() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    buffers = dict(produced.buffers)
    original = bytes(buffers["root_kinds"])
    backing = bytearray(original)
    buffers["root_kinds"] = memoryview(backing).toreadonly()

    frozen = _validate(_publication(produced, buffers=buffers), produced)
    backing[0] ^= 0xFF

    assert bytes(frozen.buffers["root_kinds"]) == original
    assert type(frozen.buffers["root_kinds"].obj) is bytes


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"schema_name": "hostile"}, "ENCODED_VIEW_DESCRIPTOR"),
        ({"schema_version": True}, "ENCODED_VIEW_DESCRIPTOR"),
        ({"model_schema": 2}, "ENCODED_VIEW_DESCRIPTOR"),
        ({"descriptor": b"hostile"}, "ENCODED_VIEW_DESCRIPTOR"),
        ({"scope": AxiomScope.ROOT}, "ENCODED_VIEW_OPTIONS"),
        ({"document_key": "hostile"}, "ENCODED_VIEW_OPTIONS"),
    ),
)
def test_validator_rejects_hostile_scalar_descriptor_fields(
    changes: dict[str, object], code: str
) -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, **changes), produced)
    assert raised.value.code == code


def test_validator_rejects_wrong_owner_by_identity() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    other = complete_constructor_snapshot()
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, owner=other), produced)
    assert raised.value.code == "ENCODED_VIEW_OWNER"

    with pytest.raises(TypeError, match="expected_owner"):
        validate_encoded_structural_view_v1(
            produced,
            expected_owner=cast(OntologyView, object()),
            expected_scope=AxiomScope.CLOSURE,
            expected_document_key=None,
        )


def test_validator_rejects_mutable_noncontiguous_missing_and_extra_buffers() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    cases: list[dict[str, memoryview]] = []

    mutable = dict(produced.buffers)
    mutable["root_kinds"] = memoryview(bytearray(mutable["root_kinds"]))
    cases.append(mutable)

    noncontiguous = dict(produced.buffers)
    noncontiguous["scalar_bytes"] = noncontiguous["scalar_bytes"][::2]
    cases.append(noncontiguous)

    missing = dict(produced.buffers)
    missing.pop("root_ids")
    cases.append(missing)

    extra = dict(produced.buffers)
    extra["hostile"] = memoryview(b"")
    cases.append(extra)

    for buffers in cases:
        with pytest.raises(BackendProtocolError) as raised:
            _validate(_publication(produced, buffers=buffers), produced)
        assert raised.value.code == "ENCODED_VIEW_BUFFERS"


def test_validator_rejects_unknown_tag_bad_offsets_and_wrong_fingerprint() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())

    unknown_tag = dict(produced.buffers)
    tag_bytes = bytearray(unknown_tag["node_tags"])
    tag_bytes[:2] = (0xFFFF).to_bytes(2, "little")
    unknown_tag["node_tags"] = memoryview(bytes(tag_bytes))
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, buffers=unknown_tag), produced)
    assert raised.value.code == "ENCODED_VIEW_UNSUPPORTED_TAG"

    bad_offsets = dict(produced.buffers)
    offsets = bytearray(bad_offsets["node_field_offsets"])
    offsets[-8:] = (2**64 - 1).to_bytes(8, "little")
    bad_offsets["node_field_offsets"] = memoryview(bytes(offsets))
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, buffers=bad_offsets), produced)
    assert raised.value.code == "ENCODED_VIEW_STRUCTURE"

    wrong_fingerprint = Fingerprint("sha256", 1, b"\xff" * 32)
    with pytest.raises(BackendProtocolError) as raised:
        _validate(
            _publication(produced, structural_fingerprint=wrong_fingerprint),
            produced,
        )
    assert raised.value.code == "ENCODED_VIEW_FINGERPRINT"


def test_validator_rejects_duplicate_set_roots_and_enforces_owner_limits() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    duplicate = dict(produced.buffers)
    root_kinds = bytes(duplicate["root_kinds"])
    root_ids = bytes(duplicate["root_ids"])
    duplicate["root_kinds"] = memoryview(root_kinds[:1] + root_kinds)
    duplicate["root_ids"] = memoryview(root_ids[:4] + root_ids)
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, buffers=duplicate), produced)
    assert raised.value.code == "ENCODED_VIEW_STRUCTURE"

    tiny_bytes = replace(ParseLimits(), max_index_bytes=1)
    with pytest.raises(ResourceLimitError) as limited:
        validate_encoded_structural_view_v1(
            produced,
            expected_owner=produced.owner,
            expected_scope=AxiomScope.CLOSURE,
            expected_document_key=None,
            limits=tiny_bytes,
        )
    assert limited.value.limit == "max_index_bytes"

    shallow = replace(ParseLimits(), max_nesting_depth=1)
    with pytest.raises(ResourceLimitError) as limited:
        validate_encoded_structural_view_v1(
            produced,
            expected_owner=produced.owner,
            expected_scope=AxiomScope.CLOSURE,
            expected_document_key=None,
            limits=shallow,
        )
    assert limited.value.limit == "max_nesting_depth"


def test_segment_validator_rejects_bad_postings_and_all_mode_payloads() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    root_count = len(produced.buffers["root_ids"]) // 4

    out_of_range = _segment(
        produced,
        posting_mode=1,
        root_ids=memoryview((root_count + 1).to_bytes(4, "little")),
    )
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, segments=(out_of_range,)), produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"

    all_with_postings = _segment(
        produced,
        root_ids=memoryview((1).to_bytes(4, "little")),
    )
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, segments=(all_with_postings,)), produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"

    duplicate_postings = _segment(
        produced,
        posting_mode=1,
        root_ids=memoryview((1).to_bytes(4, "little") * 2),
    )
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, segments=(duplicate_postings,)), produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"

    empty_include = _segment(
        produced,
        posting_mode=1,
    )
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, segments=(empty_include,)), produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"


def test_segment_validator_rejects_hostile_anonymous_scope_maps() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    referenced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    first = b"a" * 32
    second = b"b" * 32
    third = b"c" * 32
    cases = (
        memoryview(b"x" * 63),
        memoryview(bytearray(first + second)).toreadonly(),
        memoryview(first + first),
        memoryview(first + second + first + third),
        memoryview(second + third + first + third),
    )
    for scope_map in cases:
        base = _segment(
            produced,
            role=2,
            source=referenced,
            owner=referenced.owner,
            anonymous_scope_map=scope_map,
        )
        with pytest.raises(BackendProtocolError) as raised:
            _validate(_publication(produced, segments=(base,)), produced)
        assert raised.value.code == "ENCODED_VIEW_SEGMENTS"

    local = _segment(produced, anonymous_scope_map=memoryview(first + second))
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, segments=(local,)), produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"


def test_segment_fingerprint_covers_exact_anonymous_scope_map_bytes() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    referenced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    direct = produced.segments[0]
    first = replace(
        direct,
        role=2,
        owner=referenced.owner,
        source=referenced,
        anonymous_scope_map=memoryview(b"a" * 32 + b"b" * 32),
    )
    second = replace(
        first,
        anonymous_scope_map=memoryview(b"a" * 32 + b"c" * 32),
    )
    fingerprint = cast(
        Callable[[object, object], Fingerprint],
        cast(Any, native_views)._fingerprint,
    )

    assert fingerprint(produced.buffers, (first,)) != fingerprint(
        produced.buffers, (second,)
    )


def test_referenced_anonymous_scope_map_freezes_and_fingerprints_exact_bytes() -> None:
    empty_owner = load_snapshot(
        b"Ontology(<urn:encoded-empty>)",
        options=LoadOptions(
            backend=BackendPreference.PYTHON,
            imports=ImportPolicy.IGNORE,
        ),
    )
    top = produce_encoded_structural_view_v1(empty_owner)
    referenced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    scope_map = memoryview(b"a" * 32 + b"b" * 32)
    base = _segment(
        top,
        role=2,
        source=referenced,
        owner=referenced.owner,
        anonymous_scope_map=scope_map,
    )
    fingerprint = cast(
        Callable[[object, object], Fingerprint],
        cast(Any, native_views)._fingerprint,
    )
    candidate = _publication(
        top,
        segments=(base,),
        structural_fingerprint=fingerprint(top.buffers, (base,)),
    )

    frozen = _validate(candidate, top)

    assert bytes(frozen.segments[0].anonymous_scope_map) == bytes(scope_map)
    assert frozen.segments[0].anonymous_scope_map is not scope_map
    assert type(frozen.segments[0].anonymous_scope_map.obj) is bytes


def test_segment_validator_rejects_unselected_local_roots() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    referenced = produce_encoded_structural_view_v1(complete_constructor_snapshot())

    base_only = _segment(produced, role=2, source=referenced, owner=referenced.owner)
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, segments=(base_only,)), produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"

    delta_with_subset = _segment(
        produced,
        role=3,
        posting_mode=1,
        root_ids=memoryview((1).to_bytes(4, "little")),
    )
    with pytest.raises(BackendProtocolError) as raised:
        _validate(
            _publication(produced, segments=(base_only, delta_with_subset)),
            produced,
        )
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"


def test_segment_validator_rejects_cycles_and_member_token_collisions() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    cyclic = _publication(produced)
    cyclic.segments = (_segment(produced, role=2, source=cyclic, owner=produced.owner),)
    with pytest.raises(BackendProtocolError) as raised:
        _validate(cyclic, produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"

    token = b"m" * 32
    first = _segment(
        produced,
        role=4,
        source=produced,
        owner=produced.owner,
        member_token=token,
    )
    second = _segment(
        produced,
        role=4,
        source=produced,
        owner=produced.owner,
        member_token=token,
    )
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(produced, segments=(first, second)), produced)
    assert raised.value.code == "ENCODED_VIEW_SEGMENTS"


def test_segment_validator_rechecks_referenced_view_fingerprint_and_exporters() -> None:
    top = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    corrupt = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    object.__setattr__(
        corrupt,
        "structural_fingerprint",
        Fingerprint("sha256", 1, b"\x11" * 32),
    )
    base = _segment(top, role=2, source=corrupt, owner=corrupt.owner)
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(top, segments=(base,)), top)
    assert raised.value.code == "ENCODED_VIEW_FINGERPRINT"

    mutable = produce_encoded_structural_view_v1(complete_constructor_snapshot())
    source_buffers = dict(mutable.buffers)
    source_buffers["root_kinds"] = memoryview(bytearray(source_buffers["root_kinds"])).toreadonly()
    object.__setattr__(mutable, "buffers", source_buffers)
    base = _segment(top, role=2, source=mutable, owner=mutable.owner)
    with pytest.raises(BackendProtocolError) as raised:
        _validate(_publication(top, segments=(base,)), top)
    assert raised.value.code == "ENCODED_VIEW_BUFFERS"


def test_validator_contains_raising_descriptors() -> None:
    produced = produce_encoded_structural_view_v1(complete_constructor_snapshot())

    class Hostile:
        @property
        def schema_name(self) -> str:
            raise RuntimeError("descriptor executed")

    with pytest.raises(BackendProtocolError) as raised:
        _validate(Hostile(), produced)
    assert raised.value.code == "ENCODED_VIEW_DESCRIPTOR"
