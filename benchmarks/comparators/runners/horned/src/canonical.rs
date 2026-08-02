//! Minimal canonical structural encoder used only by the comparator adapter.
//!
//! This deliberately mirrors the public, language-neutral pyowl-core
//! canonical contract without depending on pyowl-core's runtime crate.  The
//! Horned comparator remains an independently built development tool.

use std::collections::BTreeSet;

use sha2::{Digest, Sha256};

use crate::RunnerError;

const NONE: u8 = 0;
const NODE: u8 = 1;
const TEXT: u8 = 2;
const BYTES: u8 = 3;
const INTEGER: u8 = 4;
const ENUM: u8 = 5;
const SET: u8 = 6;
const SEQUENCE: u8 = 7;

pub(crate) const IRI: u64 = 1;
pub(crate) const ENTITY: u64 = 2;
pub(crate) const ANONYMOUS_INDIVIDUAL: u64 = 3;
pub(crate) const LITERAL: u64 = 4;
pub(crate) const ANNOTATION: u64 = 5;
pub(crate) const OBJECT_INVERSE_OF: u64 = 10;
pub(crate) const OBJECT_PROPERTY_CHAIN: u64 = 11;
pub(crate) const FACET_RESTRICTION: u64 = 20;
pub(crate) const DATA_INTERSECTION_OF: u64 = 21;
pub(crate) const DATA_UNION_OF: u64 = 22;
pub(crate) const DATA_COMPLEMENT_OF: u64 = 23;
pub(crate) const DATA_ONE_OF: u64 = 24;
pub(crate) const DATATYPE_RESTRICTION: u64 = 25;
pub(crate) const OBJECT_INTERSECTION_OF: u64 = 30;
pub(crate) const OBJECT_UNION_OF: u64 = 31;
pub(crate) const OBJECT_COMPLEMENT_OF: u64 = 32;
pub(crate) const OBJECT_ONE_OF: u64 = 33;
pub(crate) const OBJECT_SOME_VALUES_FROM: u64 = 34;
pub(crate) const OBJECT_ALL_VALUES_FROM: u64 = 35;
pub(crate) const OBJECT_HAS_VALUE: u64 = 36;
pub(crate) const OBJECT_HAS_SELF: u64 = 37;
pub(crate) const OBJECT_MIN_CARDINALITY: u64 = 38;
pub(crate) const OBJECT_MAX_CARDINALITY: u64 = 39;
pub(crate) const OBJECT_EXACT_CARDINALITY: u64 = 40;
pub(crate) const DATA_SOME_VALUES_FROM: u64 = 41;
pub(crate) const DATA_ALL_VALUES_FROM: u64 = 42;
pub(crate) const DATA_HAS_VALUE: u64 = 43;
pub(crate) const DATA_MIN_CARDINALITY: u64 = 44;
pub(crate) const DATA_MAX_CARDINALITY: u64 = 45;
pub(crate) const DATA_EXACT_CARDINALITY: u64 = 46;
pub(crate) const DECLARATION: u64 = 60;
pub(crate) const SUB_CLASS_OF: u64 = 61;
pub(crate) const EQUIVALENT_CLASSES: u64 = 62;
pub(crate) const DISJOINT_CLASSES: u64 = 63;
pub(crate) const DISJOINT_UNION: u64 = 64;
pub(crate) const SUB_OBJECT_PROPERTY_OF: u64 = 70;
pub(crate) const EQUIVALENT_OBJECT_PROPERTIES: u64 = 71;
pub(crate) const DISJOINT_OBJECT_PROPERTIES: u64 = 72;
pub(crate) const INVERSE_OBJECT_PROPERTIES: u64 = 73;
pub(crate) const OBJECT_PROPERTY_DOMAIN: u64 = 74;
pub(crate) const OBJECT_PROPERTY_RANGE: u64 = 75;
pub(crate) const FUNCTIONAL_OBJECT_PROPERTY: u64 = 76;
pub(crate) const INVERSE_FUNCTIONAL_OBJECT_PROPERTY: u64 = 77;
pub(crate) const REFLEXIVE_OBJECT_PROPERTY: u64 = 78;
pub(crate) const IRREFLEXIVE_OBJECT_PROPERTY: u64 = 79;
pub(crate) const SYMMETRIC_OBJECT_PROPERTY: u64 = 80;
pub(crate) const ASYMMETRIC_OBJECT_PROPERTY: u64 = 81;
pub(crate) const TRANSITIVE_OBJECT_PROPERTY: u64 = 82;
pub(crate) const SUB_DATA_PROPERTY_OF: u64 = 90;
pub(crate) const EQUIVALENT_DATA_PROPERTIES: u64 = 91;
pub(crate) const DISJOINT_DATA_PROPERTIES: u64 = 92;
pub(crate) const DATA_PROPERTY_DOMAIN: u64 = 93;
pub(crate) const DATA_PROPERTY_RANGE: u64 = 94;
pub(crate) const FUNCTIONAL_DATA_PROPERTY: u64 = 95;
pub(crate) const DATATYPE_DEFINITION: u64 = 100;
pub(crate) const HAS_KEY: u64 = 101;
pub(crate) const SAME_INDIVIDUAL: u64 = 110;
pub(crate) const DIFFERENT_INDIVIDUALS: u64 = 111;
pub(crate) const CLASS_ASSERTION: u64 = 112;
pub(crate) const OBJECT_PROPERTY_ASSERTION: u64 = 113;
pub(crate) const NEGATIVE_OBJECT_PROPERTY_ASSERTION: u64 = 114;
pub(crate) const DATA_PROPERTY_ASSERTION: u64 = 115;
pub(crate) const NEGATIVE_DATA_PROPERTY_ASSERTION: u64 = 116;
pub(crate) const ANNOTATION_ASSERTION: u64 = 120;
pub(crate) const SUB_ANNOTATION_PROPERTY_OF: u64 = 121;
pub(crate) const ANNOTATION_PROPERTY_DOMAIN: u64 = 122;
pub(crate) const ANNOTATION_PROPERTY_RANGE: u64 = 123;
pub(crate) const VARIABLE: u64 = 140;
pub(crate) const CLASS_ATOM: u64 = 141;
pub(crate) const DATA_RANGE_ATOM: u64 = 142;
pub(crate) const OBJECT_PROPERTY_ATOM: u64 = 143;
pub(crate) const DATA_PROPERTY_ATOM: u64 = 144;
pub(crate) const BUILT_IN_ATOM: u64 = 145;
pub(crate) const SAME_INDIVIDUAL_ATOM: u64 = 146;
pub(crate) const DIFFERENT_INDIVIDUALS_ATOM: u64 = 147;
pub(crate) const SWRL_RULE: u64 = 148;

