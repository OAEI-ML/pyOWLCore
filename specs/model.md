# Complete OWL 2 structural model

This specification follows the OWL 2 Structural Specification. The model is
syntax-neutral and complete even when a consumer supports only a profile. The
constructor inventory is machine-audited against the W3C grammar before 1.0.

## 1. General value rules

All public values are immutable, recursively hashable, and safely shareable.
The reference Python form uses frozen slotted dataclasses and immutable
collections. A native-backed implementation must have indistinguishable public
behavior and must not expose its arena IDs.

Every constructor validates local structural invariants (type, arity, nonempty
sets, nonnegative cardinalities) at creation. Whole-ontology restrictions such
as property simplicity and typing are checked separately so incremental model
construction remains possible. There is no “partly initialized” public value.

Unordered W3C associations are duplicate-free and stored/exposed in canonical
order. Ordered associations retain repetitions and order where the standard
permits them. Hashing/equality does not depend on Python hash seed or input
collection iteration.

Canonicalization is syntactic and semantics-preserving. It MUST NOT classify,
rewrite an equivalence into inclusions, simplify a tautology, distribute an
expression, normalize to negation normal form, or apply reasoner rules.

## 2. Primitive values

### 2.1 IRI

```python
@dataclass(frozen=True, slots=True, order=True)
class IRI:
    value: str
```

Public structural IRIs are absolute RFC 3987 IRIs after syntactic prefix/base
resolution. Equality compares exact Unicode scalar sequences. The core does not
case-fold schemes/hosts, percent-decode, Unicode-normalize, follow redirects, or
equate lexical aliases. Those transformations can change identifiers and are
resolver concerns. Ill-formed Unicode, control characters forbidden by the
syntax, and relative IRIs at the structural boundary raise `InvalidIRIError`.

`IRI` is distinct from filesystem paths and URLs. `str(iri)` returns the full
IRI; `repr` is unambiguous; no implicit CURIE expansion occurs outside a
document prefix context.

### 2.2 Entity kinds and punning

```python
class EntityKind(str, Enum):
    CLASS = "class"
    DATATYPE = "datatype"
    OBJECT_PROPERTY = "object_property"
    DATA_PROPERTY = "data_property"
    ANNOTATION_PROPERTY = "annotation_property"
    NAMED_INDIVIDUAL = "named_individual"

@dataclass(frozen=True, slots=True, order=True)
class Entity:
    kind: EntityKind
    iri: IRI
```

Convenience frozen subclasses/type aliases are exported as `Class`, `Datatype`,
`ObjectProperty`, `DataProperty`, `AnnotationProperty`, and `NamedIndividual`.
Entity identity includes kind and IRI. Legal OWL 2 punning therefore yields
distinct entities with the same IRI. Whole-ontology validation diagnoses
illegal use; the model never collapses by IRI alone.

Built-ins are interned constants but compare exactly like constructed values.
Their implicit declarations are exposed by signature policy, not inserted as
source axioms.

### 2.3 Anonymous individuals

```python
@dataclass(frozen=True, slots=True, order=True)
class AnonymousIndividual:
    document_scope: bytes   # 32-byte canonical document scope
    local_key: bytes        # deterministic alpha-canonical key
```

RDF blank labels and functional-syntax node IDs are document-local syntax, not
portable identity. During document freeze, anonymous individuals are
deterministically alpha-canonicalized from the complete structural document;
raw labels live only in `SourceMap`. Distinct documents are standardized apart,
including documents in an import cycle or composition. Renaming source blank
labels does not change document/logical fingerprints.

Model schema 2 partitions the structural blank graph into connected components
before canonical labeling, charges canonical work per component while retaining
document-global term/memory limits, and derives the document scope from a
sorted multiplicity-preserving manifest of complete canonical component graph
bytes. Component-local indices alone are insufficient: repeated isomorphic
components receive distinct canonical occurrence ordinals so distinct
anonymous individuals cannot collapse. The complete algorithm, the narrowly
permitted internal association of indistinguishable source components to
already-fixed slots, and all v2 domains are frozen in
[`large-document-reliability.md`](large-document-reliability.md).

The canonical-label algorithm handles symmetric blank-node structures using
partition refinement plus deterministic tie resolution over complete
structural neighborhoods. Object address, parse order, and hash iteration are
forbidden tie-breakers. A source label may be inspected only inside an exact
component equivalence class after output slots are fixed, where every
association provably yields the same canonical set. It never enters canonical
bytes or identity. Golden adversarial symmetry/multiplicity cases and an
independent implementation verify this rule.

