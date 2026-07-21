//! Complete advertised native parser implementations and result framing.

#[cfg(not(fuzzing))]
mod anonymous;
mod functional;
#[cfg(not(fuzzing))]
mod retained;

#[cfg(not(fuzzing))]
pub(crate) use anonymous::{scope_rdfxml_anonymous_rows_v2, ScopedAnonymousRowsV2};
#[cfg(not(fuzzing))]
pub(crate) use retained::{
    build_rdfxml_seed as build_retained_rdfxml_seed_v2,
    contains_anonymous_rows as retained_rows_contain_anonymous_v2,
    prepare_publication as prepare_retained_publication_v2, PreparedRetainedPublicationV2,
    RetainedParseMetadataV2,
};

#[cfg(not(fuzzing))]
use std::mem::size_of;
#[cfg(not(fuzzing))]
use std::time::Instant;

#[cfg(not(fuzzing))]
use crate::cancel::{Cancellation, InterruptSlot};
use crate::canonical::{encode_frame, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::{LimitKey, Limits};
use crate::model::{scan_canonical, Category, ScanBudget};
#[cfg(not(fuzzing))]
use crate::publication::{TypedFacadeBuilderV2, TypedFacadeStorageV2};
use crate::session::Session;
use crate::source::SourceRequest;

const RESULT_MAGIC: &[u8; 8] = b"PYNFSSR1";
const RESULT_SCHEMA: u16 = 1;
const FORMAT_FUNCTIONAL: u16 = 4;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Span {
    pub(crate) byte_start: u64,
    pub(crate) byte_end: u64,
    pub(crate) line: u64,
    pub(crate) column: u64,
}

#[derive(Clone, Debug)]
pub(crate) struct SpannedNode {
    pub(crate) node: Node,
    pub(crate) span: Span,
}

#[derive(Clone, Debug)]
pub(crate) struct ParsedDocument {
    pub(crate) ontology_iri: Option<Node>,
    pub(crate) version_iri: Option<Node>,
    pub(crate) imports: Vec<Node>,
    pub(crate) annotations: Vec<SpannedNode>,
    pub(crate) axioms: Vec<SpannedNode>,
    pub(crate) extensions: Vec<SpannedNode>,
    pub(crate) prefixes: Vec<(String, String)>,
    pub(crate) decoded_codepoints: u64,
    pub(crate) language_spellings: Vec<String>,
}

#[cfg(not(fuzzing))]
pub(crate) struct RetainedParseOutcome {
    pub(crate) encoded: Vec<u8>,
    pub(crate) storage: TypedFacadeStorageV2,
    pub(crate) metadata: Option<RetainedParseMetadataV2>,
    pub(crate) phases: RetainedParsePhases,
}

#[cfg(not(fuzzing))]
#[derive(Clone, Copy, Debug)]
pub(crate) struct RetainedParsePhases {
    pub(crate) syntax_parse_ns: u64,
    pub(crate) result_encode_ns: u64,
    pub(crate) arena_construction_ns: u64,
    pub(crate) freeze_ns: u64,
}

impl ParsedDocument {
    pub(crate) fn encode(&self, session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
        self.validate(session)?;
        let output_size = self.encoded_size()?;
        if u64::try_from(output_size).map_or(true, |value| {
            value > session.limits().value(LimitKey::MaxTemporaryBytes)
        }) {
            return Err(NativeError::limit(
                "native parser result exceeds max_temporary_bytes",
            ));
        }
        session.reserve_bytes(output_size)?;
        let mut output = Vec::new();
        output
            .try_reserve_exact(output_size)
            .map_err(|_| NativeError::limit("native parser result allocation failed"))?;
        output.extend_from_slice(RESULT_MAGIC);
        output.extend_from_slice(&RESULT_SCHEMA.to_le_bytes());
        output.extend_from_slice(&FORMAT_FUNCTIONAL.to_le_bytes());
        output.extend_from_slice(&self.decoded_codepoints.to_le_bytes());
        encode_optional(self.ontology_iri.as_ref(), &mut output)?;
        encode_optional(self.version_iri.as_ref(), &mut output)?;
        encode_nodes(&self.imports, &mut output)?;
        encode_spanned(&self.annotations, &mut output)?;
        encode_spanned(&self.axioms, &mut output)?;
        encode_spanned(&self.extensions, &mut output)?;
        output.extend_from_slice(
            &u64::try_from(self.prefixes.len())
                .map_err(|_| NativeError::limit("native prefix count exceeds u64"))?
                .to_le_bytes(),
        );
        for (prefix, iri) in &self.prefixes {
            encode_frame(prefix.as_bytes(), &mut output)?;
            encode_frame(iri.as_bytes(), &mut output)?;
        }
        if output.len() != output_size {
            return Err(NativeError::protocol(
                "native parser result size ledger diverged",
            ));
        }
        session.finish()?;
        Ok(output)
    }

    #[cfg(not(fuzzing))]
    fn into_structural_rows(self) -> [Vec<Vec<u8>>; 3] {
        [
            canonical_root_rows(self.annotations),
            canonical_root_rows(self.axioms),
            canonical_root_rows(self.extensions),
        ]
    }

    fn validate(&self, session: &mut Session<'_>) -> NativeResult<()> {
        let mut budget = ScanBudget::from_limits(session.limits());
        for value in [self.ontology_iri.as_ref(), self.version_iri.as_ref()]
            .into_iter()
            .flatten()
            .chain(self.imports.iter())
        {
            validate_category(value, Category::Iri, &mut budget)?;
        }
        for value in &self.annotations {
            validate_category(&value.node, Category::Annotation, &mut budget)?;
        }
        for value in &self.axioms {
            validate_category(&value.node, Category::Axiom, &mut budget)?;
        }
        for value in &self.extensions {
            validate_category(&value.node, Category::Swrl, &mut budget)?;
        }
        let mut retained = [self.ontology_iri.as_ref(), self.version_iri.as_ref()]
            .into_iter()
            .flatten()
            .chain(self.imports.iter())
            .map(|value| value.as_bytes().len())
            .chain(
                self.annotations
                    .iter()
                    .map(|value| value.node.as_bytes().len()),
            )
            .chain(self.axioms.iter().map(|value| value.node.as_bytes().len()))
            .chain(
                self.extensions
                    .iter()
                    .map(|value| value.node.as_bytes().len()),
            )
            .try_fold(0_usize, |total, size| total.checked_add(size))
            .ok_or_else(|| NativeError::limit("native parser model size overflow"))?;
        for (prefix, iri) in &self.prefixes {
            retained = checked_add(retained, prefix.len())?;
            retained = checked_add(retained, iri.len())?;
        }
        session.reserve_bytes(retained)?;
        Ok(())
    }

    fn encoded_size(&self) -> NativeResult<usize> {
        let mut size = 20_usize;
        for value in [self.ontology_iri.as_ref(), self.version_iri.as_ref()] {
            size = checked_add(size, 1)?;
            if let Some(node) = value {
                size = checked_add(size, frame_size(node.as_bytes().len())?)?;
            }
        }
        size = checked_add(size, 8)?;
        for value in &self.imports {
            size = checked_add(size, frame_size(value.as_bytes().len())?)?;
        }
        for values in [&self.annotations, &self.axioms, &self.extensions] {
            size = checked_add(size, 8)?;
            for value in values {
                size = checked_add(size, 32)?;
                size = checked_add(size, frame_size(value.node.as_bytes().len())?)?;
            }
        }
        size = checked_add(size, 8)?;
        for (prefix, iri) in &self.prefixes {
            size = checked_add(size, frame_size(prefix.len())?)?;
            size = checked_add(size, frame_size(iri.len())?)?;
        }
        Ok(size)
    }
}

pub(crate) fn parse(
    request: SourceRequest<'_>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    functional::parse_functional(request.source, request.allow_swrl, false, session)?
        .encode(session)
}

#[cfg(not(fuzzing))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn parse_retained(
    request: SourceRequest<'_>,
    session: &mut Session<'_>,
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    input_bytes: usize,
    collect_provenance: bool,
    preserve_source_map: bool,
    record_unresolved: bool,
    require_empty_imports: bool,
) -> NativeResult<RetainedParseOutcome> {
    let parse_started = Instant::now();
    let parsed = functional::parse_functional(
        request.source,
        request.allow_swrl,
        preserve_source_map,
        session,
    )?;
    let syntax_parse_ns = elapsed_ns(parse_started)?;

    let import_diagnostics_exceed_publication_limit = record_unresolved
        && u64::try_from(parsed.imports.len()).map_or(true, |count| {
            count > limits.value(LimitKey::MaxDiagnostics) / 2
        });
    let contains_anonymous = retained::contains_anonymous(&parsed, &limits)?;
    let requires_full_result = import_diagnostics_exceed_publication_limit
        || (require_empty_imports && !parsed.imports.is_empty());
    let encode_started = Instant::now();
    let (encoded, metadata, rows, effective_rows) = if requires_full_result {
        let encoded = parsed.encode(session)?;
        let rows = parsed.into_structural_rows();
        (encoded, None, rows, None)
    } else {
        parsed.validate(session)?;
        let (encoded, metadata, rows, effective_rows) = retained::build_seed(
            parsed,
            collect_provenance,
            preserve_source_map,
            &limits,
            &cancellation,
            session,
            contains_anonymous,
        )?;
        session.finish()?;
        (encoded, Some(metadata), rows, effective_rows)
    };
    let result_encode_ns = elapsed_ns(encode_started)?;
    let metadata_bytes = metadata
        .as_ref()
        .map_or(Ok(0), RetainedParseMetadataV2::retained_bytes)?;
    let external_bytes = retained_parse_external_bytes(
        input_bytes,
        encoded.capacity(),
        metadata_bytes,
        &rows,
        effective_rows.as_ref(),
    )?;

    let arena_started = Instant::now();
    let mut builder = TypedFacadeBuilderV2::new(limits, cancellation, interrupt, external_bytes)?;
    if let Some(effective) = &effective_rows {
        builder.add_scoped_document(
            &rows[0],
            &rows[1],
            &rows[2],
            &effective[0],
            &effective[1],
            &effective[2],
        )?;
    } else {
        builder.add_document(&rows[0], &rows[1], &rows[2])?;
    }
    let arena_construction_ns = elapsed_ns(arena_started)?;

    let freeze_started = Instant::now();
    let storage = builder.freeze(&[vec![0]], &[0])?;
    let freeze_ns = elapsed_ns(freeze_started)?;
    Ok(RetainedParseOutcome {
        encoded,
        storage,
        metadata,
        phases: RetainedParsePhases {
            syntax_parse_ns,
            result_encode_ns,
            arena_construction_ns,
            freeze_ns,
        },
    })
}

