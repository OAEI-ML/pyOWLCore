//! Typed builder-to-publication seam for retained V2 structural storage.
//!
//! Syntax owners contribute canonical document rows once. Import reachability
//! is expressed as document ordinals, and freeze derives effective document,
//! closure, raw-owner, and signature tables from the single component arena.

use std::collections::HashSet;
use std::mem::size_of;

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{
    scan_canonical, Category, ComponentFieldRef, ComponentId, NativeComponentArena,
    NativeComponentBuilder, PendingComponentId, ScanBudget,
};
use crate::session::Session;

use super::typed_v2::FlatDocumentV2;
use super::{
    TypedFacadeCollectionV2, TypedFacadeCoordinateV2, TypedFacadeScopeV2,
    TypedFacadeSignatureKindV2, TypedFacadeStorageV2, TypedFacadeTableV2,
};

const STRUCTURAL_COLLECTIONS: [TypedFacadeCollectionV2; 3] = [
    TypedFacadeCollectionV2::OntologyAnnotations,
    TypedFacadeCollectionV2::Axioms,
    TypedFacadeCollectionV2::Extensions,
];
const STRUCTURAL_CATEGORIES: [Category; 3] =
    [Category::Annotation, Category::Axiom, Category::Swrl];
const SIGNATURE_KINDS: [TypedFacadeSignatureKindV2; 7] = [
    TypedFacadeSignatureKindV2::All,
    TypedFacadeSignatureKindV2::Class,
    TypedFacadeSignatureKindV2::Datatype,
    TypedFacadeSignatureKindV2::ObjectProperty,
    TypedFacadeSignatureKindV2::DataProperty,
    TypedFacadeSignatureKindV2::AnnotationProperty,
    TypedFacadeSignatureKindV2::NamedIndividual,
];

type BorrowedDocumentRoots<'a> = (&'a [Vec<u8>], &'a [Vec<u8>], &'a [Vec<u8>]);

#[derive(Clone, Copy, Debug)]
struct ValidatedInputRows<'a> {
    rows: &'a [Vec<u8>],
    expected: Category,
}

#[derive(Debug, Default)]
struct PendingDocumentV2 {
    roots: [Vec<PendingComponentId>; 3],
    effective_roots: Option<[Vec<PendingComponentId>; 3]>,
}

#[derive(Debug, Default)]
struct ResolvedDocumentV2 {
    roots: [Vec<ComponentId>; 3],
    effective_roots: [Option<Vec<ComponentId>>; 3],
}

#[derive(Debug)]
struct EffectiveScopeV2 {
    scope: TypedFacadeScopeV2,
    document_ordinal: Option<u64>,
    roots: [Vec<ComponentId>; 3],
}

#[derive(Debug)]
pub(crate) struct TypedFacadeBuilderV2 {
    components: NativeComponentBuilder,
    documents: Vec<PendingDocumentV2>,
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    base_external_bytes: usize,
    poisoned: bool,
}

impl TypedFacadeBuilderV2 {
    pub(crate) fn new(
        limits: Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        external_bytes: usize,
    ) -> NativeResult<Self> {
        let components = NativeComponentBuilder::with_control(
            &limits,
            cancellation.clone(),
            interrupt.clone(),
            external_bytes,
        )?;
        Ok(Self {
            components,
            documents: Vec::new(),
            limits,
            cancellation,
            interrupt,
            base_external_bytes: external_bytes,
            poisoned: false,
        })
    }

    pub(crate) fn add_document(
        &mut self,
        ontology_annotations: &[Vec<u8>],
        axioms: &[Vec<u8>],
        extensions: &[Vec<u8>],
    ) -> NativeResult<u64> {
        if self.poisoned {
            return Err(NativeError::protocol(
                "typed V2 builder is poisoned after a failed mutation",
            ));
        }
        let result = self.add_document_inner(ontology_annotations, axioms, extensions, None);
        if result.is_err() {
            self.poisoned = true;
        }
        result
    }

