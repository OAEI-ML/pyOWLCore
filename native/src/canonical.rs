//! Safe canonical-model builders used by native parsers and indexes.

use crate::error::{NativeError, NativeResult};

const MARKER_NONE: u8 = 0;
const MARKER_NODE: u8 = 1;
const MARKER_TEXT: u8 = 2;
const MARKER_BYTES: u8 = 3;
const MARKER_INTEGER: u8 = 4;
const MARKER_ENUM: u8 = 5;
const MARKER_SET: u8 = 6;
const MARKER_SEQUENCE: u8 = 7;

pub(crate) const PROVISIONAL_SCOPE: [u8; 32] = [
    0x9b, 0x38, 0x99, 0xd1, 0x03, 0x23, 0x85, 0xa3, 0x21, 0xe2, 0x7b, 0xfa, 0xc9, 0x56, 0xcf, 0xc4,
    0x71, 0x31, 0x6b, 0x7b, 0x8b, 0x87, 0x29, 0xaf, 0x81, 0x9e, 0xb0, 0x93, 0x4d, 0xc6, 0x59, 0xef,
];
pub(crate) const LEXICAL_KEY: &[u8] = b"pyowl-core:parser-blank-label:v2\0";

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Node {
    tag: u64,
    encoded: Vec<u8>,
    flattened_members: Option<Vec<Node>>,
}

#[derive(Clone, Debug)]
pub(crate) enum Field {
    None,
    Node(Node),
    Text(String),
    Bytes(Vec<u8>),
    Integer(String),
    Enumeration(&'static str),
    Set(Vec<Node>),
    Sequence(Vec<Node>),
}

impl Node {
    pub(crate) fn build(tag: u64, fields: Vec<Field>) -> NativeResult<Self> {
        let mut encoded = Vec::new();
        encoded
            .try_reserve(16)
            .map_err(|_| NativeError::limit("native canonical allocation failed"))?;
        encode_u64(tag, &mut encoded);
        for field in &fields {
            encode_field(field, &mut encoded)?;
        }
        let flattened_members = if matches!(tag, 21 | 22 | 30 | 31) {
            fields.into_iter().next().and_then(|field| match field {
                Field::Set(values) => Some(values),
                _ => None,
            })
        } else {
            None
        };
        Ok(Self {
            tag,
            encoded,
            flattened_members,
        })
    }

    pub(crate) fn as_bytes(&self) -> &[u8] {
        &self.encoded
    }

