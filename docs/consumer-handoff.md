# Parse once and hand off to consumers

The complete runnable example is [`examples/parse_once.py`](examples/parse_once.py).
It proves one acquisition, provider identity, overlay/base retention,
source/target composition, and validated wire transfer without importing Java.

## Standalone and Exact-OM provider

Load once at the application boundary. Exact-OM stores that view and implements
`owl_snapshot()` by returning the exact object. Any downstream call to
`coerce_snapshot(provider)` retains the same identity and performs no parse,
resolver, wire, mmap, or path operation.

## pyELK and pyHermiT

Pass the same view to the reasoner constructors. pyELK compiles an EL-specific
IR; pyHermiT compiles its OWL 2 DL/tableau IR. Neither receives an ontology path
from another in-process component. Reasoner completeness and profile choice
remain explicit consumer decisions.

```python
with pyelk.Reasoner(shared_view) as elk:
    # ELK makes incompleteness explicit; reject a partial answer here.
    elk_consistent = elk.is_consistent().require_complete()

with pyhermit.Reasoner(shared_view) as hermit:
    hermit_consistent = hermit.is_consistent()
```

## OWL2Vec* projector

Pass the same view or repair overlay to the projector. Projection options and
edge buffers remain projector-owned; core does not cache or expose graph edges.

```python
projector = pyowl2vec_star_projector.Projector()
edges = projector.project(shared_view)
```

## OAEI evaluation

Compose the source and target views and vary only the bridge delta. Select
pyELK only for its complete supported EL scope; otherwise select pyHermiT or
raise the configured policy error. Micro versus macro metric averaging is an
evaluator policy and is not encoded in the ontology view.

```python
trial = compose_views(source, target, delta=bridge, roles=("source", "target"))

if use_elk:
    with pyelk.Reasoner(trial) as reasoner:
        coherent = reasoner.is_consistent().require_complete()
else:
    with pyhermit.Reasoner(trial) as reasoner:
        coherent = reasoner.is_consistent()
```

The evaluator must choose `use_elk` only when its profile policy establishes
that pyELK is complete for the requested task. Calling `require_complete()` is
intentional: pyELK returns a `ReasoningResult[bool]`, whereas pyHermiT returns a
`bool` for the same operation.

Cross-process workers receive authenticated core wire bytes or a mapped core
wire file—not original paths, pickle, ROBOT, DeepOnto, or OWLAPI objects.