    /// Retain one document whose raw roots and already-scoped effective roots
    /// intentionally differ. Both identities are interned into the same arena;
    /// document owners select the raw table while snapshot owners select the
    /// effective table.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn add_scoped_document(
        &mut self,
        ontology_annotations: &[Vec<u8>],
        axioms: &[Vec<u8>],
        extensions: &[Vec<u8>],
        effective_ontology_annotations: &[Vec<u8>],
        effective_axioms: &[Vec<u8>],
        effective_extensions: &[Vec<u8>],
    ) -> NativeResult<u64> {
        if self.poisoned {
            return Err(NativeError::protocol(
                "typed V2 builder is poisoned after a failed mutation",
            ));
        }
        let result = self.add_document_inner(
            ontology_annotations,
            axioms,
            extensions,
            Some((
                effective_ontology_annotations,
                effective_axioms,
                effective_extensions,
            )),
        );
        if result.is_err() {
            self.poisoned = true;
        }
        result
    }

    fn add_document_inner(
        &mut self,
        ontology_annotations: &[Vec<u8>],
        axioms: &[Vec<u8>],
        extensions: &[Vec<u8>],
        effective: Option<BorrowedDocumentRoots<'_>>,
    ) -> NativeResult<u64> {
        self.cancellation.checkpoint()?;
        let following = self
            .documents
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("typed V2 document count overflow"))?;
        let following = u64::try_from(following)
            .map_err(|_| NativeError::limit("typed V2 document count exceeds u64"))?;
        if following > self.limits.max_documents {
            return Err(self.limits.resource_limit(
                LimitKey::MaxDocuments,
                following,
                "typed V2 builder exceeds max_documents",
            ));
        }
        check_input_count(
            ontology_annotations.len(),
            &self.limits,
            LimitKey::MaxAnnotations,
            "typed V2 document exceeds max_annotations",
        )?;
        check_input_count(
            axioms.len(),
            &self.limits,
            LimitKey::MaxAxioms,
            "typed V2 document exceeds max_axioms",
        )?;
        let ontology_annotations =
            validate_input_rows(ontology_annotations, Category::Annotation, &self.limits)?;
        let axioms = validate_input_rows(axioms, Category::Axiom, &self.limits)?;
        let extensions = validate_input_rows(extensions, Category::Swrl, &self.limits)?;
        let effective = if let Some((
            effective_annotations,
            effective_axioms,
            effective_extensions,
        )) = effective
        {
            check_input_count(
                effective_annotations.len(),
                &self.limits,
                LimitKey::MaxAnnotations,
                "typed V2 effective document exceeds max_annotations",
            )?;
            check_input_count(
                effective_axioms.len(),
                &self.limits,
                LimitKey::MaxAxioms,
                "typed V2 effective document exceeds max_axioms",
            )?;
            Some((
                validate_input_rows(effective_annotations, Category::Annotation, &self.limits)?,
                validate_input_rows(effective_axioms, Category::Axiom, &self.limits)?,
                validate_input_rows(effective_extensions, Category::Swrl, &self.limits)?,
            ))
        } else {
            None
        };

        let mut staged_bytes = 0_usize;
        let annotations = self.intern_rows(ontology_annotations, staged_bytes)?;
        staged_bytes = pending_bytes(&annotations)?;
        let axioms = self.intern_rows(axioms, staged_bytes)?;
        staged_bytes = staged_bytes
            .checked_add(pending_bytes(&axioms)?)
            .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?;
        let extensions = self.intern_rows(extensions, staged_bytes)?;
        staged_bytes = staged_bytes
            .checked_add(pending_bytes(&extensions)?)
            .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?;
        let effective_roots = if let Some((annotations, axioms, extensions)) = effective {
            let annotations = self.intern_rows(annotations, staged_bytes)?;
            staged_bytes = staged_bytes
                .checked_add(pending_bytes(&annotations)?)
                .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?;
            let axioms = self.intern_rows(axioms, staged_bytes)?;
            staged_bytes = staged_bytes
                .checked_add(pending_bytes(&axioms)?)
                .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?;
            let extensions = self.intern_rows(extensions, staged_bytes)?;
            staged_bytes = staged_bytes
                .checked_add(pending_bytes(&extensions)?)
                .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?;
            Some([annotations, axioms, extensions])
        } else {
            None
        };

        self.preflight_document_capacity(staged_bytes)?;
        self.documents
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("typed V2 document table allocation failed"))?;
        self.documents.push(PendingDocumentV2 {
            roots: [annotations, axioms, extensions],
            effective_roots,
        });
        self.refresh_component_external(0)?;
        u64::try_from(following - 1)
            .map_err(|_| NativeError::limit("typed V2 document ordinal exceeds u64"))
    }

    pub(crate) fn freeze(
        mut self,
        effective_documents: &[Vec<u64>],
        closure_documents: &[u64],
    ) -> NativeResult<TypedFacadeStorageV2> {
        if self.poisoned {
            return Err(NativeError::protocol(
                "cannot freeze a poisoned typed V2 builder",
            ));
        }
        validate_reachability(effective_documents, closure_documents, self.documents.len())?;
        self.cancellation.checkpoint()?;
        self.refresh_component_external(0)?;
        let frozen = self.components.freeze()?;
        let mut resolve_guard = match self.interrupt.as_ref() {
            Some(slot) => Guard::with_interrupt(
                self.cancellation.clone(),
                self.limits.deadline,
                self.limits.cancellation_stride,
                slot.clone(),
            ),
            None => Guard::new(
                self.cancellation.clone(),
                self.limits.deadline,
                self.limits.cancellation_stride,
            ),
        };
        let mut resolve_work = 0_u64;
        let mut resolved = Vec::new();
        resolved
            .try_reserve_exact(self.documents.len())
            .map_err(|_| NativeError::limit("typed V2 resolved document allocation failed"))?;
        for document in self.documents.drain(..) {
            self.cancellation.checkpoint()?;
            let roots = [
                resolve_roots(
                    &frozen,
                    document.roots[0].as_slice(),
                    &mut resolve_guard,
                    &mut resolve_work,
                    &self.limits,
                )?,
                resolve_roots(
                    &frozen,
                    document.roots[1].as_slice(),
                    &mut resolve_guard,
                    &mut resolve_work,
                    &self.limits,
                )?,
                resolve_roots(
                    &frozen,
                    document.roots[2].as_slice(),
                    &mut resolve_guard,
                    &mut resolve_work,
                    &self.limits,
                )?,
            ];
            let effective_roots = match document.effective_roots {
                Some(values) => [
                    Some(resolve_roots(
                        &frozen,
                        values[0].as_slice(),
                        &mut resolve_guard,
                        &mut resolve_work,
                        &self.limits,
                    )?),
                    Some(resolve_roots(
                        &frozen,
                        values[1].as_slice(),
                        &mut resolve_guard,
                        &mut resolve_work,
                        &self.limits,
                    )?),
                    Some(resolve_roots(
                        &frozen,
                        values[2].as_slice(),
                        &mut resolve_guard,
                        &mut resolve_work,
                        &self.limits,
                    )?),
                ],
                None => Default::default(),
            };
            resolved.push(ResolvedDocumentV2 {
                roots,
                effective_roots,
            });
        }
        resolve_guard.check(resolve_work, true)?;
        let arena = frozen.into_arena();

        let mut scopes = Vec::new();
        scopes
            .try_reserve_exact(effective_documents.len().saturating_add(1))
            .map_err(|_| NativeError::limit("typed V2 effective scope allocation failed"))?;
        for (ordinal, reachable) in effective_documents.iter().enumerate() {
            self.cancellation.checkpoint()?;
            scopes.push(EffectiveScopeV2 {
                scope: TypedFacadeScopeV2::Document,
                document_ordinal: Some(
                    u64::try_from(ordinal)
                        .map_err(|_| NativeError::limit("typed V2 document ordinal exceeds u64"))?,
                ),
                roots: union_document_roots(
                    &arena,
                    &resolved,
                    reachable,
                    &self.limits,
                    self.cancellation.clone(),
                    self.interrupt.clone(),
                    self.base_external_bytes,
                )?,
            });
        }
        scopes.push(EffectiveScopeV2 {
            scope: TypedFacadeScopeV2::Closure,
            document_ordinal: None,
            roots: union_document_roots(
                &arena,
                &resolved,
                closure_documents,
                &self.limits,
                self.cancellation.clone(),
                self.interrupt.clone(),
                self.base_external_bytes,
            )?,
        });

        let mut effective_tables = Vec::new();
        append_scope_signature_tables(
            &arena,
            &scopes,
            effective_documents,
            closure_documents,
            &self.limits,
            self.cancellation.clone(),
            self.interrupt.clone(),
            self.base_external_bytes,
            &mut effective_tables,
        )?;

        let mut raw_document_tables = Vec::new();
        for (ordinal, document) in resolved.into_iter().enumerate() {
            for (index, (collection, roots)) in STRUCTURAL_COLLECTIONS
                .into_iter()
                .zip(document.roots)
                .enumerate()
            {
                if roots != scopes[ordinal].roots[index] {
                    push_table(
                        &mut raw_document_tables,
                        TypedFacadeTableV2::new(
                            TypedFacadeCoordinateV2::document(
                                collection,
                                u64::try_from(ordinal).map_err(|_| {
                                    NativeError::limit("typed V2 document ordinal exceeds u64")
                                })?,
                            ),
                            roots,
                        ),
                    )?;
                }
            }
        }
        let document_count = scopes.len().saturating_sub(1);
        for scope in scopes {
            for (collection, roots) in STRUCTURAL_COLLECTIONS.into_iter().zip(scope.roots) {
                if !roots.is_empty() {
                    push_table(
                        &mut effective_tables,
                        TypedFacadeTableV2::new(
                            structural_coordinate(scope.scope, scope.document_ordinal, collection)?,
                            roots,
                        ),
                    )?;
                }
            }
        }
        self.cancellation.checkpoint()?;
        TypedFacadeStorageV2::freeze_with_external(
            arena,
            effective_tables,
            raw_document_tables,
            u64::try_from(document_count)
                .map_err(|_| NativeError::limit("typed V2 document count exceeds u64"))?,
            self.limits,
            self.cancellation,
            self.interrupt,
            self.base_external_bytes,
        )
    }

    /// Compose parser-built single-document owners without re-interning their
    /// component tables. The source root vectors are moved into the final
    /// document manifests, while the composite arena retains strong references
    /// to each immutable source partition.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn compose_native_documents(
        sources: Vec<TypedFacadeStorageV2>,
        effective_documents: &[Vec<u64>],
        closure_documents: &[u64],
        anonymous_scope_targets: &[Option<[u8; 32]>],
        limits: Limits,
        cancellation: Cancellation,
        interrupt: Option<InterruptSlot>,
        base_external_bytes: usize,
        source_owner_bytes: usize,
    ) -> NativeResult<TypedFacadeStorageV2> {
        if sources.is_empty() {
            return Err(NativeError::protocol(
                "native closure composition requires at least one parser owner",
            ));
        }
        validate_reachability(effective_documents, closure_documents, sources.len())?;
        if anonymous_scope_targets.len() != sources.len() {
            return Err(NativeError::protocol(
                "native anonymous scope targets are not document-aligned",
            ));
        }
        cancellation.checkpoint()?;

        let mut flat_documents = Vec::new();
        flat_documents
            .try_reserve_exact(sources.len())
            .map_err(|_| NativeError::limit("native closure document allocation failed"))?;
        for source in sources {
            cancellation.checkpoint()?;
            let flat = source.into_flat_document()?;
            check_input_count(
                flat.1[0].len(),
                &limits,
                LimitKey::MaxAnnotations,
                "native closure document exceeds max_annotations",
            )?;
            check_input_count(
                flat.1[1].len(),
                &limits,
                LimitKey::MaxAxioms,
                "native closure document exceeds max_axioms",
            )?;
            for (index, roots) in flat.2.iter().enumerate() {
                let Some(roots) = roots else {
                    continue;
                };
                check_input_count(
                    roots.len(),
                    &limits,
                    if index == 0 {
                        LimitKey::MaxAnnotations
                    } else {
                        LimitKey::MaxAxioms
                    },
                    if index == 0 {
                        "native closure effective document exceeds max_annotations"
                    } else {
                        "native closure effective document exceeds max_axioms"
                    },
                )?;
            }
            flat_documents.push(flat);
        }

        let derived_arena = rescope_flat_documents(
            &mut flat_documents,
            anonymous_scope_targets,
            &limits,
            cancellation.clone(),
            interrupt.clone(),
            base_external_bytes,
            source_owner_bytes,
        )?;
        let mut source_arenas = Vec::new();
        source_arenas
            .try_reserve_exact(
                flat_documents
                    .len()
                    .saturating_add(usize::from(derived_arena.is_some())),
            )
            .map_err(|_| NativeError::limit("native closure arena manifest allocation failed"))?;
        source_arenas.extend(flat_documents.iter().map(|document| &document.0));
        if let Some(arena) = derived_arena.as_ref() {
            source_arenas.push(arena);
        }
        let arena = NativeComponentArena::compose_flat(&source_arenas)?;
        drop(source_arenas);

        let mut resolved = Vec::new();
        resolved
            .try_reserve_exact(flat_documents.len())
            .map_err(|_| NativeError::limit("native closure root allocation failed"))?;
        for (_source_arena, roots, effective_roots) in flat_documents {
            resolved.push(ResolvedDocumentV2 {
                roots,
                effective_roots,
            });
        }

        let mut scopes = Vec::new();
        scopes
            .try_reserve_exact(effective_documents.len().saturating_add(1))
            .map_err(|_| NativeError::limit("typed V2 effective scope allocation failed"))?;
        for (ordinal, reachable) in effective_documents.iter().enumerate() {
            cancellation.checkpoint()?;
            scopes.push(EffectiveScopeV2 {
                scope: TypedFacadeScopeV2::Document,
                document_ordinal: Some(
                    u64::try_from(ordinal)
                        .map_err(|_| NativeError::limit("typed V2 document ordinal exceeds u64"))?,
                ),
                roots: union_document_roots(
                    &arena,
                    &resolved,
                    reachable,
                    &limits,
                    cancellation.clone(),
                    interrupt.clone(),
                    base_external_bytes,
                )?,
            });
        }
        scopes.push(EffectiveScopeV2 {
            scope: TypedFacadeScopeV2::Closure,
            document_ordinal: None,
            roots: union_document_roots(
                &arena,
                &resolved,
                closure_documents,
                &limits,
                cancellation.clone(),
                interrupt.clone(),
                base_external_bytes,
            )?,
        });

        let mut effective_tables = Vec::new();
        append_scope_signature_tables(
            &arena,
            &scopes,
            effective_documents,
            closure_documents,
            &limits,
            cancellation.clone(),
            interrupt.clone(),
            base_external_bytes,
            &mut effective_tables,
        )?;

        let mut raw_document_tables = Vec::new();
        for (ordinal, document) in resolved.into_iter().enumerate() {
            for (index, (collection, roots)) in STRUCTURAL_COLLECTIONS
                .into_iter()
                .zip(document.roots)
                .enumerate()
            {
                if roots != scopes[ordinal].roots[index] {
                    push_table(
                        &mut raw_document_tables,
                        TypedFacadeTableV2::new(
                            TypedFacadeCoordinateV2::document(
                                collection,
                                u64::try_from(ordinal).map_err(|_| {
                                    NativeError::limit("typed V2 document ordinal exceeds u64")
                                })?,
                            ),
                            roots,
                        ),
                    )?;
                }
            }
        }
        let document_count = scopes.len().saturating_sub(1);
        for scope in scopes {
            for (collection, roots) in STRUCTURAL_COLLECTIONS.into_iter().zip(scope.roots) {
                if !roots.is_empty() {
                    push_table(
                        &mut effective_tables,
                        TypedFacadeTableV2::new(
                            structural_coordinate(scope.scope, scope.document_ordinal, collection)?,
                            roots,
                        ),
                    )?;
                }
            }
        }
        cancellation.checkpoint()?;
        TypedFacadeStorageV2::freeze_with_external(
            arena,
            effective_tables,
            raw_document_tables,
            u64::try_from(document_count)
                .map_err(|_| NativeError::limit("typed V2 document count exceeds u64"))?,
            limits,
            cancellation,
            interrupt,
            base_external_bytes,
        )
    }

    fn intern_rows(
        &mut self,
        input: ValidatedInputRows<'_>,
        prior_staged_bytes: usize,
    ) -> NativeResult<Vec<PendingComponentId>> {
        let ValidatedInputRows { rows, expected } = input;
        let mut output = Vec::new();
        let predicted = rows
            .len()
            .checked_mul(size_of::<PendingComponentId>())
            .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?;
        self.refresh_component_external(
            prior_staged_bytes
                .checked_add(predicted)
                .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?,
        )?;
        output
            .try_reserve_exact(rows.len())
            .map_err(|_| NativeError::limit("typed V2 pending root allocation failed"))?;
        self.refresh_component_external(
            prior_staged_bytes
                .checked_add(pending_bytes(&output)?)
                .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))?,
        )?;
        for row in rows {
            self.cancellation.checkpoint()?;
            let identifier = self.components.intern_validated_canonical(row)?;
            output.push(identifier);
        }
        if !STRUCTURAL_CATEGORIES.contains(&expected) {
            return Err(NativeError::protocol(
                "typed V2 builder received an unsupported root category",
            ));
        }
        Ok(output)
    }

    fn preflight_document_capacity(&mut self, staged_bytes: usize) -> NativeResult<()> {
        let predicted_capacity = self.documents.capacity().max(
            self.documents
                .len()
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("typed V2 document capacity overflow"))?,
        );
        let predicted_documents = predicted_capacity
            .checked_mul(size_of::<PendingDocumentV2>())
            .ok_or_else(|| NativeError::limit("typed V2 document metadata size overflow"))?;
        let existing_roots = pending_document_root_bytes(&self.documents)?;
        let external = self
            .base_external_bytes
            .checked_add(predicted_documents)
            .and_then(|value| value.checked_add(existing_roots))
            .and_then(|value| value.checked_add(staged_bytes))
            .ok_or_else(|| NativeError::limit("typed V2 external memory size overflow"))?;
        self.components.set_external_bytes(external)
    }

    fn refresh_component_external(&mut self, staged_bytes: usize) -> NativeResult<()> {
        let documents = self
            .documents
            .capacity()
            .checked_mul(size_of::<PendingDocumentV2>())
            .ok_or_else(|| NativeError::limit("typed V2 document metadata size overflow"))?;
        let external = self
            .base_external_bytes
            .checked_add(documents)
            .and_then(|value| value.checked_add(pending_document_root_bytes(&self.documents).ok()?))
            .and_then(|value| value.checked_add(staged_bytes))
            .ok_or_else(|| NativeError::limit("typed V2 external memory size overflow"))?;
        self.components.set_external_bytes(external)
    }
}

