//! Complete OWL 2 Functional-Style parser for the advertised native capability.

use std::collections::BTreeMap;

use crate::canonical::{anonymous, canonical_set, entity, iri, literal, Field, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;

use super::{ParsedDocument, Span, SpannedNode};

const RDF_PLAIN_LITERAL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
const RDFS_LITERAL: &str = "http://www.w3.org/2000/01/rdf-schema#Literal";
const OWL_THING: &str = "http://www.w3.org/2002/07/owl#Thing";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Kind {
    Word,
    Iri,
    String,
    Language,
    Integer,
    Hat,
    Open,
    Close,
    Equal,
    Eof,
}

#[derive(Clone, Debug)]
struct Token {
    kind: Kind,
    value: String,
    byte_start: u64,
    byte_end: u64,
    line: u64,
    column: u64,
}

impl Token {
    fn span_to(&self, end: &Self) -> Span {
        Span {
            byte_start: self.byte_start,
            byte_end: end.byte_end,
            line: self.line,
            column: self.column,
        }
    }
}

pub(super) fn parse_functional(
    data: &[u8],
    allow_swrl: bool,
    session: &mut Session<'_>,
) -> NativeResult<ParsedDocument> {
    let text = std::str::from_utf8(data).map_err(|_| {
        NativeError::new(
            "NATIVE_FORMAT_ENCODING",
            "native Functional Syntax source is not valid UTF-8",
        )
    })?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let decoded_codepoints = u64::try_from(text.chars().count())
        .map_err(|_| NativeError::limit("native decoded source length exceeds u64"))?;
    let tokens = tokenize(text, session)?;
    Parser::new(tokens, allow_swrl, session, decoded_codepoints).parse()
}

fn tokenize(text: &str, session: &mut Session<'_>) -> NativeResult<Vec<Token>> {
    let bytes = text.as_bytes();
    let mut tokens = Vec::new();
    let mut index = 0_usize;
    let mut line = 1_u64;
    let mut column = 1_u64;
    while index < bytes.len() {
        session.step(1)?;
        match bytes[index] {
            b' ' | b'\t' => {
                index += 1;
                column = column.saturating_add(1);
                continue;
            }
            b'\r' => {
                index += 1;
                if bytes.get(index) == Some(&b'\n') {
                    index += 1;
                }
                line = line.saturating_add(1);
                column = 1;
                continue;
            }
            b'\n' => {
                index += 1;
                line = line.saturating_add(1);
                column = 1;
                continue;
            }
            b'#' => {
                while index < bytes.len() && !matches!(bytes[index], b'\r' | b'\n') {
                    session.step(1)?;
                    index += 1;
                    column = column.saturating_add(1);
                }
                continue;
            }
            _ => {}
        }
        let start = index;
        let start_line = line;
        let start_column = column;
        let (kind, value, end) = match bytes[index] {
            b'(' => (Kind::Open, "(".to_owned(), index + 1),
            b')' => (Kind::Close, ")".to_owned(), index + 1),
            b'=' => (Kind::Equal, "=".to_owned(), index + 1),
            b'^' if bytes.get(index + 1) == Some(&b'^') => (Kind::Hat, "^^".to_owned(), index + 2),
            b'<' => lex_iri(text, index, session)?,
            b'"' => lex_string(text, index, session)?,
            b'@' => lex_language(text, index, session)?,
            _ => lex_word(text, index, session)?,
        };
        advance_position(&text[index..end], &mut line, &mut column, session)?;
        index = end;
        session.reserve_bytes(std::mem::size_of::<Token>().saturating_add(value.len()))?;
        tokens.push(Token {
            kind,
            value,
            byte_start: u64::try_from(start)
                .map_err(|_| NativeError::limit("native token offset exceeds u64"))?,
            byte_end: u64::try_from(end)
                .map_err(|_| NativeError::limit("native token offset exceeds u64"))?,
            line: start_line,
            column: start_column,
        });
    }
    tokens.push(Token {
        kind: Kind::Eof,
        value: String::new(),
        byte_start: u64::try_from(bytes.len())
            .map_err(|_| NativeError::limit("native token offset exceeds u64"))?,
        byte_end: u64::try_from(bytes.len())
            .map_err(|_| NativeError::limit("native token offset exceeds u64"))?,
        line,
        column,
    });
    Ok(tokens)
}

fn lex_iri(
    text: &str,
    start: usize,
    session: &mut Session<'_>,
) -> NativeResult<(Kind, String, usize)> {
    let bytes = text.as_bytes();
    let mut end = start + 1;
    let mut escaped = false;
    while end < bytes.len() {
        session.step(1)?;
        let byte = bytes[end];
        if byte == b'>' && !escaped {
            let value = text.get(start + 1..end).ok_or_else(syntax)?.to_owned();
            return Ok((Kind::Iri, value, end + 1));
        }
        if matches!(byte, b'\r' | b'\n') {
            return Err(syntax());
        }
        escaped = byte == b'\\' && !escaped;
        if byte != b'\\' {
            escaped = false;
        }
        end += 1;
    }
    Err(syntax())
}

fn lex_string(
    text: &str,
    start: usize,
    session: &mut Session<'_>,
) -> NativeResult<(Kind, String, usize)> {
    let bytes = text.as_bytes();
    let mut end = start + 1;
    let mut value = Vec::new();
    while end < bytes.len() {
        session.step(1)?;
        match bytes[end] {
            b'"' => {
                let value = String::from_utf8(value).map_err(|_| syntax())?;
                enforce(
                    session,
                    LimitKey::MaxLiteralBytes,
                    value.len(),
                    "native literal exceeds max_literal_bytes",
                )?;
                return Ok((Kind::String, value, end + 1));
            }
            b'\\' => {
                let escaped = *bytes.get(end + 1).ok_or_else(syntax)?;
                if !matches!(escaped, b'"' | b'\\') {
                    return Err(syntax());
                }
                value.push(escaped);
                end += 2;
            }
            byte => {
                value.push(byte);
                end += 1;
            }
        }
    }
    Err(syntax())
}

fn lex_language(
    text: &str,
    start: usize,
    session: &mut Session<'_>,
) -> NativeResult<(Kind, String, usize)> {
    let bytes = text.as_bytes();
    let mut end = start + 1;
    if !bytes.get(end).is_some_and(u8::is_ascii_alphanumeric) {
        return Err(syntax());
    }
    while bytes.get(end).is_some_and(u8::is_ascii_alphanumeric) {
        session.step(1)?;
        end += 1;
    }
    loop {
        if bytes.get(end) != Some(&b'-')
            || !bytes.get(end + 1).is_some_and(u8::is_ascii_alphanumeric)
        {
            break;
        }
        end += 1;
        while bytes.get(end).is_some_and(u8::is_ascii_alphanumeric) {
            session.step(1)?;
            end += 1;
        }
    }
    Ok((
        Kind::Language,
        text[start + 1..end].to_ascii_lowercase(),
        end,
    ))
}

fn lex_word(
    text: &str,
    start: usize,
    session: &mut Session<'_>,
) -> NativeResult<(Kind, String, usize)> {
    let bytes = text.as_bytes();
    let mut end = start;
    while end < bytes.len() && !is_delimiter(bytes[end]) {
        session.step(1)?;
        end += 1;
    }
    if end == start {
        return Err(syntax());
    }
    let value = text.get(start..end).ok_or_else(syntax)?.to_owned();
    let kind = if value.bytes().all(|byte| byte.is_ascii_digit()) {
        Kind::Integer
    } else {
        Kind::Word
    };
    Ok((kind, value, end))
}

fn is_delimiter(byte: u8) -> bool {
    matches!(
        byte,
        b'(' | b')'
            | b'='
            | b'<'
            | b'>'
            | b'@'
            | b'^'
            | b'"'
            | b'\''
            | b' '
            | b'\t'
            | b'\r'
            | b'\n'
    )
}

fn advance_position(
    fragment: &str,
    line: &mut u64,
    column: &mut u64,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let mut previous_cr = false;
    for character in fragment.chars() {
        session.step(1)?;
        match character {
            '\r' => {
                *line = line.saturating_add(1);
                *column = 1;
                previous_cr = true;
            }
            '\n' if previous_cr => {
                previous_cr = false;
            }
            '\n' => {
                *line = line.saturating_add(1);
                *column = 1;
                previous_cr = false;
            }
            _ => {
                *column = column.saturating_add(1);
                previous_cr = false;
            }
        }
    }
    Ok(())
}

struct Parser<'a, 'b> {
    tokens: Vec<Token>,
    index: usize,
    depth: u64,
    allow_swrl: bool,
    prefixes: BTreeMap<String, String>,
    decoded_codepoints: u64,
    session: &'a mut Session<'b>,
}

impl<'a, 'b> Parser<'a, 'b> {
    fn new(
        tokens: Vec<Token>,
        allow_swrl: bool,
        session: &'a mut Session<'b>,
        decoded_codepoints: u64,
    ) -> Self {
        let prefixes = BTreeMap::from([
            ("owl".into(), "http://www.w3.org/2002/07/owl#".into()),
            (
                "rdf".into(),
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#".into(),
            ),
            (
                "rdfs".into(),
                "http://www.w3.org/2000/01/rdf-schema#".into(),
            ),
            ("xsd".into(), "http://www.w3.org/2001/XMLSchema#".into()),
        ]);
        Self {
            tokens,
            index: 0,
            depth: 0,
            allow_swrl,
            prefixes,
            decoded_codepoints,
            session,
        }
    }

    fn parse(mut self) -> NativeResult<ParsedDocument> {
        while self.word("Prefix") {
            self.parse_prefix()?;
        }
        self.expect_word("Ontology")?;
        self.open()?;
        let mut ontology_iri = None;
        let mut version_iri = None;
        if self.starts_iri() && !self.starts_document_member() {
            ontology_iri = Some(self.parse_iri()?);
            if self.starts_iri() && !self.starts_document_member() {
                version_iri = Some(self.parse_iri()?);
            }
        }
        let mut imports = Vec::new();
        let mut annotations = Vec::new();
        let mut axioms = Vec::new();
        let mut extensions = Vec::new();
        while !self.at(Kind::Close) {
            let start = self.peek().clone();
            if self.word("Import") {
                self.take()?;
                self.open()?;
                imports.push(self.parse_iri()?);
                self.close()?;
            } else if self.word("Annotation") {
                let node = self.parse_annotation()?;
                annotations.push(SpannedNode {
                    node,
                    span: start.span_to(self.previous()),
                });
            } else if self.word("SWRLRule") {
                if !self.allow_swrl {
                    return Err(NativeError::new(
                        "NATIVE_EXTENSION_DISABLED",
                        "native SWRL parsing requires explicit enablement",
                    ));
                }
                let node = self.parse_swrl_rule()?;
                extensions.push(SpannedNode {
                    node,
                    span: start.span_to(self.previous()),
                });
            } else {
                let node = self.parse_axiom()?;
                axioms.push(SpannedNode {
                    node,
                    span: start.span_to(self.previous()),
                });
                enforce(
                    self.session,
                    LimitKey::MaxAxioms,
                    axioms.len(),
                    "native axiom count exceeds max_axioms",
                )?;
            }
        }
        self.close()?;
        self.expect(Kind::Eof)?;
        Ok(ParsedDocument {
            ontology_iri,
            version_iri,
            imports,
            annotations,
            axioms,
            extensions,
            prefixes: self.prefixes.into_iter().collect(),
            decoded_codepoints: self.decoded_codepoints,
        })
    }

    fn parse_prefix(&mut self) -> NativeResult<()> {
        self.expect_word("Prefix")?;
        self.open()?;
        let token = self.expect(Kind::Word)?;
        let prefix = token.value.strip_suffix(':').ok_or_else(syntax)?.to_owned();
        self.expect(Kind::Equal)?;
        let raw = self.expect(Kind::Iri)?.value;
        let value = decode_iri_escapes(&raw)?;
        enforce(
            self.session,
            LimitKey::MaxIriBytes,
            value.len(),
            "native prefix IRI exceeds max_iri_bytes",
        )?;
        self.prefixes.insert(prefix, value);
        enforce(
            self.session,
            LimitKey::MaxPrefixes,
            self.prefixes.len(),
            "native prefix count exceeds max_prefixes",
        )?;
        self.close()
    }

    fn parse_axiom(&mut self) -> NativeResult<Node> {
        let name = self.expect(Kind::Word)?.value;
        let tag = axiom_tag(&name).ok_or_else(syntax)?;
        self.open()?;
        let annotations = self.parse_annotations()?;
        let fields = match name.as_str() {
            "Declaration" => vec![Field::Node(self.parse_entity()?), Field::Set(annotations)],
            "SubClassOf" => vec![
                Field::Node(self.parse_class_expression()?),
                Field::Node(self.parse_class_expression()?),
                Field::Set(annotations),
            ],
            "EquivalentClasses" | "DisjointClasses" => vec![
                self.set_many(Self::parse_class_expression, 2, None)?,
                Field::Set(annotations),
            ],
            "DisjointUnion" => vec![
                Field::Node(self.parse_class()?),
                self.set_many(Self::parse_class_expression, 2, None)?,
                Field::Set(annotations),
            ],
            "SubObjectPropertyOf" => vec![
                Field::Node(self.parse_sub_object_property()?),
                Field::Node(self.parse_object_property()?),
                Field::Set(annotations),
            ],
            "EquivalentObjectProperties" | "DisjointObjectProperties" => vec![
                self.set_many(Self::parse_object_property, 2, None)?,
                Field::Set(annotations),
            ],
            "InverseObjectProperties" => {
                let mut first = self.parse_object_property()?;
                let mut second = self.parse_object_property()?;
                if second.as_bytes() < first.as_bytes() {
                    std::mem::swap(&mut first, &mut second);
                }
                vec![
                    Field::Node(first),
                    Field::Node(second),
                    Field::Set(annotations),
                ]
            }
            "ObjectPropertyDomain" | "ObjectPropertyRange" => vec![
                Field::Node(self.parse_object_property()?),
                Field::Node(self.parse_class_expression()?),
                Field::Set(annotations),
            ],
            name if object_characteristic(name) => vec![
                Field::Node(self.parse_object_property()?),
                Field::Set(annotations),
            ],
            "SubDataPropertyOf" => vec![
                Field::Node(self.parse_data_property()?),
                Field::Node(self.parse_data_property()?),
                Field::Set(annotations),
            ],
            "EquivalentDataProperties" | "DisjointDataProperties" => vec![
                self.set_many(Self::parse_data_property, 2, None)?,
                Field::Set(annotations),
            ],
            "DataPropertyDomain" => vec![
                Field::Node(self.parse_data_property()?),
                Field::Node(self.parse_class_expression()?),
                Field::Set(annotations),
            ],
            "DataPropertyRange" => vec![
                Field::Node(self.parse_data_property()?),
                Field::Node(self.parse_data_range()?),
                Field::Set(annotations),
            ],
            "FunctionalDataProperty" => vec![
                Field::Node(self.parse_data_property()?),
                Field::Set(annotations),
            ],
            "DatatypeDefinition" => vec![
                Field::Node(self.parse_datatype()?),
                Field::Node(self.parse_data_range()?),
                Field::Set(annotations),
            ],
            "HasKey" => {
                let expression = self.parse_class_expression()?;
                self.open()?;
                let object_values =
                    canonical_set(self.many(Self::parse_object_property)?, 0, None)?;
                self.close()?;
                self.open()?;
                let data_values = canonical_set(self.many(Self::parse_data_property)?, 0, None)?;
                self.close()?;
                if object_values.is_empty() && data_values.is_empty() {
                    return Err(syntax());
                }
                vec![
                    Field::Node(expression),
                    Field::Set(object_values),
                    Field::Set(data_values),
                    Field::Set(annotations),
                ]
            }
            "SameIndividual" | "DifferentIndividuals" => vec![
                self.set_many(Self::parse_individual, 2, None)?,
                Field::Set(annotations),
            ],
            "ClassAssertion" => vec![
                Field::Node(self.parse_class_expression()?),
                Field::Node(self.parse_individual()?),
                Field::Set(annotations),
            ],
            "ObjectPropertyAssertion" | "NegativeObjectPropertyAssertion" => vec![
                Field::Node(self.parse_object_property()?),
                Field::Node(self.parse_individual()?),
                Field::Node(self.parse_individual()?),
                Field::Set(annotations),
            ],
            "DataPropertyAssertion" | "NegativeDataPropertyAssertion" => vec![
                Field::Node(self.parse_data_property()?),
                Field::Node(self.parse_individual()?),
                Field::Node(self.parse_literal()?),
                Field::Set(annotations),
            ],
            "AnnotationAssertion" => vec![
                Field::Node(self.parse_annotation_property()?),
                Field::Node(self.parse_annotation_subject()?),
                Field::Node(self.parse_annotation_value()?),
                Field::Set(annotations),
            ],
            "SubAnnotationPropertyOf" => vec![
                Field::Node(self.parse_annotation_property()?),
                Field::Node(self.parse_annotation_property()?),
                Field::Set(annotations),
            ],
            "AnnotationPropertyDomain" | "AnnotationPropertyRange" => vec![
                Field::Node(self.parse_annotation_property()?),
                Field::Node(self.parse_iri()?),
                Field::Set(annotations),
            ],
            _ => return Err(syntax()),
        };
        self.close()?;
        Node::build(tag, fields)
    }

    fn parse_entity(&mut self) -> NativeResult<Node> {
        let kind = match self.expect(Kind::Word)?.value.as_str() {
            "Class" => "class",
            "Datatype" => "datatype",
            "ObjectProperty" => "object_property",
            "DataProperty" => "data_property",
            "AnnotationProperty" => "annotation_property",
            "NamedIndividual" => "named_individual",
            _ => return Err(syntax()),
        };
        self.open()?;
        let value = entity(kind, self.parse_iri()?)?;
        self.close()?;
        Ok(value)
    }

    fn parse_annotation(&mut self) -> NativeResult<Node> {
        self.expect_word("Annotation")?;
        self.open()?;
        let annotations = self.parse_annotations()?;
        let property = self.parse_annotation_property()?;
        let value = self.parse_annotation_value()?;
        self.close()?;
        Node::build(
            5,
            vec![
                Field::Node(property),
                Field::Node(value),
                Field::Set(annotations),
            ],
        )
    }

    fn parse_annotations(&mut self) -> NativeResult<Vec<Node>> {
        let mut values = Vec::new();
        while self.word("Annotation") {
            values.push(self.parse_annotation()?);
            enforce(
                self.session,
                LimitKey::MaxAnnotations,
                values.len(),
                "native annotation count exceeds max_annotations",
            )?;
        }
        canonical_set(values, 0, None)
    }

    fn parse_class_expression(&mut self) -> NativeResult<Node> {
        let Some(name) = self.call_name().map(str::to_owned) else {
            return self.parse_class();
        };
        self.take()?;
        self.open()?;
        let (tag, fields) = match name.as_str() {
            "ObjectIntersectionOf" => (
                30,
                vec![self.set_many(Self::parse_class_expression, 2, Some(30))?],
            ),
            "ObjectUnionOf" => (
                31,
                vec![self.set_many(Self::parse_class_expression, 2, Some(31))?],
            ),
            "ObjectComplementOf" => (32, vec![Field::Node(self.parse_class_expression()?)]),
            "ObjectOneOf" => (33, vec![self.set_many(Self::parse_individual, 1, None)?]),
            "ObjectSomeValuesFrom" | "ObjectAllValuesFrom" => (
                if name == "ObjectSomeValuesFrom" {
                    34
                } else {
                    35
                },
                vec![
                    Field::Node(self.parse_object_property()?),
                    Field::Node(self.parse_class_expression()?),
                ],
            ),
            "ObjectHasValue" => (
                36,
                vec![
                    Field::Node(self.parse_object_property()?),
                    Field::Node(self.parse_individual()?),
                ],
            ),
            "ObjectHasSelf" => (37, vec![Field::Node(self.parse_object_property()?)]),
            "ObjectMinCardinality" | "ObjectMaxCardinality" | "ObjectExactCardinality" => {
                let tag = match name.as_str() {
                    "ObjectMinCardinality" => 38,
                    "ObjectMaxCardinality" => 39,
                    _ => 40,
                };
                let cardinality = self.expect(Kind::Integer)?.value;
                let property = self.parse_object_property()?;
                let filler = if self.at(Kind::Close) {
                    entity("class", iri(OWL_THING.into())?)?
                } else {
                    self.parse_class_expression()?
                };
                (
                    tag,
                    vec![
                        Field::Integer(cardinality),
                        Field::Node(property),
                        Field::Node(filler),
                    ],
                )
            }
            "DataSomeValuesFrom" | "DataAllValuesFrom" => {
                let (properties, filler) = self.parse_data_quantified_arguments()?;
                (
                    if name == "DataSomeValuesFrom" { 41 } else { 42 },
                    vec![Field::Sequence(properties), Field::Node(filler)],
                )
            }
            "DataHasValue" => (
                43,
                vec![
                    Field::Node(self.parse_data_property()?),
                    Field::Node(self.parse_literal()?),
                ],
            ),
            "DataMinCardinality" | "DataMaxCardinality" | "DataExactCardinality" => {
                let tag = match name.as_str() {
                    "DataMinCardinality" => 44,
                    "DataMaxCardinality" => 45,
                    _ => 46,
                };
                let cardinality = self.expect(Kind::Integer)?.value;
                let property = self.parse_data_property()?;
                let filler = if self.at(Kind::Close) {
                    entity("datatype", iri(RDFS_LITERAL.into())?)?
                } else {
                    self.parse_data_range()?
                };
                (
                    tag,
                    vec![
                        Field::Integer(cardinality),
                        Field::Node(property),
                        Field::Node(filler),
                    ],
                )
            }
            _ => return Err(syntax()),
        };
        self.close()?;
        Node::build(tag, fields)
    }

    fn parse_data_quantified_arguments(&mut self) -> NativeResult<(Vec<Node>, Node)> {
        let mut properties = Vec::new();
        while !self.at(Kind::Close) {
            if self.call_name().is_some_and(data_range_constructor) {
                if properties.is_empty() {
                    return Err(syntax());
                }
                return Ok((properties, self.parse_data_range()?));
            }
            let selected_iri = self.parse_iri()?;
            if self.at(Kind::Close) {
                if properties.is_empty() {
                    return Err(syntax());
                }
                return Ok((properties, entity("datatype", selected_iri)?));
            }
            properties.push(entity("data_property", selected_iri)?);
            enforce_sequence(self.session, properties.len())?;
        }
        Err(syntax())
    }

    fn parse_data_range(&mut self) -> NativeResult<Node> {
        let Some(name) = self.call_name().map(str::to_owned) else {
            return self.parse_datatype();
        };
        self.take()?;
        self.open()?;
        let (tag, fields) = match name.as_str() {
            "DataIntersectionOf" => (
                21,
                vec![self.set_many(Self::parse_data_range, 2, Some(21))?],
            ),
            "DataUnionOf" => (
                22,
                vec![self.set_many(Self::parse_data_range, 2, Some(22))?],
            ),
            "DataComplementOf" => (23, vec![Field::Node(self.parse_data_range()?)]),
            "DataOneOf" => (24, vec![self.set_many(Self::parse_literal, 1, None)?]),
            "DatatypeRestriction" => (
                25,
                vec![
                    Field::Node(self.parse_datatype()?),
                    self.set_many(Self::parse_facet_restriction, 1, None)?,
                ],
            ),
            _ => return Err(syntax()),
        };
        self.close()?;
        Node::build(tag, fields)
    }

    fn parse_facet_restriction(&mut self) -> NativeResult<Node> {
        Node::build(
            20,
            vec![
                Field::Node(self.parse_iri()?),
                Field::Node(self.parse_literal()?),
            ],
        )
    }

    fn parse_sub_object_property(&mut self) -> NativeResult<Node> {
        if self.word("ObjectPropertyChain") {
            self.take()?;
            self.open()?;
            let values = self.many(Self::parse_object_property)?;
            enforce_minimum(&values, 2)?;
            self.close()?;
            return Node::build(11, vec![Field::Sequence(values)]);
        }
        self.parse_object_property()
    }

    fn parse_object_property(&mut self) -> NativeResult<Node> {
        if self.word("ObjectInverseOf") {
            self.take()?;
            self.open()?;
            let property = entity("object_property", self.parse_iri()?)?;
            self.close()?;
            return Node::build(10, vec![Field::Node(property)]);
        }
        entity("object_property", self.parse_iri()?)
    }

    fn parse_data_property(&mut self) -> NativeResult<Node> {
        entity("data_property", self.parse_iri()?)
    }

    fn parse_annotation_property(&mut self) -> NativeResult<Node> {
        entity("annotation_property", self.parse_iri()?)
    }

    fn parse_class(&mut self) -> NativeResult<Node> {
        entity("class", self.parse_iri()?)
    }

    fn parse_datatype(&mut self) -> NativeResult<Node> {
        entity("datatype", self.parse_iri()?)
    }

    fn parse_individual(&mut self) -> NativeResult<Node> {
        if self.peek().kind == Kind::Word && self.peek().value.starts_with("_:") {
            let label = self.take()?.value;
            let label = label.strip_prefix("_:").ok_or_else(syntax)?;
            if label.is_empty() {
                return Err(syntax());
            }
            return anonymous(label);
        }
        entity("named_individual", self.parse_iri()?)
    }

    fn parse_annotation_subject(&mut self) -> NativeResult<Node> {
        if self.peek().kind == Kind::Word && self.peek().value.starts_with("_:") {
            return self.parse_individual();
        }
        self.parse_iri()
    }

    fn parse_annotation_value(&mut self) -> NativeResult<Node> {
        if self.peek().kind == Kind::String {
            return self.parse_literal();
        }
        if self.peek().kind == Kind::Word && self.peek().value.starts_with("_:") {
            return self.parse_individual();
        }
        self.parse_iri()
    }

    fn parse_literal(&mut self) -> NativeResult<Node> {
        let lexical = self.expect(Kind::String)?.value;
        if self.at(Kind::Language) {
            let language = self.take()?.value;
            return literal(
                lexical,
                entity("datatype", iri(RDF_PLAIN_LITERAL.into())?)?,
                Some(language),
            );
        }
        let datatype = if self.at(Kind::Hat) {
            self.take()?;
            entity("datatype", self.parse_iri()?)?
        } else {
            entity("datatype", iri(RDF_PLAIN_LITERAL.into())?)?
        };
        literal(lexical, datatype, None)
    }

    fn parse_iri(&mut self) -> NativeResult<Node> {
        let token = self.peek().clone();
        let value = if token.kind == Kind::Iri {
            self.take()?;
            decode_iri_escapes(&token.value)?
        } else if token.kind == Kind::Word
            && token.value.contains(':')
            && !token.value.starts_with("_:")
        {
            self.take()?;
            let (prefix, local) = token.value.split_once(':').ok_or_else(syntax)?;
            let base = self.prefixes.get(prefix).ok_or_else(syntax)?;
            let local = decode_iri_escapes(local)?;
            let size = base
                .len()
                .checked_add(local.len())
                .ok_or_else(|| NativeError::limit("native IRI size overflow"))?;
            let mut value = String::new();
            value
                .try_reserve_exact(size)
                .map_err(|_| NativeError::limit("native IRI allocation failed"))?;
            value.push_str(base);
            value.push_str(&local);
            value
        } else {
            return Err(syntax());
        };
        enforce(
            self.session,
            LimitKey::MaxIriBytes,
            value.len(),
            "native IRI exceeds max_iri_bytes",
        )?;
        iri(value)
    }

    fn parse_swrl_rule(&mut self) -> NativeResult<Node> {
        self.expect_word("SWRLRule")?;
        self.open()?;
        let annotations = self.parse_annotations()?;
        self.open()?;
        let body = canonical_set(self.many(Self::parse_swrl_atom)?, 0, None)?;
        self.close()?;
        self.open()?;
        let head = canonical_set(self.many(Self::parse_swrl_atom)?, 0, None)?;
        self.close()?;
        enforce(
            self.session,
            LimitKey::MaxRuleAtoms,
            body.len().max(head.len()),
            "native SWRL rule exceeds max_rule_atoms",
        )?;
        self.close()?;
        Node::build(
            148,
            vec![Field::Set(body), Field::Set(head), Field::Set(annotations)],
        )
    }

    fn parse_swrl_atom(&mut self) -> NativeResult<Node> {
        let name = self.expect(Kind::Word)?.value;
        self.open()?;
        let (tag, fields) = match name.as_str() {
            "ClassAtom" => (
                141,
                vec![
                    Field::Node(self.parse_class_expression()?),
                    Field::Node(self.parse_swrl_iarg()?),
                ],
            ),
            "DataRangeAtom" => (
                142,
                vec![
                    Field::Node(self.parse_data_range()?),
                    Field::Node(self.parse_swrl_darg()?),
                ],
            ),
            "ObjectPropertyAtom" => (
                143,
                vec![
                    Field::Node(self.parse_object_property()?),
                    Field::Node(self.parse_swrl_iarg()?),
                    Field::Node(self.parse_swrl_iarg()?),
                ],
            ),
            "DataPropertyAtom" => (
                144,
                vec![
                    Field::Node(self.parse_data_property()?),
                    Field::Node(self.parse_swrl_iarg()?),
                    Field::Node(self.parse_swrl_darg()?),
                ],
            ),
            "BuiltInAtom" => (
                145,
                vec![
                    Field::Node(self.parse_iri()?),
                    Field::Sequence(self.many(Self::parse_swrl_darg)?),
                ],
            ),
            "SameIndividualAtom" | "DifferentIndividualsAtom" => (
                if name == "SameIndividualAtom" {
                    146
                } else {
                    147
                },
                vec![
                    Field::Node(self.parse_swrl_iarg()?),
                    Field::Node(self.parse_swrl_iarg()?),
                ],
            ),
            _ => return Err(syntax()),
        };
        self.close()?;
        Node::build(tag, fields)
    }

    fn parse_swrl_iarg(&mut self) -> NativeResult<Node> {
        if self.word("Variable") {
            self.parse_variable()
        } else {
            self.parse_individual()
        }
    }

    fn parse_swrl_darg(&mut self) -> NativeResult<Node> {
        if self.word("Variable") {
            self.parse_variable()
        } else {
            self.parse_literal()
        }
    }

    fn parse_variable(&mut self) -> NativeResult<Node> {
        self.expect_word("Variable")?;
        self.open()?;
        let value = self.parse_iri()?;
        self.close()?;
        Node::build(140, vec![Field::Node(value)])
    }

    fn many(&mut self, parser: fn(&mut Self) -> NativeResult<Node>) -> NativeResult<Vec<Node>> {
        let mut values = Vec::new();
        while !self.at(Kind::Close) {
            values.push(parser(self)?);
            enforce_sequence(self.session, values.len())?;
        }
        Ok(values)
    }

    fn set_many(
        &mut self,
        parser: fn(&mut Self) -> NativeResult<Node>,
        minimum: usize,
        flatten_tag: Option<u64>,
    ) -> NativeResult<Field> {
        Ok(Field::Set(canonical_set(
            self.many(parser)?,
            minimum,
            flatten_tag,
        )?))
    }

    fn open(&mut self) -> NativeResult<()> {
        self.expect(Kind::Open)?;
        self.depth = self
            .depth
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native parser depth overflow"))?;
        if self.depth > self.session.limits().value(LimitKey::MaxNestingDepth) {
            return Err(NativeError::limit(
                "native parser exceeds max_nesting_depth",
            ));
        }
        Ok(())
    }

    fn close(&mut self) -> NativeResult<()> {
        self.expect(Kind::Close)?;
        self.depth = self.depth.checked_sub(1).ok_or_else(syntax)?;
        Ok(())
    }

    fn starts_iri(&self) -> bool {
        self.peek().kind == Kind::Iri
            || (self.peek().kind == Kind::Word
                && self.peek().value.contains(':')
                && !self.peek().value.starts_with("_:"))
    }

    fn starts_document_member(&self) -> bool {
        self.peek().kind == Kind::Word
            && (matches!(
                self.peek().value.as_str(),
                "Import" | "Annotation" | "SWRLRule"
            ) || axiom_tag(&self.peek().value).is_some())
    }

    fn call_name(&self) -> Option<&str> {
        (self.peek().kind == Kind::Word
            && self
                .tokens
                .get(self.index + 1)
                .is_some_and(|token| token.kind == Kind::Open))
        .then_some(self.peek().value.as_str())
    }

    fn word(&self, value: &str) -> bool {
        self.peek().kind == Kind::Word && self.peek().value == value
    }

    fn at(&self, kind: Kind) -> bool {
        self.peek().kind == kind
    }

    fn peek(&self) -> &Token {
        &self.tokens[self.index]
    }

    fn previous(&self) -> &Token {
        &self.tokens[self.index.saturating_sub(1)]
    }

    fn take(&mut self) -> NativeResult<Token> {
        let token = self.tokens.get(self.index).ok_or_else(syntax)?.clone();
        self.index = self
            .index
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native token index overflow"))?;
        self.session.step(1)?;
        Ok(token)
    }

    fn expect(&mut self, kind: Kind) -> NativeResult<Token> {
        if self.peek().kind != kind {
            return Err(syntax());
        }
        self.take()
    }

    fn expect_word(&mut self, value: &str) -> NativeResult<Token> {
        let token = self.expect(Kind::Word)?;
        if token.value != value {
            return Err(syntax());
        }
        Ok(token)
    }
}

fn decode_iri_escapes(value: &str) -> NativeResult<String> {
    if !value.contains('\\') {
        return Ok(value.to_owned());
    }
    let bytes = value.as_bytes();
    let mut output = String::new();
    output
        .try_reserve(value.len())
        .map_err(|_| NativeError::limit("native IRI allocation failed"))?;
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] != b'\\' {
            let character = value[index..].chars().next().ok_or_else(syntax)?;
            output.push(character);
            index += character.len_utf8();
            continue;
        }
        let marker = *bytes.get(index + 1).ok_or_else(syntax)?;
        let width = match marker {
            b'u' => 4,
            b'U' => 8,
            _ => return Err(syntax()),
        };
        let start = index + 2;
        let end = start + width;
        let encoded = value.get(start..end).ok_or_else(syntax)?;
        if !encoded.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            return Err(syntax());
        }
        let codepoint = u32::from_str_radix(encoded, 16).map_err(|_| syntax())?;
        let character = char::from_u32(codepoint).ok_or_else(syntax)?;
        output.push(character);
        index = end;
    }
    Ok(output)
}

