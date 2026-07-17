//! Complete advertised native parser implementations and result framing.

mod functional;

use crate::canonical::{encode_frame, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::model::{scan_canonical, Category, ScanBudget};
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
}

impl ParsedDocument {
    pub(crate) fn encode(self, session: &mut Session<'_>) -> NativeResult<Vec<u8>> {
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
        encode_optional(self.ontology_iri, &mut output)?;
        encode_optional(self.version_iri, &mut output)?;
        encode_nodes(self.imports, &mut output)?;
        encode_spanned(self.annotations, &mut output)?;
        encode_spanned(self.axioms, &mut output)?;
        encode_spanned(self.extensions, &mut output)?;
        output.extend_from_slice(
            &u64::try_from(self.prefixes.len())
                .map_err(|_| NativeError::limit("native prefix count exceeds u64"))?
                .to_le_bytes(),
        );
        for (prefix, iri) in self.prefixes {
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
    functional::parse_functional(request.source, request.allow_swrl, session)?.encode(session)
}

fn encode_optional(value: Option<Node>, output: &mut Vec<u8>) -> NativeResult<()> {
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

fn encode_nodes(values: Vec<Node>, output: &mut Vec<u8>) -> NativeResult<()> {
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

fn encode_spanned(values: Vec<SpannedNode>, output: &mut Vec<u8>) -> NativeResult<()> {
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