fn rescope_flat_documents(
    documents: &mut [FlatDocumentV2],
    targets: &[Option<[u8; 32]>],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    base_external_bytes: usize,
    source_owner_bytes: usize,
) -> NativeResult<Option<NativeComponentArena>> {
    let target_count = targets.iter().filter(|target| target.is_some()).count();
    if target_count == 0 {
        return Ok(None);
    }
    let mut rescoped: Vec<(usize, [Vec<Vec<u8>>; 3])> = Vec::new();
    rescoped
        .try_reserve_exact(target_count)
        .map_err(|_| NativeError::limit("native re-scoped document allocation failed"))?;
    let rescoped_metadata_bytes = rescoped
        .capacity()
        .checked_mul(size_of::<(usize, [Vec<Vec<u8>>; 3])>())
        .ok_or_else(|| NativeError::limit("native re-scoped document size overflow"))?;
    let mut retained_rescoped_bytes = 0_usize;

    for (document_ordinal, target) in targets.iter().copied().enumerate() {
        let Some(target) = target else {
            continue;
        };
        cancellation.checkpoint()?;
        let document = documents.get(document_ordinal).ok_or_else(|| {
            NativeError::protocol("native anonymous scope target is out of bounds")
        })?;
        let current_arena_bytes = usize::try_from(document.0.counters().retained_bytes)
            .map_err(|_| NativeError::limit("native source component arena exceeds usize"))?;
        let other_source_bytes = source_owner_bytes
            .checked_sub(current_arena_bytes)
            .ok_or_else(|| {
                NativeError::protocol("native source owner accounting is inconsistent")
            })?;
        let mut raw_rows: [Vec<Vec<u8>>; 3] = Default::default();
        let mut has_anonymous_roots = false;
        for (collection_index, roots) in document.1.iter().enumerate() {
            if document.2[collection_index].is_none() {
                continue;
            }
            has_anonymous_roots = true;
            raw_rows[collection_index]
                .try_reserve_exact(roots.len())
                .map_err(|_| NativeError::limit("native raw re-scope row allocation failed"))?;
            for identifier in roots {
                cancellation.checkpoint()?;
                let external_bytes = checked_external_bytes(
                    &[
                        base_external_bytes,
                        other_source_bytes,
                        rescoped_metadata_bytes,
                        retained_rescoped_bytes,
                        canonical_row_storage_bytes(&raw_rows)?,
                    ],
                    "native raw re-scope accounting overflow",
                )?;
                raw_rows[collection_index].push(document.0.encode(
                    *identifier,
                    limits,
                    cancellation.clone(),
                    interrupt.clone(),
                    external_bytes,
                )?);
            }
        }
        if !has_anonymous_roots {
            return Err(NativeError::protocol(
                "native anonymous scope target has no raw parser roots",
            ));
        }
        let raw_bytes = canonical_row_storage_bytes(&raw_rows)?;
        let input_bytes = checked_external_bytes(
            &[
                base_external_bytes,
                source_owner_bytes,
                rescoped_metadata_bytes,
                retained_rescoped_bytes,
                raw_bytes,
            ],
            "native anonymous re-scope input size overflow",
        )?;
        let mut guard = match interrupt.as_ref() {
            Some(slot) => Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                slot.clone(),
            ),
            None => Guard::new(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
            ),
        };
        let mut session = Session::new(&mut guard, limits, input_bytes)?;
        let transformed = crate::parse::rescope_anonymous_rows_v2(
            [
                raw_rows[0].as_slice(),
                raw_rows[1].as_slice(),
                raw_rows[2].as_slice(),
            ],
            target,
            &mut session,
            &cancellation,
        )?;
        session.finish()?;
        retained_rescoped_bytes = retained_rescoped_bytes
            .checked_add(canonical_row_storage_bytes(&transformed)?)
            .ok_or_else(|| NativeError::limit("native re-scoped row size overflow"))?;
        rescoped.push((document_ordinal, transformed));
    }

    let pending_count = rescoped
        .iter()
        .flat_map(|(_ordinal, collections)| collections)
        .try_fold(0_usize, |total, rows| {
            total
                .checked_add(rows.len())
                .ok_or_else(|| NativeError::limit("native re-scoped root count overflow"))
        })?;
    let pending_root_bytes = pending_count
        .checked_mul(size_of::<PendingComponentId>())
        .ok_or_else(|| NativeError::limit("native re-scoped pending root size overflow"))?;
    let mut pending_documents: Vec<(usize, [Option<Vec<PendingComponentId>>; 3])> = Vec::new();
    pending_documents
        .try_reserve_exact(rescoped.len())
        .map_err(|_| NativeError::limit("native pending re-scope allocation failed"))?;
    let pending_metadata_bytes = pending_documents
        .capacity()
        .checked_mul(size_of::<(usize, [Option<Vec<PendingComponentId>>; 3])>())
        .ok_or_else(|| NativeError::limit("native pending re-scope size overflow"))?;
    let builder_external_bytes = checked_external_bytes(
        &[
            base_external_bytes,
            source_owner_bytes,
            rescoped_metadata_bytes,
            retained_rescoped_bytes,
            pending_metadata_bytes,
            pending_root_bytes,
        ],
        "native re-scope builder accounting overflow",
    )?;
    let mut builder = NativeComponentBuilder::with_control(
        limits,
        cancellation.clone(),
        interrupt.clone(),
        builder_external_bytes,
    )?;
    for (document_ordinal, collections) in rescoped {
        let mut pending: [Option<Vec<PendingComponentId>>; 3] = Default::default();
        for (collection_index, rows) in collections.into_iter().enumerate() {
            if rows.is_empty() {
                continue;
            }
            let mut roots = Vec::new();
            roots.try_reserve_exact(rows.len()).map_err(|_| {
                NativeError::limit("native pending re-scope root allocation failed")
            })?;
            for row in rows {
                cancellation.checkpoint()?;
                roots.push(builder.intern_canonical(&row)?);
            }
            pending[collection_index] = Some(roots);
        }
        pending_documents.push((document_ordinal, pending));
    }
    let live_pending_bytes = pending_re_scope_bytes(&pending_documents)?;
    builder.set_external_bytes(checked_external_bytes(
        &[base_external_bytes, source_owner_bytes, live_pending_bytes],
        "native frozen re-scope accounting overflow",
    )?)?;
    let frozen = builder.freeze()?;
    let mut resolve_guard = match interrupt.as_ref() {
        Some(slot) => Guard::with_interrupt(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
            slot.clone(),
        ),
        None => Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        ),
    };
    let mut resolve_work = 0_u64;
    for (document_ordinal, pending) in pending_documents {
        let document = documents.get_mut(document_ordinal).ok_or_else(|| {
            NativeError::protocol("native pending anonymous scope is out of bounds")
        })?;
        for (collection_index, roots) in pending.into_iter().enumerate() {
            let Some(roots) = roots else {
                continue;
            };
            document.2[collection_index] = Some(resolve_roots(
                &frozen,
                &roots,
                &mut resolve_guard,
                &mut resolve_work,
                limits,
            )?);
        }
    }
    let arena = frozen.into_arena();
    for (document, target) in documents.iter().zip(targets) {
        if target.is_none() {
            continue;
        }
        for (collection_index, roots) in document.2.iter().enumerate() {
            let Some(roots) = roots else {
                continue;
            };
            for identifier in roots {
                if arena.category(*identifier)? != STRUCTURAL_CATEGORIES[collection_index] {
                    return Err(NativeError::protocol(
                        "native re-scoped root has the wrong structural category",
                    ));
                }
            }
        }
    }
    cancellation.checkpoint()?;
    Ok(Some(arena))
}

fn canonical_row_storage_bytes(rows: &[Vec<Vec<u8>>; 3]) -> NativeResult<usize> {
    rows.iter().try_fold(0_usize, |total, collection| {
        let metadata = collection
            .capacity()
            .checked_mul(size_of::<Vec<u8>>())
            .ok_or_else(|| NativeError::limit("native canonical row metadata size overflow"))?;
        collection.iter().try_fold(
            total
                .checked_add(metadata)
                .ok_or_else(|| NativeError::limit("native canonical row size overflow"))?,
            |subtotal, row| {
                subtotal
                    .checked_add(row.capacity())
                    .ok_or_else(|| NativeError::limit("native canonical row size overflow"))
            },
        )
    })
}

