from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from pyowl_core import (
    IRI,
    AxiomScope,
    BackendPreference,
    BackendProtocolError,
    EncodedStructuralViewV2,
    ImportPolicy,
    LoadOptions,
    MappingResolver,
    ResolvedDocument,
    ResourceLimitError,
    encoded_scopes_equivalent,
    load_snapshot,
    native_validation_report,
    validate_encoded_structural_view_v2,
)
from pyowl_core.backends import native_views
from tests.native.foundation._support import load_extension

SOURCE = b"""Ontology(<urn:receipt>
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 SubClassOf(<urn:A> <urn:B>)
 AnnotationAssertion(<urn:label> <urn:A> "A"@en)
)"""


@pytest.fixture(scope="module", autouse=True)
def extension() -> Any:
    selected = load_extension()
    assert getattr(selected, "NATIVE_VALIDATED_COLUMNS_API_VERSION", None) == 1
    return selected


def snapshot(backend: BackendPreference = BackendPreference.NATIVE) -> Any:
    return load_snapshot(SOURCE, options=LoadOptions(backend=backend, imports=ImportPolicy.IGNORE))


def validate(view: Any, **options: Any) -> Any:
    return validate_encoded_structural_view_v2(
        view,
        expected_owner=view.owner,
        expected_scope=view.scope,
        expected_document_key=view.document_key,
        require_native_validation=True,
        **options,
    )


def forbidden(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("strict publication entered Python structural work")


def test_native_receipt_matches_reference_without_python_validation(monkeypatch: Any) -> None:
    reference = snapshot(BackendPreference.PYTHON).view(EncodedStructuralViewV2)
    owner = snapshot()
    monkeypatch.setattr(native_views, "_validate_columns", forbidden)
    monkeypatch.setattr(native_views, "_fingerprint", forbidden)
    monkeypatch.setattr(type(owner), "iter_axioms", forbidden)
    view = owner.view(EncodedStructuralViewV2, require_native_validation=True)
    assert {k: bytes(v) for k, v in view.buffers.items()} == {
        k: bytes(v) for k, v in reference.buffers.items()
    }
    assert view.structural_fingerprint == reference.structural_fingerprint
    assert validate(view) is view
    assert owner.view(EncodedStructuralViewV2, require_native_validation=True) is view
    report = native_validation_report(view)
    assert report["backend"] == "native"
    assert report["native_validation_passes"] == 1
    assert report["canonical_work"] > 0


@pytest.mark.parametrize("change", ["buffers", "owner", "scope", "fingerprint", "receipt"])
def test_receipt_rejects_foreign_or_changed_publications(change: str) -> None:
    owner = snapshot()
    view = owner.view(EncodedStructuralViewV2, require_native_validation=True)
    if change == "buffers":
        values = dict(view.buffers)
        values["root_ids"] = memoryview(bytes(values["root_ids"]))
        changed = replace(view, buffers=MappingProxyType(values))
    elif change == "owner":
        changed = replace(view, owner=snapshot())
    elif change == "scope":
        changed = replace(view, scope=AxiomScope.ROOT)
    elif change == "fingerprint":
        changed = replace(
            view, structural_fingerprint=replace(view.structural_fingerprint, digest=b"0" * 32)
        )
    else:
        changed = replace(view, _native_receipt=object())
    with pytest.raises(BackendProtocolError, match="native validation"):
        validate(changed)


def test_strict_rejects_scalar_owner_before_traversal(monkeypatch: Any) -> None:
    owner = snapshot(BackendPreference.PYTHON)
    monkeypatch.setattr(native_views, "_produce_local_encoded_structural_view_v2", forbidden)
    with pytest.raises(BackendProtocolError, match="native column validation"):
        owner.view(EncodedStructuralViewV2, require_native_validation=True)


def test_changed_budget_is_enforced_natively(monkeypatch: Any) -> None:
    view = snapshot().view(EncodedStructuralViewV2, require_native_validation=True)
    monkeypatch.setattr(native_views, "_validate_columns", forbidden)
    with pytest.raises(ResourceLimitError):
        validate(view, limits=replace(view.owner.load_options.limits, max_axioms=1))


def test_scope_proof_does_not_publish_columns(monkeypatch: Any) -> None:
    owner = snapshot()
    monkeypatch.setattr(native_views, "_produce_native_direct_view_v2", forbidden)
    assert encoded_scopes_equivalent(owner, AxiomScope.ROOT, AxiomScope.CLOSURE)
    assert not encoded_scopes_equivalent(
        snapshot(BackendPreference.PYTHON), AxiomScope.ROOT, AxiomScope.CLOSURE
    )


def test_imported_selections_are_not_aliased(tmp_path: Path) -> None:
    imported = tmp_path / "imported.ofn"
    imported.write_text("Ontology(<urn:imported> Declaration(Class(<urn:C>)))")
    root = tmp_path / "root.ofn"
    root.write_text(
        f"Ontology(<urn:root> Import(<{imported.as_uri()}>) Declaration(Class(<urn:A>)))"
    )
    owner = load_snapshot(
        root,
        options=LoadOptions(backend=BackendPreference.NATIVE),
        resolver=MappingResolver(
            {imported.as_uri(): ResolvedDocument(imported.read_bytes(), IRI(imported.as_uri()))}
        ),
    )
    assert not encoded_scopes_equivalent(owner, AxiomScope.ROOT, AxiomScope.CLOSURE)
    root_view = owner.view(
        EncodedStructuralViewV2, scope=AxiomScope.ROOT, require_native_validation=True
    )
    closure_view = owner.view(EncodedStructuralViewV2, require_native_validation=True)
    assert len(root_view.buffers["root_ids"]) < len(closure_view.buffers["root_ids"])


def test_receipt_owner_cycle_is_collectible() -> None:
    import gc
    import weakref

    owner = snapshot()
    view = owner.view(EncodedStructuralViewV2, require_native_validation=True)
    retained = weakref.ref(view)
    del owner, view
    gc.collect()
    assert retained() is None


def test_binary_probe_rejects_mixed_or_missing_native_capability(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from pyowl_core import native_validation_available
    from pyowl_core.backends import native_validation

    assert native_validation_available()
    monkeypatch.setattr(
        native_validation.importlib, "import_module", lambda _name: SimpleNamespace()
    )
    assert not native_validation_available()


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_name", "foreign"),
        ("schema_version", 2.0),
        ("model_schema", 2.0),
        ("descriptor", b"hostile"),
    ],
)
def test_receipt_preserves_public_descriptor_errors(
    field: str, value: object, strict: bool, monkeypatch: Any
) -> None:
    owner = snapshot()
    view = owner.view(EncodedStructuralViewV2, require_native_validation=True)
    monkeypatch.setattr(native_views, "_validate_columns", forbidden)
    monkeypatch.setattr(type(owner), "iter_axioms", forbidden)
    with pytest.raises(BackendProtocolError) as raised:
        validate_encoded_structural_view_v2(
            replace(view, **{field: value}),
            expected_owner=owner,
            expected_scope=view.scope,
            expected_document_key=view.document_key,
            require_native_validation=strict,
        )
    assert raised.value.code == "ENCODED_VIEW_DESCRIPTOR"
