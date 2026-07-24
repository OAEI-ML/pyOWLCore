//! Production Rust-to-Rust handoff from mapped documents to retained V2 roots.

use std::mem::size_of;
use std::time::Instant;

use crate::cancel::{Cancellation, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::Limits;
use crate::publication::{TypedFacadeBuilderV2, TypedFacadeStorageV2};

use super::CanonicalDocument;

pub(super) struct PublishedV2 {
    pub(super) storage: TypedFacadeStorageV2,
    pub(super) arena_construction_ns: u64,
    pub(super) freeze_ns: u64,
}

pub(super) fn publish(
    documents: &[CanonicalDocument],
    effective_documents: &[Vec<u64>],
    closure_documents: &[u64],
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<TypedFacadeStorageV2> {
    Ok(publish_timed(
        documents,
        effective_documents,
        closure_documents,
        limits,
        cancellation,
        interrupt,
        caller_external_bytes,
    )?
    .storage)
}

pub(super) fn publish_timed(
    documents: &[CanonicalDocument],
    effective_documents: &[Vec<u64>],
    closure_documents: &[u64],
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<PublishedV2> {
    publish_timed_inner(
        documents,
        None,
        effective_documents,
        closure_documents,
        limits,
        cancellation,
        interrupt,
        caller_external_bytes,
    )
}

#[allow(clippy::too_many_arguments)]
pub(super) fn publish_scoped_timed(
    documents: &[CanonicalDocument],
    effective_roots: &[[Vec<Vec<u8>>; 3]],
    effective_documents: &[Vec<u64>],
    closure_documents: &[u64],
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<PublishedV2> {
    publish_timed_inner(
        documents,
        Some(effective_roots),
        effective_documents,
        closure_documents,
        limits,
        cancellation,
        interrupt,
        caller_external_bytes,
    )
}

#[allow(clippy::too_many_arguments)]
fn publish_timed_inner(
    documents: &[CanonicalDocument],
    scoped_effective_roots: Option<&[[Vec<Vec<u8>>; 3]]>,
    effective_documents: &[Vec<u64>],
    closure_documents: &[u64],
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    caller_external_bytes: usize,
) -> NativeResult<PublishedV2> {
    if scoped_effective_roots.is_some_and(|rows| rows.len() != documents.len()) {
        return Err(NativeError::protocol(
            "native scoped document count diverges from mapped documents",
        ));
    }
    let mapped_document_bytes = mapped_document_bytes(documents)?
        .checked_add(
            scoped_effective_roots
                .map(|documents| {
                    documents
                        .iter()
                        .try_fold(0_usize, |total, rows| checked_add(total, rows_bytes(rows)?))
                })
                .transpose()?
                .unwrap_or(0),
        )
        .ok_or_else(|| NativeError::limit("native V2 scoped memory accounting overflow"))?;
    let external_bytes = caller_external_bytes
        .checked_add(mapped_document_bytes)
        .ok_or_else(|| NativeError::limit("native V2 ingestion memory accounting overflow"))?;
    let arena_started = Instant::now();
    let mut builder = TypedFacadeBuilderV2::new(limits, cancellation, interrupt, external_bytes)?;
    for (index, document) in documents.iter().enumerate() {
        if let Some(effective) = scoped_effective_roots.and_then(|rows| rows.get(index)) {
            builder.add_scoped_document(
                &document.ontology_annotations,
                &document.axioms,
                &document.extensions,
                &effective[0],
                &effective[1],
                &effective[2],
            )?;
            continue;
        }
        builder.add_document(
            &document.ontology_annotations,
            &document.axioms,
            &document.extensions,
        )?;
    }
    let arena_construction_ns = elapsed_ns(arena_started)?;
    let freeze_started = Instant::now();
    let storage = builder.freeze(effective_documents, closure_documents)?;
    let freeze_ns = elapsed_ns(freeze_started)?;
    Ok(PublishedV2 {
        storage,
        arena_construction_ns,
        freeze_ns,
    })
}

fn rows_bytes(rows: &[Vec<Vec<u8>>; 3]) -> NativeResult<usize> {
    rows.iter().try_fold(0_usize, |total, values| {
        let metadata = values
            .capacity()
            .checked_mul(size_of::<Vec<u8>>())
            .ok_or_else(|| NativeError::limit("native V2 scoped row metadata overflow"))?;
        values.iter().try_fold(
            total
                .checked_add(metadata)
                .ok_or_else(|| NativeError::limit("native V2 scoped row size overflow"))?,
            |subtotal, row| checked_add(subtotal, row.capacity()),
        )
    })
}

fn elapsed_ns(started: Instant) -> NativeResult<u64> {
    u64::try_from(started.elapsed().as_nanos())
        .map_err(|_| NativeError::limit("native RDF/XML phase time exceeds u64"))
}

fn mapped_document_bytes(documents: &[CanonicalDocument]) -> NativeResult<usize> {
    let mut total = documents
        .len()
        .checked_mul(size_of::<CanonicalDocument>())
        .ok_or_else(|| NativeError::limit("native V2 document metadata size overflow"))?;
    for document in documents {
        for value in [
            document.document_iri.as_ref(),
            document.ontology_iri.as_ref(),
            document.version_iri.as_ref(),
        ]
        .into_iter()
        .flatten()
        {
            total = checked_add(total, value.capacity())?;
        }
        total = checked_add(
            total,
            document
                .imports
                .capacity()
                .checked_mul(size_of::<String>())
                .ok_or_else(|| NativeError::limit("native V2 import metadata size overflow"))?,
        )?;
        for value in &document.imports {
            total = checked_add(total, value.capacity())?;
        }
        for rows in [
            &document.ontology_annotations,
            &document.axioms,
            &document.extensions,
        ] {
            total = checked_add(
                total,
                rows.capacity()
                    .checked_mul(size_of::<Vec<u8>>())
                    .ok_or_else(|| {
                        NativeError::limit("native V2 canonical row metadata size overflow")
                    })?,
            )?;
            for row in rows {
                total = checked_add(total, row.capacity())?;
            }
        }
    }
    Ok(total)
}

fn checked_add(left: usize, right: usize) -> NativeResult<usize> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("native V2 document memory accounting overflow"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::Guard;
    use crate::canonical::{entity, iri, Field, Node};
    use crate::publication::{
        TypedFacadeCollectionV2, TypedFacadeCoordinateV2, TypedFacadePageRequestV2,
    };
    use crate::session::Session;

    #[test]
    fn mapped_document_crosses_the_typed_v2_seam_without_a_retained_row_copy() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Ontology rdf:about="urn:o"/>
          <owl:Class rdf:about="urn:C"/>
        </rdf:RDF>"#;
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("session");
        let document = super::super::parse_rdfxml(source, Some("urn:document"), true, &mut session)
            .expect("mapped document");
        session.finish().expect("parse finish");
        let expected = document.axioms.clone();
        let storage = publish(
            std::slice::from_ref(&document),
            &[vec![0]],
            &[0],
            limits,
            Cancellation::with_duration(None),
            None,
            source.len(),
        )
        .expect("typed storage");
        drop(document);

        let page = storage
            .page(
                TypedFacadePageRequestV2::new(
                    TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                    false,
                    0,
                    64,
                    8 * 1024 * 1024,
                ),
                Cancellation::with_duration(None),
                None,
            )
            .expect("typed page");
        assert_eq!(page.rows, expected);
        let observation = storage.observation_for_tests().expect("observation");
        assert_eq!(observation.arena_fields, 1);
        assert_eq!(observation.retained_canonical_byte_rows, 0);
        let counters = storage.counters().expect("counters");
        assert_eq!(counters.canonical_input_rows, 1);
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(counters.publication_structural_bytes_copied, 0);
    }

    fn anonymous_assertion(scope: u8, class: &str) -> Vec<u8> {
        let individual = Node::build(
            3,
            vec![Field::Bytes(vec![scope; 32]), Field::Bytes(vec![scope])],
        )
        .expect("anonymous individual");
        Node::build(
            112,
            vec![
                Field::Node(
                    entity("class", iri(class.to_owned()).expect("class IRI"))
                        .expect("class entity"),
                ),
                Field::Node(individual),
                Field::Set(Vec::new()),
            ],
        )
        .expect("class assertion")
        .into_bytes()
    }

    fn mapped_document(ordinal: u8, axiom: Vec<u8>) -> CanonicalDocument {
        CanonicalDocument {
            document_iri: Some(format!("urn:adapter:document:{ordinal}")),
            ontology_iri: Some(format!("urn:adapter:ontology:{ordinal}")),
            version_iri: None,
            imports: Vec::new(),
            ontology_annotations: Vec::new(),
            axioms: vec![axiom],
            extensions: Vec::new(),
            occurrences: Vec::new(),
            language_spellings: Vec::new(),
            source_blank_labels: Vec::new(),
            source_prefixes: Vec::new(),
            source_sha256: [ordinal; 32],
            byte_length: 0,
            decoded_codepoints: 0,
            mapping: super::super::MappingEvidence {
                total_triples: 0,
                consumed_triples: 0,
                occurrence_count: 0,
                rule_ids: &[],
                unconsumed: Vec::new(),
            },
        }
    }

    fn axiom_page(
        storage: &TypedFacadeStorageV2,
        document_ordinal: u64,
        raw: bool,
    ) -> Vec<Vec<u8>> {
        storage
            .page(
                TypedFacadePageRequestV2::new(
                    TypedFacadeCoordinateV2::document(
                        TypedFacadeCollectionV2::Axioms,
                        document_ordinal,
                    ),
                    raw,
                    0,
                    64,
                    8 * 1024 * 1024,
                ),
                Cancellation::with_duration(None),
                None,
            )
            .expect("axiom page")
            .rows
    }

    fn sorted(mut rows: Vec<Vec<u8>>) -> Vec<Vec<u8>> {
        rows.sort_unstable();
        rows.dedup();
        rows
    }

    #[test]
    fn scoped_documents_cross_diamond_and_cycle_without_flattening() {
        let documents = (0_u8..4)
            .map(|ordinal| {
                mapped_document(
                    ordinal + 1,
                    anonymous_assertion(ordinal + 1, &format!("urn:adapter:C{ordinal}")),
                )
            })
            .collect::<Vec<_>>();
        let effective = (0_u8..4)
            .map(|ordinal| {
                [
                    Vec::new(),
                    vec![anonymous_assertion(
                        ordinal + 11,
                        &format!("urn:adapter:C{ordinal}"),
                    )],
                    Vec::new(),
                ]
            })
            .collect::<Vec<_>>();
        let expected_diamond = sorted(
            effective
                .iter()
                .flat_map(|rows| rows[1].iter().cloned())
                .collect(),
        );
        let limits = Limits::default();
        let diamond = publish_scoped_timed(
            &documents,
            &effective,
            &[vec![0, 1, 2, 3], vec![1, 3], vec![2, 3], vec![3]],
            &[0, 1, 2, 3],
            limits,
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("scoped diamond")
        .storage;
        assert_eq!(axiom_page(&diamond, 0, true), documents[0].axioms);
        assert_eq!(axiom_page(&diamond, 0, false), expected_diamond);
        assert_eq!(
            axiom_page(&diamond, 1, false),
            sorted(vec![effective[1][1][0].clone(), effective[3][1][0].clone()])
        );
        let counters = diamond.counters().expect("diamond counters");
        assert_eq!(counters.canonical_input_rows, 4);
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(counters.publication_structural_bytes_copied, 0);

        let cycle = publish_scoped_timed(
            &documents[..2],
            &effective[..2],
            &[vec![0, 1], vec![0, 1]],
            &[0, 1],
            limits,
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("scoped cycle")
        .storage;
        let expected_cycle = sorted(vec![effective[0][1][0].clone(), effective[1][1][0].clone()]);
        assert_eq!(axiom_page(&cycle, 0, false), expected_cycle);
        assert_eq!(axiom_page(&cycle, 1, false), expected_cycle);
        assert_eq!(axiom_page(&cycle, 0, true), documents[0].axioms);
        assert_eq!(axiom_page(&cycle, 1, true), documents[1].axioms);
        let counters = cycle.counters().expect("cycle counters");
        assert_eq!(counters.canonical_input_rows, 2);
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(counters.publication_structural_bytes_copied, 0);
    }

    #[test]
    fn scoped_publication_rejects_partial_effective_document_tables() {
        let document = mapped_document(1, anonymous_assertion(1, "urn:adapter:C"));
        let error = publish_scoped_timed(
            std::slice::from_ref(&document),
            &[],
            &[vec![0]],
            &[0],
            Limits::default(),
            Cancellation::with_duration(None),
            None,
            0,
        )
        .err()
        .expect("missing effective document must fail");
        assert_eq!(error.code, "NATIVE_PROTOCOL");
        assert_eq!(
            error.message,
            "native scoped document count diverges from mapped documents"
        );
    }
}