fn pending_re_scope_bytes(
    documents: &[(usize, [Option<Vec<PendingComponentId>>; 3])],
) -> NativeResult<usize> {
    let metadata = documents
        .len()
        .checked_mul(size_of::<(usize, [Option<Vec<PendingComponentId>>; 3])>())
        .ok_or_else(|| NativeError::limit("native pending re-scope metadata size overflow"))?;
    documents
        .iter()
        .try_fold(metadata, |total, (_ordinal, roots)| {
            roots.iter().try_fold(total, |subtotal, values| {
                let bytes = values.as_ref().map_or(Ok(0), |values| {
                    values
                        .capacity()
                        .checked_mul(size_of::<PendingComponentId>())
                        .ok_or_else(|| {
                            NativeError::limit("native pending re-scope root size overflow")
                        })
                })?;
                subtotal
                    .checked_add(bytes)
                    .ok_or_else(|| NativeError::limit("native pending re-scope size overflow"))
            })
        })
}

fn checked_external_bytes(values: &[usize], message: &'static str) -> NativeResult<usize> {
    values.iter().try_fold(0_usize, |total, value| {
        total
            .checked_add(*value)
            .ok_or_else(|| NativeError::limit(message))
    })
}

fn resolve_roots(
    frozen: &crate::model::FrozenComponentBuild,
    pending: &[PendingComponentId],
    guard: &mut Guard,
    work: &mut u64,
    limits: &Limits,
) -> NativeResult<Vec<ComponentId>> {
    let mut result = Vec::new();
    result
        .try_reserve_exact(pending.len())
        .map_err(|_| NativeError::limit("typed V2 resolved root allocation failed"))?;
    for identifier in pending {
        *work = work
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("typed V2 root resolution work overflow"))?;
        if *work > limits.max_canonical_work {
            return Err(limits.resource_limit(
                LimitKey::MaxCanonicalWork,
                *work,
                "typed V2 root resolution exceeds max_canonical_work",
            ));
        }
        guard.check(*work, false)?;
        result.push(frozen.resolve(*identifier)?);
    }
    Ok(result)
}

fn union_document_roots(
    arena: &NativeComponentArena,
    documents: &[ResolvedDocumentV2],
    ordinals: &[u64],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    external_bytes: usize,
) -> NativeResult<[Vec<ComponentId>; 3]> {
    let mut result: [Vec<ComponentId>; 3] = Default::default();
    for (index, expected) in STRUCTURAL_CATEGORIES.into_iter().enumerate() {
        let count = ordinals.iter().try_fold(0_usize, |total, ordinal| {
            let selected = documents
                .get(usize::try_from(*ordinal).map_err(|_| {
                    NativeError::protocol("typed V2 reachability ordinal exceeds usize")
                })?)
                .ok_or_else(|| NativeError::protocol("typed V2 reachability ordinal is invalid"))?;
            let roots = selected.effective_roots[index]
                .as_ref()
                .unwrap_or(&selected.roots[index]);
            total
                .checked_add(roots.len())
                .ok_or_else(|| NativeError::limit("typed V2 effective root count overflow"))
        })?;
        let observed = u64::try_from(count)
            .map_err(|_| NativeError::protocol("typed V2 effective root count exceeds u64"))?;
        if observed > limits.value(LimitKey::MaxIndexRows) {
            return Err(limits.resource_limit(
                LimitKey::MaxIndexRows,
                observed,
                "typed V2 effective roots exceed max_index_rows",
            ));
        }
        result[index]
            .try_reserve_exact(count)
            .map_err(|_| NativeError::limit("typed V2 effective root allocation failed"))?;
        for ordinal in ordinals {
            let selected = &documents[usize::try_from(*ordinal).map_err(|_| {
                NativeError::protocol("typed V2 reachability ordinal exceeds usize")
            })?];
            let roots = selected.effective_roots[index]
                .as_ref()
                .unwrap_or(&selected.roots[index]);
            result[index].extend_from_slice(roots);
        }
        // Each resolved document slice preserves the canonical ascending,
        // duplicate-free order validated at ingestion. A one-document scope
        // is already the required union, so avoid reconstructing every
        // canonical row solely to rediscover the same order. Multi-document
        // closures still require cross-owner/order canonicalization.
        if ordinals.len() > 1 {
            arena.sort_deduplicate_ids(
                &mut result[index],
                expected,
                limits,
                cancellation.clone(),
                interrupt.clone(),
                external_bytes,
            )?;
        }
    }
    Ok(result)
}

#[allow(clippy::too_many_arguments)]
fn append_scope_signature_tables(
    arena: &NativeComponentArena,
    scopes: &[EffectiveScopeV2],
    effective_documents: &[Vec<u64>],
    closure_documents: &[u64],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    external_bytes: usize,
    tables: &mut Vec<TypedFacadeTableV2>,
) -> NativeResult<()> {
    if scopes.len() != effective_documents.len().saturating_add(1) {
        return Err(NativeError::protocol(
            "typed V2 signature scopes are not document-aligned",
        ));
    }
    let mut closure_entities = None;
    for (scope, reachable) in scopes[..effective_documents.len()]
        .iter()
        .zip(effective_documents)
    {
        let live_external = signature_collection_external_bytes(
            external_bytes,
            tables,
            tables.capacity(),
            closure_entities.as_ref().map_or(0, Vec::capacity),
        )?;
        let entities = collect_signature(
            arena,
            scope.roots.iter().map(Vec::as_slice),
            limits,
            cancellation.clone(),
            interrupt.clone(),
            live_external,
        )?;
        append_signature_tables(arena, scope, &entities, tables)?;
        if closure_entities.is_none() && reachable.as_slice() == closure_documents {
            // The closure has the same root union, so its signature is exactly
            // this canonical entity set. Retain one small ID vector until the
            // closure tables are emitted instead of traversing every
            // structural component a second time.
            closure_entities = Some(entities);
        }
    }
    let closure = scopes
        .last()
        .ok_or_else(|| NativeError::protocol("typed V2 closure scope is missing"))?;
    let computed;
    let entities = if let Some(entities) = closure_entities.as_ref() {
        entities
    } else {
        let live_external =
            signature_collection_external_bytes(external_bytes, tables, tables.capacity(), 0)?;
        computed = collect_signature(
            arena,
            closure.roots.iter().map(Vec::as_slice),
            limits,
            cancellation,
            interrupt,
            live_external,
        )?;
        &computed
    };
    append_signature_tables(arena, closure, entities, tables)
}

fn signature_collection_external_bytes(
    base_external_bytes: usize,
    tables: &[TypedFacadeTableV2],
    table_capacity: usize,
    retained_entity_capacity: usize,
) -> NativeResult<usize> {
    let table_metadata = table_capacity
        .checked_mul(size_of::<TypedFacadeTableV2>())
        .ok_or_else(|| NativeError::limit("typed V2 signature metadata size overflow"))?;
    let table_roots = tables.iter().try_fold(0_usize, |total, table| {
        table
            .root_capacity()
            .checked_mul(size_of::<ComponentId>())
            .and_then(|bytes| total.checked_add(bytes))
            .ok_or_else(|| NativeError::limit("typed V2 signature root size overflow"))
    })?;
    let retained_entities = retained_entity_capacity
        .checked_mul(size_of::<ComponentId>())
        .ok_or_else(|| NativeError::limit("typed V2 retained signature size overflow"))?;
    base_external_bytes
        .checked_add(table_metadata)
        .and_then(|total| total.checked_add(table_roots))
        .and_then(|total| total.checked_add(retained_entities))
        .ok_or_else(|| NativeError::limit("typed V2 signature external memory overflow"))
}

fn collect_signature<'a>(
    arena: &NativeComponentArena,
    roots: impl Iterator<Item = &'a [ComponentId]>,
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    external_bytes: usize,
) -> NativeResult<Vec<ComponentId>> {
    let sort_cancellation = cancellation.clone();
    let mut guard = match interrupt.as_ref() {
        Some(slot) => Guard::with_interrupt(
            cancellation,
            limits.deadline,
            limits.cancellation_stride,
            slot.clone(),
        ),
        None => Guard::new(cancellation, limits.deadline, limits.cancellation_stride),
    };
    let mut visited = HashSet::new();
    let mut entities = HashSet::new();
    let mut stack = Vec::new();
    let mut work = 0_u64;
    for roots in roots {
        for root in roots {
            reserve_hash_item(&mut visited)?;
            if !visited.insert(*root) {
                continue;
            }
            stack
                .try_reserve(1)
                .map_err(|_| NativeError::limit("typed V2 signature stack allocation failed"))?;
            stack.push(*root);
            while let Some(identifier) = stack.pop() {
                signature_step(&mut guard, &mut work, limits)?;
                if arena.category(identifier)? == Category::Entity {
                    reserve_hash_item(&mut entities)?;
                    entities.insert(identifier);
                    continue;
                }
                let record = arena.record(identifier)?;
                for field_index in 0..record.field_count() {
                    collect_field_nodes(
                        record.field(field_index)?,
                        &mut stack,
                        &mut visited,
                        &mut guard,
                        &mut work,
                        limits,
                    )?;
                }
            }
        }
    }
    guard.check(work, true)?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(entities.len())
        .map_err(|_| NativeError::limit("typed V2 signature allocation failed"))?;
    result.extend(entities);
    arena.sort_deduplicate_ids(
        &mut result,
        Category::Entity,
        limits,
        sort_cancellation,
        interrupt,
        external_bytes,
    )?;
    Ok(result)
}

fn collect_field_nodes(
    field: ComponentFieldRef<'_>,
    stack: &mut Vec<ComponentId>,
    visited: &mut HashSet<ComponentId>,
    guard: &mut Guard,
    work: &mut u64,
    limits: &Limits,
) -> NativeResult<()> {
    signature_step(guard, work, limits)?;
    match field {
        ComponentFieldRef::Node(identifier) => {
            reserve_hash_item(visited)?;
            if visited.insert(identifier) {
                stack.try_reserve(1).map_err(|_| {
                    NativeError::limit("typed V2 signature stack allocation failed")
                })?;
                stack.push(identifier);
            }
        }
        ComponentFieldRef::CanonicalSet(sequence)
        | ComponentFieldRef::OrderedSequence(sequence) => {
            for index in 0..sequence.len() {
                collect_field_nodes(sequence.item(index)?, stack, visited, guard, work, limits)?;
            }
        }
        ComponentFieldRef::None
        | ComponentFieldRef::Text(_)
        | ComponentFieldRef::Bytes(_)
        | ComponentFieldRef::NonnegativeIntegerVarint(_)
        | ComponentFieldRef::Enum(_) => {}
    }
    Ok(())
}

