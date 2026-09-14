from __future__ import annotations

import random

import pytest

import pyowl_core as core
from tests.native.foundation._support import load_extension


@pytest.fixture(scope="module", autouse=True)
def extension():
    assert load_extension().NATIVE_CLASS_FEATURES_API_VERSION == 1


def named(value):
    return core.Class(core.IRI("urn:" + value))


def load(rows, **options):
    return core.load_snapshot(
        ("Ontology(<urn:features> " + " ".join(rows) + ")").encode(),
        options=core.LoadOptions(backend=core.BackendPreference.NATIVE, **options),
    )


def iris(values):
    return tuple(value.iri.value for value in values)


def closure(edges, size):
    result = [[(i, j) in edges for j in range(size)] for i in range(size)]
    for middle in range(size):
        for left in range(size):
            for right in range(size):
                result[left][right] |= result[left][middle] and result[middle][right]
    return result


@pytest.mark.parametrize("seed", range(8))
def test_graph_reduction_matches_independent_reachability_matrix(seed, monkeypatch):
    rng = random.Random(seed)
    size = 9
    edges = {(i, j) for i in range(size) for j in range(size) if rng.random() < 0.16}
    owner = load([f"SubClassOf(<urn:{i}> <urn:{j}>)" for i, j in sorted(edges)])
    edges = {(i, j) for i, j in edges if i != j}
    reach = closure(edges, size)
    direct = {
        (i, j)
        for i, j in edges
        if not any(k != j and (i, k) in edges and reach[k][j] for k in range(size))
    }
    transitive = closure(direct, size)

    def forbidden(*args, **kwargs):
        raise AssertionError("native class features requested Python ontology traversal")

    monkeypatch.setattr(type(owner), "iter_axioms", forbidden)
    view = owner.view(core.ClassFeatureView, require_native_pipeline=True)
    assert owner.view(core.ClassFeatureView, require_native_pipeline=True) is view
    for i in range(size):
        value = named(str(i))
        assert iris(view.direct_parents(value)) == tuple(
            f"urn:{j}" for j in range(size) if (i, j) in direct
        )
        assert iris(view.direct_children(value)) == tuple(
            f"urn:{j}" for j in range(size) if (j, i) in direct
        )
        assert iris(view.ancestors(value)) == tuple(
            f"urn:{j}" for j in range(size) if transitive[i][j]
        )
        assert iris(view.descendants(value)) == tuple(
            f"urn:{j}" for j in range(size) if transitive[j][i]
        )


def test_equivalent_operand_policy_and_restriction_order():
    owner = load(
        [
            "EquivalentClasses(<urn:A> <urn:Alias> ObjectIntersectionOf(<urn:B> <urn:C>))",
            "EquivalentClasses(<urn:Alias> <urn:Alias2>)",
            "EquivalentClasses(<urn:Union> ObjectUnionOf(<urn:B> <urn:C>))",
            "SubClassOf(<urn:A> ObjectSomeValuesFrom(<urn:p> <urn:LongName>))",
            "SubClassOf(<urn:A> <urn:ZZ>)",
            'SubClassOf(Annotation(<urn:note> "same expression") <urn:A> <urn:ZZ>)',
            "SubClassOf(<urn:ZZ> <urn:B>)",
        ]
    )
    plain = owner.view(core.ClassFeatureView)
    selected = owner.view(core.ClassFeatureView, equivalent_operands=True)
    assert iris(plain.direct_parents(named("A"))) == ("urn:ZZ",)
    assert iris(selected.direct_parents(named("A"))) == ("urn:C", "urn:ZZ")
    assert iris(selected.direct_children(named("Union"))) == ("urn:B", "urn:C")
    assert iris(selected.component(named("A"))) == ("urn:A", "urn:Alias", "urn:Alias2")
    assert selected.component(named("Absent")) == (named("Absent"),)
    expected = []
    for axiom in owner.iter_axioms(core.SubClassOf):
        if axiom.sub_class == named("A"):
            expected.append(axiom.super_class)
    for axiom in owner.iter_axioms(core.EquivalentClasses):
        if named("A") in axiom.expressions:
            expected.extend(
                value for value in axiom.expressions if not isinstance(value, core.Class)
            )
    assert tuple(selected.restrictions(named("A"))) == tuple(dict.fromkeys(expected))
    # Restrictions belong to the requested named anchor, not all equivalent aliases.
    assert tuple(selected.restrictions(named("Alias2"))) == ()


