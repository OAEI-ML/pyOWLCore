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

```text
elk = pyelk.Reasoner(shared_view)
hermit = pyhermit.Reasoner(shared_view)
```

## OWL2Vec* projector

Pass the same view or repair overlay to the projector. Projection options and
edge buffers remain projector-owned; core does not cache or expose graph edges.

```text
edges = pyowl2vec_star_projector.project(shared_view)
```

## OAEI evaluation

Compose the source and target views and vary only the bridge delta. Select
pyELK only for its complete supported EL scope; otherwise select pyHermiT or
raise the configured policy error. Micro versus macro metric averaging is an
evaluator policy and is not encoded in the ontology view.

```text
trial = compose_views(source, target, delta=bridge, roles=("source", "target"))
coherent = selected_reasoner(trial).check_consistency()
```

Cross-process workers receive authenticated core wire bytes or a mapped core
wire file—not original paths, pickle, ROBOT, DeepOnto, or OWLAPI objects.

