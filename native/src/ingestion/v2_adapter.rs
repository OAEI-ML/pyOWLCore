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
    let mapped_document_bytes = mapped_document_bytes(documents)?;
    let external_bytes = caller_external_bytes
        .checked_add(mapped_document_bytes)
        .ok_or_else(|| NativeError::limit("native V2 ingestion memory accounting overflow"))?;
    let arena_started = Instant::now();
    let mut builder = TypedFacadeBuilderV2::new(limits, cancellation, interrupt, external_bytes)?;
    for document in documents {
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
        let document = super::super::parse_rdfxml(source, Some("urn:document"), &mut session)
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
}