Programmatic anonymous individuals are created through a `DocumentBuilder`
scope, not a process-global/random constructor. Moving one between documents
requires an explicit `re_scope` operation that records provenance.

### 2.4 Literals

```python
@dataclass(frozen=True, slots=True, order=True)
class Literal:
    lexical_form: str
    datatype: Datatype
    language: str | None = None
```

The lexical form is preserved exactly; the model does not parse a Python value
or canonicalize datatype lexical space. Language tags are validated as BCP 47
where applicable and ASCII-lowercased for equality, hashing, canonical wire,
and fingerprints because tag matching is case-insensitive. A source map may
retain the exact original tag token. This resolves one shared identity rule:

- pyHermiT consumes canonical lowercase identity directly;
- a pinned pyELK compatibility adapter may derive an `ElkCompatibilityKey`
  using preserved source spelling where its observable reference behavior
  requires it; and
- no consumer defines a divergent shared `Literal` class.

The OWL 2 model representation uses `rdf:PlainLiteral`, as required by the OWL
2 datatype map. For that datatype, `lexical_form` is the string component and
`language` is the optional tag component; canonical functional encoding is the
corresponding `"string@tag"^^rdf:PlainLiteral` (or `"string@"` without a tag).
RDF 1.1 `rdf:langString` input is mapped at the RDF/OWL exchange boundary to
this OWL 2 representation and rendered appropriately for the target RDF
version. A language value and an incompatible datatype are rejected. An
untagged Functional-Style plain string uses `rdf:PlainLiteral`; an explicitly
typed `xsd:string` remains distinct.

Ill-typed datatype lexical forms remain representable because syntax parsing is
not datatype satisfiability. `validate_lexical_form` reports them under a chosen
datatype-map policy; a reasoner decides whether that is profile-fatal.

## 3. Annotations

```python
AnnotationSubject = IRI | AnonymousIndividual
AnnotationValue = IRI | Literal | AnonymousIndividual

@dataclass(frozen=True, slots=True)
class Annotation:
    property: AnnotationProperty
    value: AnnotationValue
    annotations: frozenset[Annotation] = frozenset()
```

Nested annotation annotations are retained recursively. Cyclic Python object
graphs cannot be constructed. Annotation sets are unordered/duplicate-free.
Ontology annotations and axiom annotations use the same value.

Annotations do not affect Direct Semantics, but they do affect complete
structural/document fingerprints and exact render round trips. They do not
affect `logical_fingerprint`.

## 4. Property expressions

```text
ObjectPropertyExpression =
    ObjectProperty
  | ObjectInverseOf(property: ObjectProperty)

DataPropertyExpression = DataProperty

SubObjectPropertyExpression =
    ObjectPropertyExpression
  | ObjectPropertyChain(properties: tuple[ObjectPropertyExpression, ...])
```

An object property chain has at least two members and preserves order. The
canonical model prevents nested `ObjectInverseOf`; inverse of an inverse is
represented by its named property as required by canonical parsing. It does not
otherwise rewrite inverse axioms.

## 5. Data ranges

`DataRange` is the closed union:

```text
Datatype
DataIntersectionOf(operands: canonical frozenset[DataRange])        # >= 2
DataUnionOf(operands: canonical frozenset[DataRange])               # >= 2
DataComplementOf(operand: DataRange)
DataOneOf(values: canonical frozenset[Literal])                      # >= 1
DatatypeRestriction(
    datatype: Datatype,
    restrictions: canonical frozenset[FacetRestriction],            # >= 1
)
FacetRestriction(facet: IRI, value: Literal)
```

Facet IRIs are retained even when an active datatype map does not recognize
them; profile/datatype validation diagnoses invalid combinations. Constructor
flattening and duplicate elimination follows W3C canonical parsing only where
the Recommendation requires it; arbitrary algebraic simplification is absent.

## 6. Class expressions

`ClassExpression` is the closed union below. Qualified cardinality fillers are
always explicit in the model: syntactic unqualified object cardinalities use
`owl:Thing`, and unqualified data cardinalities use `rdfs:Literal`.