fn signature_step(guard: &mut Guard, work: &mut u64, limits: &Limits) -> NativeResult<()> {
    *work = work
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("typed V2 signature work overflow"))?;
    if *work > limits.max_canonical_work {
        return Err(limits.resource_limit(
            LimitKey::MaxCanonicalWork,
            *work,
            "typed V2 signature exceeds max_canonical_work",
        ));
    }
    guard.check(*work, false)
}

fn append_signature_tables(
    arena: &NativeComponentArena,
    scope: &EffectiveScopeV2,
    entities: &[ComponentId],
    tables: &mut Vec<TypedFacadeTableV2>,
) -> NativeResult<()> {
    const TABLE_COUNT: usize = SIGNATURE_KINDS.len() * 2;
    let mut counts = [0_usize; TABLE_COUNT];
    for identifier in entities {
        let descriptor = entity_descriptor(arena, *identifier)?;
        let builtin = is_builtin(descriptor);
        for (include_index, include_builtins) in [false, true].into_iter().enumerate() {
            if !include_builtins && builtin {
                continue;
            }
            for (kind_index, signature_kind) in SIGNATURE_KINDS.into_iter().enumerate() {
                if signature_kind == TypedFacadeSignatureKindV2::All
                    || descriptor.kind == signature_kind
                {
                    let slot = include_index * SIGNATURE_KINDS.len() + kind_index;
                    counts[slot] = counts[slot]
                        .checked_add(1)
                        .ok_or_else(|| NativeError::limit("typed V2 signature count overflow"))?;
                }
            }
        }
    }
    let mut selected: [Vec<ComponentId>; TABLE_COUNT] = Default::default();
    for (slot, count) in counts.into_iter().enumerate() {
        selected[slot]
            .try_reserve_exact(count)
            .map_err(|_| NativeError::limit("typed V2 signature table allocation failed"))?;
    }
    for identifier in entities {
        let descriptor = entity_descriptor(arena, *identifier)?;
        let builtin = is_builtin(descriptor);
        for (include_index, include_builtins) in [false, true].into_iter().enumerate() {
            if !include_builtins && builtin {
                continue;
            }
            for (kind_index, signature_kind) in SIGNATURE_KINDS.into_iter().enumerate() {
                if signature_kind == TypedFacadeSignatureKindV2::All
                    || descriptor.kind == signature_kind
                {
                    let slot = include_index * SIGNATURE_KINDS.len() + kind_index;
                    selected[slot].push(*identifier);
                }
            }
        }
    }
    for (include_index, include_builtins) in [false, true].into_iter().enumerate() {
        for (kind_index, signature_kind) in SIGNATURE_KINDS.into_iter().enumerate() {
            let slot = include_index * SIGNATURE_KINDS.len() + kind_index;
            let roots = std::mem::take(&mut selected[slot]);
            if !roots.is_empty() {
                push_table(
                    tables,
                    TypedFacadeTableV2::new(
                        TypedFacadeCoordinateV2 {
                            collection: TypedFacadeCollectionV2::Signature,
                            scope: scope.scope,
                            document_ordinal: scope.document_ordinal,
                            signature_kind,
                            include_builtins,
                        },
                        roots,
                    ),
                )?;
            }
        }
    }
    Ok(())
}

#[derive(Clone, Copy)]
struct EntityDescriptor<'a> {
    kind: TypedFacadeSignatureKindV2,
    iri: &'a str,
}

fn entity_descriptor(
    arena: &NativeComponentArena,
    identifier: ComponentId,
) -> NativeResult<EntityDescriptor<'_>> {
    let entity = arena.record(identifier)?;
    if entity.tag() != 2 || entity.field_count() != 2 {
        return Err(NativeError::protocol(
            "typed V2 signature contains a malformed entity",
        ));
    }
    let ComponentFieldRef::Enum(kind) = entity.field(0)? else {
        return Err(NativeError::protocol(
            "typed V2 signature entity kind is malformed",
        ));
    };
    let ComponentFieldRef::Node(iri_identifier) = entity.field(1)? else {
        return Err(NativeError::protocol(
            "typed V2 signature entity IRI is malformed",
        ));
    };
    let iri = arena.record(iri_identifier)?;
    if iri.tag() != 1 || iri.field_count() != 1 {
        return Err(NativeError::protocol(
            "typed V2 signature entity IRI is malformed",
        ));
    }
    let ComponentFieldRef::Text(iri) = iri.field(0)? else {
        return Err(NativeError::protocol(
            "typed V2 signature entity IRI is malformed",
        ));
    };
    let kind = match kind {
        b"class" => TypedFacadeSignatureKindV2::Class,
        b"datatype" => TypedFacadeSignatureKindV2::Datatype,
        b"object_property" => TypedFacadeSignatureKindV2::ObjectProperty,
        b"data_property" => TypedFacadeSignatureKindV2::DataProperty,
        b"annotation_property" => TypedFacadeSignatureKindV2::AnnotationProperty,
        b"named_individual" => TypedFacadeSignatureKindV2::NamedIndividual,
        _ => {
            return Err(NativeError::protocol(
                "typed V2 signature entity kind is unknown",
            ));
        }
    };
    Ok(EntityDescriptor {
        kind,
        iri: std::str::from_utf8(iri)
            .map_err(|_| NativeError::protocol("typed V2 signature IRI is not UTF-8"))?,
    })
}

fn is_builtin(descriptor: EntityDescriptor<'_>) -> bool {
    match descriptor.kind {
        TypedFacadeSignatureKindV2::Class => matches!(
            descriptor.iri,
            "http://www.w3.org/2002/07/owl#Thing" | "http://www.w3.org/2002/07/owl#Nothing"
        ),
        TypedFacadeSignatureKindV2::ObjectProperty => matches!(
            descriptor.iri,
            "http://www.w3.org/2002/07/owl#topObjectProperty"
                | "http://www.w3.org/2002/07/owl#bottomObjectProperty"
        ),
        TypedFacadeSignatureKindV2::DataProperty => matches!(
            descriptor.iri,
            "http://www.w3.org/2002/07/owl#topDataProperty"
                | "http://www.w3.org/2002/07/owl#bottomDataProperty"
        ),
        TypedFacadeSignatureKindV2::Datatype => {
            matches!(
                descriptor.iri,
                "http://www.w3.org/2000/01/rdf-schema#Literal"
                    | "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral"
                    | "http://www.w3.org/1999/02/22-rdf-syntax-ns#XMLLiteral"
            ) || descriptor
                .iri
                .strip_prefix("http://www.w3.org/2001/XMLSchema#")
                .is_some_and(is_xsd_builtin)
        }
        TypedFacadeSignatureKindV2::AnnotationProperty => matches!(
            descriptor.iri,
            "http://www.w3.org/2000/01/rdf-schema#label"
                | "http://www.w3.org/2000/01/rdf-schema#comment"
                | "http://www.w3.org/2000/01/rdf-schema#seeAlso"
                | "http://www.w3.org/2000/01/rdf-schema#isDefinedBy"
                | "http://www.w3.org/2002/07/owl#deprecated"
                | "http://www.w3.org/2002/07/owl#versionInfo"
                | "http://www.w3.org/2002/07/owl#priorVersion"
                | "http://www.w3.org/2002/07/owl#backwardCompatibleWith"
                | "http://www.w3.org/2002/07/owl#incompatibleWith"
        ),
        TypedFacadeSignatureKindV2::All | TypedFacadeSignatureKindV2::NamedIndividual => false,
    }
}

fn is_xsd_builtin(local: &str) -> bool {
    matches!(
        local,
        "anyURI"
            | "base64Binary"
            | "boolean"
            | "byte"
            | "dateTime"
            | "dateTimeStamp"
            | "decimal"
            | "double"
            | "float"
            | "hexBinary"
            | "int"
            | "integer"
            | "language"
            | "long"
            | "Name"
            | "NCName"
            | "negativeInteger"
            | "NMTOKEN"
            | "nonNegativeInteger"
            | "nonPositiveInteger"
            | "normalizedString"
            | "positiveInteger"
            | "short"
            | "string"
            | "token"
            | "unsignedByte"
            | "unsignedInt"
            | "unsignedLong"
            | "unsignedShort"
    )
}

fn structural_coordinate(
    scope: TypedFacadeScopeV2,
    document_ordinal: Option<u64>,
    collection: TypedFacadeCollectionV2,
) -> NativeResult<TypedFacadeCoordinateV2> {
    match (scope, document_ordinal) {
        (TypedFacadeScopeV2::Document, Some(ordinal)) => {
            Ok(TypedFacadeCoordinateV2::document(collection, ordinal))
        }
        (TypedFacadeScopeV2::Closure, None) => Ok(TypedFacadeCoordinateV2::closure(collection)),
        _ => Err(NativeError::protocol(
            "typed V2 effective scope coordinate is malformed",
        )),
    }
}

fn validate_reachability(
    effective: &[Vec<u64>],
    closure: &[u64],
    document_count: usize,
) -> NativeResult<()> {
    if effective.len() != document_count || document_count == 0 {
        return Err(NativeError::protocol(
            "typed V2 reachability does not cover every document",
        ));
    }
    for (ordinal, reachable) in effective.iter().enumerate() {
        validate_ordinal_set(reachable, document_count)?;
        let ordinal = u64::try_from(ordinal)
            .map_err(|_| NativeError::limit("typed V2 document ordinal exceeds u64"))?;
        if reachable.binary_search(&ordinal).is_err() {
            return Err(NativeError::protocol(
                "typed V2 effective document closure excludes its owner",
            ));
        }
    }
    validate_ordinal_set(closure, document_count)?;
    if closure.is_empty() {
        return Err(NativeError::protocol("typed V2 snapshot closure is empty"));
    }
    Ok(())
}