#[derive(Clone, Debug)]
pub(crate) enum Field {
    None,
    Node(Vec<u8>),
    Text(String),
    Bytes(Vec<u8>),
    Integer(u64),
    Enum(String),
    Set(Vec<Vec<u8>>),
    Sequence(Vec<Vec<u8>>),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ParsedNode {
    pub(crate) tag: u64,
    pub(crate) fields: Vec<ParsedField>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum ParsedField {
    None,
    Node(ParsedNode),
    Text(String),
    Bytes(Vec<u8>),
    Integer(u64),
    Enum(String),
    Set(Vec<ParsedNode>),
    Sequence(Vec<ParsedNode>),
}

impl ParsedNode {
    pub(crate) fn encode(&self) -> Result<Vec<u8>, RunnerError> {
        let fields = self
            .fields
            .iter()
            .cloned()
            .map(ParsedField::into_field)
            .collect::<Result<Vec<_>, _>>()?;
        node(self.tag, fields)
    }

    pub(crate) fn contains_tag(&self, tag: u64) -> bool {
        self.tag == tag
            || self.fields.iter().any(|field| match field {
                ParsedField::Node(value) => value.contains_tag(tag),
                ParsedField::Set(values) | ParsedField::Sequence(values) => {
                    values.iter().any(|value| value.contains_tag(tag))
                }
                _ => false,
            })
    }
}

impl ParsedField {
    fn into_field(self) -> Result<Field, RunnerError> {
        Ok(match self {
            Self::None => Field::None,
            Self::Node(value) => Field::Node(value.encode()?),
            Self::Text(value) => Field::Text(value),
            Self::Bytes(value) => Field::Bytes(value),
            Self::Integer(value) => Field::Integer(value),
            Self::Enum(value) => Field::Enum(value),
            Self::Set(values) => Field::Set(
                values
                    .into_iter()
                    .map(|value| value.encode())
                    .collect::<Result<Vec<_>, _>>()?,
            ),
            Self::Sequence(values) => Field::Sequence(
                values
                    .into_iter()
                    .map(|value| value.encode())
                    .collect::<Result<Vec<_>, _>>()?,
            ),
        })
    }
}

pub(crate) fn encode_varint(mut value: u64) -> Vec<u8> {
    let mut encoded = Vec::with_capacity(10);
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        encoded.push(byte | if value == 0 { 0 } else { 0x80 });
        if value == 0 {
            return encoded;
        }
    }
}

pub(crate) fn frame(value: &[u8]) -> Result<Vec<u8>, RunnerError> {
    let length = u64::try_from(value.len())
        .map_err(|_| RunnerError::new("canonical frame length exceeds u64"))?;
    let mut output = encode_varint(length);
    output.extend_from_slice(value);
    Ok(output)
}

pub(crate) fn normalize_set(values: impl IntoIterator<Item = Vec<u8>>) -> Vec<Vec<u8>> {
    values
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

pub(crate) fn node(
    tag: u64,
    fields: impl IntoIterator<Item = Field>,
) -> Result<Vec<u8>, RunnerError> {
    let mut output = encode_varint(tag);
    for field in fields {
        match field {
            Field::None => output.push(NONE),
            Field::Node(value) => {
                output.push(NODE);
                output.extend(frame(&value)?);
            }
            Field::Text(value) => {
                output.push(TEXT);
                output.extend(frame(value.as_bytes())?);
            }
            Field::Bytes(value) => {
                output.push(BYTES);
                output.extend(frame(&value)?);
            }
            Field::Integer(value) => {
                output.push(INTEGER);
                output.extend(encode_varint(value));
            }
            Field::Enum(value) => {
                output.push(ENUM);
                output.extend(frame(value.as_bytes())?);
            }
            Field::Set(values) => {
                let values = normalize_set(values);
                output.push(SET);
                output.extend(encode_varint(u64::try_from(values.len()).map_err(
                    |_| RunnerError::new("canonical set cardinality exceeds u64"),
                )?));
                for value in values {
                    output.extend(frame(&value)?);
                }
            }
            Field::Sequence(values) => {
                output.push(SEQUENCE);
                output.extend(encode_varint(u64::try_from(values.len()).map_err(
                    |_| RunnerError::new("canonical sequence cardinality exceeds u64"),
                )?));
                for value in values {
                    output.push(NODE);
                    output.extend(frame(&value)?);
                }
            }
        }
    }
    Ok(output)
}

pub(crate) fn iri(value: &str) -> Result<Vec<u8>, RunnerError> {
    node(IRI, [Field::Text(value.to_owned())])
}

pub(crate) fn entity(kind: &'static str, value: &str) -> Result<Vec<u8>, RunnerError> {
    node(
        ENTITY,
        [Field::Enum(kind.to_owned()), Field::Node(iri(value)?)],
    )
}

pub(crate) fn structural_digest(value: &[u8]) -> Vec<u8> {
    let mut hasher = Sha256::new();
    hasher.update(b"pyowl-core:structural-value:v1\0");
    hasher.update(encode_varint(2));
    hasher.update(value);
    hasher.finalize().to_vec()
}

pub(crate) fn parse_node(value: &[u8]) -> Result<ParsedNode, RunnerError> {
    let (node, offset) = parse_node_at(value, 0)?;
    if offset != value.len() {
        return Err(RunnerError::new(
            "canonical model value contains trailing bytes",
        ));
    }
    Ok(node)
}

fn parse_node_at(value: &[u8], mut offset: usize) -> Result<(ParsedNode, usize), RunnerError> {
    let (tag, next) = decode_varint(value, offset)?;
    offset = next;
    let mut fields = Vec::new();
    while offset < value.len() {
        let marker = value[offset];
        offset += 1;
        let field = match marker {
            NONE => ParsedField::None,
            NODE => {
                let (payload, next) = take_frame(value, offset)?;
                offset = next;
                ParsedField::Node(parse_node(payload)?)
            }
            TEXT => {
                let (payload, next) = take_frame(value, offset)?;
                offset = next;
                ParsedField::Text(
                    std::str::from_utf8(payload)
                        .map_err(|_| RunnerError::new("canonical text is not UTF-8"))?
                        .to_owned(),
                )
            }
            BYTES => {
                let (payload, next) = take_frame(value, offset)?;
                offset = next;
                ParsedField::Bytes(payload.to_vec())
            }
            INTEGER => {
                let (integer, next) = decode_varint(value, offset)?;
                offset = next;
                ParsedField::Integer(integer)
            }
            ENUM => {
                let (payload, next) = take_frame(value, offset)?;
                offset = next;
                ParsedField::Enum(
                    std::str::from_utf8(payload)
                        .map_err(|_| RunnerError::new("canonical enum is not ASCII"))?
                        .to_owned(),
                )
            }
            SET | SEQUENCE => {
                let (count, next) = decode_varint(value, offset)?;
                offset = next;
                let mut members = Vec::new();
                members
                    .try_reserve_exact(usize::try_from(count).map_err(|_| {
                        RunnerError::new("canonical collection count exceeds usize")
                    })?)
                    .map_err(|_| RunnerError::new("canonical collection allocation failed"))?;
                for _ in 0..count {
                    if marker == SEQUENCE {
                        if value.get(offset).copied() != Some(NODE) {
                            return Err(RunnerError::new(
                                "canonical sequence contains a non-node member",
                            ));
                        }
                        offset += 1;
                    }
                    let (payload, next) = take_frame(value, offset)?;
                    offset = next;
                    members.push(parse_node(payload)?);
                }
                if marker == SET {
                    ParsedField::Set(members)
                } else {
                    ParsedField::Sequence(members)
                }
            }
            _ => return Err(RunnerError::new("canonical field marker is unsupported")),
        };
        fields.push(field);
    }
    Ok((ParsedNode { tag, fields }, offset))
}

fn decode_varint(value: &[u8], mut offset: usize) -> Result<(u64, usize), RunnerError> {
    let start = offset;
    let mut result = 0_u64;
    let mut shift = 0_u32;
    while offset < value.len() && shift < 64 {
        let byte = value[offset];
        offset += 1;
        result |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            if value[start..offset] != encode_varint(result) {
                return Err(RunnerError::new("canonical varint is nonminimal"));
            }
            return Ok((result, offset));
        }
        shift += 7;
    }
    Err(RunnerError::new("canonical varint is truncated"))
}

fn take_frame(value: &[u8], offset: usize) -> Result<(&[u8], usize), RunnerError> {
    let (length, offset) = decode_varint(value, offset)?;
    let end = offset
        .checked_add(
            usize::try_from(length)
                .map_err(|_| RunnerError::new("canonical frame length exceeds usize"))?,
        )
        .ok_or_else(|| RunnerError::new("canonical frame length overflow"))?;
    let payload = value
        .get(offset..end)
        .ok_or_else(|| RunnerError::new("canonical frame is truncated"))?;
    Ok((payload, end))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_iri_matches_model_schema_two() {
        assert_eq!(iri("urn:test").unwrap(), b"\x01\x02\x08urn:test".to_vec());
    }

    #[test]
    fn canonical_sets_are_sorted_and_deduplicated() {
        let encoded = node(OBJECT_ONE_OF, [Field::Set(vec![vec![2], vec![1], vec![2]])]).unwrap();
        assert_eq!(encoded, vec![OBJECT_ONE_OF as u8, SET, 2, 1, 1, 1, 2]);
    }
}