```text
Class

ObjectIntersectionOf(operands: canonical frozenset[ClassExpression]) # >= 2
ObjectUnionOf(operands: canonical frozenset[ClassExpression])        # >= 2
ObjectComplementOf(operand: ClassExpression)
ObjectOneOf(individuals: canonical frozenset[Individual])            # >= 1
ObjectSomeValuesFrom(property: ObjectPropertyExpression,
                     filler: ClassExpression)
ObjectAllValuesFrom(property: ObjectPropertyExpression,
                    filler: ClassExpression)
ObjectHasValue(property: ObjectPropertyExpression, value: Individual)
ObjectHasSelf(property: ObjectPropertyExpression)
ObjectMinCardinality(cardinality: int,
                     property: ObjectPropertyExpression,
                     filler: ClassExpression)
ObjectMaxCardinality(cardinality: int,
                     property: ObjectPropertyExpression,
                     filler: ClassExpression)
ObjectExactCardinality(cardinality: int,
                       property: ObjectPropertyExpression,
                       filler: ClassExpression)

DataSomeValuesFrom(properties: tuple[DataProperty, ...],
                   filler: DataRange)
DataAllValuesFrom(properties: tuple[DataProperty, ...],
                  filler: DataRange)
DataHasValue(property: DataProperty, value: Literal)
DataMinCardinality(cardinality: int,
                   property: DataProperty,
                   filler: DataRange)
DataMaxCardinality(cardinality: int,
                   property: DataProperty,
                   filler: DataRange)
DataExactCardinality(cardinality: int,
                     property: DataProperty,
                     filler: DataRange)
```

`Individual = NamedIndividual | AnonymousIndividual`. Multi-property
`DataSomeValuesFrom`/`DataAllValuesFrom` property tuples are nonempty and
ordered because positions participate in the n-ary data range. Cardinalities
are arbitrary nonnegative integers in the model and use checked arbitrary-
precision Python semantics; a backend may reject a value exceeding configured
resource limits before narrowing to an internal integer.

Intersections/unions/one-of operands are unordered and duplicate-free. W3C
canonical parsing flattening is applied consistently. The original grouping,
operand order, prefixes, and redundant duplicates may be retained as source
provenance but are not structural identity.

## 7. Axiom foundation

Every axiom has `annotations: frozenset[Annotation]`. The annotations are not a
wrapper node and cannot be discarded by equality. All arity constraints below
are enforced locally. The model exports a closed OWL 2 `Axiom` union and
category unions (`LogicalAxiom`, `DeclarationAxiom`, `AnnotationAxiom`). Rules
are separately namespaced extension components, not OWL 2 axioms.

### 7.1 Declarations

```text
Declaration(entity: Entity)
```

Declarations are nonlogical axioms. Repeated declarations collapse
structurally but all source occurrences remain in provenance.

### 7.2 Class axioms

```text
SubClassOf(sub_class: ClassExpression, super_class: ClassExpression)
EquivalentClasses(expressions: canonical frozenset[ClassExpression]) # >= 2
DisjointClasses(expressions: canonical frozenset[ClassExpression])   # >= 2
DisjointUnion(defined_class: Class,
              expressions: canonical frozenset[ClassExpression])    # >= 2
```

`EquivalentClasses` and `DisjointClasses` are symmetric sets. `SubClassOf` and
`DisjointUnion` roles are not reordered.

### 7.3 Object-property axioms

```text
SubObjectPropertyOf(sub_property: SubObjectPropertyExpression,
                    super_property: ObjectPropertyExpression)
EquivalentObjectProperties(properties: canonical frozenset[ObjectPropertyExpression]) # >= 2
DisjointObjectProperties(properties: canonical frozenset[ObjectPropertyExpression])    # >= 2
InverseObjectProperties(first: ObjectPropertyExpression,
                        second: ObjectPropertyExpression)              # symmetric pair
ObjectPropertyDomain(property: ObjectPropertyExpression,
                     domain: ClassExpression)
ObjectPropertyRange(property: ObjectPropertyExpression,
                    range: ClassExpression)
FunctionalObjectProperty(property: ObjectPropertyExpression)
InverseFunctionalObjectProperty(property: ObjectPropertyExpression)
ReflexiveObjectProperty(property: ObjectPropertyExpression)
IrreflexiveObjectProperty(property: ObjectPropertyExpression)
SymmetricObjectProperty(property: ObjectPropertyExpression)
AsymmetricObjectProperty(property: ObjectPropertyExpression)
TransitiveObjectProperty(property: ObjectPropertyExpression)
```

The inverse pair is canonicalized as an unordered pair. A subproperty chain
remains ordered. Regularity and simplicity restrictions are whole-ontology
profile checks, not constructor rewrites.

### 7.4 Data-property axioms

```text
SubDataPropertyOf(sub_property: DataProperty, super_property: DataProperty)
EquivalentDataProperties(properties: canonical frozenset[DataProperty]) # >= 2
DisjointDataProperties(properties: canonical frozenset[DataProperty])   # >= 2
DataPropertyDomain(property: DataProperty, domain: ClassExpression)
DataPropertyRange(property: DataProperty, range: DataRange)
FunctionalDataProperty(property: DataProperty)
```