fn axiom_tag(value: &str) -> Option<u64> {
    Some(match value {
        "Declaration" => 60,
        "SubClassOf" => 61,
        "EquivalentClasses" => 62,
        "DisjointClasses" => 63,
        "DisjointUnion" => 64,
        "SubObjectPropertyOf" => 70,
        "EquivalentObjectProperties" => 71,
        "DisjointObjectProperties" => 72,
        "InverseObjectProperties" => 73,
        "ObjectPropertyDomain" => 74,
        "ObjectPropertyRange" => 75,
        "FunctionalObjectProperty" => 76,
        "InverseFunctionalObjectProperty" => 77,
        "ReflexiveObjectProperty" => 78,
        "IrreflexiveObjectProperty" => 79,
        "SymmetricObjectProperty" => 80,
        "AsymmetricObjectProperty" => 81,
        "TransitiveObjectProperty" => 82,
        "SubDataPropertyOf" => 90,
        "EquivalentDataProperties" => 91,
        "DisjointDataProperties" => 92,
        "DataPropertyDomain" => 93,
        "DataPropertyRange" => 94,
        "FunctionalDataProperty" => 95,
        "DatatypeDefinition" => 100,
        "HasKey" => 101,
        "SameIndividual" => 110,
        "DifferentIndividuals" => 111,
        "ClassAssertion" => 112,
        "ObjectPropertyAssertion" => 113,
        "NegativeObjectPropertyAssertion" => 114,
        "DataPropertyAssertion" => 115,
        "NegativeDataPropertyAssertion" => 116,
        "AnnotationAssertion" => 120,
        "SubAnnotationPropertyOf" => 121,
        "AnnotationPropertyDomain" => 122,
        "AnnotationPropertyRange" => 123,
        _ => return None,
    })
}

