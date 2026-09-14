# Native named-class features

`owner.view(ClassFeatureView, require_native_pipeline=True)` retains a native,
syntax-only index. Existing asserted hierarchy APIs keep their original semantics.
This additional view supports the structural feature queries used by Exact-OM.

Named `EquivalentClasses` operands form equivalence components. Named `SubClassOf`
endpoints provide graph edges. `equivalent_operands=True` additionally projects
named operands of equivalent intersections (anchor to operand) and unions (operand
to anchor). It does not interpret other complex constructors or disjoint unions.
`direct_parents` removes a candidate reachable from another candidate;
`direct_children` is its inverse. Cycles follow that exact reduction rule and are
not implicitly merged. `ancestors` and `descendants` traverse the reduced graph.
Results are classes in exact IRI order; `component` includes the queried member.

`include_builtins=False` removes owl:Thing/owl:Nothing from query output and from
transitive traversal, while preserving them during direct-edge reduction.
`restrictions(class_)` returns that named subject's superclass expressions followed
by nonnamed equivalent expressions, in canonical axiom/operand order with first
occurrence deduplication. It does not inherit another equivalent anchor's expressions.
Only requested expressions cross the Python model boundary.

The view supports retained native snapshots and imports under ROOT, DOCUMENT and
CLOSURE selection. An overlay may share its base index when its selected delta
contains no subclass/equivalence change; changed overlays and other owner families
raise `BackendProtocolError` before scalar traversal. No scalar fallback exists for
this new view. `ClassFeatureView.supports_native()` probes the installed binary
without parsing an ontology. Existing package default APIs are unchanged.

The owner-local index cache supplies lifetime, cancellation and aggregate admission.
Native construction/query work releases the GIL. Native allocation charges cover
compact keys, graph entries, selected expression IDs and query-local traversal;
they are conservative reservation estimates, not measured RSS. Queries retain no
unbounded per-entity cache. `native_report` separates cold scanned roots/selected
axioms/charged bytes from query count, visited neighbors and published rows.

Validation: independent matrix-based reachability/reduction fixtures (including
cycles), operand projection/restriction order, built-in traversal, imported scopes,
overlay reuse/rejection, tiny budgets, cancellation and owner closure. A warm
single-edge query performs identical counted work with 2 or 500 unrelated edges.