fn validate_ordinal_set(values: &[u64], document_count: usize) -> NativeResult<()> {
    if values.windows(2).any(|pair| pair[0] >= pair[1])
        || values
            .iter()
            .any(|value| usize::try_from(*value).map_or(true, |ordinal| ordinal >= document_count))
    {
        return Err(NativeError::protocol(
            "typed V2 reachability ordinals are not ascending unique and in range",
        ));
    }
    Ok(())
}

fn check_input_count(
    count: usize,
    limits: &Limits,
    key: LimitKey,
    message: &'static str,
) -> NativeResult<()> {
    let observed =
        u64::try_from(count).map_err(|_| NativeError::limit("typed V2 input count exceeds u64"))?;
    if observed > limits.value(key) {
        return Err(limits.resource_limit(key, observed, message));
    }
    Ok(())
}

fn validate_input_rows<'a>(
    rows: &'a [Vec<u8>],
    expected: Category,
    limits: &Limits,
) -> NativeResult<ValidatedInputRows<'a>> {
    if rows
        .windows(2)
        .any(|pair| pair[0].as_slice() >= pair[1].as_slice())
    {
        return Err(NativeError::protocol(
            "typed V2 input roots are not canonical ascending unique",
        ));
    }
    for row in rows {
        let mut scan = ScanBudget::from_limits(limits);
        if scan_canonical(row, &mut scan)? != expected {
            return Err(NativeError::protocol(
                "typed V2 input root is in the wrong structural collection",
            ));
        }
    }
    Ok(ValidatedInputRows { rows, expected })
}

fn pending_bytes(values: &Vec<PendingComponentId>) -> NativeResult<usize> {
    values
        .capacity()
        .checked_mul(size_of::<PendingComponentId>())
        .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))
}

fn pending_document_root_bytes(documents: &[PendingDocumentV2]) -> NativeResult<usize> {
    documents.iter().try_fold(0_usize, |total, document| {
        let raw = document.roots.iter().try_fold(total, |total, roots| {
            total
                .checked_add(pending_bytes(roots)?)
                .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))
        })?;
        document
            .effective_roots
            .as_ref()
            .map_or(Ok(raw), |effective| {
                effective.iter().try_fold(raw, |total, roots| {
                    total
                        .checked_add(pending_bytes(roots)?)
                        .ok_or_else(|| NativeError::limit("typed V2 pending root size overflow"))
                })
            })
    })
}

fn reserve_hash_item<T: Eq + std::hash::Hash>(values: &mut HashSet<T>) -> NativeResult<()> {
    if values.len() == values.capacity() {
        values
            .try_reserve(1)
            .map_err(|_| NativeError::limit("typed V2 signature set allocation failed"))?;
    }
    Ok(())
}