fn encode_optional(value: Option<&Node>, output: &mut Vec<u8>) -> NativeResult<()> {
    match value {
        Some(node) => {
            output.push(1);
            encode_frame(node.as_bytes(), output)
        }
        None => {
            output.push(0);
            Ok(())
        }
    }
}

fn encode_nodes(values: &[Node], output: &mut Vec<u8>) -> NativeResult<()> {
    output.extend_from_slice(
        &u64::try_from(values.len())
            .map_err(|_| NativeError::limit("native parser row count exceeds u64"))?
            .to_le_bytes(),
    );
    for value in values {
        encode_frame(value.as_bytes(), output)?;
    }
    Ok(())
}

fn encode_spanned(values: &[SpannedNode], output: &mut Vec<u8>) -> NativeResult<()> {
    output.extend_from_slice(
        &u64::try_from(values.len())
            .map_err(|_| NativeError::limit("native parser row count exceeds u64"))?
            .to_le_bytes(),
    );
    for value in values {
        output.extend_from_slice(&value.span.byte_start.to_le_bytes());
        output.extend_from_slice(&value.span.byte_end.to_le_bytes());
        output.extend_from_slice(&value.span.line.to_le_bytes());
        output.extend_from_slice(&value.span.column.to_le_bytes());
        encode_frame(value.node.as_bytes(), output)?;
    }
    Ok(())
}