    pub(crate) fn into_bytes(self) -> Vec<u8> {
        self.encoded
    }
}

pub(crate) fn iri(value: String) -> NativeResult<Node> {
    Node::build(1, vec![Field::Text(value)])
}

pub(crate) fn entity(kind: &'static str, value: Node) -> NativeResult<Node> {
    Node::build(2, vec![Field::Enumeration(kind), Field::Node(value)])
}

pub(crate) fn anonymous(label: &str) -> NativeResult<Node> {
    let label = label.as_bytes();
    let mut local_key = Vec::new();
    local_key
        .try_reserve(
            LEXICAL_KEY
                .len()
                .saturating_add(label.len())
                .saturating_add(10),
        )
        .map_err(|_| NativeError::limit("native anonymous-key allocation failed"))?;
    local_key.extend_from_slice(LEXICAL_KEY);
    encode_usize(label.len(), &mut local_key);
    local_key.extend_from_slice(label);
    Node::build(
        3,
        vec![
            Field::Bytes(PROVISIONAL_SCOPE.to_vec()),
            Field::Bytes(local_key),
        ],
    )
}

pub(crate) fn literal(
    lexical: String,
    datatype: Node,
    language: Option<String>,
) -> NativeResult<Node> {
    Node::build(
        4,
        vec![
            Field::Text(lexical),
            Field::Node(datatype),
            language.map_or(Field::None, Field::Text),
        ],
    )
}

pub(crate) fn canonical_set(
    values: Vec<Node>,
    minimum: usize,
    flatten_tag: Option<u64>,
) -> NativeResult<Vec<Node>> {
    let mut flattened = Vec::new();
    for value in values {
        if flatten_tag == Some(value.tag) {
            if let Some(members) = value.flattened_members {
                flattened
                    .try_reserve(members.len())
                    .map_err(|_| NativeError::limit("native canonical set allocation failed"))?;
                flattened.extend(members);
                continue;
            }
        }
        flattened
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native canonical set allocation failed"))?;
        flattened.push(value);
    }
    flattened.sort_unstable_by(|left, right| left.encoded.cmp(&right.encoded));
    flattened.dedup_by(|left, right| left.encoded == right.encoded);
    if flattened.len() < minimum {
        return Err(NativeError::new(
            "NATIVE_FORMAT_SYNTAX",
            "native Functional Syntax collection has too few distinct members",
        ));
    }
    Ok(flattened)
}

fn encode_field(field: &Field, output: &mut Vec<u8>) -> NativeResult<()> {
    match field {
        Field::None => output.push(MARKER_NONE),
        Field::Node(value) => {
            output.push(MARKER_NODE);
            encode_frame(&value.encoded, output)?;
        }
        Field::Text(value) => {
            output.push(MARKER_TEXT);
            encode_frame(value.as_bytes(), output)?;
        }
        Field::Bytes(value) => {
            output.push(MARKER_BYTES);
            encode_frame(value, output)?;
        }
        Field::Integer(value) => {
            output.push(MARKER_INTEGER);
            encode_decimal(value, output)?;
        }
        Field::Enumeration(value) => {
            output.push(MARKER_ENUM);
            encode_frame(value.as_bytes(), output)?;
        }
        Field::Set(values) => {
            output.push(MARKER_SET);
            encode_usize(values.len(), output);
            for value in values {
                encode_frame(&value.encoded, output)?;
            }
        }
        Field::Sequence(values) => {
            output.push(MARKER_SEQUENCE);
            encode_usize(values.len(), output);
            for value in values {
                output.push(MARKER_NODE);
                encode_frame(&value.encoded, output)?;
            }
        }
    }
    Ok(())
}

pub(crate) fn encode_frame(value: &[u8], output: &mut Vec<u8>) -> NativeResult<()> {
    let additional = value
        .len()
        .checked_add(10)
        .ok_or_else(|| NativeError::limit("native canonical frame size overflow"))?;
    output
        .try_reserve(additional)
        .map_err(|_| NativeError::limit("native canonical frame allocation failed"))?;
    encode_usize(value.len(), output);
    output.extend_from_slice(value);
    Ok(())
}

pub(crate) fn encode_u64(mut value: u64, output: &mut Vec<u8>) {
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        output.push(byte | if value == 0 { 0 } else { 0x80 });
        if value == 0 {
            break;
        }
    }
}

pub(crate) fn encode_usize(value: usize, output: &mut Vec<u8>) {
    encode_u64(u64::try_from(value).unwrap_or(u64::MAX), output);
}

fn encode_decimal(value: &str, output: &mut Vec<u8>) -> NativeResult<()> {
    if value == "0" {
        output.push(0);
        return Ok(());
    }
    let mut digits = Vec::new();
    digits
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native integer allocation failed"))?;
    digits.extend(value.bytes().map(|byte| byte - b'0'));
    let mut encoded = Vec::new();
    encoded
        .try_reserve(value.len())
        .map_err(|_| NativeError::limit("native integer allocation failed"))?;
    while !digits.is_empty() {
        let mut carry = 0_u16;
        let mut first_nonzero = None;
        for (index, digit) in digits.iter_mut().enumerate() {
            let selected = carry * 10 + u16::from(*digit);
            *digit = (selected / 128) as u8;
            carry = selected % 128;
            if first_nonzero.is_none() && *digit != 0 {
                first_nonzero = Some(index);
            }
        }
        encoded.push(carry as u8);
        match first_nonzero {
            Some(index) => {
                digits.drain(..index);
            }
            None => digits.clear(),
        }
    }
    output
        .try_reserve(encoded.len())
        .map_err(|_| NativeError::limit("native integer allocation failed"))?;
    let last = encoded.len().saturating_sub(1);
    for (index, byte) in encoded.into_iter().enumerate() {
        output.push(byte | if index == last { 0 } else { 0x80 });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arbitrary_decimal_integer_matches_known_varint() {
        let node = Node::build(38, vec![Field::Integer("128".into())]).unwrap();
        assert_eq!(node.as_bytes(), &[38, 4, 128, 1]);
    }

    #[test]
    fn canonical_set_flattens_sorts_and_deduplicates() {
        let a = entity("class", iri("urn:a".into()).unwrap()).unwrap();
        let b = entity("class", iri("urn:b".into()).unwrap()).unwrap();
        let inner = Node::build(
            30,
            vec![Field::Set(
                canonical_set(vec![b.clone(), a.clone()], 2, Some(30)).unwrap(),
            )],
        )
        .unwrap();
        let values = canonical_set(vec![inner, a], 2, Some(30)).unwrap();
        assert_eq!(values.len(), 2);
        assert!(values[0].as_bytes() < values[1].as_bytes());
    }
}