fn push_table(tables: &mut Vec<TypedFacadeTableV2>, table: TypedFacadeTableV2) -> NativeResult<()> {
    tables
        .try_reserve_exact(1)
        .map_err(|_| NativeError::limit("typed V2 facade table allocation failed"))?;
    tables.push(table);
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;
    use crate::canonical::{entity, iri, Field, Node};
    use crate::publication::TypedFacadePageRequestV2;

    const OWL_THING: &str = "http://www.w3.org/2002/07/owl#Thing";

    fn entity_row(kind: &'static str, value: &str) -> Vec<u8> {
        entity(kind, iri(value.to_owned()).expect("IRI"))
            .expect("entity")
            .as_bytes()
            .to_vec()
    }

    fn declaration(kind: &'static str, value: &str) -> Vec<u8> {
        Node::build(
            60,
            vec![
                Field::Node(entity(kind, iri(value.to_owned()).expect("IRI")).expect("entity")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("declaration")
        .as_bytes()
        .to_vec()
    }

    fn subclass(sub_class: &str, super_class: &str) -> Vec<u8> {
        Node::build(
            61,
            vec![
                Field::Node(
                    entity("class", iri(sub_class.to_owned()).expect("subclass IRI"))
                        .expect("subclass"),
                ),
                Field::Node(
                    entity(
                        "class",
                        iri(super_class.to_owned()).expect("superclass IRI"),
                    )
                    .expect("superclass"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("SubClassOf")
        .as_bytes()
        .to_vec()
    }

    fn sorted(mut rows: Vec<Vec<u8>>) -> Vec<Vec<u8>> {
        rows.sort_unstable();
        rows
    }

    fn sorted_unique(mut rows: Vec<Vec<u8>>) -> Vec<Vec<u8>> {
        rows.sort_unstable();
        rows.dedup();
        rows
    }

    fn two_document_owner() -> (TypedFacadeStorageV2, [Vec<Vec<u8>>; 2]) {
        let documents = [
            sorted(vec![
                declaration("class", "urn:builder:A"),
                declaration("class", OWL_THING),
            ]),
            sorted(vec![
                declaration("class", "urn:builder:B"),
                declaration("data_property", "urn:builder:p"),
            ]),
        ];
        let limits = Limits::default();
        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("typed builder");
        for rows in &documents {
            builder
                .add_document(&[], rows, &[])
                .expect("typed document");
        }
        let storage = builder
            .freeze(&[vec![0, 1], vec![1]], &[0, 1])
            .expect("typed freeze");
        (storage, documents)
    }

    fn page(
        storage: &TypedFacadeStorageV2,
        coordinate: TypedFacadeCoordinateV2,
        raw_document_owner: bool,
    ) -> Vec<Vec<u8>> {
        storage
            .page(
                TypedFacadePageRequestV2::new(
                    coordinate,
                    raw_document_owner,
                    0,
                    64,
                    8 * 1024 * 1024,
                ),
                Cancellation::with_duration(None),
                None,
            )
            .expect("typed page")
            .rows
    }

    fn encoded_root_kinds(columns: &crate::model::EncodedStructuralColumnsV2) -> &[u8] {
        columns
            .buffers()
            .named()
            .into_iter()
            .find_map(|(name, value)| (name == "root_kinds").then_some(value))
            .expect("root kind buffer")
    }

    fn assert_prevalidation_left_builder_unmutated(builder: &TypedFacadeBuilderV2) {
        let counters = builder.components.counters();
        assert_eq!(counters.node_requests, 0);
        assert_eq!(counters.unique_nodes, 0);
        assert_eq!(counters.string_requests, 0);
        assert_eq!(counters.unique_strings, 0);
        assert_eq!(counters.sequence_requests, 0);
        assert_eq!(counters.unique_sequences, 0);
        assert!(builder.documents.is_empty());
    }

    #[test]
    fn direct_columns_borrow_effective_and_raw_root_tables_without_retaining_a_copy() {
        let (storage, _) = two_document_owner();
        let limits = Limits::default();
        let closure = storage
            .encoded_structural_columns(
                TypedFacadeScopeV2::Closure,
                None,
                false,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("closure columns");
        assert_eq!(closure.counters().root_rows, 4);
        assert_eq!(encoded_root_kinds(&closure), [2, 2, 2, 2]);
        assert_eq!(closure.counters().retained_metadata_bytes, 0);

        let effective_document = storage
            .encoded_structural_columns(
                TypedFacadeScopeV2::Document,
                Some(0),
                false,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("effective document columns");
        let raw_document = storage
            .encoded_structural_columns(
                TypedFacadeScopeV2::Document,
                Some(0),
                true,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("raw document columns");
        assert_eq!(effective_document.counters().root_rows, 4);
        assert_eq!(raw_document.counters().root_rows, 2);
        assert_eq!(raw_document.counters().retained_metadata_bytes, 0);

        let retained = storage
            .counters()
            .expect("storage counters")
            .retained_owner_bytes;
        let required = retained
            .checked_add(closure.counters().retained_buffer_bytes)
            .and_then(|value| value.checked_add(closure.counters().peak_workspace_bytes))
            .expect("column peak");
        let mut tight = limits;
        tight.max_memory_bytes = Some(required - 1);
        assert_eq!(
            storage
                .encoded_structural_columns(
                    TypedFacadeScopeV2::Closure,
                    None,
                    false,
                    &tight,
                    Cancellation::with_duration(None),
                    None,
                )
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT"
        );
    }

    #[test]
    fn scoped_document_retains_distinct_raw_and_effective_roots_in_one_arena() {
        let raw = sorted(vec![declaration("class", "urn:builder:raw")]);
        let effective = sorted(vec![declaration("class", "urn:builder:effective")]);
        let limits = Limits::default();
        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("typed builder");
        builder
            .add_scoped_document(&[], &raw, &[], &[], &effective, &[])
            .expect("scoped document");
        let storage = builder.freeze(&[vec![0]], &[0]).expect("scoped freeze");

        assert_eq!(
            page(
                &storage,
                TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0),
                true,
            ),
            raw
        );
        assert_eq!(
            page(
                &storage,
                TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0),
                false,
            ),
            effective
        );
        assert_eq!(
            page(
                &storage,
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                false,
            ),
            effective
        );
        assert_eq!(
            storage.structural_counts().expect("counts").stored_axioms,
            1
        );
        assert_eq!(
            storage
                .structural_counts()
                .expect("counts")
                .effective_axioms,
            1
        );
    }

    #[test]
    fn retained_axiom_type_index_borrows_effective_and_raw_root_tables() {
        let (storage, _) = two_document_owner();
        let limits = Limits::default();
        let closure = storage
            .axiom_type_index(
                TypedFacadeScopeV2::Closure,
                None,
                false,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("closure axiom-type index");
        assert_eq!(closure.tags(), [60]);
        assert_eq!(closure.offsets(), [0, 4]);
        assert_eq!(closure.category_codes(), [1]);
        assert_eq!(closure.category_offsets(), [0, 4]);
        assert_eq!(closure.postings(), [0, 1, 2, 3]);
        assert_eq!(closure.counters().axiom_rows, 4);
        assert_eq!(closure.counters().complete_root_encode_calls, 0);
        assert!(closure.owner().shares_storage_with(storage.arena()));

        let raw_document = storage
            .axiom_type_index(
                TypedFacadeScopeV2::Document,
                Some(0),
                true,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("raw document axiom-type index");
        assert_eq!(raw_document.offsets(), [0, 2]);
        assert_eq!(raw_document.postings(), [0, 1]);

        let retained_owner_bytes = storage
            .counters()
            .expect("storage counters")
            .retained_owner_bytes;
        let required = retained_owner_bytes
            .checked_add(closure.counters().retained_buffer_bytes)
            .expect("index peak");
        let mut tight = limits;
        tight.max_memory_bytes = Some(required - 1);
        assert_eq!(
            storage
                .axiom_type_index(
                    TypedFacadeScopeV2::Closure,
                    None,
                    false,
                    &tight,
                    Cancellation::with_duration(None),
                    None,
                )
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT"
        );
    }

    #[test]
    fn builder_derives_effective_raw_closure_and_signature_tables_from_one_arena() {
        let (storage, documents) = two_document_owner();
        let document_zero = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let document_one = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 1);
        let closure = TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms);

        let expected_closure = sorted(
            documents
                .iter()
                .flat_map(|rows| rows.iter().cloned())
                .collect(),
        );
        assert_eq!(page(&storage, document_zero, false), expected_closure);
        assert_eq!(page(&storage, document_zero, true), documents[0]);
        assert_eq!(page(&storage, document_one, false), documents[1]);
        assert_eq!(page(&storage, document_one, true), documents[1]);
        assert_eq!(page(&storage, closure, false), expected_closure);

        let all_without_builtins = TypedFacadeCoordinateV2 {
            collection: TypedFacadeCollectionV2::Signature,
            scope: TypedFacadeScopeV2::Document,
            document_ordinal: Some(0),
            signature_kind: TypedFacadeSignatureKindV2::All,
            include_builtins: false,
        };
        let classes_with_builtins = TypedFacadeCoordinateV2 {
            signature_kind: TypedFacadeSignatureKindV2::Class,
            include_builtins: true,
            ..all_without_builtins
        };
        assert_eq!(
            page(&storage, all_without_builtins, false),
            sorted(vec![
                entity_row("class", "urn:builder:A"),
                entity_row("class", "urn:builder:B"),
                entity_row("data_property", "urn:builder:p"),
            ])
        );
        assert_eq!(
            page(&storage, classes_with_builtins, false),
            sorted(vec![
                entity_row("class", "urn:builder:A"),
                entity_row("class", "urn:builder:B"),
                entity_row("class", OWL_THING),
            ])
        );

        let observation = storage.observation_for_tests().expect("observation");
        assert_eq!(observation.arena_fields, 1);
        assert_eq!(observation.retained_canonical_byte_rows, 0);
        let counters = storage.counters().expect("counters");
        assert_eq!(counters.canonical_input_rows, 4);
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(counters.publication_structural_bytes_copied, 0);
    }

    #[test]
    fn retained_closure_signature_is_inside_later_scope_memory_envelope() {
        let source_rows = sorted(
            (0..256)
                .map(|index| declaration("class", &format!("urn:builder:retained:{index:04}")))
                .collect(),
        );
        let later_rows = sorted(vec![
            declaration("class", "urn:builder:later:a"),
            declaration("class", "urn:builder:later:b"),
        ]);
        let default_limits = Limits::default();
        let mut builder = NativeComponentBuilder::new(&default_limits).expect("component builder");
        let source_pending = source_rows
            .iter()
            .map(|row| builder.intern_canonical(row).expect("source axiom"))
            .collect::<Vec<_>>();
        let later_pending = later_rows
            .iter()
            .map(|row| builder.intern_canonical(row).expect("later axiom"))
            .collect::<Vec<_>>();
        let frozen = builder.freeze().expect("component arena");
        let source_roots = source_pending
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("source root"))
            .collect::<Vec<_>>();
        let later_roots = later_pending
            .into_iter()
            .map(|identifier| frozen.resolve(identifier).expect("later root"))
            .collect::<Vec<_>>();
        let arena = frozen.into_arena();
        let source_scope = EffectiveScopeV2 {
            scope: TypedFacadeScopeV2::Document,
            document_ordinal: Some(0),
            roots: [Vec::new(), source_roots.clone(), Vec::new()],
        };
        let later_scope = EffectiveScopeV2 {
            scope: TypedFacadeScopeV2::Document,
            document_ordinal: Some(1),
            roots: [Vec::new(), later_roots.clone(), Vec::new()],
        };
        let closure_scope = EffectiveScopeV2 {
            scope: TypedFacadeScopeV2::Closure,
            document_ordinal: None,
            roots: [Vec::new(), source_roots, Vec::new()],
        };

        let retained_entities = collect_signature(
            &arena,
            source_scope.roots.iter().map(Vec::as_slice),
            &default_limits,
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("source signature");
        let mut retained_tables = Vec::new();
        append_signature_tables(
            &arena,
            &source_scope,
            &retained_entities,
            &mut retained_tables,
        )
        .expect("source signature tables");
        let retained_external = signature_collection_external_bytes(
            0,
            &retained_tables,
            retained_tables.capacity(),
            retained_entities.capacity(),
        )
        .expect("retained signature allocation");
        let mut bounded = default_limits;
        bounded.max_memory_bytes = Some(
            arena
                .counters()
                .retained_bytes
                .checked_add(u64::try_from(retained_external).expect("external bytes"))
                .and_then(|value| value.checked_sub(1))
                .expect("boundary"),
        );

        collect_signature(
            &arena,
            later_scope.roots.iter().map(Vec::as_slice),
            &bounded,
            Cancellation::with_duration(None),
            None,
            0,
        )
        .expect("later signature fits when retained allocations are omitted");
        assert_eq!(
            collect_signature(
                &arena,
                later_scope.roots.iter().map(Vec::as_slice),
                &bounded,
                Cancellation::with_duration(None),
                None,
                retained_external,
            )
            .expect_err("retained signature allocations must be counted")
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        let scopes = [source_scope, later_scope, closure_scope];
        assert_eq!(
            append_scope_signature_tables(
                &arena,
                &scopes,
                &[vec![0], vec![1]],
                &[0],
                &bounded,
                Cancellation::with_duration(None),
                None,
                0,
                &mut Vec::new(),
            )
            .expect_err("production signature collection must use the retained envelope")
            .code,
            "NATIVE_WIRE_LIMIT"
        );
    }

    #[test]
    fn builder_retains_diamond_and_cycle_reachability_without_flattening() {
        let shared = declaration("class", "urn:builder:shared");
        let diamond_documents = [
            sorted(vec![
                declaration("class", "urn:builder:diamond-root"),
                shared.clone(),
            ]),
            sorted(vec![
                declaration("class", "urn:builder:diamond-left"),
                shared.clone(),
            ]),
            sorted(vec![
                declaration("class", "urn:builder:diamond-right"),
                shared.clone(),
            ]),
            sorted(vec![
                declaration("class", "urn:builder:diamond-leaf"),
                shared.clone(),
            ]),
        ];
        let limits = Limits::default();
        let mut diamond =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("diamond builder");
        for document in &diamond_documents {
            diamond
                .add_document(&[], document, &[])
                .expect("diamond document");
        }
        let diamond = diamond
            .freeze(
                &[vec![0, 1, 2, 3], vec![1, 3], vec![2, 3], vec![3]],
                &[0, 1, 2, 3],
            )
            .expect("diamond closure");
        let axiom_coordinate =
            |ordinal| TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, ordinal);
        assert_eq!(
            page(&diamond, axiom_coordinate(0), false),
            sorted_unique(
                diamond_documents
                    .iter()
                    .flat_map(|rows| rows.iter().cloned())
                    .collect(),
            )
        );
        assert_eq!(
            page(&diamond, axiom_coordinate(1), false),
            sorted_unique(
                diamond_documents[1]
                    .iter()
                    .chain(&diamond_documents[3])
                    .cloned()
                    .collect(),
            )
        );
        assert_eq!(
            page(&diamond, axiom_coordinate(2), false),
            sorted_unique(
                diamond_documents[2]
                    .iter()
                    .chain(&diamond_documents[3])
                    .cloned()
                    .collect(),
            )
        );
        assert_eq!(
            page(&diamond, axiom_coordinate(0), true),
            diamond_documents[0]
        );
        assert_eq!(
            page(
                &diamond,
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                false,
            ),
            sorted_unique(
                diamond_documents
                    .iter()
                    .flat_map(|rows| rows.iter().cloned())
                    .collect(),
            )
        );
        let diamond_counters = diamond.counters().expect("diamond counters");
        assert_eq!(diamond_counters.canonical_input_rows, 8);
        assert_eq!(diamond_counters.publication_structural_rows_copied, 0);
        assert_eq!(diamond_counters.publication_structural_bytes_copied, 0);

        let cycle_documents = [
            sorted(vec![
                declaration("class", "urn:builder:cycle-a"),
                shared.clone(),
            ]),
            sorted(vec![declaration("class", "urn:builder:cycle-b"), shared]),
        ];
        let mut cycle =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("cycle builder");
        for document in &cycle_documents {
            cycle
                .add_document(&[], document, &[])
                .expect("cycle document");
        }
        let cycle = cycle
            .freeze(&[vec![0, 1], vec![0, 1]], &[0, 1])
            .expect("legal import cycle");
        let expected_cycle = sorted_unique(
            cycle_documents
                .iter()
                .flat_map(|rows| rows.iter().cloned())
                .collect(),
        );
        assert_eq!(page(&cycle, axiom_coordinate(0), false), expected_cycle);
        assert_eq!(page(&cycle, axiom_coordinate(1), false), expected_cycle);
        assert_eq!(page(&cycle, axiom_coordinate(0), true), cycle_documents[0]);
        assert_eq!(page(&cycle, axiom_coordinate(1), true), cycle_documents[1]);
        let cycle_counters = cycle.counters().expect("cycle counters");
        assert_eq!(cycle_counters.canonical_input_rows, 4);
        assert_eq!(cycle_counters.publication_structural_rows_copied, 0);
        assert_eq!(cycle_counters.publication_structural_bytes_copied, 0);
    }

    #[test]
    fn builder_composes_single_document_native_owners_without_reinterning() {
        let shared = declaration("class", "urn:builder:native-shared");
        let documents = [
            sorted(vec![
                declaration("class", "urn:builder:native-root"),
                shared.clone(),
            ]),
            sorted(vec![
                declaration("class", "urn:builder:native-child"),
                shared,
            ]),
        ];
        let limits = Limits::default();
        let mut sources = Vec::new();
        for rows in &documents {
            let mut builder =
                TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                    .expect("source builder");
            builder
                .add_document(&[], rows, &[])
                .expect("source document");
            sources.push(builder.freeze(&[vec![0]], &[0]).expect("source storage"));
        }
        let source_arenas = sources
            .iter()
            .map(|source| source.arena().clone())
            .collect::<Vec<_>>();
        let source_unique_nodes = sources
            .iter()
            .map(|source| {
                source
                    .counters()
                    .expect("source counters")
                    .component
                    .unique_nodes
            })
            .sum::<u64>();
        let source_component_bytes = sources
            .iter()
            .map(|source| {
                source
                    .counters()
                    .expect("source counters")
                    .retained_component_bytes
            })
            .sum::<u64>();

        let merged = TypedFacadeBuilderV2::compose_native_documents(
            sources,
            &[vec![0], vec![1]],
            &[0, 1],
            &[None, None],
            limits,
            Cancellation::with_duration(None),
            None,
            0,
            0,
        )
        .expect("composite closure");
        assert_eq!(
            page(
                &merged,
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                false,
            ),
            sorted_unique(documents.iter().flatten().cloned().collect()),
        );
        assert_eq!(
            page(
                &merged,
                TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0),
                true,
            ),
            documents[0],
        );
        assert_eq!(
            page(
                &merged,
                TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 1),
                true,
            ),
            documents[1],
        );
        let counters = merged.counters().expect("merged counters");
        assert_eq!(counters.retained_document_tables, 2);
        assert_eq!(counters.canonical_input_rows, 4);
        assert_eq!(counters.component.unique_nodes, source_unique_nodes);
        assert_eq!(
            counters.retained_component_bytes,
            source_component_bytes + merged.arena().partition_manifest_bytes()
        );
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(merged.arena().partition_count(), 2);
        assert!(source_arenas
            .iter()
            .all(|source| merged.arena().shares_storage_with(source)));
    }

    #[test]
    fn builder_composes_one_native_owner_and_still_rejects_empty_input() {
        let rows = sorted(vec![
            declaration("class", "urn:builder:one-root"),
            declaration("class", "urn:builder:one-child"),
        ]);
        let limits = Limits::default();
        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("source builder");
        builder
            .add_document(&[], &rows, &[])
            .expect("source document");
        let source = builder.freeze(&[vec![0]], &[0]).expect("source storage");
        let source_arena = source.arena().clone();
        let source_counters = source.counters().expect("source counters");

        let merged = TypedFacadeBuilderV2::compose_native_documents(
            vec![source],
            &[vec![0]],
            &[0],
            &[None],
            limits,
            Cancellation::with_duration(None),
            None,
            0,
            0,
        )
        .expect("one-owner closure");
        let document = TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, 0);
        let closure = TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms);
        assert_eq!(page(&merged, document, false), rows);
        assert_eq!(page(&merged, document, true), rows);
        assert_eq!(page(&merged, closure, false), rows);
        let counters = merged.counters().expect("merged counters");
        assert_eq!(counters.retained_document_tables, 1);
        assert_eq!(
            counters.canonical_input_rows,
            source_counters.canonical_input_rows
        );
        assert_eq!(
            counters.component.unique_nodes,
            source_counters.component.unique_nodes
        );
        assert_eq!(
            counters.retained_component_bytes,
            source_counters.retained_component_bytes + merged.arena().partition_manifest_bytes()
        );
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(counters.publication_structural_bytes_copied, 0);
        assert_eq!(merged.arena().partition_count(), 1);
        assert!(merged.arena().shares_storage_with(&source_arena));

        let empty = TypedFacadeBuilderV2::compose_native_documents(
            Vec::new(),
            &[],
            &[],
            &[],
            limits,
            Cancellation::with_duration(None),
            None,
            0,
            0,
        )
        .expect_err("empty parser-owner closure must fail");
        assert_eq!(
            empty,
            NativeError::protocol("native closure composition requires at least one parser owner")
        );
    }

    #[test]
    fn builder_composes_scoped_native_owners_without_reinterning() {
        let raw_documents = [
            sorted(vec![subclass("urn:builder:raw-root", "urn:builder:shared")]),
            sorted(vec![subclass(
                "urn:builder:raw-child",
                "urn:builder:shared",
            )]),
        ];
        let effective_documents = [
            sorted(vec![subclass(
                "urn:builder:effective-root",
                "urn:builder:shared",
            )]),
            sorted(vec![subclass(
                "urn:builder:effective-child",
                "urn:builder:shared",
            )]),
        ];
        let limits = Limits::default();
        let mut sources = Vec::new();
        for (raw, effective) in raw_documents.iter().zip(&effective_documents) {
            let mut builder =
                TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                    .expect("source builder");
            builder
                .add_scoped_document(&[], raw, &[], &[], effective, &[])
                .expect("scoped source document");
            sources.push(builder.freeze(&[vec![0]], &[0]).expect("source storage"));
        }
        let source_arenas = sources
            .iter()
            .map(|source| source.arena().clone())
            .collect::<Vec<_>>();
        let source_unique_nodes = sources
            .iter()
            .map(|source| {
                source
                    .counters()
                    .expect("source counters")
                    .component
                    .unique_nodes
            })
            .sum::<u64>();

        let merged = TypedFacadeBuilderV2::compose_native_documents(
            sources,
            &[vec![0], vec![1]],
            &[0, 1],
            &[None, None],
            limits,
            Cancellation::with_duration(None),
            None,
            0,
            0,
        )
        .expect("scoped composite closure");
        for ordinal in 0..2 {
            let coordinate =
                TypedFacadeCoordinateV2::document(TypedFacadeCollectionV2::Axioms, ordinal);
            assert_eq!(
                page(&merged, coordinate, true),
                raw_documents[ordinal as usize]
            );
            assert_eq!(
                page(&merged, coordinate, false),
                effective_documents[ordinal as usize]
            );
        }
        assert_eq!(
            page(
                &merged,
                TypedFacadeCoordinateV2::closure(TypedFacadeCollectionV2::Axioms),
                false,
            ),
            sorted_unique(effective_documents.iter().flatten().cloned().collect()),
        );
        let counters = merged.counters().expect("merged counters");
        assert_eq!(counters.canonical_input_rows, 2);
        assert_eq!(counters.component.unique_nodes, source_unique_nodes);
        assert_eq!(counters.publication_structural_rows_copied, 0);
        assert_eq!(counters.publication_structural_bytes_copied, 0);
        assert_eq!(merged.arena().partition_count(), 2);
        assert!(source_arenas
            .iter()
            .all(|source| merged.arena().shares_storage_with(source)));
        let columns = merged
            .encoded_structural_columns(
                TypedFacadeScopeV2::Closure,
                None,
                false,
                &limits,
                Cancellation::with_duration(None),
                None,
            )
            .expect("composite columns deduplicate shared child nodes");
        assert_eq!(columns.counters().root_rows, 2);
        assert_eq!(columns.counters().node_rows, 8);
        assert_eq!(encoded_root_kinds(&columns), [2, 2]);
    }

    #[test]
    fn builder_rejects_partition_order_and_reachability_drift() {
        let a = declaration("class", "urn:builder:A");
        let b = declaration("class", "urn:builder:B");
        let mut reversed = sorted(vec![a.clone(), b.clone()]);
        reversed.reverse();
        let limits = Limits::default();
        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("builder");
        assert_eq!(
            builder
                .add_document(&[], &reversed, &[])
                .expect_err("reversed roots"),
            NativeError::protocol("typed V2 input roots are not canonical ascending unique")
        );
        assert_prevalidation_left_builder_unmutated(&builder);
        assert_eq!(
            builder
                .add_document(&[], std::slice::from_ref(&a), &[])
                .expect_err("poisoned builder"),
            NativeError::protocol("typed V2 builder is poisoned after a failed mutation")
        );
        assert!(builder.freeze(&[vec![0]], &[0]).is_err());

        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("builder");
        assert!(builder
            .add_document(std::slice::from_ref(&a), &[], &[])
            .is_err());
        assert!(builder
            .add_document(&[], std::slice::from_ref(&a), &[])
            .is_err());

        for (effective, closure) in [
            (vec![vec![]], vec![0]),
            (vec![vec![1]], vec![0]),
            (vec![vec![0]], vec![]),
        ] {
            let mut builder =
                TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                    .expect("builder");
            builder
                .add_document(&[], std::slice::from_ref(&a), &[])
                .expect("document");
            assert!(builder.freeze(&effective, &closure).is_err());
        }
    }

    #[test]
    fn builder_prevalidates_late_wrong_category_and_corruption_before_mutation() {
        let raw = sorted(vec![declaration("class", "urn:builder:raw")]);
        let wrong_category = vec![entity_row("class", "urn:builder:not-an-axiom")];
        let limits = Limits::default();
        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("wrong-category builder");
        assert_eq!(
            builder
                .add_scoped_document(&[], &raw, &[], &[], &wrong_category, &[],)
                .expect_err("late effective category mismatch"),
            NativeError::protocol("typed V2 input root is in the wrong structural collection")
        );
        assert_prevalidation_left_builder_unmutated(&builder);
        assert_eq!(
            builder
                .add_document(&[], &raw, &[])
                .expect_err("wrong-category failure poisons builder"),
            NativeError::protocol("typed V2 builder is poisoned after a failed mutation")
        );

        let malformed = vec![vec![0x80]];
        let mut builder =
            TypedFacadeBuilderV2::new(limits, Cancellation::with_duration(None), None, 0)
                .expect("corrupt-row builder");
        let failure = builder
            .add_scoped_document(&[], &raw, &[], &[], &malformed, &[])
            .expect_err("late malformed effective row");
        assert_eq!(failure.code, "NATIVE_WIRE_CORRUPTION");
        assert_prevalidation_left_builder_unmutated(&builder);
        assert_eq!(
            builder
                .add_document(&[], &raw, &[])
                .expect_err("corrupt-row failure poisons builder"),
            NativeError::protocol("typed V2 builder is poisoned after a failed mutation")
        );
    }

    #[test]
    fn builder_observes_cancellation_before_mutation() {
        let mut builder = TypedFacadeBuilderV2::new(
            Limits::default(),
            Cancellation::with_duration(Some(Duration::ZERO)),
            None,
            0,
        )
        .expect("builder");
        let expired_error = builder
            .add_document(&[], &[declaration("class", "urn:builder:A")], &[])
            .expect_err("expired deadline");
        assert_eq!(expired_error.code, "NATIVE_DEADLINE");
        assert_prevalidation_left_builder_unmutated(&builder);
        assert_eq!(
            builder
                .add_document(&[], &[declaration("class", "urn:builder:B")], &[])
                .expect_err("deadline failure poisons builder"),
            NativeError::protocol("typed V2 builder is poisoned after a failed mutation")
        );
    }
}