### 7.5 Datatype definitions and keys

```text
DatatypeDefinition(datatype: Datatype, data_range: DataRange)
HasKey(
    class_expression: ClassExpression,
    object_properties: canonical frozenset[ObjectPropertyExpression],
    data_properties: canonical frozenset[DataProperty],
)
```

At least one key property is required. Datatype-definition dependency/global
restrictions and key restrictions are profile-validation concerns.

### 7.6 Assertions

```text
SameIndividual(individuals: canonical frozenset[Individual])          # >= 2
DifferentIndividuals(individuals: canonical frozenset[Individual])     # >= 2
ClassAssertion(class_expression: ClassExpression,
               individual: Individual)
ObjectPropertyAssertion(property: ObjectPropertyExpression,
                        source: Individual, target: Individual)
NegativeObjectPropertyAssertion(property: ObjectPropertyExpression,
                                source: Individual, target: Individual)
DataPropertyAssertion(property: DataProperty,
                      source: Individual, value: Literal)
NegativeDataPropertyAssertion(property: DataProperty,
                              source: Individual, value: Literal)
```

Assertion argument order is significant except in explicit same/different sets.
Inverse property assertions are preserved as written structurally; renderers
may use the syntax's normative mapping without changing model identity.

### 7.7 Annotation axioms

```text
AnnotationAssertion(property: AnnotationProperty,
                    subject: AnnotationSubject,
                    value: AnnotationValue)
SubAnnotationPropertyOf(sub_property: AnnotationProperty,
                        super_property: AnnotationProperty)
AnnotationPropertyDomain(property: AnnotationProperty, domain: IRI)
AnnotationPropertyRange(property: AnnotationProperty, range: IRI)
```

Each also carries axiom annotations. Annotation-property domain/range targets
are IRIs, not class/data expressions.

## 8. Optional SWRL/DL-safe-rule extension

DL-safe/SWRL rules are not constructors in the normative OWL 2 structural axiom
grammar. Because OWLAPI/Horned-OWL ecosystems and biomedical ontologies may
carry them, the shared kernel defines an optional, explicitly namespaced
`pyowl_core.extensions.swrl` structural extension rather than forcing rules
through RDF triples or falsely labeling them as OWL 2 axioms.

```text
Variable(iri: IRI)
IndividualArgument = Individual | Variable
DataArgument = Literal | Variable

ClassAtom(predicate: ClassExpression, argument: IndividualArgument)
DataRangeAtom(predicate: DataRange, argument: DataArgument)
ObjectPropertyAtom(predicate: ObjectPropertyExpression,
                   source: IndividualArgument, target: IndividualArgument)
DataPropertyAtom(predicate: DataProperty,
                 source: IndividualArgument, target: DataArgument)
BuiltInAtom(predicate: IRI, arguments: tuple[DataArgument, ...])
SameIndividualAtom(first: IndividualArgument, second: IndividualArgument)
DifferentIndividualsAtom(first: IndividualArgument, second: IndividualArgument)

SWRLRule(
    body: canonical frozenset[Atom],
    head: canonical frozenset[Atom],
)
```

Body/head constraints follow the pinned SWRL/DL-safe extension specification,
not the OWL 2 constructor ledger. Variable safety, built-in support, and a
reasoner's rule capability are validator/compiler diagnostics. Extension
components and annotations participate in complete structural identity and are
domain-tagged in logical fingerprints so adding a rule invalidates consumer
caches; OWL-2-only consumers reject the advertised extension capability.

## 9. Ontology structure

An `OntologyDocument` contains:

```text
OntologyID(ontology_iri?, version_iri?)
document_iri?
direct_imports: canonical tuple/set of IRI
ontology_annotations: canonical frozenset[Annotation]
axioms: canonical frozenset[Axiom]
extension_components: canonical frozenset[ExtensionComponent]
provenance/source map/prefix map (nonstructural acquisition metadata)
```

Ontology IRI and version IRI constraints follow W3C section 3. Duplicate imports
collapse while origin occurrences remain available. Prefix declarations are a
document-syntax convenience and are not part of structural equivalence; a
writer accepts an explicit prefix policy. Source comments/whitespace are source
map trivia and need not round-trip.

Imports are not axioms. A document records only direct import declarations; a
snapshot retains the resolved closure and manifest separately.

## 10. Canonical structural encoding