def test_bounds_filter_controls_traversal_not_just_final_publication():
    owner = load(
        [
            "SubClassOf(<urn:A> <http://www.w3.org/2002/07/owl#Thing>)",
            "SubClassOf(<http://www.w3.org/2002/07/owl#Thing> <urn:B>)",
        ]
    )
    assert iris(owner.view(core.ClassFeatureView).ancestors(named("A"))) == (
        "http://www.w3.org/2002/07/owl#Thing",
        "urn:B",
    )
    assert owner.view(core.ClassFeatureView, include_builtins=False).ancestors(named("A")) == ()


def test_warm_query_work_is_independent_of_unrelated_rows():
    results = []
    for unrelated in (2, 500):
        owner = load(
            ["SubClassOf(<urn:A> <urn:B>)"]
            + [f"SubClassOf(<urn:U{i}> <urn:V{i}>)" for i in range(unrelated)]
        )
        view = owner.view(core.ClassFeatureView, require_native_pipeline=True)
        before = dict(view.native_report)
        for _ in range(5):
            assert iris(view.direct_parents(named("A"))) == ("urn:B",)
        after = dict(view.native_report)
        results.append(
            (
                after["neighbor_visits"] - before["neighbor_visits"],
                after["published_rows"] - before["published_rows"],
            )
        )
    assert results == [(10, 5), (10, 5)]


def test_native_admission_limits_cancellation_and_closed_owner():
    python = core.load_snapshot(
        b"Ontology()", options=core.LoadOptions(backend=core.BackendPreference.PYTHON)
    )
    with pytest.raises(core.BackendProtocolError, match="native class features"):
        python.view(core.ClassFeatureView, require_native_pipeline=True)
    owner = load(["SubClassOf(<urn:A> <urn:B>)"])
    cancel = core.CancellationSource()
    cancel.cancel()
    with pytest.raises(core.OperationCancelledError):
        owner.view(core.ClassFeatureView, cancellation_token=cancel.token)
    view = owner.view(core.ClassFeatureView)
    with pytest.raises(core.OperationCancelledError):
        view.direct_parents(named("A"), cancellation_token=cancel.token)
    owner.close()
    with pytest.raises(core.ClosedSnapshotError):
        view.direct_parents(named("A"))


@pytest.mark.parametrize(
    "scope", [core.AxiomScope.CLOSURE, core.AxiomScope.ROOT, core.AxiomScope.DOCUMENT]
)
def test_import_scope_and_annotation_only_overlay_reuse(scope):
    owner = core.load_snapshot(
        b"Ontology(<urn:root> Import(<urn:imported>) SubClassOf(<urn:A> <urn:B>))",
        document_iri="urn:root",
        options=core.LoadOptions(backend=core.BackendPreference.NATIVE),
        resolver=core.MappingResolver(
            {"urn:imported": b"Ontology(<urn:imported> SubClassOf(<urn:B> <urn:C>))"}
        ),
    )
    options = {"scope": scope, "require_native_pipeline": True}
    if scope is core.AxiomScope.DOCUMENT:
        options["document_key"] = next(
            record.document_key
            for record in owner.import_manifest.documents
            if record.document_key != owner.root_document_key
        )
    view = owner.view(core.ClassFeatureView, **options)
    expected = (
        ("urn:B", "urn:C")
        if scope is core.AxiomScope.CLOSURE
        else (("urn:B",) if scope is core.AxiomScope.ROOT else ())
    )
    assert iris(view.ancestors(named("A"))) == expected
    overlay = core.apply_delta(
        owner, core.OntologyDelta(add_axioms=(core.Declaration(named("Unrelated")),))
    )
    reused = overlay.view(core.ClassFeatureView, **options)
    assert iris(reused.ancestors(named("A"))) == expected
    assert reused._ontology is overlay
    assert reused.report.shared_bytes > 0
    changed = core.apply_delta(
        owner, core.OntologyDelta(add_axioms=(core.SubClassOf(named("A"), named("C")),))
    )
    if scope is core.AxiomScope.CLOSURE:
        with pytest.raises(core.BackendProtocolError, match="overlay changes"):
            changed.view(core.ClassFeatureView, **options)
    else:
        assert (
            iris(changed.view(core.ClassFeatureView, **options).ancestors(named("A"))) == expected
        )


@pytest.mark.parametrize(
    "limits", [core.ParseLimits(max_index_rows=1), core.ParseLimits(max_index_bytes=512)]
)
def test_native_index_rejects_tiny_budget_before_result_publication(limits):
    from pyowl_core.backends.native_validation import _native_owner
    from pyowl_core.backends.native_views import _invoke_native_column_operation_v2

    owner = load(["SubClassOf(<urn:A> <urn:B>)", "SubClassOf(<urn:B> <urn:C>)"])
    raw, extension = _native_owner(owner)
    with pytest.raises(core.ResourceLimitError):
        _invoke_native_column_operation_v2(
            extension,
            extension._class_features_v1,
            (raw, "closure", None, True, False),
            limits,
            None,
        )