fn object_characteristic(value: &str) -> bool {
    matches!(
        value,
        "FunctionalObjectProperty"
            | "InverseFunctionalObjectProperty"
            | "ReflexiveObjectProperty"
            | "IrreflexiveObjectProperty"
            | "SymmetricObjectProperty"
            | "AsymmetricObjectProperty"
            | "TransitiveObjectProperty"
    )
}

fn data_range_constructor(value: &str) -> bool {
    matches!(
        value,
        "DataIntersectionOf"
            | "DataUnionOf"
            | "DataComplementOf"
            | "DataOneOf"
            | "DatatypeRestriction"
    )
}

fn enforce(
    session: &Session<'_>,
    key: LimitKey,
    observed: usize,
    message: &'static str,
) -> NativeResult<()> {
    if u64::try_from(observed).map_or(true, |value| value > session.limits().value(key)) {
        return Err(NativeError::limit(message));
    }
    Ok(())
}

fn enforce_sequence(session: &Session<'_>, observed: usize) -> NativeResult<()> {
    enforce(
        session,
        LimitKey::MaxSequenceArity,
        observed,
        "native collection exceeds max_sequence_arity",
    )
}

fn enforce_minimum(values: &[Node], minimum: usize) -> NativeResult<()> {
    if values.len() < minimum {
        return Err(syntax());
    }
    Ok(())
}

fn syntax() -> NativeError {
    NativeError::new(
        "NATIVE_FORMAT_SYNTAX",
        "native Functional Syntax input is invalid",
    )
}