Each value has one language-neutral canonical encoding used by sorting,
fingerprints, golden fixtures, and the wire term/axiom tables:

1. fixed constructor tag from the model schema (never Python class name);
2. fixed field order;
3. unsigned varint lengths with minimal encoding;
4. UTF-8 strings without Unicode normalization;
5. canonical language tags and explicit expanded IRIs;
6. ordered sequence members in order;
7. unordered members sorted by their complete canonical bytes, after duplicate
   elimination;
8. axiom/nested annotations encoded as unordered sets;
9. arbitrary integers as minimal unsigned magnitude; and
10. document-scoped alpha-canonical anonymous keys.

Constructor tags are committed in a generated schema ledger and never reused.
Adding a constructor requires a model schema decision. Canonical bytes are not
the entire wire file; the wire may intern values while producing identical
logical canonical content.

`hash(value)` need only satisfy Python equality during a process and is never
persisted. A stable per-value `structural_digest(value)` is SHA-256 over a
domain separator, model schema, and canonical bytes.

## 11. Fingerprint domains

All hashes use SHA-256 and explicit ASCII domain separators:

- `source_sha256`: exact acquired bytes; for `TextIO`, UTF-8 of returned code
  points with provenance marking synthetic normalized text input;
- `document_fingerprint`: ontology ID, direct imports, ontology annotations,
  and all annotated axioms after blank-node alpha-canonicalization; excludes
  format, prefix choices, source order, comments, path, and timestamps;
- `structural_fingerprint`: full view structure, document/member boundaries,
  import/composition manifest and policy outcomes, annotations, declarations,
  extension components, and axioms; excludes acquisition location/timing;
- `logical_fingerprint`: set of logical axioms plus domain-tagged logical
  extension components across the effective view,
  document-scoped anonymous identity, active datatype semantic policy, and
  model schema; excludes declarations, annotation axioms, axiom/ontology
  annotations, prefixes, document IRIs/import traversal, and provenance;
- `signature_fingerprint`: sorted `(EntityKind, IRI)` effective signature,
  built-in inclusion policy, and model schema.

The snapshot structural fingerprint therefore states what closure/policy was
observed even if two policies happen to yield equal logical axioms. Consumer
reasoning caches normally use `logical_fingerprint` plus
`signature_fingerprint`, profile/datatype policy, and compiler schema. A
projector that emits annotations uses `structural_fingerprint`.

Hashes are recomputed independently in Python/native tests. Digest fields are
never trusted merely because they occur in a cache header.

## 12. Validation boundaries

Three reports are distinct:

1. `StructuralReport`: local W3C arity/type/canonical-parsing validity;
2. `OWL2DLReport`: entity typing, reserved vocabulary, property regularity and
   simplicity, datatype definitions, keys, anonymous-individual restrictions,
   and all global restrictions; and
3. `ProfileReport(profile=EL|QL|RL)`: grammar and global restrictions from OWL
   2 Profiles.

Parsing can return a structurally representable OWL Full RDF graph only through
an explicit partial-mapping report; it must not call arbitrary leftover RDF an
OWL 2 DL document. Required RDF-to-structure mapping failures include precise
unconsumed triples and rule IDs. pyELK evaluates EL profile/compiler support;
pyHermiT requires a passing OWL 2 DL report. The core does not infer profile
membership from file extensions or declarations.

## 13. Factories, visitors, and matching

Public constructors are available directly and through an optional
`OWLFactory` that interns common IRIs/entities/expressions. Factory use never
changes identity. No global mutable factory is required.

The model exports exhaustive visitors/pattern matching with an `UnknownNode`
failure on a future required constructor. A visitor must opt into a default
handler; silently skipping a new axiom kind is forbidden. Generated exhaustive
type tests ensure every constructor is handled by canonical encoding, signature
walking, reference indexing, all required writers, and wire decoding.

## 14. Acceptance matrix

Before model schema 1 is declared stable:

- every constructor above has valid/invalid unit fixtures, equality/hash tests,
  annotations, visitors, canonical bytes, and round trips;
- unordered permutation/duplicate metamorphic tests and ordered-chain tests
  pass in both backends;
- blank-node alpha-renaming, symmetric-graph, cross-document scoping, and import
  cycle cases pass;
- language-tag case identity and source-spelling provenance pass, including the
  pyELK compatibility adapter and pyHermiT canonical consumer;
- arbitrary/deep cardinality limits fail safely without integer narrowing;
- a generated coverage table maps every W3C production to a constructor; and
- no reasoner- or projector-specific class appears in the public model.
