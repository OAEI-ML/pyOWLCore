from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import pyowl_core.model as m


@st.composite
def _named_class(draw: st.DrawFn) -> m.Class:
    index = draw(st.integers(min_value=0, max_value=100_000))
    return m.Class(m.IRI(f"https://example.org/fuzz#C{index}"))


@st.composite
def _property(draw: st.DrawFn) -> m.ObjectProperty:
    index = draw(st.integers(min_value=0, max_value=100_000))
    return m.ObjectProperty(m.IRI(f"https://example.org/fuzz#p{index}"))


CLASS_EXPRESSIONS = st.recursive(
    _named_class(),
    lambda child: st.one_of(
        child.map(m.ObjectComplementOf),
        st.tuples(_property(), child).map(lambda values: m.ObjectSomeValuesFrom(*values)),
        st.tuples(_property(), child).map(lambda values: m.ObjectAllValuesFrom(*values)),
        st.tuples(st.integers(min_value=0, max_value=20), _property(), child).map(
            lambda values: m.ObjectMinCardinality(*values)
        ),
    ),
    max_leaves=12,
)


@settings(max_examples=96, deadline=None, derandomize=True)
@given(CLASS_EXPRESSIONS)
def test_recursive_model_values_canonical_round_trip(expression: m.ClassExpression) -> None:
    encoded = m.canonical_bytes(expression)
    decoded = m.decode_canonical(encoded)
    assert decoded == expression
    assert m.canonical_bytes(decoded) == encoded
    assert m.structural_digest(decoded) == m.structural_digest(expression)
    assert tuple(m.walk(decoded)) == tuple(m.walk(expression))


@settings(max_examples=64, deadline=None, derandomize=True)
@given(left=CLASS_EXPRESSIONS, right=CLASS_EXPRESSIONS)
def test_generated_axioms_preserve_annotations_and_signature(
    left: m.ClassExpression,
    right: m.ClassExpression,
) -> None:
    annotation = m.Annotation(
        m.AnnotationProperty(m.IRI("https://example.org/fuzz#evidence")),
        m.Literal("generated", m.XSD_STRING),
    )
    axiom = m.SubClassOf(left, right, m.CanonicalSet((annotation,)))
    decoded = m.decode_canonical(m.canonical_bytes(axiom))
    assert decoded == axiom
    assert m.signature(decoded) == m.signature(axiom)