#[cfg(not(fuzzing))]
fn canonical_root_rows(values: Vec<SpannedNode>) -> Vec<Vec<u8>> {
    let mut rows: Vec<Vec<u8>> = values
        .into_iter()
        .map(|value| value.node.into_bytes())
        .collect();
    rows.sort_unstable();
    rows.dedup();
    rows
}

#[cfg(not(fuzzing))]
fn retained_parse_external_bytes(
    input_bytes: usize,
    encoded_capacity: usize,
    metadata_bytes: usize,
    rows: &[Vec<Vec<u8>>; 3],
    effective_rows: Option<&[Vec<Vec<u8>>; 3]>,
) -> NativeResult<usize> {
    let mut total = checked_add(input_bytes, encoded_capacity)?;
    total = checked_add(total, metadata_bytes)?;
    for collection in rows.iter().chain(effective_rows.into_iter().flatten()) {
        total = checked_add(
            total,
            collection
                .capacity()
                .checked_mul(size_of::<Vec<u8>>())
                .ok_or_else(|| NativeError::limit("native retained parser metadata overflow"))?,
        )?;
        for row in collection {
            total = checked_add(total, row.capacity())?;
        }
    }
    Ok(total)
}

#[cfg(not(fuzzing))]
fn elapsed_ns(started: Instant) -> NativeResult<u64> {
    u64::try_from(started.elapsed().as_nanos())
        .map_err(|_| NativeError::limit("native parser phase duration exceeds u64"))
}

fn validate_category(node: &Node, expected: Category, budget: &mut ScanBudget) -> NativeResult<()> {
    let category = match scan_canonical(node.as_bytes(), budget) {
        Ok(value) => value,
        Err(error) if error.code == "NATIVE_WIRE_CORRUPTION" => {
            return Err(NativeError::new(
                "NATIVE_FORMAT_SYNTAX",
                "native Functional Syntax value violates the structural model",
            ));
        }
        Err(error) => return Err(error),
    };
    if category != expected {
        return Err(NativeError::protocol(
            "native parser produced an invalid result partition",
        ));
    }
    Ok(())
}

fn frame_size(size: usize) -> NativeResult<usize> {
    checked_add(varint_size(size), size)
}

fn varint_size(mut value: usize) -> usize {
    let mut size = 1;
    while value >= 0x80 {
        value >>= 7;
        size += 1;
    }
    size
}

fn checked_add(left: usize, right: usize) -> NativeResult<usize> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("native parser result size overflow"))
}
