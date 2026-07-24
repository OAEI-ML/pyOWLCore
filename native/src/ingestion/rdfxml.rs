//! Forward-only Unicode RDF/XML tokenization and a closed OWL mapping slice.
//!
//! This intentionally unadvertised slice accepts ontology headers, imports,
//! named entity declarations, named axioms, and boolean class expressions. The
//! event/token model is not tied to that subset: later RDF/XML productions
//! extend the graph sink and RDF mapper rather than replacing the bounded XML
//! scanner.

use crate::canonical::{canonical_set, entity, iri, Field, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;
use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::time::Instant;

use super::rdf_class_expressions::{
    DecodedClassCollection, DecodedClassExpression, DecodedDataRange, DecodedIndividualCollection,
    DecodedKeyCollection, DecodedPropertyCollection, DecodedPropertyExpression,
    RdfClassExpressionDecoder,
};
use super::rdf_lists::{RdfResource as ListResource, RdfTerm as ListTerm, RdfTriple as ListTriple};
use super::{CanonicalDocument, CanonicalOccurrence, MappingEvidence, RdfTripleEvidence};

const RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
const OWL: &str = "http://www.w3.org/2002/07/owl#";
const XML: &str = "http://www.w3.org/XML/1998/namespace";
const XMLNS: &str = "http://www.w3.org/2000/xmlns/";
const XML_BASE: &str = "http://www.w3.org/XML/1998/namespacebase";
const XML_LANG: &str = "http://www.w3.org/XML/1998/namespacelang";
const XINCLUDE: &str = "http://www.w3.org/2001/XInclude";
const SWRL: &str = "http://www.w3.org/2003/11/swrl#";
// NUL cannot occur in an XML NCName, so generated identities cannot collide
// with an explicit rdf:nodeID spelling.
const GENERATED_BLANK_PREFIX: &str = "\0";

const RDF_RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#RDF";
const RDF_DESCRIPTION: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Description";
const RDF_TYPE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const RDF_ID: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#ID";
const RDF_ABOUT: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#about";
const RDF_PARSE_TYPE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#parseType";
const RDF_RESOURCE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#resource";
const RDF_NODE_ID: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nodeID";
const RDF_DATATYPE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#datatype";
const RDF_LI: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#li";
const RDF_XML_LITERAL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#XMLLiteral";
const RDF_STATEMENT: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Statement";
const RDF_SUBJECT: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#subject";
const RDF_PREDICATE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#predicate";
const RDF_OBJECT: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#object";
const RDF_ABOUT_EACH: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#aboutEach";
const RDF_ABOUT_EACH_PREFIX: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#aboutEachPrefix";
const RDF_BAG_ID: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#bagID";
const RDF_PROPERTY: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Property";
const RDF_PLAIN_LITERAL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
const RDF_FIRST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first";
const RDF_REST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest";
const RDF_NIL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil";
const RDFS_CLASS: &str = "http://www.w3.org/2000/01/rdf-schema#Class";
const RDFS_DATATYPE: &str = "http://www.w3.org/2000/01/rdf-schema#Datatype";
const RDFS_LITERAL: &str = "http://www.w3.org/2000/01/rdf-schema#Literal";
const OWL_ONTOLOGY: &str = "http://www.w3.org/2002/07/owl#Ontology";
const OWL_IMPORTS: &str = "http://www.w3.org/2002/07/owl#imports";
const OWL_VERSION_IRI: &str = "http://www.w3.org/2002/07/owl#versionIRI";
const OWL_RATIONAL: &str = "http://www.w3.org/2002/07/owl#rational";
const OWL_REAL: &str = "http://www.w3.org/2002/07/owl#real";
const XSD_STRING: &str = "http://www.w3.org/2001/XMLSchema#string";
const XSD_BOOLEAN: &str = "http://www.w3.org/2001/XMLSchema#boolean";

const RDFS_SUB_CLASS_OF: &str = "http://www.w3.org/2000/01/rdf-schema#subClassOf";
const RDFS_SUB_PROPERTY_OF: &str = "http://www.w3.org/2000/01/rdf-schema#subPropertyOf";
const RDFS_DOMAIN: &str = "http://www.w3.org/2000/01/rdf-schema#domain";
const RDFS_RANGE: &str = "http://www.w3.org/2000/01/rdf-schema#range";
const OWL_EQUIVALENT_CLASS: &str = "http://www.w3.org/2002/07/owl#equivalentClass";
const OWL_DISJOINT_WITH: &str = "http://www.w3.org/2002/07/owl#disjointWith";
const OWL_INTERSECTION_OF: &str = "http://www.w3.org/2002/07/owl#intersectionOf";
const OWL_UNION_OF: &str = "http://www.w3.org/2002/07/owl#unionOf";
const OWL_COMPLEMENT_OF: &str = "http://www.w3.org/2002/07/owl#complementOf";
const OWL_DATATYPE_COMPLEMENT_OF: &str = "http://www.w3.org/2002/07/owl#datatypeComplementOf";
const OWL_ONE_OF: &str = "http://www.w3.org/2002/07/owl#oneOf";
const OWL_ON_DATATYPE: &str = "http://www.w3.org/2002/07/owl#onDatatype";
const OWL_WITH_RESTRICTIONS: &str = "http://www.w3.org/2002/07/owl#withRestrictions";
const OWL_EQUIVALENT_PROPERTY: &str = "http://www.w3.org/2002/07/owl#equivalentProperty";
const OWL_PROPERTY_DISJOINT_WITH: &str = "http://www.w3.org/2002/07/owl#propertyDisjointWith";
const OWL_PROPERTY_CHAIN_AXIOM: &str = "http://www.w3.org/2002/07/owl#propertyChainAxiom";
const OWL_INVERSE_OF: &str = "http://www.w3.org/2002/07/owl#inverseOf";
const OWL_SAME_AS: &str = "http://www.w3.org/2002/07/owl#sameAs";
const OWL_DIFFERENT_FROM: &str = "http://www.w3.org/2002/07/owl#differentFrom";
const OWL_ALL_DIFFERENT: &str = "http://www.w3.org/2002/07/owl#AllDifferent";
const OWL_ALL_DISJOINT_CLASSES: &str = "http://www.w3.org/2002/07/owl#AllDisjointClasses";
const OWL_ALL_DISJOINT_PROPERTIES: &str = "http://www.w3.org/2002/07/owl#AllDisjointProperties";
const OWL_DISTINCT_MEMBERS: &str = "http://www.w3.org/2002/07/owl#distinctMembers";
const OWL_MEMBERS: &str = "http://www.w3.org/2002/07/owl#members";
const OWL_HAS_KEY: &str = "http://www.w3.org/2002/07/owl#hasKey";
const OWL_DISJOINT_UNION_OF: &str = "http://www.w3.org/2002/07/owl#disjointUnionOf";
const OWL_NEGATIVE_PROPERTY_ASSERTION: &str =
    "http://www.w3.org/2002/07/owl#NegativePropertyAssertion";
const OWL_SOURCE_INDIVIDUAL: &str = "http://www.w3.org/2002/07/owl#sourceIndividual";
const OWL_ASSERTION_PROPERTY: &str = "http://www.w3.org/2002/07/owl#assertionProperty";
const OWL_TARGET_INDIVIDUAL: &str = "http://www.w3.org/2002/07/owl#targetIndividual";
const OWL_TARGET_VALUE: &str = "http://www.w3.org/2002/07/owl#targetValue";
const OWL_AXIOM: &str = "http://www.w3.org/2002/07/owl#Axiom";
const OWL_ANNOTATION: &str = "http://www.w3.org/2002/07/owl#Annotation";
const OWL_CLASS: &str = "http://www.w3.org/2002/07/owl#Class";
const OWL_DATA_RANGE: &str = "http://www.w3.org/2002/07/owl#DataRange";
const OWL_RESTRICTION: &str = "http://www.w3.org/2002/07/owl#Restriction";
const OWL_OBJECT_PROPERTY: &str = "http://www.w3.org/2002/07/owl#ObjectProperty";
const OWL_DATATYPE_PROPERTY: &str = "http://www.w3.org/2002/07/owl#DatatypeProperty";
const OWL_ANNOTATION_PROPERTY: &str = "http://www.w3.org/2002/07/owl#AnnotationProperty";
const OWL_ONTOLOGY_PROPERTY: &str = "http://www.w3.org/2002/07/owl#OntologyProperty";
const OWL_FUNCTIONAL_PROPERTY: &str = "http://www.w3.org/2002/07/owl#FunctionalProperty";
const OWL_INVERSE_FUNCTIONAL_PROPERTY: &str =
    "http://www.w3.org/2002/07/owl#InverseFunctionalProperty";
const OWL_SYMMETRIC_PROPERTY: &str = "http://www.w3.org/2002/07/owl#SymmetricProperty";
const OWL_TRANSITIVE_PROPERTY: &str = "http://www.w3.org/2002/07/owl#TransitiveProperty";
const OWL_DEPRECATED_CLASS: &str = "http://www.w3.org/2002/07/owl#DeprecatedClass";
const OWL_DEPRECATED_PROPERTY: &str = "http://www.w3.org/2002/07/owl#DeprecatedProperty";
const OWL_DEPRECATED: &str = "http://www.w3.org/2002/07/owl#deprecated";
const OWL_ANNOTATED_SOURCE: &str = "http://www.w3.org/2002/07/owl#annotatedSource";
const OWL_ANNOTATED_PROPERTY: &str = "http://www.w3.org/2002/07/owl#annotatedProperty";
const OWL_ANNOTATED_TARGET: &str = "http://www.w3.org/2002/07/owl#annotatedTarget";
const OWL_NOTHING: &str = "http://www.w3.org/2002/07/owl#Nothing";
const SWRL_IMP: &str = "http://www.w3.org/2003/11/swrl#Imp";
const SWRL_BODY: &str = "http://www.w3.org/2003/11/swrl#body";
const SWRL_HEAD: &str = "http://www.w3.org/2003/11/swrl#head";
const SWRL_VARIABLE: &str = "http://www.w3.org/2003/11/swrl#Variable";
const SWRL_CLASS_ATOM: &str = "http://www.w3.org/2003/11/swrl#ClassAtom";
const SWRL_DATA_RANGE_ATOM: &str = "http://www.w3.org/2003/11/swrl#DataRangeAtom";
const SWRL_INDIVIDUAL_PROPERTY_ATOM: &str = "http://www.w3.org/2003/11/swrl#IndividualPropertyAtom";
const SWRL_DATAVALUED_PROPERTY_ATOM: &str = "http://www.w3.org/2003/11/swrl#DatavaluedPropertyAtom";
const SWRL_BUILTIN_ATOM: &str = "http://www.w3.org/2003/11/swrl#BuiltinAtom";
const SWRL_SAME_INDIVIDUAL_ATOM: &str = "http://www.w3.org/2003/11/swrl#SameIndividualAtom";
const SWRL_DIFFERENT_INDIVIDUALS_ATOM: &str =
    "http://www.w3.org/2003/11/swrl#DifferentIndividualsAtom";
const SWRL_CLASS_PREDICATE: &str = "http://www.w3.org/2003/11/swrl#classPredicate";
const SWRL_DATA_RANGE: &str = "http://www.w3.org/2003/11/swrl#dataRange";
const SWRL_PROPERTY_PREDICATE: &str = "http://www.w3.org/2003/11/swrl#propertyPredicate";
const SWRL_ARGUMENT_1: &str = "http://www.w3.org/2003/11/swrl#argument1";
const SWRL_ARGUMENT_2: &str = "http://www.w3.org/2003/11/swrl#argument2";
const SWRL_BUILTIN: &str = "http://www.w3.org/2003/11/swrl#builtin";
const SWRL_ARGUMENTS: &str = "http://www.w3.org/2003/11/swrl#arguments";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Span {
    byte_start: u64,
    byte_end: u64,
    line: u64,
    column: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Attribute {
    name: String,
    value: String,
}

fn is_reserved_xml_attribute(attribute: &Attribute) -> bool {
    let (prefix, local) = attribute
        .name
        .split_once(':')
        .map_or(("", attribute.name.as_str()), |(prefix, local)| {
            (prefix, local)
        });
    let reserved_part = if prefix.is_empty() { local } else { prefix };
    reserved_part
        .get(..3)
        .is_some_and(|value| value.eq_ignore_ascii_case("xml"))
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct StartEvent {
    name: String,
    attributes: Vec<Attribute>,
    empty: bool,
    span: Span,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum XmlEvent {
    Start(StartEvent),
    End { name: String, span: Span },
    Text { value: String, span: Span },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum XmlSourceEncoding {
    Utf8,
    Utf16Le,
    Utf16Be,
}

struct XmlStream<'a> {
    text: &'a str,
    source_encoding: XmlSourceEncoding,
    offset: usize,
    line: u64,
    column: u64,
    xml_declaration_seen: bool,
}

impl<'a> XmlStream<'a> {
    fn new(text: &'a str, source_encoding: XmlSourceEncoding) -> Self {
        Self {
            text,
            source_encoding,
            offset: 0,
            line: 1,
            column: 1,
            xml_declaration_seen: false,
        }
    }

    fn next(&mut self, session: &mut Session<'_>) -> NativeResult<Option<XmlEvent>> {
        loop {
            if self.offset == self.text.len() {
                return Ok(None);
            }
            let start = self.offset;
            let line = self.line;
            let column = self.column;
            if self.byte(start) != Some(b'<') {
                let end = self.find_byte(start, b'<').unwrap_or(self.text.len());
                let raw = &self.text[start..end];
                if raw.contains("]]>") {
                    return Err(xml_syntax());
                }
                let value = decode_references(raw, session)?;
                self.advance(end, session)?;
                return Ok(Some(XmlEvent::Text {
                    value,
                    span: self.span(start, end, line, column)?,
                }));
            }
            if self.starts_with(start, "<!--") {
                let body_start = start + 4;
                let marker = bounded_find(
                    self.text.as_bytes(),
                    body_start,
                    self.text.len(),
                    b"-->",
                    session,
                )?
                .ok_or_else(xml_syntax)?;
                let body = &self.text[body_start..marker];
                if body.ends_with('-')
                    || bounded_find(self.text.as_bytes(), body_start, marker, b"--", session)?
                        .is_some()
                {
                    return Err(xml_syntax());
                }
                validate_xml_characters(body)?;
                let end = marker + 3;
                self.advance(end, session)?;
                continue;
            }
            if self.starts_with(start, "<![CDATA[") {
                let body_start = start + 9;
                let body_end = bounded_find(
                    self.text.as_bytes(),
                    body_start,
                    self.text.len(),
                    b"]]>",
                    session,
                )?
                .ok_or_else(xml_syntax)?;
                let value = normalize_xml_characters(
                    &self.text[body_start..body_end],
                    XmlValueKind::Text,
                    session,
                )?;
                let end = body_end + 3;
                self.advance(end, session)?;
                return Ok(Some(XmlEvent::Text {
                    value,
                    span: self.span(start, end, line, column)?,
                }));
            }
            if self.starts_with(start, "<?") {
                let marker = bounded_find(
                    self.text.as_bytes(),
                    start + 2,
                    self.text.len(),
                    b"?>",
                    session,
                )?
                .ok_or_else(xml_syntax)?;
                let end = marker + 2;
                let body = &self.text[start + 2..end - 2];
                let target_end = scan_name(body, 0)?;
                if target_end != body.len()
                    && !body
                        .as_bytes()
                        .get(target_end)
                        .is_some_and(|value| is_xml_space(*value))
                {
                    return Err(xml_syntax());
                }
                let target = &body[..target_end];
                if !is_xml_ncname(target) {
                    return Err(xml_syntax());
                }
                if target == "xml" {
                    if start != 0 || self.xml_declaration_seen {
                        return Err(xml_syntax());
                    }
                    validate_xml_declaration(body, self.source_encoding)?;
                    self.xml_declaration_seen = true;
                } else {
                    // RDF/XML maps no processing-instruction Infoset item to
                    // a graph event. Validate the XML envelope, then discard
                    // the instruction without invoking any target handler.
                    if target.eq_ignore_ascii_case("xml") {
                        return Err(xml_syntax());
                    }
                    validate_xml_characters(&body[target_end..])?;
                }
                self.advance(end, session)?;
                continue;
            }
            if self.starts_with(start, "<!") {
                return Err(xml_forbidden());
            }
            if self.starts_with(start, "</") {
                let mut cursor = start + 2;
                skip_space(self.text.as_bytes(), &mut cursor);
                let name_end = scan_name(self.text, cursor)?;
                let name = owned_text(&self.text[cursor..name_end], session)?;
                cursor = name_end;
                skip_space(self.text.as_bytes(), &mut cursor);
                if self.byte(cursor) != Some(b'>') {
                    return Err(xml_syntax());
                }
                let end = cursor + 1;
                self.advance(end, session)?;
                return Ok(Some(XmlEvent::End {
                    name,
                    span: self.span(start, end, line, column)?,
                }));
            }
            let event = self.start_event(start, line, column, session)?;
            self.advance(event.span.byte_end as usize, session)?;
            return Ok(Some(XmlEvent::Start(event)));
        }
    }

    fn start_event(
        &self,
        start: usize,
        line: u64,
        column: u64,
        session: &mut Session<'_>,
    ) -> NativeResult<StartEvent> {
        let bytes = self.text.as_bytes();
        let mut cursor = start + 1;
        let name_end = scan_name(self.text, cursor)?;
        let name = owned_text(&self.text[cursor..name_end], session)?;
        cursor = name_end;
        let mut attributes = Vec::new();
        let empty;
        loop {
            skip_space(bytes, &mut cursor);
            match bytes.get(cursor).copied() {
                Some(b'>') => {
                    empty = false;
                    cursor += 1;
                    break;
                }
                Some(b'/') if bytes.get(cursor + 1) == Some(&b'>') => {
                    empty = true;
                    cursor += 2;
                    break;
                }
                Some(_) => {}
                None => return Err(xml_syntax()),
            }
            let attribute_end = scan_name(self.text, cursor)?;
            let attribute_name = owned_text(&self.text[cursor..attribute_end], session)?;
            if attributes
                .iter()
                .any(|value: &Attribute| value.name == attribute_name)
            {
                return Err(xml_syntax());
            }
            cursor = attribute_end;
            skip_space(bytes, &mut cursor);
            if bytes.get(cursor) != Some(&b'=') {
                return Err(xml_syntax());
            }
            cursor += 1;
            skip_space(bytes, &mut cursor);
            let quote = *bytes.get(cursor).ok_or_else(xml_syntax)?;
            if !matches!(quote, b'\'' | b'"') {
                return Err(xml_syntax());
            }
            cursor += 1;
            let value_start = cursor;
            while bytes.get(cursor).is_some_and(|value| *value != quote) {
                if bytes[cursor] == b'<' {
                    return Err(xml_syntax());
                }
                cursor += 1;
            }
            if bytes.get(cursor) != Some(&quote) {
                return Err(xml_syntax());
            }
            let value = decode_attribute_references(&self.text[value_start..cursor], session)?;
            cursor += 1;
            reserve_vec_item::<Attribute>(&mut attributes, session)?;
            attributes.push(Attribute {
                name: attribute_name,
                value,
            });
        }
        Ok(StartEvent {
            name,
            attributes,
            empty,
            span: self.span(start, cursor, line, column)?,
        })
    }

    fn advance(&mut self, end: usize, session: &mut Session<'_>) -> NativeResult<()> {
        let fragment = self.text.get(self.offset..end).ok_or_else(xml_syntax)?;
        let mut previous_cr = false;
        for character in fragment.chars() {
            session.step(1)?;
            match character {
                '\r' => {
                    self.line = self.line.saturating_add(1);
                    self.column = 1;
                    previous_cr = true;
                }
                '\n' if previous_cr => previous_cr = false,
                '\n' => {
                    self.line = self.line.saturating_add(1);
                    self.column = 1;
                    previous_cr = false;
                }
                _ => {
                    self.column = self.column.saturating_add(1);
                    previous_cr = false;
                }
            }
        }
        self.offset = end;
        Ok(())
    }

    fn span(&self, start: usize, end: usize, line: u64, column: u64) -> NativeResult<Span> {
        Ok(Span {
            byte_start: u64::try_from(start)
                .map_err(|_| NativeError::limit("native XML offset exceeds u64"))?,
            byte_end: u64::try_from(end)
                .map_err(|_| NativeError::limit("native XML offset exceeds u64"))?,
            line,
            column,
        })
    }

    fn byte(&self, offset: usize) -> Option<u8> {
        self.text.as_bytes().get(offset).copied()
    }

    fn starts_with(&self, offset: usize, value: &str) -> bool {
        self.text.as_bytes()[offset..].starts_with(value.as_bytes())
    }

    fn find_byte(&self, offset: usize, byte: u8) -> Option<usize> {
        self.text.as_bytes()[offset..]
            .iter()
            .position(|value| *value == byte)
            .map(|value| offset + value)
    }
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum Resource {
    Iri(String),
    Blank(String),
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum Term {
    Iri(String),
    Blank(String),
    Literal {
        lexical: String,
        datatype: Option<String>,
        language: Option<String>,
    },
}

impl From<Resource> for Term {
    fn from(value: Resource) -> Self {
        match value {
            Resource::Iri(value) => Self::Iri(value),
            Resource::Blank(value) => Self::Blank(value),
        }
    }
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct Triple {
    subject: Resource,
    predicate: String,
    object: Term,
}

#[derive(Clone, Copy, Debug)]
enum PythonResourceAnchor<'graph> {
    Blank(&'graph str),
    Iri(&'graph str),
}

#[derive(Clone, Copy, Debug)]
struct ComponentOccurrenceAnchor<'graph> {
    phase: u8,
    member: PythonResourceAnchor<'graph>,
}

#[derive(Clone, Debug)]
struct NamespaceBinding {
    prefix: String,
    iri: String,
}

#[derive(Clone, Debug)]
struct RdfIdBinding {
    value: String,
    base: String,
}

#[derive(Clone, Debug)]
struct XmlLiteralName {
    namespace: Option<String>,
    local: String,
}

#[derive(Clone, Debug)]
struct XmlLiteralAttribute {
    name: XmlLiteralName,
    value: String,
}

#[derive(Clone, Debug)]
enum XmlLiteralEvent {
    Start {
        name: XmlLiteralName,
        attributes: Vec<XmlLiteralAttribute>,
    },
    End,
    Text(String),
}

#[derive(Clone, Debug, Default)]
struct XmlLiteralCapture {
    events: Vec<XmlLiteralEvent>,
}

#[derive(Clone, Debug)]
struct XmlLiteralNamespace {
    iri: String,
    prefix: String,
}

#[derive(Clone, Debug)]
enum FrameRole {
    Root,
    Node {
        subject: Resource,
        next_li: u64,
    },
    Property {
        subject: Resource,
        predicate: String,
        object_set: bool,
        text: String,
        datatype: Option<String>,
        language: Option<String>,
        reification: Option<String>,
    },
    XmlLiteralProperty {
        subject: Resource,
        predicate: String,
        text: String,
        reification: Option<String>,
    },
    XmlLiteralElement,
    Collection {
        subject: Resource,
        predicate: String,
        head: Option<Resource>,
        tail: Option<Resource>,
        member_count: u64,
        reification: Option<String>,
    },
}

#[derive(Clone, Debug)]
struct Frame {
    raw_name: String,
    namespace_start: usize,
    base: Option<String>,
    language: Option<String>,
    role: FrameRole,
}

struct ParsedGraph {
    triples: Vec<Triple>,
    language_spellings: Vec<String>,
    source_blank_labels: Vec<String>,
    source_prefixes: Vec<(String, String)>,
}

struct GraphParser<'text, 'session, 'guard> {
    stream: XmlStream<'text>,
    session: &'session mut Session<'guard>,
    namespaces: Vec<NamespaceBinding>,
    rdf_ids: Vec<RdfIdBinding>,
    frames: Vec<Frame>,
    triples: Vec<Triple>,
    document_base: Option<String>,
    blank_counter: u64,
    prefix_declarations: u64,
    root_closed: bool,
    xml_literal_capture: Option<XmlLiteralCapture>,
    language_spellings: Vec<String>,
    source_blank_labels: Vec<String>,
    source_prefixes: Vec<(String, String)>,
    preserve_source_map: bool,
}

impl<'text, 'session, 'guard> GraphParser<'text, 'session, 'guard> {
    fn new(
        text: &'text str,
        document_iri: Option<&str>,
        source_encoding: XmlSourceEncoding,
        preserve_source_map: bool,
        session: &'session mut Session<'guard>,
    ) -> NativeResult<Self> {
        let binding = NamespaceBinding {
            prefix: owned_text("xml", session)?,
            iri: owned_text(XML, session)?,
        };
        let document_base = document_iri
            .map(|value| owned_text(value, session))
            .transpose()?;
        let mut namespaces = Vec::new();
        reserve_vec_item(&mut namespaces, session)?;
        namespaces.push(binding);
        Ok(Self {
            stream: XmlStream::new(text, source_encoding),
            session,
            namespaces,
            rdf_ids: Vec::new(),
            frames: Vec::new(),
            triples: Vec::new(),
            document_base,
            blank_counter: 0,
            prefix_declarations: 0,
            root_closed: false,
            xml_literal_capture: None,
            language_spellings: Vec::new(),
            source_blank_labels: Vec::new(),
            source_prefixes: Vec::new(),
            preserve_source_map,
        })
    }

    fn parse(mut self) -> NativeResult<ParsedGraph> {
        while let Some(event) = self.stream.next(self.session)? {
            match event {
                XmlEvent::Start(value) => {
                    let empty = value.empty;
                    self.start(value)?;
                    if empty {
                        self.end_empty()?;
                    }
                }
                XmlEvent::End { name, span } => self.end(&name, span)?,
                XmlEvent::Text { value, span } => self.text(value, span)?,
            }
        }
        if !self.frames.is_empty() || !self.root_closed {
            return Err(xml_syntax());
        }
        // Duplicate RDF triples collapse deterministically before mapping.
        self.triples.sort_unstable();
        self.triples.dedup();
        self.source_prefixes
            .sort_unstable_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
        self.source_blank_labels
            .sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
        self.source_blank_labels.dedup();
        Ok(ParsedGraph {
            triples: self.triples,
            language_spellings: self.language_spellings,
            source_blank_labels: self.source_blank_labels,
            source_prefixes: self.source_prefixes,
        })
    }

    fn start(&mut self, event: StartEvent) -> NativeResult<()> {
        if self.root_closed {
            return Err(xml_syntax());
        }
        let namespace_start = self.namespaces.len();
        if self.preserve_source_map {
            let xml_language = event
                .attributes
                .iter()
                .find(|attribute| attribute.name == "xml:lang")
                .map(|attribute| attribute.value.as_str());
            let plain_language = event
                .attributes
                .iter()
                .find(|attribute| attribute.name == "lang")
                .map(|attribute| attribute.value.as_str());
            let selected = xml_language
                .filter(|value| !value.is_empty())
                .or(plain_language);
            if let Some(language) = selected {
                let language = owned_text(language, self.session)?;
                reserve_vec_item(&mut self.language_spellings, self.session)?;
                self.language_spellings.push(language);
            }
        }
        for attribute in &event.attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                let prefix = attribute.name.strip_prefix("xmlns:").unwrap_or("");
                if (!prefix.is_empty() && !is_xml_ncname(prefix))
                    || prefix == "xmlns"
                    || (prefix == "xml" && attribute.value != XML)
                    || (!prefix.is_empty() && attribute.value.is_empty())
                    || attribute.value == XMLNS
                    || (attribute.value == XML && prefix != "xml")
                {
                    return Err(xml_syntax());
                }
                self.prefix_declarations = self
                    .prefix_declarations
                    .checked_add(1)
                    .ok_or_else(|| NativeError::limit("native XML prefix counter overflow"))?;
                enforce_u64(
                    self.prefix_declarations,
                    self.session.limits().value(LimitKey::MaxPrefixes),
                    "native XML namespace declarations exceed max_prefixes",
                )?;
                self.retain_source_prefix(prefix, &attribute.value)?;
                let binding = NamespaceBinding {
                    prefix: owned_text(prefix, self.session)?,
                    iri: owned_text(&attribute.value, self.session)?,
                };
                reserve_vec_item(&mut self.namespaces, self.session)?;
                self.namespaces.push(binding);
            }
        }
        let captures_xml_literal = matches!(
            self.frames.last().map(|frame| &frame.role),
            Some(FrameRole::XmlLiteralProperty { .. } | FrameRole::XmlLiteralElement)
        );
        self.validate_expanded_attribute_uniqueness(&event.attributes, !captures_xml_literal)?;
        let expanded_name = self.expand(&event.name, false)?;
        if expanded_name.starts_with(XINCLUDE) {
            return Err(xml_forbidden());
        }
        if captures_xml_literal {
            self.capture_xml_literal_start(&event.name, &expanded_name, &event.attributes)?;
            return self.push_frame(
                event.name,
                namespace_start,
                None,
                None,
                FrameRole::XmlLiteralElement,
            );
        }
        if self.preserve_source_map {
            if let Some(label) = self.attribute(&event.attributes, RDF, "nodeID")? {
                self.retain_source_blank_label(label)?;
            }
        }
        super::check_iri(
            &expanded_name,
            self.session,
            "native RDF/XML element IRI exceeds max_iri_bytes",
        )?;
        let parent_base = self
            .frames
            .last()
            .and_then(|frame| frame.base.as_deref())
            .or(self.document_base.as_deref())
            .map(|value| owned_text(value, self.session))
            .transpose()?;
        let parent_language = self
            .frames
            .last()
            .and_then(|frame| frame.language.as_deref())
            .map(|value| owned_text(value, self.session))
            .transpose()?;
        let base = match self.attribute(&event.attributes, XML, "base")? {
            Some(value) => Some(resolve_iri(value, parent_base.as_deref(), self.session)?),
            None => parent_base,
        };
        let language = self
            .attribute(&event.attributes, XML, "lang")?
            .map(|value| owned_ascii_lowercase(value, self.session))
            .transpose()?
            .or(parent_language);
        let role = if self.frames.is_empty() {
            if expanded_name == RDF_RDF {
                self.reject_unknown_attributes(&event.attributes, &[(XML, "base"), (XML, "lang")])?;
                FrameRole::Root
            } else {
                self.node_role(
                    &event.attributes,
                    &expanded_name,
                    base.as_deref(),
                    language.as_deref(),
                    None,
                )?
            }
        } else {
            match self.frames.last().map(|frame| &frame.role) {
                Some(FrameRole::Root) => self.node_role(
                    &event.attributes,
                    &expanded_name,
                    base.as_deref(),
                    language.as_deref(),
                    None,
                )?,
                Some(FrameRole::Node { subject, .. }) => {
                    let subject = clone_resource(subject, self.session)?;
                    let membership_predicate = if expanded_name == RDF_LI {
                        Some(self.next_li_property()?)
                    } else {
                        None
                    };
                    self.property_role(
                        &event.attributes,
                        subject,
                        membership_predicate.as_deref().unwrap_or(&expanded_name),
                        base.as_deref(),
                        language.as_deref(),
                    )?
                }
                Some(FrameRole::Property { object_set, .. }) if !*object_set => {
                    let role = self.node_role(
                        &event.attributes,
                        &expanded_name,
                        base.as_deref(),
                        language.as_deref(),
                        None,
                    )?;
                    let object = match &role {
                        FrameRole::Node { subject, .. } => clone_resource(subject, self.session)?,
                        _ => return Err(xml_syntax()),
                    };
                    self.set_parent_object(object)?;
                    role
                }
                Some(FrameRole::Collection { .. }) => {
                    self.check_collection_member_limit()?;
                    let role = self.node_role(
                        &event.attributes,
                        &expanded_name,
                        base.as_deref(),
                        language.as_deref(),
                        None,
                    )?;
                    let member = match &role {
                        FrameRole::Node { subject, .. } => clone_resource(subject, self.session)?,
                        _ => return Err(xml_syntax()),
                    };
                    self.append_collection_member(member)?;
                    role
                }
                _ => return Err(xml_syntax()),
            }
        };
        self.push_frame(event.name, namespace_start, base, language, role)
    }

    fn push_frame(
        &mut self,
        raw_name: String,
        namespace_start: usize,
        base: Option<String>,
        language: Option<String>,
        role: FrameRole,
    ) -> NativeResult<()> {
        let depth =
            self.frames.len().checked_add(1).ok_or_else(|| {
                NativeError::limit("native RDF/XML nesting depth counter overflow")
            })?;
        if u64::try_from(depth).map_or(true, |value| {
            value > self.session.limits().value(LimitKey::MaxNestingDepth)
        }) {
            return Err(NativeError::limit(
                "native RDF/XML nesting exceeds max_nesting_depth",
            ));
        }
        reserve_vec_item::<Frame>(&mut self.frames, self.session)?;
        self.frames.push(Frame {
            raw_name,
            namespace_start,
            base,
            language,
            role,
        });
        Ok(())
    }

    fn retain_source_prefix(&mut self, prefix: &str, iri: &str) -> NativeResult<()> {
        if !self.preserve_source_map {
            return Ok(());
        }
        let mut selected = None;
        for (index, (candidate, _value)) in self.source_prefixes.iter().enumerate() {
            self.session.step(1)?;
            if candidate == prefix {
                selected = Some(index);
                break;
            }
        }
        if iri.is_empty() {
            if let Some(index) = selected {
                self.source_prefixes.remove(index);
            }
            return Ok(());
        }
        let value = owned_text(iri, self.session)?;
        if let Some(index) = selected {
            self.source_prefixes[index].1 = value;
            return Ok(());
        }
        let prefix = owned_text(prefix, self.session)?;
        reserve_vec_item(&mut self.source_prefixes, self.session)?;
        self.source_prefixes.push((prefix, value));
        Ok(())
    }

    fn retain_source_blank_label(&mut self, label: &str) -> NativeResult<()> {
        let label = owned_text(label, self.session)?;
        reserve_vec_item(&mut self.source_blank_labels, self.session)?;
        self.source_blank_labels.push(label);
        Ok(())
    }

    fn node_role(
        &mut self,
        attributes: &[Attribute],
        expanded_name: &str,
        base: Option<&str>,
        language: Option<&str>,
        linked_subject: Option<Resource>,
    ) -> NativeResult<FrameRole> {
        if !is_node_element_iri(expanded_name) {
            return Err(xml_syntax());
        }
        let about = self.attribute(attributes, RDF, "about")?;
        let id = self.attribute(attributes, RDF, "ID")?;
        let node_id = self.attribute(attributes, RDF, "nodeID")?;
        let identity_count = usize::from(about.is_some())
            + usize::from(id.is_some())
            + usize::from(node_id.is_some());
        if identity_count > 1 {
            return Err(xml_syntax());
        }
        let subject = if let Some(subject) = linked_subject {
            if identity_count != 0 {
                return Err(xml_syntax());
            }
            subject
        } else if let Some(value) = about {
            Resource::Iri(resolve_iri(value, base, self.session)?)
        } else if let Some(value) = id {
            Resource::Iri(self.resolve_rdf_id(value, base)?)
        } else if let Some(value) = node_id {
            if !is_xml_ncname(value) {
                return Err(xml_syntax());
            }
            Resource::Blank(owned_text(value, self.session)?)
        } else {
            self.fresh_blank()?
        };
        if expanded_name != RDF_DESCRIPTION {
            let triple_subject = clone_resource(&subject, self.session)?;
            let predicate = owned_text(RDF_TYPE, self.session)?;
            let object = Term::Iri(owned_text(expanded_name, self.session)?);
            self.add(Triple {
                subject: triple_subject,
                predicate,
                object,
            })?;
        }
        self.add_node_property_attributes(attributes, &subject, base, language)?;
        Ok(FrameRole::Node {
            subject,
            next_li: 1,
        })
    }

    fn add_node_property_attributes(
        &mut self,
        attributes: &[Attribute],
        subject: &Resource,
        base: Option<&str>,
        language: Option<&str>,
    ) -> NativeResult<()> {
        self.add_property_attributes(
            attributes,
            subject,
            base,
            language,
            &[RDF_ABOUT, RDF_ID, RDF_NODE_ID, XML_BASE, XML_LANG],
            true,
        )
    }

    fn add_property_attributes(
        &mut self,
        attributes: &[Attribute],
        subject: &Resource,
        base: Option<&str>,
        language: Option<&str>,
        ignored: &[&str],
        reject_forbidden_rdf_syntax: bool,
    ) -> NativeResult<()> {
        for attribute in attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                continue;
            }
            if is_reserved_xml_attribute(attribute) {
                continue;
            }
            let predicate = self.expand_rdf_attribute(&attribute.name)?;
            if ignored.contains(&predicate.as_str()) {
                continue;
            }
            if !is_property_attribute_iri(&predicate) {
                if reject_forbidden_rdf_syntax
                    && is_forbidden_rdf_property_attribute_iri(&predicate)
                {
                    return Err(xml_syntax());
                }
                return Err(mapping_incomplete());
            }
            super::check_iri(
                &predicate,
                self.session,
                "native RDF/XML property attribute IRI exceeds max_iri_bytes",
            )?;
            let triple_subject = clone_resource(subject, self.session)?;
            let object = if predicate == RDF_TYPE {
                Term::Iri(resolve_iri(&attribute.value, base, self.session)?)
            } else {
                enforce_usize(
                    attribute.value.len(),
                    self.session.limits().value(LimitKey::MaxLiteralBytes),
                    "native RDF/XML property attribute exceeds max_literal_bytes",
                )?;
                let lexical = owned_text(&attribute.value, self.session)?;
                let language = language.filter(|value| !value.is_empty());
                let (datatype, language) = match language {
                    Some(value) => (None, Some(owned_text(value, self.session)?)),
                    None => (Some(owned_text(XSD_STRING, self.session)?), None),
                };
                Term::Literal {
                    lexical,
                    datatype,
                    language,
                }
            };
            self.add(Triple {
                subject: triple_subject,
                predicate,
                object,
            })?;
        }
        Ok(())
    }

    fn has_empty_property_attributes(&mut self, attributes: &[Attribute]) -> NativeResult<bool> {
        let mut found = false;
        for attribute in attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                continue;
            }
            if is_reserved_xml_attribute(attribute) {
                continue;
            }
            let expanded = self.expand_rdf_attribute(&attribute.name)?;
            if matches!(
                expanded.as_str(),
                RDF_ID
                    | RDF_RESOURCE
                    | RDF_NODE_ID
                    | RDF_PARSE_TYPE
                    | RDF_DATATYPE
                    | XML_BASE
                    | XML_LANG
            ) {
                continue;
            }
            if !is_property_attribute_iri(&expanded) {
                if is_forbidden_rdf_property_attribute_iri(&expanded) {
                    return Err(xml_syntax());
                }
                return Err(mapping_incomplete());
            }
            found = true;
        }
        Ok(found)
    }

    fn property_role(
        &mut self,
        attributes: &[Attribute],
        subject: Resource,
        predicate: &str,
        base: Option<&str>,
        language: Option<&str>,
    ) -> NativeResult<FrameRole> {
        if !is_property_element_iri(predicate) {
            return Err(xml_syntax());
        }
        let resource = self.attribute(attributes, RDF, "resource")?;
        let node_id = self.attribute(attributes, RDF, "nodeID")?;
        let parse_type = self.attribute(attributes, RDF, "parseType")?;
        let datatype_attribute = self.attribute(attributes, RDF, "datatype")?;
        let id = self.attribute(attributes, RDF, "ID")?;
        if usize::from(resource.is_some())
            + usize::from(node_id.is_some())
            + usize::from(parse_type.is_some())
            + usize::from(datatype_attribute.is_some())
            > 1
        {
            return Err(xml_syntax());
        }
        if node_id.is_some_and(|value| !is_xml_ncname(value)) {
            return Err(xml_syntax());
        }
        let reification = id
            .map(|value| self.resolve_rdf_id(value, base))
            .transpose()?;
        if let Some(parse_type) = parse_type {
            self.reject_unknown_attributes(
                attributes,
                &[
                    (RDF, "ID"),
                    (RDF, "parseType"),
                    (XML, "base"),
                    (XML, "lang"),
                ],
            )?;
            return match parse_type {
                "Collection" => Ok(FrameRole::Collection {
                    subject,
                    predicate: owned_text(predicate, self.session)?,
                    head: None,
                    tail: None,
                    member_count: 0,
                    reification,
                }),
                "Resource" => {
                    let object = self.fresh_blank()?;
                    let linked_object = clone_resource(&object, self.session)?;
                    if let Some(statement) = reification.as_deref() {
                        self.add_resource_statement_reification(
                            statement, &subject, predicate, &object,
                        )?;
                    }
                    self.add_resource_edge(subject, predicate, linked_object)?;
                    // RDF/XML parseType="Resource" is an implicit blank node.
                    // Reusing the normal node role lets its nested property
                    // elements stream into that node without a synthetic XML
                    // frame or an intermediate graph representation.
                    Ok(FrameRole::Node {
                        subject: object,
                        next_li: 1,
                    })
                }
                // RDF/XML treats every other parseType value exactly like
                // parseType="Literal" and emits no value-specific triples.
                _ => Ok(FrameRole::XmlLiteralProperty {
                    subject,
                    predicate: owned_text(predicate, self.session)?,
                    text: String::new(),
                    reification,
                }),
            };
        }
        let has_property_attributes = self.has_empty_property_attributes(attributes)?;
        let datatype = datatype_attribute
            .map(|value| resolve_iri(value, base, self.session))
            .transpose()?;
        let object = if let Some(value) = resource {
            Some(Resource::Iri(resolve_iri(value, base, self.session)?))
        } else {
            node_id
                .map(|value| owned_text(value, self.session).map(Resource::Blank))
                .transpose()?
        };
        // Keep the established Python/RDFLib compatibility behavior: on the
        // legacy datatype-plus-property-attribute form, rdf:datatype selects
        // the typed literal and the extra property attributes are ignored.
        let object = if object.is_none() && has_property_attributes && datatype_attribute.is_none()
        {
            Some(self.fresh_blank()?)
        } else {
            object
        };
        let object_set = object.is_some();
        if let Some(object) = object {
            if has_property_attributes {
                self.add_property_attributes(
                    attributes,
                    &object,
                    base,
                    language,
                    &[RDF_ID, RDF_RESOURCE, RDF_NODE_ID, XML_BASE, XML_LANG],
                    true,
                )?;
            }
            let triple_subject = clone_resource(&subject, self.session)?;
            if let Some(statement) = reification.as_deref() {
                self.add_resource_statement_reification(
                    statement,
                    &triple_subject,
                    predicate,
                    &object,
                )?;
            }
            self.add_resource_edge(triple_subject, predicate, object)?;
        }
        Ok(FrameRole::Property {
            subject,
            predicate: owned_text(predicate, self.session)?,
            object_set,
            text: String::new(),
            datatype,
            language: language
                .map(|value| owned_text(value, self.session))
                .transpose()?,
            reification: if object_set { None } else { reification },
        })
    }

    fn next_li_property(&mut self) -> NativeResult<String> {
        let next = match self.frames.last_mut().map(|frame| &mut frame.role) {
            Some(FrameRole::Node { next_li, .. }) => {
                let current = *next_li;
                *next_li = next_li
                    .checked_add(1)
                    .ok_or_else(|| NativeError::limit("native RDF/XML rdf:li counter overflow"))?;
                current
            }
            _ => return Err(xml_syntax()),
        };
        rdf_membership_property(next, self.session)
    }

    fn resolve_rdf_id(&mut self, value: &str, base: Option<&str>) -> NativeResult<String> {
        if !is_xml_ncname(value) {
            return Err(xml_syntax());
        }
        let fragment = prefixed_text("#", value, self.session)?;
        let resolved = resolve_iri(&fragment, base, self.session)?;
        let base = base.ok_or_else(|| {
            NativeError::protocol("native RDF/XML resolved rdf:ID is missing its base")
        })?;
        self.session.step(
            u64::try_from(self.rdf_ids.len())
                .map_err(|_| NativeError::limit("native RDF/XML rdf:ID work exceeds u64"))?,
        )?;
        if self
            .rdf_ids
            .iter()
            .any(|binding| binding.value == value && binding.base == base)
        {
            return Err(xml_syntax());
        }
        let binding = RdfIdBinding {
            value: owned_text(value, self.session)?,
            base: owned_text(base, self.session)?,
        };
        reserve_vec_item(&mut self.rdf_ids, self.session)?;
        self.rdf_ids.push(binding);
        Ok(resolved)
    }

    fn set_parent_object(&mut self, object: Resource) -> NativeResult<()> {
        let (subject, predicate, reification) =
            match self.frames.last_mut().map(|frame| &mut frame.role) {
                Some(FrameRole::Property {
                    subject,
                    predicate,
                    object_set,
                    reification,
                    ..
                }) if !*object_set => {
                    *object_set = true;
                    (
                        clone_resource(subject, self.session)?,
                        owned_text(predicate, self.session)?,
                        reification.take(),
                    )
                }
                _ => return Err(xml_syntax()),
            };
        if let Some(statement) = reification.as_deref() {
            self.add_resource_statement_reification(statement, &subject, &predicate, &object)?;
        }
        self.add(Triple {
            subject,
            predicate,
            object: object.into(),
        })
    }

    fn check_collection_member_limit(&self) -> NativeResult<()> {
        let member_count = match self.frames.last().map(|frame| &frame.role) {
            Some(FrameRole::Collection { member_count, .. }) => *member_count,
            _ => return Err(xml_syntax()),
        };
        let following = member_count
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF list length overflow"))?;
        enforce_u64(
            following,
            self.session.limits().value(LimitKey::MaxRdfListLength),
            "native RDF list exceeds max_rdf_list_length",
        )
    }

    fn append_collection_member(&mut self, member: Resource) -> NativeResult<()> {
        let (subject, predicate, tail, member_count) =
            match self.frames.last().map(|frame| &frame.role) {
                Some(FrameRole::Collection {
                    subject,
                    predicate,
                    tail,
                    member_count,
                    ..
                }) => (
                    clone_resource(subject, self.session)?,
                    owned_text(predicate, self.session)?,
                    tail.as_ref()
                        .map(|value| clone_resource(value, self.session))
                        .transpose()?,
                    *member_count,
                ),
                _ => return Err(xml_syntax()),
            };
        let following = member_count
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF list length overflow"))?;
        enforce_u64(
            following,
            self.session.limits().value(LimitKey::MaxRdfListLength),
            "native RDF list exceeds max_rdf_list_length",
        )?;
        let cell = self.fresh_blank()?;
        let linked_cell = clone_resource(&cell, self.session)?;
        let head = if tail.is_none() {
            Some(clone_resource(&cell, self.session)?)
        } else {
            None
        };
        match tail {
            Some(tail) => self.add_resource_edge(tail, RDF_REST, linked_cell)?,
            None => self.add_resource_edge(subject, &predicate, linked_cell)?,
        }
        let first_subject = clone_resource(&cell, self.session)?;
        self.add_resource_edge(first_subject, RDF_FIRST, member)?;
        match self.frames.last_mut().map(|frame| &mut frame.role) {
            Some(FrameRole::Collection {
                head: retained_head,
                tail,
                member_count,
                ..
            }) => {
                if retained_head.is_none() {
                    *retained_head = head;
                }
                *tail = Some(cell);
                *member_count = following;
                Ok(())
            }
            _ => Err(xml_syntax()),
        }
    }

    fn add_resource_edge(
        &mut self,
        subject: Resource,
        predicate: &str,
        object: Resource,
    ) -> NativeResult<()> {
        let predicate = owned_text(predicate, self.session)?;
        self.add(Triple {
            subject,
            predicate,
            object: object.into(),
        })
    }

    fn add_resource_statement_reification(
        &mut self,
        statement: &str,
        subject: &Resource,
        predicate: &str,
        object: &Resource,
    ) -> NativeResult<()> {
        let object = clone_resource(object, self.session)?.into();
        self.add_statement_reification(statement, subject, predicate, object)
    }

    fn add_statement_reification(
        &mut self,
        statement: &str,
        subject: &Resource,
        predicate: &str,
        object: Term,
    ) -> NativeResult<()> {
        let type_triple = Triple {
            subject: Resource::Iri(owned_text(statement, self.session)?),
            predicate: owned_text(RDF_TYPE, self.session)?,
            object: Term::Iri(owned_text(RDF_STATEMENT, self.session)?),
        };
        self.add(type_triple)?;
        let subject_triple = Triple {
            subject: Resource::Iri(owned_text(statement, self.session)?),
            predicate: owned_text(RDF_SUBJECT, self.session)?,
            object: clone_resource(subject, self.session)?.into(),
        };
        self.add(subject_triple)?;
        let predicate_triple = Triple {
            subject: Resource::Iri(owned_text(statement, self.session)?),
            predicate: owned_text(RDF_PREDICATE, self.session)?,
            object: Term::Iri(owned_text(predicate, self.session)?),
        };
        self.add(predicate_triple)?;
        let object_triple = Triple {
            subject: Resource::Iri(owned_text(statement, self.session)?),
            predicate: owned_text(RDF_OBJECT, self.session)?,
            object,
        };
        self.add(object_triple)
    }

    fn fresh_blank(&mut self) -> NativeResult<Resource> {
        self.blank_counter = self
            .blank_counter
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF blank counter overflow"))?;
        generated_blank(self.blank_counter, self.session).map(Resource::Blank)
    }

    fn text(&mut self, value: String, _span: Span) -> NativeResult<()> {
        if value.is_empty() {
            return Ok(());
        }
        let capture = matches!(
            self.frames.last().map(|frame| &frame.role),
            Some(FrameRole::XmlLiteralElement)
        ) || (self.xml_literal_capture.is_some()
            && matches!(
                self.frames.last().map(|frame| &frame.role),
                Some(FrameRole::XmlLiteralProperty { .. })
            ));
        if capture {
            return self.capture_xml_literal_text(value);
        }
        match self.frames.last_mut().map(|frame| &mut frame.role) {
            Some(
                FrameRole::Property {
                    object_set: false,
                    text,
                    ..
                }
                | FrameRole::XmlLiteralProperty { text, .. },
            ) => {
                let next = text
                    .len()
                    .checked_add(value.len())
                    .ok_or_else(|| NativeError::limit("native XML literal size overflow"))?;
                enforce_usize(
                    next,
                    self.session.limits().value(LimitKey::MaxLiteralBytes),
                    "native XML literal exceeds max_literal_bytes",
                )?;
                self.session.reserve_bytes(value.len())?;
                text.try_reserve_exact(value.len())
                    .map_err(|_| NativeError::limit("native XML literal allocation failed"))?;
                text.push_str(&value);
                Ok(())
            }
            Some(_) if value.chars().all(char::is_whitespace) => Ok(()),
            None if value.chars().all(char::is_whitespace) => Ok(()),
            _ => Err(xml_syntax()),
        }
    }

    fn end_empty(&mut self) -> NativeResult<()> {
        let name = self
            .frames
            .last()
            .map(|frame| owned_text(&frame.raw_name, self.session))
            .transpose()?
            .ok_or_else(xml_syntax)?;
        self.end(
            &name,
            Span {
                byte_start: 0,
                byte_end: 0,
                line: 1,
                column: 1,
            },
        )
    }

    fn end(&mut self, raw_name: &str, _span: Span) -> NativeResult<()> {
        let frame = self.frames.pop().ok_or_else(xml_syntax)?;
        if frame.raw_name != raw_name {
            return Err(xml_syntax());
        }
        match frame.role {
            FrameRole::Property {
                subject,
                predicate,
                object_set,
                text,
                datatype,
                language,
                reification,
            } => {
                if object_set {
                    if !text.chars().all(char::is_whitespace) {
                        return Err(xml_syntax());
                    }
                    if reification.is_some() {
                        return Err(NativeError::protocol(
                            "native RDF/XML resource statement reification was not emitted",
                        ));
                    }
                } else {
                    let (datatype, language) = match (datatype, language) {
                        (Some(value), _) => (Some(value), None),
                        (None, Some(value)) if !value.is_empty() => (None, Some(value)),
                        (None, _) => (Some(owned_text(XSD_STRING, self.session)?), None),
                    };
                    let object = Term::Literal {
                        lexical: text,
                        datatype,
                        language,
                    };
                    if let Some(statement) = reification.as_deref() {
                        let reified_object = clone_term(&object, self.session)?;
                        self.add_statement_reification(
                            statement,
                            &subject,
                            &predicate,
                            reified_object,
                        )?;
                    }
                    self.add(Triple {
                        subject,
                        predicate,
                        object,
                    })?;
                }
            }
            FrameRole::Collection {
                subject,
                predicate,
                head,
                tail,
                reification,
                ..
            } => {
                let nil = Resource::Iri(owned_text(RDF_NIL, self.session)?);
                let object = match tail {
                    Some(tail) => {
                        let linked_nil = clone_resource(&nil, self.session)?;
                        self.add_resource_edge(tail, RDF_REST, linked_nil)?;
                        head.ok_or_else(|| {
                            NativeError::protocol(
                                "native RDF/XML nonempty collection has no retained head",
                            )
                        })?
                    }
                    None => {
                        let linked_subject = clone_resource(&subject, self.session)?;
                        let linked_nil = clone_resource(&nil, self.session)?;
                        self.add_resource_edge(linked_subject, &predicate, linked_nil)?;
                        nil
                    }
                };
                if let Some(statement) = reification.as_deref() {
                    self.add_resource_statement_reification(
                        statement, &subject, &predicate, &object,
                    )?;
                }
            }
            FrameRole::XmlLiteralProperty {
                subject,
                predicate,
                mut text,
                reification,
            } => {
                if let Some(capture) = self.xml_literal_capture.take() {
                    self.append_xml_literal_capture(&capture, &mut text)?;
                }
                let datatype = owned_text(RDF_XML_LITERAL, self.session)?;
                let object = Term::Literal {
                    lexical: text,
                    datatype: Some(datatype),
                    language: None,
                };
                if let Some(statement) = reification.as_deref() {
                    let reified_object = clone_term(&object, self.session)?;
                    self.add_statement_reification(
                        statement,
                        &subject,
                        &predicate,
                        reified_object,
                    )?;
                }
                self.add(Triple {
                    subject,
                    predicate,
                    object,
                })?;
            }
            FrameRole::XmlLiteralElement => self.capture_xml_literal_end()?,
            FrameRole::Root | FrameRole::Node { .. } => {}
        }
        self.namespaces.truncate(frame.namespace_start);
        if self.frames.is_empty() {
            self.root_closed = true;
        }
        Ok(())
    }

    fn capture_xml_literal_start(
        &mut self,
        raw_name: &str,
        expanded_name: &str,
        attributes: &[Attribute],
    ) -> NativeResult<()> {
        let name = self.xml_literal_name(raw_name, expanded_name)?;
        let mut captured_attributes = Vec::new();
        for attribute in attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                continue;
            }
            let expanded = self.expand(&attribute.name, true)?;
            let name = self.xml_literal_name(&attribute.name, &expanded)?;
            let value = owned_text(&attribute.value, self.session)?;
            reserve_vec_item(&mut captured_attributes, self.session)?;
            captured_attributes.push(XmlLiteralAttribute { name, value });
        }
        let capture = self
            .xml_literal_capture
            .get_or_insert_with(XmlLiteralCapture::default);
        reserve_vec_item(&mut capture.events, self.session)?;
        capture.events.push(XmlLiteralEvent::Start {
            name,
            attributes: captured_attributes,
        });
        Ok(())
    }

    fn capture_xml_literal_text(&mut self, value: String) -> NativeResult<()> {
        let capture = self
            .xml_literal_capture
            .as_mut()
            .ok_or_else(|| NativeError::protocol("native XML literal capture is absent"))?;
        reserve_vec_item(&mut capture.events, self.session)?;
        capture.events.push(XmlLiteralEvent::Text(value));
        Ok(())
    }

    fn capture_xml_literal_end(&mut self) -> NativeResult<()> {
        let capture = self
            .xml_literal_capture
            .as_mut()
            .ok_or_else(|| NativeError::protocol("native XML literal capture is absent"))?;
        reserve_vec_item(&mut capture.events, self.session)?;
        capture.events.push(XmlLiteralEvent::End);
        Ok(())
    }

    fn xml_literal_name(&mut self, raw: &str, expanded: &str) -> NativeResult<XmlLiteralName> {
        let local = raw.split_once(':').map_or(raw, |(_, local)| local);
        let namespace_length = expanded.len().checked_sub(local.len()).ok_or_else(|| {
            NativeError::protocol("native XML literal expanded name is shorter than its local name")
        })?;
        if !expanded.ends_with(local) {
            return Err(NativeError::protocol(
                "native XML literal expanded name does not preserve its local name",
            ));
        }
        let namespace = (namespace_length != 0)
            .then(|| owned_text(&expanded[..namespace_length], self.session))
            .transpose()?;
        Ok(XmlLiteralName {
            namespace,
            local: owned_text(local, self.session)?,
        })
    }

    fn append_xml_literal_capture(
        &mut self,
        capture: &XmlLiteralCapture,
        output: &mut String,
    ) -> NativeResult<()> {
        let mut cursor = 0;
        while cursor < capture.events.len() {
            if !matches!(
                capture.events.get(cursor),
                Some(XmlLiteralEvent::Start { .. })
            ) {
                return Err(NativeError::protocol(
                    "native XML literal capture has a non-element root",
                ));
            }
            let end = self.xml_literal_element_end(&capture.events, cursor)?;
            let mut namespaces = self.xml_literal_namespaces(&capture.events[cursor..=end])?;
            namespaces.sort_unstable_by(|left, right| left.prefix.cmp(&right.prefix));
            self.serialize_xml_literal_element(
                &capture.events,
                &mut cursor,
                &namespaces,
                output,
                true,
            )?;
            while let Some(XmlLiteralEvent::Text(value)) = capture.events.get(cursor) {
                self.append_xml_literal_escaped(output, value, false)?;
                cursor += 1;
            }
        }
        Ok(())
    }

    fn xml_literal_element_end(
        &mut self,
        events: &[XmlLiteralEvent],
        start: usize,
    ) -> NativeResult<usize> {
        let mut depth = 0_u64;
        for (offset, event) in events[start..].iter().enumerate() {
            self.session.step(1)?;
            match event {
                XmlLiteralEvent::Start { .. } => {
                    depth = depth.checked_add(1).ok_or_else(|| {
                        NativeError::limit("native XML literal capture depth overflow")
                    })?;
                }
                XmlLiteralEvent::End => {
                    depth = depth.checked_sub(1).ok_or_else(|| {
                        NativeError::protocol("native XML literal capture closes before it opens")
                    })?;
                    if depth == 0 {
                        return start.checked_add(offset).ok_or_else(|| {
                            NativeError::limit("native XML literal capture offset overflow")
                        });
                    }
                }
                XmlLiteralEvent::Text(_) => {}
            }
        }
        Err(NativeError::protocol(
            "native XML literal capture has an unclosed element",
        ))
    }

    fn xml_literal_namespaces(
        &mut self,
        events: &[XmlLiteralEvent],
    ) -> NativeResult<Vec<XmlLiteralNamespace>> {
        let mut namespaces = Vec::new();
        for event in events {
            self.session.step(1)?;
            let XmlLiteralEvent::Start { name, attributes } = event else {
                continue;
            };
            self.add_xml_literal_namespace(&mut namespaces, name)?;
            for attribute in attributes {
                self.add_xml_literal_namespace(&mut namespaces, &attribute.name)?;
            }
        }
        Ok(namespaces)
    }

    fn add_xml_literal_namespace(
        &mut self,
        namespaces: &mut Vec<XmlLiteralNamespace>,
        name: &XmlLiteralName,
    ) -> NativeResult<()> {
        let Some(iri) = name.namespace.as_deref() else {
            return Ok(());
        };
        if iri == XML || namespaces.iter().any(|entry| entry.iri == iri) {
            return Ok(());
        }
        let prefix = match element_tree_namespace_prefix(iri) {
            Some(value) => owned_text(value, self.session)?,
            None => numbered_xml_prefix(namespaces.len(), self.session)?,
        };
        reserve_vec_item(namespaces, self.session)?;
        namespaces.push(XmlLiteralNamespace {
            iri: owned_text(iri, self.session)?,
            prefix,
        });
        Ok(())
    }

    fn serialize_xml_literal_element(
        &mut self,
        events: &[XmlLiteralEvent],
        cursor: &mut usize,
        namespaces: &[XmlLiteralNamespace],
        output: &mut String,
        root: bool,
    ) -> NativeResult<()> {
        self.session.step(1)?;
        let (name, attributes) = match events.get(*cursor) {
            Some(XmlLiteralEvent::Start { name, attributes }) => (name, attributes),
            _ => {
                return Err(NativeError::protocol(
                    "native XML literal serializer expected an element",
                ))
            }
        };
        *cursor = cursor
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native XML literal cursor overflow"))?;
        self.append_xml_literal_piece(output, "<")?;
        self.append_xml_literal_name(output, name, namespaces)?;
        if root {
            for namespace in namespaces {
                self.append_xml_literal_piece(output, " xmlns:")?;
                self.append_xml_literal_piece(output, &namespace.prefix)?;
                self.append_xml_literal_piece(output, "=\"")?;
                self.append_xml_literal_escaped(output, &namespace.iri, true)?;
                self.append_xml_literal_piece(output, "\"")?;
            }
        }
        for attribute in attributes {
            self.append_xml_literal_piece(output, " ")?;
            self.append_xml_literal_name(output, &attribute.name, namespaces)?;
            self.append_xml_literal_piece(output, "=\"")?;
            self.append_xml_literal_escaped(output, &attribute.value, true)?;
            self.append_xml_literal_piece(output, "\"")?;
        }
        if matches!(events.get(*cursor), Some(XmlLiteralEvent::End)) {
            self.append_xml_literal_piece(output, ">")?;
            *cursor = cursor
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native XML literal cursor overflow"))?;
            self.append_xml_literal_piece(output, "</")?;
            self.append_xml_literal_name(output, name, namespaces)?;
            self.append_xml_literal_piece(output, ">")?;
            return Ok(());
        }
        self.append_xml_literal_piece(output, ">")?;
        loop {
            match events.get(*cursor) {
                Some(XmlLiteralEvent::Text(value)) => {
                    self.append_xml_literal_escaped(output, value, false)?;
                    *cursor = cursor
                        .checked_add(1)
                        .ok_or_else(|| NativeError::limit("native XML literal cursor overflow"))?;
                }
                Some(XmlLiteralEvent::Start { .. }) => {
                    self.serialize_xml_literal_element(events, cursor, namespaces, output, false)?
                }
                Some(XmlLiteralEvent::End) => {
                    *cursor = cursor
                        .checked_add(1)
                        .ok_or_else(|| NativeError::limit("native XML literal cursor overflow"))?;
                    self.append_xml_literal_piece(output, "</")?;
                    self.append_xml_literal_name(output, name, namespaces)?;
                    self.append_xml_literal_piece(output, ">")?;
                    return Ok(());
                }
                None => {
                    return Err(NativeError::protocol(
                        "native XML literal serializer reached an unclosed element",
                    ))
                }
            }
        }
    }

    fn append_xml_literal_name(
        &mut self,
        output: &mut String,
        name: &XmlLiteralName,
        namespaces: &[XmlLiteralNamespace],
    ) -> NativeResult<()> {
        if let Some(iri) = name.namespace.as_deref() {
            let prefix = if iri == XML {
                "xml"
            } else {
                namespaces
                    .iter()
                    .find(|entry| entry.iri == iri)
                    .map(|entry| entry.prefix.as_str())
                    .ok_or_else(|| {
                        NativeError::protocol("native XML literal namespace is absent")
                    })?
            };
            if !prefix.is_empty() {
                self.append_xml_literal_piece(output, prefix)?;
                self.append_xml_literal_piece(output, ":")?;
            }
        }
        self.append_xml_literal_piece(output, &name.local)
    }

    fn append_xml_literal_escaped(
        &mut self,
        output: &mut String,
        value: &str,
        attribute: bool,
    ) -> NativeResult<()> {
        let mut start = 0;
        let mut checkpoint = 0;
        for (offset, byte) in value.bytes().enumerate() {
            if offset == checkpoint {
                self.session.finish()?;
                checkpoint = checkpoint.saturating_add(64 * 1024);
            }
            let replacement = match byte {
                b'&' => Some("&amp;"),
                b'<' => Some("&lt;"),
                b'>' => Some("&gt;"),
                b'\"' if attribute => Some("&quot;"),
                b'\r' if attribute => Some("&#13;"),
                b'\n' if attribute => Some("&#10;"),
                b'\t' if attribute => Some("&#09;"),
                _ => None,
            };
            let Some(replacement) = replacement else {
                continue;
            };
            self.append_xml_literal_piece(output, &value[start..offset])?;
            self.append_xml_literal_piece(output, replacement)?;
            start = offset + 1;
        }
        self.append_xml_literal_piece(output, &value[start..])
    }

    fn append_xml_literal_piece(&mut self, output: &mut String, value: &str) -> NativeResult<()> {
        let next = output
            .len()
            .checked_add(value.len())
            .ok_or_else(|| NativeError::limit("native XML literal size overflow"))?;
        enforce_usize(
            next,
            self.session.limits().value(LimitKey::MaxLiteralBytes),
            "native XML literal exceeds max_literal_bytes",
        )?;
        self.session.reserve_bytes(value.len())?;
        output
            .try_reserve_exact(value.len())
            .map_err(|_| NativeError::limit("native XML literal allocation failed"))?;
        output.push_str(value);
        Ok(())
    }

    fn expand(&mut self, raw: &str, attribute: bool) -> NativeResult<String> {
        let (prefix, local) = match raw.split_once(':') {
            Some((prefix, local)) if is_xml_ncname(prefix) && is_xml_ncname(local) => {
                (Some(prefix), local)
            }
            Some(_) => return Err(xml_syntax()),
            None if is_xml_ncname(raw) => (None, raw),
            None => return Err(xml_syntax()),
        };
        let namespace = match prefix {
            Some(prefix) => self
                .namespaces
                .iter()
                .rev()
                .find(|binding| binding.prefix == prefix)
                .map(|binding| binding.iri.as_str())
                .ok_or_else(xml_syntax)?,
            None if attribute => "",
            None => self
                .namespaces
                .iter()
                .rev()
                .find(|binding| binding.prefix.is_empty())
                .map_or("", |binding| binding.iri.as_str()),
        };
        let size = namespace
            .len()
            .checked_add(local.len())
            .ok_or_else(|| NativeError::limit("native XML expanded-name size overflow"))?;
        self.session.reserve_bytes(size)?;
        let mut expanded = String::new();
        expanded
            .try_reserve_exact(size)
            .map_err(|_| NativeError::limit("native XML expanded-name allocation failed"))?;
        expanded.push_str(namespace);
        expanded.push_str(local);
        Ok(expanded)
    }

    fn expand_rdf_attribute(&mut self, raw: &str) -> NativeResult<String> {
        if let Some((_, expanded)) = legacy_unqualified_rdf_attribute(raw) {
            owned_text(expanded, self.session)
        } else {
            self.expand(raw, true)
        }
    }

    fn attribute<'c>(
        &mut self,
        attributes: &'c [Attribute],
        namespace: &str,
        local: &str,
    ) -> NativeResult<Option<&'c str>> {
        for attribute in attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                continue;
            }
            if namespace != XML && is_reserved_xml_attribute(attribute) {
                continue;
            }
            let expanded = self.expand_rdf_attribute(&attribute.name)?;
            if expanded_name_matches(&expanded, namespace, local) {
                return Ok(Some(&attribute.value));
            }
        }
        Ok(None)
    }

    fn reject_unknown_attributes(
        &mut self,
        attributes: &[Attribute],
        allowed: &[(&str, &str)],
    ) -> NativeResult<()> {
        for attribute in attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                continue;
            }
            if is_reserved_xml_attribute(attribute) {
                continue;
            }
            let expanded = self.expand_rdf_attribute(&attribute.name)?;
            if !allowed
                .iter()
                .any(|(namespace, local)| expanded_name_matches(&expanded, namespace, local))
            {
                return Err(xml_syntax());
            }
        }
        Ok(())
    }

    fn validate_expanded_attribute_uniqueness(
        &mut self,
        attributes: &[Attribute],
        rdf_semantics: bool,
    ) -> NativeResult<()> {
        let mut legacy_attributes = [false; 5];
        let count = attributes
            .iter()
            .filter(|attribute| attribute.name != "xmlns" && !attribute.name.starts_with("xmlns:"))
            .count();
        let metadata = count
            .checked_mul(std::mem::size_of::<(String, bool)>())
            .ok_or_else(|| NativeError::limit("native XML attribute accounting overflow"))?;
        self.session.reserve_bytes(metadata)?;
        let mut expanded = Vec::new();
        expanded
            .try_reserve_exact(count)
            .map_err(|_| NativeError::limit("native XML attribute ledger allocation failed"))?;
        for attribute in attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                continue;
            }
            let reserved = is_reserved_xml_attribute(attribute);
            if rdf_semantics && !reserved {
                if let Some((index, _)) = legacy_unqualified_rdf_attribute(&attribute.name) {
                    legacy_attributes[index] = true;
                }
            }
            expanded.push((self.expand(&attribute.name, true)?, reserved));
        }
        expanded.sort_unstable_by(|left, right| left.0.cmp(&right.0));
        if expanded.windows(2).any(|pair| pair[0].0 == pair[1].0) {
            return Err(xml_syntax());
        }
        if rdf_semantics
            && legacy_attributes
                .into_iter()
                .zip([RDF_ID, RDF_ABOUT, RDF_RESOURCE, RDF_PARSE_TYPE, RDF_TYPE])
                .any(|(present, target)| {
                    present
                        && expanded
                            .iter()
                            .any(|(value, reserved)| !reserved && value == target)
                })
        {
            return Err(xml_syntax());
        }
        Ok(())
    }

    fn add(&mut self, triple: Triple) -> NativeResult<()> {
        self.session.step(
            u64::try_from(self.triples.len())
                .map_err(|_| NativeError::limit("native RDF duplicate work exceeds u64"))?,
        )?;
        if self.triples.contains(&triple) {
            return Ok(());
        }
        let next = self
            .triples
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF triple count overflow"))?;
        enforce_usize(
            next,
            self.session.limits().value(LimitKey::MaxTriples),
            "native RDF graph exceeds max_triples",
        )?;
        reserve_vec_item(&mut self.triples, self.session)?;
        self.triples.push(triple);
        Ok(())
    }
}

pub(super) fn parse_and_map(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<CanonicalDocument> {
    Ok(parse_and_map_timed(source, document_iri, true, false, false, false, session)?.0)
}

pub(super) fn parse_and_map_timed(
    source: &[u8],
    document_iri: Option<&str>,
    allow_swrl: bool,
    allow_partial_rdf_mapping: bool,
    capture_occurrences: bool,
    preserve_source_map: bool,
    session: &mut Session<'_>,
) -> NativeResult<(CanonicalDocument, u64)> {
    let (graph, decoded_codepoints) =
        parse_graph_source(source, document_iri, preserve_source_map, session)?;
    let mapping_started = Instant::now();
    let mut document = map_graph(
        graph.triples,
        decoded_codepoints,
        allow_swrl,
        allow_partial_rdf_mapping,
        capture_occurrences,
        graph.language_spellings,
        session,
    )?;
    document.source_blank_labels = graph.source_blank_labels;
    document.source_prefixes = graph.source_prefixes;
    let mapping_ns = u64::try_from(mapping_started.elapsed().as_nanos())
        .map_err(|_| NativeError::limit("native RDF mapping phase time exceeds u64"))?;
    Ok((document, mapping_ns))
}

fn parse_graph_source(
    source: &[u8],
    document_iri: Option<&str>,
    preserve_source_map: bool,
    session: &mut Session<'_>,
) -> NativeResult<(ParsedGraph, u64)> {
    let (text, decoded_codepoints, source_encoding) = decode_xml(source, session)?;
    let utf8_bom =
        source_encoding == XmlSourceEncoding::Utf8 && source.starts_with(&[0xef, 0xbb, 0xbf]);
    let text = if utf8_bom {
        text.strip_prefix('\u{feff}').ok_or_else(xml_syntax)?
    } else {
        &text
    };
    let decoded_codepoints = decoded_codepoints.saturating_sub(u64::from(utf8_bom));
    let graph = GraphParser::new(
        text,
        document_iri,
        source_encoding,
        preserve_source_map,
        session,
    )?
    .parse()?;
    Ok((graph, decoded_codepoints))
}

#[cfg(feature = "test-hooks")]
pub(super) fn parse_graph_observation(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let (mut graph, _) = parse_graph_source(source, document_iri, false, session)?;
    graph.triples.sort_unstable();
    encode_graph_observation(&graph.triples, session)
}

#[cfg(feature = "test-hooks")]
fn encode_graph_observation(
    triples: &[Triple],
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    const MAGIC: &[u8; 8] = b"PYRXGRF1";

    let size = graph_observation_size(triples)?;
    if u64::try_from(size).map_or(true, |size| {
        size > session.limits().value(LimitKey::MaxTemporaryBytes)
    }) {
        return Err(NativeError::limit(
            "native RDF/XML graph observation exceeds max_temporary_bytes",
        ));
    }
    session.reserve_bytes(size)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF/XML graph observation allocation failed"))?;
    output.extend_from_slice(MAGIC);
    output.extend_from_slice(&1_u16.to_le_bytes());
    output.extend_from_slice(&0_u16.to_le_bytes());
    output.extend_from_slice(
        &u64::try_from(triples.len())
            .map_err(|_| NativeError::limit("native RDF/XML graph triple count exceeds u64"))?
            .to_le_bytes(),
    );
    for triple in triples {
        encode_graph_resource(&triple.subject, &mut output)?;
        encode_graph_frame(&triple.predicate, &mut output)?;
        encode_graph_term(&triple.object, &mut output)?;
    }
    if output.len() != size {
        return Err(NativeError::protocol(
            "native RDF/XML graph observation size ledger diverged",
        ));
    }
    Ok(output)
}

#[cfg(feature = "test-hooks")]
fn graph_observation_size(triples: &[Triple]) -> NativeResult<usize> {
    let mut size = checked_graph_observation_add(8, 2 + 2 + 8)?;
    for triple in triples {
        size = checked_graph_observation_add(size, graph_resource_size(&triple.subject)?)?;
        size = checked_graph_observation_add(size, graph_frame_size(&triple.predicate)?)?;
        size = checked_graph_observation_add(size, graph_term_size(&triple.object)?)?;
    }
    Ok(size)
}

#[cfg(feature = "test-hooks")]
fn graph_resource_size(value: &Resource) -> NativeResult<usize> {
    let value = match value {
        Resource::Iri(value) | Resource::Blank(value) => value,
    };
    checked_graph_observation_add(1, graph_frame_size(value)?)
}

#[cfg(feature = "test-hooks")]
fn graph_term_size(value: &Term) -> NativeResult<usize> {
    match value {
        Term::Iri(value) | Term::Blank(value) => {
            checked_graph_observation_add(1, graph_frame_size(value)?)
        }
        Term::Literal {
            lexical,
            datatype,
            language,
        } => {
            let mut size = checked_graph_observation_add(1, graph_frame_size(lexical)?)?;
            size = checked_graph_observation_add(size, graph_optional_size(datatype.as_deref())?)?;
            checked_graph_observation_add(size, graph_optional_size(language.as_deref())?)
        }
    }
}

#[cfg(feature = "test-hooks")]
fn graph_optional_size(value: Option<&str>) -> NativeResult<usize> {
    match value {
        Some(value) => checked_graph_observation_add(1, graph_frame_size(value)?),
        None => Ok(1),
    }
}

#[cfg(feature = "test-hooks")]
fn graph_frame_size(value: &str) -> NativeResult<usize> {
    checked_graph_observation_add(8, value.len())
}

#[cfg(feature = "test-hooks")]
fn checked_graph_observation_add(left: usize, right: usize) -> NativeResult<usize> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("native RDF/XML graph observation size overflow"))
}

#[cfg(feature = "test-hooks")]
fn encode_graph_resource(value: &Resource, output: &mut Vec<u8>) -> NativeResult<()> {
    let (tag, value) = match value {
        Resource::Iri(value) => (0_u8, value),
        Resource::Blank(value) => (1_u8, value),
    };
    output.push(tag);
    encode_graph_frame(value, output)
}

#[cfg(feature = "test-hooks")]
fn encode_graph_term(value: &Term, output: &mut Vec<u8>) -> NativeResult<()> {
    match value {
        Term::Iri(value) => {
            output.push(0);
            encode_graph_frame(value, output)
        }
        Term::Blank(value) => {
            output.push(1);
            encode_graph_frame(value, output)
        }
        Term::Literal {
            lexical,
            datatype,
            language,
        } => {
            output.push(2);
            encode_graph_frame(lexical, output)?;
            encode_graph_optional(datatype.as_deref(), output)?;
            encode_graph_optional(language.as_deref(), output)
        }
    }
}

#[cfg(feature = "test-hooks")]
fn encode_graph_optional(value: Option<&str>, output: &mut Vec<u8>) -> NativeResult<()> {
    match value {
        Some(value) => {
            output.push(1);
            encode_graph_frame(value, output)
        }
        None => {
            output.push(0);
            Ok(())
        }
    }
}

#[cfg(feature = "test-hooks")]
fn encode_graph_frame(value: &str, output: &mut Vec<u8>) -> NativeResult<()> {
    output.extend_from_slice(
        &u64::try_from(value.len())
            .map_err(|_| NativeError::limit("native RDF/XML graph frame length exceeds u64"))?
            .to_le_bytes(),
    );
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn decode_xml(
    source: &[u8],
    session: &mut Session<'_>,
) -> NativeResult<(String, u64, XmlSourceEncoding)> {
    if source.starts_with(&[0xff, 0xfe, 0x00, 0x00])
        || source.starts_with(&[0x00, 0x00, 0xfe, 0xff])
        || source.starts_with(&[0x00, 0x00, 0x00, b'<'])
        || source.starts_with(&[b'<', 0x00, 0x00, 0x00])
    {
        return Err(NativeError::new(
            "NATIVE_FORMAT_ENCODING",
            "native RDF/XML source uses unsupported UTF-32 encoding",
        ));
    }
    if let Some(content) = source.strip_prefix(&[0xff, 0xfe]) {
        let (text, decoded_codepoints) = decode_utf16(content, true, session)?;
        return Ok((text, decoded_codepoints, XmlSourceEncoding::Utf16Le));
    }
    if let Some(content) = source.strip_prefix(&[0xfe, 0xff]) {
        let (text, decoded_codepoints) = decode_utf16(content, false, session)?;
        return Ok((text, decoded_codepoints, XmlSourceEncoding::Utf16Be));
    }
    if source.starts_with(&[b'<', 0x00]) {
        let (text, decoded_codepoints) = decode_utf16(source, true, session)?;
        return Ok((text, decoded_codepoints, XmlSourceEncoding::Utf16Le));
    }
    if source.starts_with(&[0x00, b'<']) {
        let (text, decoded_codepoints) = decode_utf16(source, false, session)?;
        return Ok((text, decoded_codepoints, XmlSourceEncoding::Utf16Be));
    }
    let (text, decoded_codepoints) = decode_utf8(source, session)?;
    Ok((text, decoded_codepoints, XmlSourceEncoding::Utf8))
}

fn decode_utf16(
    source: &[u8],
    little_endian: bool,
    session: &mut Session<'_>,
) -> NativeResult<(String, u64)> {
    session.finish()?;
    if source.len() % 2 != 0 {
        return Err(NativeError::new(
            "NATIVE_FORMAT_ENCODING",
            "native RDF/XML source has a truncated UTF-16 code unit",
        ));
    }

    let code_units = || {
        source.chunks_exact(2).map(|bytes| {
            if little_endian {
                u16::from_le_bytes([bytes[0], bytes[1]])
            } else {
                u16::from_be_bytes([bytes[0], bytes[1]])
            }
        })
    };
    let mut output_bytes = 0_usize;
    let mut codepoints = 0_u64;
    for decoded in char::decode_utf16(code_units()) {
        let character = decoded.map_err(|_| {
            NativeError::new(
                "NATIVE_FORMAT_ENCODING",
                "native RDF/XML source contains an invalid UTF-16 surrogate",
            )
        })?;
        output_bytes = output_bytes
            .checked_add(character.len_utf8())
            .ok_or_else(|| NativeError::limit("native UTF-16 decode allocation overflow"))?;
        codepoints = codepoints
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native decoded XML length overflow"))?;
        session.step(1)?;
    }

    session.reserve_bytes(output_bytes)?;
    let mut output = String::new();
    output
        .try_reserve_exact(output_bytes)
        .map_err(|_| NativeError::limit("native UTF-16 decode allocation failed"))?;
    for (index, decoded) in char::decode_utf16(code_units()).enumerate() {
        if index % (32 * 1024) == 0 {
            session.finish()?;
        }
        let character = decoded.map_err(|_| {
            NativeError::new(
                "NATIVE_FORMAT_ENCODING",
                "native RDF/XML source contains an invalid UTF-16 surrogate",
            )
        })?;
        output.push(character);
    }
    session.finish()?;
    Ok((output, codepoints))
}

fn decode_utf8(source: &[u8], session: &mut Session<'_>) -> NativeResult<(String, u64)> {
    // Force a cancellation/deadline check before touching an attacker-sized
    // buffer, then validate and copy in bounded chunks.  Chunk ends are moved
    // to UTF-8 code-point boundaries before `from_utf8` is called.
    session.finish()?;
    session.reserve_bytes(source.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(source.len())
        .map_err(|_| NativeError::limit("native UTF-8 decode allocation failed"))?;
    let mut start = 0_usize;
    let mut codepoints = 0_u64;
    while start < source.len() {
        let mut end = start.saturating_add(64 * 1024).min(source.len());
        while end < source.len() && source[end] & 0xc0 == 0x80 {
            end = end.checked_sub(1).ok_or_else(|| {
                NativeError::new(
                    "NATIVE_FORMAT_ENCODING",
                    "native RDF/XML source is not valid UTF-8",
                )
            })?;
        }
        if end == start {
            return Err(NativeError::new(
                "NATIVE_FORMAT_ENCODING",
                "native RDF/XML source is not valid UTF-8",
            ));
        }
        let fragment = std::str::from_utf8(&source[start..end]).map_err(|_| {
            NativeError::new(
                "NATIVE_FORMAT_ENCODING",
                "native RDF/XML source is not valid UTF-8",
            )
        })?;
        for _ in fragment.chars() {
            codepoints = codepoints
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native decoded XML length overflow"))?;
            session.step(1)?;
        }
        output.push_str(fragment);
        start = end;
    }
    session.finish()?;
    Ok((output, codepoints))
}

fn map_graph(
    triples: Vec<Triple>,
    decoded_codepoints: u64,
    allow_swrl: bool,
    allow_partial_rdf_mapping: bool,
    capture_occurrences: bool,
    language_spellings: Vec<String>,
    session: &mut Session<'_>,
) -> NativeResult<CanonicalDocument> {
    let total_triples = u64::try_from(triples.len())
        .map_err(|_| NativeError::limit("native RDF triple count exceeds u64"))?;
    let mut consumed = Vec::new();
    session.reserve_bytes(triples.len())?;
    consumed
        .try_reserve_exact(triples.len())
        .map_err(|_| NativeError::limit("native RDF consumed ledger allocation failed"))?;
    consumed.resize(triples.len(), false);
    let kinds = collect_entity_kinds(&triples, session)?;
    let mut header_index = None;
    for (index, triple) in triples.iter().enumerate() {
        if triple.predicate == RDF_TYPE
            && matches!(&triple.object, Term::Iri(value) if value == OWL_ONTOLOGY)
            && header_index.replace(index).is_some()
        {
            return Err(NativeError::new(
                "NATIVE_RDF_ONTOLOGY_HEADER",
                "native RDF graph contains more than one ontology header",
            ));
        }
    }
    let mut ontology_iri = None;
    let mut version_iri = None;
    let mut imports = Vec::new();
    let mut ontology_annotations = Vec::new();
    if let Some(header_index) = header_index {
        let header = &triples[header_index];
        consumed[header_index] = true;
        if let Resource::Iri(value) = &header.subject {
            ontology_iri = Some(owned_text(value, session)?);
        }
        for (index, triple) in triples.iter().enumerate() {
            if triple.subject != header.subject {
                continue;
            }
            match triple.predicate.as_str() {
                RDF_TYPE => {}
                OWL_IMPORTS => match &triple.object {
                    Term::Iri(value) => {
                        let value = owned_text(value, session)?;
                        reserve_vec_item(&mut imports, session)?;
                        imports.push(value);
                        consumed[index] = true;
                    }
                    _ => return Err(rdf_mapping_type()),
                },
                OWL_VERSION_IRI => match &triple.object {
                    Term::Iri(value) if version_iri.is_none() && ontology_iri.is_some() => {
                        version_iri = Some(owned_text(value, session)?);
                        consumed[index] = true;
                    }
                    _ => return Err(rdf_mapping_type()),
                },
                _ => {}
            }
        }
    }
    sort_iris(&mut imports);
    imports.dedup();

    let mut axioms = Vec::new();
    let mut extensions = Vec::new();
    let mut declaration_occurrences = Vec::new();
    let mut rule_occurrences = Vec::new();
    let mut special_occurrences = Vec::new();
    let mut equivalence_occurrences = Vec::new();
    let mut simple_occurrences = Vec::new();
    let list_graph = list_graph_view(&triples, session)?;
    let mut expressions = RdfClassExpressionDecoder::new(&list_graph);
    for kind in &kinds {
        match kind.kind {
            "data_property" => expressions.register_data_property(kind.iri, session)?,
            "datatype" => expressions.register_datatype(kind.iri, session)?,
            _ => {}
        }
    }
    for (index, triple) in triples.iter().enumerate() {
        if let Term::Literal {
            datatype, language, ..
        } = &triple.object
        {
            expressions.register_literal(
                index,
                datatype.as_deref(),
                language.as_deref(),
                session,
            )?;
        }
    }
    consume_owl1_redundant_types(&triples, &mut consumed, session)?;
    let mut axiom_annotations =
        collect_axiom_annotations(&triples, &mut consumed, &mut expressions, session)?;
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != RDF_TYPE {
            continue;
        }
        let Term::Iri(object) = &triple.object else {
            continue;
        };
        let (kind, inferred) = if let Some(kind) = declaration_kind(object) {
            (kind, false)
        } else if let Some(kind) = inferred_declaration_kind(object) {
            (kind, true)
        } else {
            continue;
        };
        // A blank `rdf:type owl:Class` is an optional structural-expression
        // marker rather than an OWL Declaration axiom.  Its owning expression
        // decoder consumes the exact marker later.
        let Resource::Iri(subject) = &triple.subject else {
            continue;
        };
        if inferred && has_explicit_declaration(&triples, subject, kind) {
            continue;
        }
        super::check_iri(
            subject,
            session,
            "native RDF declaration IRI exceeds max_iri_bytes",
        )?;
        let annotations = if inferred {
            Vec::new()
        } else {
            axiom_annotations.annotations_for(triple, &triples, session)?
        };
        let declaration = build_node(
            60,
            [
                Field::Node(named_entity(kind, subject, session)?),
                Field::Set(annotations),
            ],
            session,
        )?;
        push_axiom(declaration, &mut axioms, session)?;
        if !inferred {
            consumed[index] = true;
            axiom_annotations.claim(triple, &triples)?;
        }
    }
    if capture_occurrences {
        capture_occurrences_since(&axioms, 0, 1, &mut declaration_occurrences, session)?;
    }
    map_ontology_annotations(
        header_index,
        &list_graph,
        &triples,
        &mut consumed,
        &kinds,
        &mut expressions,
        &mut axiom_annotations,
        &mut ontology_annotations,
        session,
    )?;
    let extension_start = extensions.len();
    map_swrl_rules(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut extensions,
        allow_swrl,
        session,
    )?;
    if capture_occurrences {
        capture_occurrences_since(
            &extensions,
            extension_start,
            2,
            &mut rule_occurrences,
            session,
        )?;
        let anchors = swrl_rule_occurrence_anchors(&triples, session)?;
        order_occurrences_by_anchors(
            &mut rule_occurrences,
            &anchors,
            &triples,
            "native RDF rule occurrence anchors diverge from mapped roots",
            session,
        )?;
    }
    let special_start = axioms.len();
    map_negative_property_assertions(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    map_all_different(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    map_all_disjoint_collections(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    if capture_occurrences {
        capture_occurrences_since(&axioms, special_start, 1, &mut special_occurrences, session)?;
        let anchors = special_occurrence_anchors(&triples, session)?;
        order_occurrences_by_anchors(
            &mut special_occurrences,
            &anchors,
            &triples,
            "native RDF special occurrence anchors diverge from mapped roots",
            session,
        )?;
    }
    let simple_start = axioms.len();
    let mut simple_anchors = if capture_occurrences {
        pre_simple_occurrence_anchors(&triples, &consumed, &kinds, session)?
    } else {
        Vec::new()
    };
    map_property_chains(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    map_has_keys(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    map_disjoint_unions(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    map_owl1_compatibility_class_axioms(
        &list_graph,
        &triples,
        &mut consumed,
        &kinds,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    map_datatype_definitions(
        &list_graph,
        &triples,
        &mut consumed,
        &kinds,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        session,
    )?;
    if capture_occurrences {
        capture_occurrences_since(&axioms, simple_start, 1, &mut simple_occurrences, session)?;
    }
    let equivalence_start = axioms.len();
    let mut equivalence_anchors = Vec::new();
    map_equivalent_class_components(
        &list_graph,
        &triples,
        &mut consumed,
        &kinds,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        capture_occurrences.then_some(&mut equivalence_anchors),
        session,
    )?;
    map_equivalent_property_components(
        OWL_EQUIVALENT_PROPERTY,
        &list_graph,
        &triples,
        &mut consumed,
        &kinds,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        capture_occurrences.then_some(&mut equivalence_anchors),
        session,
    )?;
    map_same_individual_components(
        &list_graph,
        &triples,
        &mut consumed,
        &mut expressions,
        &mut axiom_annotations,
        &mut axioms,
        capture_occurrences.then_some(&mut equivalence_anchors),
        session,
    )?;
    if capture_occurrences {
        capture_occurrences_since(
            &axioms,
            equivalence_start,
            1,
            &mut equivalence_occurrences,
            session,
        )?;
        order_occurrences_by_component_anchors(
            &mut equivalence_occurrences,
            &equivalence_anchors,
            session,
        )?;
    }
    let trailing_simple_start = axioms.len();
    for (index, triple) in triples.iter().enumerate() {
        if consumed[index] {
            continue;
        }
        session.step(1)?;
        let annotations = axiom_annotations.annotations_for(triple, &triples, session)?;
        let class_axiom = class_expression_axiom(
            &list_graph[index],
            &kinds,
            &mut expressions,
            &mut consumed,
            &annotations,
            session,
        )?;
        let axiom = match class_axiom {
            Some(value) => Some(value),
            None => match annotation_axiom(
                index,
                triple,
                &kinds,
                &mut expressions,
                &annotations,
                session,
            )? {
                Some(value) => Some(value),
                None => match assertion_axiom(
                    index,
                    triple,
                    &kinds,
                    &mut expressions,
                    &annotations,
                    session,
                )? {
                    Some(value) => Some(value),
                    None => named_axiom(triple, &kinds, &annotations, session)?,
                },
            },
        };
        if let Some(axiom) = axiom {
            push_axiom(axiom, &mut axioms, session)?;
            if capture_occurrences {
                reserve_vec_item(&mut simple_anchors, session)?;
                simple_anchors.push(index);
            }
            consumed[index] = true;
            axiom_annotations.claim(triple, &triples)?;
        }
    }
    if capture_occurrences {
        capture_occurrences_since(
            &axioms,
            trailing_simple_start,
            1,
            &mut simple_occurrences,
            session,
        )?;
        order_occurrences_by_anchors(
            &mut simple_occurrences,
            &simple_anchors,
            &triples,
            "native RDF simple occurrence anchors diverge from mapped roots",
            session,
        )?;
    }
    consume_detached_inverse_property_expressions(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        session,
    )?;
    consume_detached_empty_class_booleans(&list_graph, &mut consumed, &mut expressions, session)?;
    consume_detached_named_class_booleans(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        session,
    )?;
    consume_detached_class_complements(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        session,
    )?;
    consume_detached_object_enumerations(&list_graph, &mut consumed, &mut expressions, session)?;
    consume_detached_named_data_booleans(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        session,
    )?;
    consume_detached_datatype_restrictions(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        session,
    )?;
    consume_detached_data_complements(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        session,
    )?;
    consume_detached_data_enumerations(&list_graph, &mut consumed, &mut expressions, session)?;
    consume_detached_owl1_data_enumerations(&list_graph, &mut consumed, &mut expressions, session)?;
    if axiom_annotations.has_unclaimed() {
        return Err(rdf_axiom_reification(
            "native owl:Axiom reification targets an unsupported axiom mapping",
        ));
    }
    let occurrence_count = u64::try_from(axioms.len())
        .ok()
        .and_then(|count| {
            u64::try_from(extensions.len())
                .ok()
                .and_then(|extensions| count.checked_add(extensions))
        })
        .ok_or_else(|| NativeError::limit("native RDF occurrence count overflow"))?;
    let mut occurrences = Vec::new();
    if capture_occurrences {
        let count = usize::try_from(occurrence_count)
            .map_err(|_| NativeError::limit("native RDF occurrence count exceeds usize"))?;
        occurrences
            .try_reserve_exact(count)
            .map_err(|_| NativeError::limit("native RDF occurrence allocation failed"))?;
        occurrences.extend(declaration_occurrences);
        occurrences.extend(rule_occurrences);
        occurrences.extend(special_occurrences);
        occurrences.extend(equivalence_occurrences);
        occurrences.extend(simple_occurrences);
        if occurrences.len() != count {
            return Err(NativeError::protocol(
                "native RDF occurrence capture diverges from mapped roots",
            ));
        }
    }
    axioms.sort_unstable();
    axioms.dedup();
    ontology_annotations.sort_unstable();
    ontology_annotations.dedup();
    extensions.sort_unstable();
    extensions.dedup();
    enforce_usize(
        ontology_annotations.len(),
        session.limits().value(LimitKey::MaxAnnotations),
        "native RDF mapping exceeds max_annotations",
    )?;
    enforce_usize(
        axioms.len(),
        session.limits().value(LimitKey::MaxAxioms),
        "native RDF mapping exceeds max_axioms",
    )?;
    let mut consumed_triples = 0_usize;
    for retained in &consumed {
        session.step(1)?;
        if *retained {
            consumed_triples = consumed_triples
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native RDF consumed count overflow"))?;
        }
    }
    if consumed_triples != triples.len() && !allow_partial_rdf_mapping {
        return Err(mapping_incomplete());
    }
    let unconsumed = if consumed_triples == triples.len() {
        Vec::new()
    } else {
        partial_mapping_evidence(
            &triples,
            &consumed,
            triples
                .len()
                .checked_sub(consumed_triples)
                .ok_or_else(|| NativeError::protocol("native RDF consumed count exceeds total"))?,
            session,
        )?
    };
    Ok(CanonicalDocument {
        document_iri: None,
        ontology_iri,
        version_iri,
        imports,
        ontology_annotations,
        axioms,
        extensions,
        occurrences,
        language_spellings,
        source_blank_labels: Vec::new(),
        source_prefixes: Vec::new(),
        source_sha256: [0; 32],
        byte_length: 0,
        decoded_codepoints,
        mapping: MappingEvidence {
            total_triples,
            consumed_triples: u64::try_from(consumed_triples)
                .map_err(|_| NativeError::limit("native consumed triple count exceeds u64"))?,
            occurrence_count,
            rule_ids: &[
                "OWL2-RDF-REVERSE-HEADER",
                "OWL2-RDF-REVERSE-DECLARATION",
                "OWL2-RDF-REVERSE-NAMED-AXIOM",
                "OWL2-RDF-REVERSE-BOOLEAN-CLASS-EXPRESSION",
                "SWRL-RDF-REVERSE-RULE",
            ],
            unconsumed,
        },
    })
}

fn list_graph_view<'graph>(
    triples: &'graph [Triple],
    session: &mut Session<'_>,
) -> NativeResult<Vec<ListTriple<'graph>>> {
    let mut output = reserved_vec(triples.len(), session)?;
    for triple in triples {
        session.step(1)?;
        let subject = match &triple.subject {
            Resource::Iri(value) => ListResource::Iri(value),
            Resource::Blank(value) => ListResource::Blank(value),
        };
        let object = match &triple.object {
            Term::Iri(value) => ListTerm::Iri(value),
            Term::Blank(value) => ListTerm::Blank(value),
            Term::Literal { lexical, .. } => ListTerm::Literal(lexical),
        };
        output.push(ListTriple {
            subject,
            predicate: &triple.predicate,
            object,
        });
    }
    Ok(output)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct KindRecord<'a> {
    iri: &'a str,
    kind: &'static str,
}

#[derive(Debug)]
struct AxiomAnnotationRecord {
    main_index: usize,
    annotations: Vec<Node>,
    claimed: bool,
}

#[derive(Clone, Copy, Debug)]
struct NestedAnnotationRecord {
    type_index: usize,
    main_index: usize,
    claimed: bool,
}

#[derive(Debug, Default)]
struct AxiomAnnotationLedger {
    records: Vec<AxiomAnnotationRecord>,
    nested_records: Vec<NestedAnnotationRecord>,
}

impl AxiomAnnotationLedger {
    fn annotations_for(
        &self,
        triple: &Triple,
        triples: &[Triple],
        session: &mut Session<'_>,
    ) -> NativeResult<Vec<Node>> {
        let mut annotations = Vec::new();
        for record in &self.records {
            session.step(1)?;
            let main = triples.get(record.main_index).ok_or_else(|| {
                NativeError::protocol("native RDF reification main index exceeds graph")
            })?;
            if main != triple {
                continue;
            }
            for annotation in &record.annotations {
                reserve_vec_item(&mut annotations, session)?;
                session.reserve_bytes(annotation.as_bytes().len())?;
                annotations.push(annotation.clone());
            }
        }
        enforce_usize(
            annotations.len(),
            session.limits().value(LimitKey::MaxAnnotations),
            "native RDF axiom annotations exceed max_annotations",
        )?;
        canonical_set(annotations, 0, None)
    }

    fn claim(&mut self, triple: &Triple, triples: &[Triple]) -> NativeResult<()> {
        for record in &mut self.records {
            let main = triples.get(record.main_index).ok_or_else(|| {
                NativeError::protocol("native RDF reification main index exceeds graph")
            })?;
            if main == triple {
                record.claimed = true;
            }
        }
        Ok(())
    }

    fn nested_annotations_for<'view, 'graph>(
        &mut self,
        main_index: usize,
        triples: &'graph [Triple],
        expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
        session: &mut Session<'_>,
    ) -> NativeResult<Vec<Node>> {
        let mut stack = Vec::new();
        nested_annotations(
            main_index,
            &mut self.nested_records,
            triples,
            expressions,
            &mut stack,
            session,
        )
    }

    fn has_unclaimed(&self) -> bool {
        self.records.iter().any(|record| !record.claimed)
            || self.nested_records.iter().any(|record| !record.claimed)
    }
}

fn consume_owl1_redundant_types(
    triples: &[Triple],
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if triple.predicate != RDF_TYPE {
            continue;
        }
        let Term::Iri(object) = &triple.object else {
            continue;
        };
        let redundant = match object.as_str() {
            RDFS_CLASS => subject_has_type(
                triples,
                &triple.subject,
                &[
                    OWL_ONTOLOGY,
                    OWL_CLASS,
                    RDFS_DATATYPE,
                    OWL_DATA_RANGE,
                    OWL_RESTRICTION,
                ],
                session,
            )?,
            RDF_PROPERTY => subject_has_type(
                triples,
                &triple.subject,
                &[
                    OWL_OBJECT_PROPERTY,
                    OWL_FUNCTIONAL_PROPERTY,
                    OWL_INVERSE_FUNCTIONAL_PROPERTY,
                    OWL_SYMMETRIC_PROPERTY,
                    OWL_TRANSITIVE_PROPERTY,
                    OWL_DATATYPE_PROPERTY,
                    OWL_ANNOTATION_PROPERTY,
                    OWL_ONTOLOGY_PROPERTY,
                ],
                session,
            )?,
            OWL_CLASS => subject_has_type(triples, &triple.subject, &[OWL_RESTRICTION], session)?,
            _ => false,
        };
        if redundant {
            let entry = consumed.get_mut(index).ok_or_else(|| {
                NativeError::protocol("native RDF compatibility index exceeds consumed ledger")
            })?;
            *entry = true;
        }
    }
    Ok(())
}

fn subject_has_type(
    triples: &[Triple],
    subject: &Resource,
    expected: &[&str],
    session: &mut Session<'_>,
) -> NativeResult<bool> {
    for triple in triples {
        session.step(1)?;
        if &triple.subject == subject
            && triple.predicate == RDF_TYPE
            && matches!(&triple.object, Term::Iri(value) if expected.contains(&value.as_str()))
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn collect_entity_kinds<'a>(
    triples: &'a [Triple],
    session: &mut Session<'_>,
) -> NativeResult<Vec<KindRecord<'a>>> {
    let mut output = Vec::new();
    for triple in triples {
        session.step(1)?;
        if triple.predicate != RDF_TYPE {
            continue;
        }
        let (Resource::Iri(subject), Term::Iri(object)) = (&triple.subject, &triple.object) else {
            continue;
        };
        let Some(kind) = declaration_kind(object).or_else(|| inferred_declaration_kind(object))
        else {
            continue;
        };
        let record = KindRecord { iri: subject, kind };
        if !output.contains(&record) {
            reserve_vec_item(&mut output, session)?;
            output.push(record);
        }
    }
    Ok(output)
}

fn has_kind(kinds: &[KindRecord<'_>], value: &str, kind: &str) -> bool {
    kinds
        .iter()
        .any(|record| record.iri == value && record.kind == kind)
}

fn is_builtin_datatype(value: &str) -> bool {
    matches!(
        value,
        RDFS_LITERAL | RDF_PLAIN_LITERAL | RDF_XML_LITERAL | OWL_REAL | OWL_RATIONAL
    ) || value
        .strip_prefix("http://www.w3.org/2001/XMLSchema#")
        .is_some_and(|local| {
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
        })
}

fn consume_detached_inverse_property_expressions<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_INVERSE_OF {
            continue;
        }
        let (ListResource::Blank(subject), ListTerm::Iri(target)) = (triple.subject, triple.object)
        else {
            continue;
        };
        if !has_kind(kinds, target, "object_property") {
            continue;
        }
        let mut inverse_targets = 0_usize;
        for candidate in triples {
            session.step(1)?;
            if candidate.subject == ListResource::Blank(subject)
                && candidate.predicate == OWL_INVERSE_OF
            {
                inverse_targets = inverse_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached inverse target count overflow")
                })?;
                if inverse_targets > 1 {
                    return Err(rdf_mapping_cardinality(
                        "native detached inverse property has more than one target",
                    ));
                }
            }
        }
        let DecodedPropertyExpression {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_object_property_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn consume_detached_empty_class_booleans<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index]
            || !matches!(triple.predicate, OWL_INTERSECTION_OF | OWL_UNION_OF)
            || triple.object != ListTerm::Iri(RDF_NIL)
        {
            continue;
        }
        let ListResource::Blank(subject) = triple.subject else {
            continue;
        };
        let mut marker_present = false;
        let mut constructor_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(OWL_CLASS)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == triple.predicate {
                constructor_targets = constructor_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached empty class-boolean target count overflow")
                })?;
            }
        }
        if !marker_present {
            continue;
        }
        if constructor_targets > 1 {
            return Err(rdf_mapping_cardinality(
                "native detached empty class boolean has more than one target",
            ));
        }
        let DecodedClassExpression {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn consume_detached_named_class_booleans<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || !matches!(triple.predicate, OWL_INTERSECTION_OF | OWL_UNION_OF) {
            continue;
        }
        let ListResource::Blank(subject) = triple.subject else {
            continue;
        };
        let mut marker_present = false;
        let mut constructor_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(OWL_CLASS)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == triple.predicate {
                constructor_targets = constructor_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached class-boolean target count overflow")
                })?;
            }
        }
        if !marker_present {
            continue;
        }
        if !established_named_list(triples, triple.object, kinds, "class", 1, session)? {
            continue;
        }
        if constructor_targets > 1 {
            return Err(rdf_mapping_cardinality(
                "native detached class boolean has more than one target",
            ));
        }
        let DecodedClassExpression {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn established_named_list<'graph>(
    triples: &[ListTriple<'graph>],
    head: ListTerm<'graph>,
    kinds: &[KindRecord<'graph>],
    expected_kind: &str,
    minimum_arity: usize,
    session: &mut Session<'_>,
) -> NativeResult<bool> {
    session.finish()?;
    let ListTerm::Blank(mut current) = head else {
        return Ok(false);
    };
    let mut visited = Vec::new();
    loop {
        let next_length = visited
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native detached named list length overflow"))?;
        enforce_usize(
            next_length,
            session.limits().value(LimitKey::MaxRdfListLength),
            "native detached named list exceeds max_rdf_list_length",
        )?;
        enforce_usize(
            next_length,
            session.limits().value(LimitKey::MaxSequenceArity),
            "native detached named list exceeds max_sequence_arity",
        )?;
        session
            .step(u64::try_from(visited.len()).map_err(|_| {
                NativeError::limit("native detached named list work exceeds u64")
            })?)?;
        if visited.contains(&current) {
            return Ok(false);
        }
        reserve_temporary_vec_item(&mut visited, session)?;
        visited.push(current);

        let mut first = None;
        let mut rest = None;
        for candidate in triples {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(current) {
                continue;
            }
            if candidate.predicate == RDF_FIRST && first.replace(candidate.object).is_some() {
                return Ok(false);
            }
            if candidate.predicate == RDF_REST && rest.replace(candidate.object).is_some() {
                return Ok(false);
            }
        }
        let Some(ListTerm::Iri(member)) = first else {
            return Ok(false);
        };
        let mut established = false;
        for record in kinds {
            session.step(1)?;
            if record.iri == member && record.kind == expected_kind {
                established = true;
                break;
            }
        }
        if !established {
            return Ok(false);
        }
        match rest {
            Some(ListTerm::Iri(RDF_NIL)) => return Ok(next_length >= minimum_arity),
            Some(ListTerm::Blank(next)) => current = next,
            Some(ListTerm::Iri(_)) | Some(ListTerm::Literal(_)) | None => return Ok(false),
        }
    }
}

fn consume_detached_class_complements<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_COMPLEMENT_OF {
            continue;
        }
        let (ListResource::Blank(subject), ListTerm::Iri(target)) = (triple.subject, triple.object)
        else {
            continue;
        };
        if !has_kind(kinds, target, "class") {
            continue;
        }
        let mut marker_present = false;
        let mut complement_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(OWL_CLASS)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == OWL_COMPLEMENT_OF {
                complement_targets = complement_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached class-complement target count overflow")
                })?;
                if complement_targets > 1 {
                    return Err(rdf_mapping_cardinality(
                        "native detached class complement has more than one target",
                    ));
                }
            }
        }
        if !marker_present {
            continue;
        }
        let DecodedClassExpression {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn consume_detached_object_enumerations<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_ONE_OF {
            continue;
        }
        let ListResource::Blank(subject) = triple.subject else {
            continue;
        };
        let mut marker_present = false;
        let mut enumeration_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(OWL_CLASS)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == OWL_ONE_OF {
                enumeration_targets = enumeration_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached object-enumeration target count overflow")
                })?;
                if enumeration_targets > 1 {
                    return Err(rdf_mapping_cardinality(
                        "native detached object enumeration has more than one target",
                    ));
                }
            }
        }
        if !marker_present {
            continue;
        }
        let DecodedClassExpression {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn consume_detached_named_data_booleans<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || !matches!(triple.predicate, OWL_INTERSECTION_OF | OWL_UNION_OF) {
            continue;
        }
        let ListResource::Blank(subject) = triple.subject else {
            continue;
        };
        let mut marker_present = false;
        let mut constructor_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(RDFS_DATATYPE)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == triple.predicate {
                constructor_targets = constructor_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached data-boolean target count overflow")
                })?;
            }
        }
        if !marker_present {
            continue;
        }
        if !established_named_list(triples, triple.object, kinds, "datatype", 2, session)? {
            continue;
        }
        if constructor_targets > 1 {
            return Err(rdf_mapping_cardinality(
                "native detached data boolean has more than one target",
            ));
        }
        let DecodedDataRange {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_data_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn consume_detached_datatype_restrictions<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_ON_DATATYPE {
            continue;
        }
        let (ListResource::Blank(subject), ListTerm::Iri(target)) = (triple.subject, triple.object)
        else {
            continue;
        };
        if !has_kind(kinds, target, "datatype") && !is_builtin_datatype(target) {
            continue;
        }
        let mut marker_present = false;
        let mut on_datatype_targets = 0_usize;
        let mut with_restrictions_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(RDFS_DATATYPE)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == OWL_ON_DATATYPE {
                on_datatype_targets = on_datatype_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached datatype-restriction base count overflow")
                })?;
            }
            if candidate.predicate == OWL_WITH_RESTRICTIONS {
                with_restrictions_targets =
                    with_restrictions_targets.checked_add(1).ok_or_else(|| {
                        NativeError::limit(
                            "native detached datatype-restriction facet-list count overflow",
                        )
                    })?;
            }
        }
        if !marker_present {
            continue;
        }
        let mut established_facets = false;
        for candidate in triples {
            session.step(1)?;
            if candidate.subject == ListResource::Blank(subject)
                && candidate.predicate == OWL_WITH_RESTRICTIONS
                && established_facet_list(triples, candidate.object, session)?
            {
                established_facets = true;
                break;
            }
        }
        if !established_facets {
            continue;
        }
        if on_datatype_targets > 1 || with_restrictions_targets > 1 {
            return Err(rdf_mapping_cardinality(
                "native detached datatype restriction has a repeated selector",
            ));
        }
        let DecodedDataRange {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_data_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn established_facet_list<'graph>(
    triples: &[ListTriple<'graph>],
    head: ListTerm<'graph>,
    session: &mut Session<'_>,
) -> NativeResult<bool> {
    session.finish()?;
    let ListTerm::Blank(mut current) = head else {
        return Ok(false);
    };
    let mut visited = Vec::new();
    loop {
        let next_length = visited
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native detached facet list length overflow"))?;
        enforce_usize(
            next_length,
            session.limits().value(LimitKey::MaxRdfListLength),
            "native detached facet list exceeds max_rdf_list_length",
        )?;
        enforce_usize(
            next_length,
            session.limits().value(LimitKey::MaxSequenceArity),
            "native detached facet list exceeds max_sequence_arity",
        )?;
        session
            .step(u64::try_from(visited.len()).map_err(|_| {
                NativeError::limit("native detached facet list work exceeds u64")
            })?)?;
        if visited.contains(&current) {
            return Ok(false);
        }
        reserve_temporary_vec_item(&mut visited, session)?;
        visited.push(current);

        let mut first = None;
        let mut rest = None;
        for candidate in triples {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(current) {
                continue;
            }
            if candidate.predicate == RDF_FIRST && first.replace(candidate.object).is_some() {
                return Ok(false);
            }
            if candidate.predicate == RDF_REST && rest.replace(candidate.object).is_some() {
                return Ok(false);
            }
        }
        let Some(ListTerm::Blank(facet)) = first else {
            return Ok(false);
        };
        let mut has_literal_facet = false;
        for candidate in triples {
            session.step(1)?;
            if candidate.subject == ListResource::Blank(facet)
                && matches!(candidate.object, ListTerm::Literal(_))
            {
                has_literal_facet = true;
                break;
            }
        }
        if !has_literal_facet {
            return Ok(false);
        }
        match rest {
            Some(ListTerm::Iri(RDF_NIL)) => return Ok(true),
            Some(ListTerm::Blank(next)) => current = next,
            Some(ListTerm::Iri(_)) | Some(ListTerm::Literal(_)) | None => return Ok(false),
        }
    }
}

fn consume_detached_data_complements<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_DATATYPE_COMPLEMENT_OF {
            continue;
        }
        let (ListResource::Blank(subject), ListTerm::Iri(target)) = (triple.subject, triple.object)
        else {
            continue;
        };
        if !has_kind(kinds, target, "datatype") {
            continue;
        }
        let mut marker_present = false;
        let mut complement_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(RDFS_DATATYPE)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == OWL_DATATYPE_COMPLEMENT_OF {
                complement_targets = complement_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached data-complement target count overflow")
                })?;
                if complement_targets > 1 {
                    return Err(rdf_mapping_cardinality(
                        "native detached datatype complement has more than one target",
                    ));
                }
            }
        }
        if !marker_present {
            continue;
        }
        let DecodedDataRange {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_data_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn consume_detached_data_enumerations<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_ONE_OF {
            continue;
        }
        let ListResource::Blank(subject) = triple.subject else {
            continue;
        };
        let mut marker_present = false;
        let mut enumeration_targets = 0_usize;
        for (candidate_index, candidate) in triples.iter().enumerate() {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == RDF_TYPE
                && candidate.object == ListTerm::Iri(RDFS_DATATYPE)
                && !consumed[candidate_index]
            {
                marker_present = true;
            }
            if candidate.predicate == OWL_ONE_OF {
                enumeration_targets = enumeration_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit("native detached data-enumeration target count overflow")
                })?;
                if enumeration_targets > 1 {
                    return Err(rdf_mapping_cardinality(
                        "native detached data enumeration has more than one target",
                    ));
                }
            }
        }
        if !marker_present {
            continue;
        }
        let DecodedDataRange {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_data_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

fn consume_detached_owl1_data_enumerations<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (marker_index, marker) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[marker_index]
            || marker.predicate != RDF_TYPE
            || marker.object != ListTerm::Iri(OWL_DATA_RANGE)
        {
            continue;
        }
        let ListResource::Blank(subject) = marker.subject else {
            continue;
        };
        let mut enumeration_targets = 0_usize;
        let mut has_other_constructor = false;
        for candidate in triples {
            session.step(1)?;
            if candidate.subject != ListResource::Blank(subject) {
                continue;
            }
            if candidate.predicate == OWL_ONE_OF {
                enumeration_targets = enumeration_targets.checked_add(1).ok_or_else(|| {
                    NativeError::limit(
                        "native detached OWL 1 data-enumeration target count overflow",
                    )
                })?;
                if enumeration_targets > 1 {
                    return Err(rdf_mapping_cardinality(
                        "native detached OWL 1 data enumeration has more than one target",
                    ));
                }
            } else if matches!(
                candidate.predicate,
                OWL_INTERSECTION_OF
                    | OWL_UNION_OF
                    | OWL_DATATYPE_COMPLEMENT_OF
                    | OWL_ON_DATATYPE
                    | OWL_WITH_RESTRICTIONS
            ) {
                has_other_constructor = true;
            }
        }
        if enumeration_targets == 0 && !has_other_constructor {
            continue;
        }
        let DecodedDataRange {
            node: _,
            consumed: expression_consumed,
        } = expressions.decode_data_term(ListTerm::Blank(subject), session)?;
        consume_collection_indexes(expression_consumed, consumed, session)?;
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ClassTerm<'graph> {
    Iri(&'graph str),
    Blank(&'graph str),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum IndividualTerm<'graph> {
    Iri(&'graph str),
    Blank(&'graph str),
}

impl<'graph> IndividualTerm<'graph> {
    fn from_resource(value: ListResource<'graph>) -> Self {
        match value {
            ListResource::Iri(value) => Self::Iri(value),
            ListResource::Blank(value) => Self::Blank(value),
        }
    }

    fn from_term(value: ListTerm<'graph>) -> Option<Self> {
        match value {
            ListTerm::Iri(value) => Some(Self::Iri(value)),
            ListTerm::Blank(value) => Some(Self::Blank(value)),
            ListTerm::Literal(_) => None,
        }
    }

    fn as_term(self) -> ListTerm<'graph> {
        match self {
            Self::Iri(value) => ListTerm::Iri(value),
            Self::Blank(value) => ListTerm::Blank(value),
        }
    }
}

impl<'graph> ClassTerm<'graph> {
    fn from_resource(value: ListResource<'graph>) -> Self {
        match value {
            ListResource::Iri(value) => Self::Iri(value),
            ListResource::Blank(value) => Self::Blank(value),
        }
    }

    fn from_term(value: ListTerm<'graph>) -> Option<Self> {
        match value {
            ListTerm::Iri(value) => Some(Self::Iri(value)),
            ListTerm::Blank(value) => Some(Self::Blank(value)),
            ListTerm::Literal(_) => None,
        }
    }

    fn as_term(self) -> ListTerm<'graph> {
        match self {
            Self::Iri(value) => ListTerm::Iri(value),
            Self::Blank(value) => ListTerm::Blank(value),
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn map_ontology_annotations<'view, 'graph>(
    header_index: Option<usize>,
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    annotations: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let Some(header_index) = header_index else {
        return Ok(());
    };
    let subject = triples
        .get(header_index)
        .ok_or_else(|| NativeError::protocol("native RDF header index exceeds graph"))?
        .subject;
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index]
            || triple.subject != subject
            || !is_annotation_property(triple.predicate, kinds)
        {
            continue;
        }
        let nested =
            reifications.nested_annotations_for(index, source_triples, expressions, session)?;
        let annotation = annotation_node(index, triple, expressions, nested, session)?;
        push_annotation(annotation, annotations, session)?;
        consumed[index] = true;
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SwrlAtomKind {
    Class,
    DataRange,
    ObjectProperty,
    DataProperty,
    Builtin,
    SameIndividual,
    DifferentIndividuals,
}

#[allow(clippy::too_many_arguments)]
fn map_swrl_rules<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    extensions: &mut Vec<Vec<u8>>,
    allow_swrl: bool,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (type_index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[type_index]
            || triple.predicate != RDF_TYPE
            || triple.object != ListTerm::Iri(SWRL_IMP)
        {
            continue;
        }
        if !allow_swrl {
            return Err(NativeError::new(
                "NATIVE_EXTENSION_DISABLED",
                "native RDF/XML SWRL mapping requires explicit enablement",
            ));
        }
        let (_body_index, body_head) = required_metadata_edge(
            triples,
            triple.subject,
            SWRL_BODY,
            "native SWRL rule has no body list",
            "native SWRL rule has more than one body list",
            session,
        )?;
        let (_head_index, head_head) = required_metadata_edge(
            triples,
            triple.subject,
            SWRL_HEAD,
            "native SWRL rule has no head list",
            "native SWRL rule has more than one head list",
            session,
        )?;
        let body = expressions.decode_raw_collection(body_head, session)?;
        let head = expressions.decode_raw_collection(head_head, session)?;
        let body_atoms =
            decode_swrl_atom_set(&body.items, triples, consumed, expressions, session)?;
        let head_atoms =
            decode_swrl_atom_set(&head.items, triples, consumed, expressions, session)?;
        enforce_usize(
            body_atoms.len().max(head_atoms.len()),
            session.limits().value(LimitKey::MaxRuleAtoms),
            "native SWRL rule exceeds max_rule_atoms",
        )?;
        let annotations = annotations_on_structural_node(
            triple.subject,
            &[RDF_TYPE, SWRL_BODY, SWRL_HEAD],
            triples,
            source_triples,
            expressions,
            reifications,
            session,
        )?;
        let rule = build_node(
            148,
            [
                Field::Set(body_atoms),
                Field::Set(head_atoms),
                Field::Set(annotations),
            ],
            session,
        )?;
        consume_collection_indexes(body.consumed, consumed, session)?;
        consume_collection_indexes(head.consumed, consumed, session)?;
        consume_subject_indexes(triple.subject, triples, consumed, session)?;
        push_extension(rule, extensions, session)?;
    }
    Ok(())
}

fn decode_swrl_atom_set<'view, 'graph>(
    values: &[ListTerm<'graph>],
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<Node>> {
    let mut atoms = reserved_vec(values.len(), session)?;
    for value in values {
        atoms.push(decode_swrl_atom(
            *value,
            triples,
            consumed,
            expressions,
            session,
        )?);
    }
    canonical_set(atoms, 0, None)
}

fn decode_swrl_atom<'view, 'graph>(
    value: ListTerm<'graph>,
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    let subject = list_term_resource(value).ok_or_else(rdf_mapping_type)?;
    let (_, atom_type) = required_metadata_edge(
        triples,
        subject,
        RDF_TYPE,
        "native SWRL atom has no rdf:type",
        "native SWRL atom has more than one rdf:type",
        session,
    )?;
    let kind = match atom_type {
        ListTerm::Iri(SWRL_CLASS_ATOM) => SwrlAtomKind::Class,
        ListTerm::Iri(SWRL_DATA_RANGE_ATOM) => SwrlAtomKind::DataRange,
        ListTerm::Iri(SWRL_INDIVIDUAL_PROPERTY_ATOM) => SwrlAtomKind::ObjectProperty,
        ListTerm::Iri(SWRL_DATAVALUED_PROPERTY_ATOM) => SwrlAtomKind::DataProperty,
        ListTerm::Iri(SWRL_BUILTIN_ATOM) => SwrlAtomKind::Builtin,
        ListTerm::Iri(SWRL_SAME_INDIVIDUAL_ATOM) => SwrlAtomKind::SameIndividual,
        ListTerm::Iri(SWRL_DIFFERENT_INDIVIDUALS_ATOM) => SwrlAtomKind::DifferentIndividuals,
        ListTerm::Iri(_) | ListTerm::Blank(_) | ListTerm::Literal(_) => {
            return Err(rdf_mapping_type())
        }
    };
    let node = match kind {
        SwrlAtomKind::Class => {
            ensure_subject_predicates(
                subject,
                &[RDF_TYPE, SWRL_CLASS_PREDICATE, SWRL_ARGUMENT_1],
                triples,
                session,
            )?;
            let (_, predicate) = required_metadata_edge(
                triples,
                subject,
                SWRL_CLASS_PREDICATE,
                "native SWRL class atom has no class predicate",
                "native SWRL class atom has more than one class predicate",
                session,
            )?;
            let (_argument_index, argument) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_1,
                "native SWRL class atom has no argument",
                "native SWRL class atom has more than one argument",
                session,
            )?;
            build_node(
                141,
                [
                    Field::Node(decode_class_expression(
                        expressions,
                        predicate,
                        consumed,
                        session,
                    )?),
                    Field::Node(decode_swrl_individual_argument(
                        argument,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                ],
                session,
            )?
        }
        SwrlAtomKind::DataRange => {
            ensure_subject_predicates(
                subject,
                &[RDF_TYPE, SWRL_DATA_RANGE, SWRL_ARGUMENT_1],
                triples,
                session,
            )?;
            let (_, predicate) = required_metadata_edge(
                triples,
                subject,
                SWRL_DATA_RANGE,
                "native SWRL data-range atom has no predicate",
                "native SWRL data-range atom has more than one predicate",
                session,
            )?;
            let (argument_index, argument) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_1,
                "native SWRL data-range atom has no argument",
                "native SWRL data-range atom has more than one argument",
                session,
            )?;
            let DecodedDataRange {
                node: predicate,
                consumed: predicate_consumed,
            } = expressions.decode_data_term(predicate, session)?;
            consume_collection_indexes(predicate_consumed, consumed, session)?;
            build_node(
                142,
                [
                    Field::Node(predicate),
                    Field::Node(decode_swrl_data_argument(
                        argument_index,
                        argument,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                ],
                session,
            )?
        }
        SwrlAtomKind::ObjectProperty => {
            ensure_subject_predicates(
                subject,
                &[
                    RDF_TYPE,
                    SWRL_PROPERTY_PREDICATE,
                    SWRL_ARGUMENT_1,
                    SWRL_ARGUMENT_2,
                ],
                triples,
                session,
            )?;
            let (_, predicate) = required_metadata_edge(
                triples,
                subject,
                SWRL_PROPERTY_PREDICATE,
                "native SWRL object-property atom has no predicate",
                "native SWRL object-property atom has more than one predicate",
                session,
            )?;
            let (_, first) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_1,
                "native SWRL object-property atom has no first argument",
                "native SWRL object-property atom has more than one first argument",
                session,
            )?;
            let (_, second) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_2,
                "native SWRL object-property atom has no second argument",
                "native SWRL object-property atom has more than one second argument",
                session,
            )?;
            let DecodedPropertyExpression {
                node: predicate,
                consumed: predicate_consumed,
            } = expressions.decode_object_property_term(predicate, session)?;
            consume_collection_indexes(predicate_consumed, consumed, session)?;
            build_node(
                143,
                [
                    Field::Node(predicate),
                    Field::Node(decode_swrl_individual_argument(
                        first,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                    Field::Node(decode_swrl_individual_argument(
                        second,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                ],
                session,
            )?
        }
        SwrlAtomKind::DataProperty => {
            ensure_subject_predicates(
                subject,
                &[
                    RDF_TYPE,
                    SWRL_PROPERTY_PREDICATE,
                    SWRL_ARGUMENT_1,
                    SWRL_ARGUMENT_2,
                ],
                triples,
                session,
            )?;
            let (_, predicate) = required_metadata_edge(
                triples,
                subject,
                SWRL_PROPERTY_PREDICATE,
                "native SWRL data-property atom has no predicate",
                "native SWRL data-property atom has more than one predicate",
                session,
            )?;
            let ListTerm::Iri(predicate) = predicate else {
                return Err(rdf_mapping_type());
            };
            let (_, first) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_1,
                "native SWRL data-property atom has no first argument",
                "native SWRL data-property atom has more than one first argument",
                session,
            )?;
            let (second_index, second) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_2,
                "native SWRL data-property atom has no second argument",
                "native SWRL data-property atom has more than one second argument",
                session,
            )?;
            build_node(
                144,
                [
                    Field::Node(named_entity("data_property", predicate, session)?),
                    Field::Node(decode_swrl_individual_argument(
                        first,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                    Field::Node(decode_swrl_data_argument(
                        second_index,
                        second,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                ],
                session,
            )?
        }
        SwrlAtomKind::Builtin => {
            ensure_subject_predicates(
                subject,
                &[RDF_TYPE, SWRL_BUILTIN, SWRL_ARGUMENTS],
                triples,
                session,
            )?;
            let (_, predicate) = required_metadata_edge(
                triples,
                subject,
                SWRL_BUILTIN,
                "native SWRL builtin atom has no predicate",
                "native SWRL builtin atom has more than one predicate",
                session,
            )?;
            let ListTerm::Iri(predicate) = predicate else {
                return Err(rdf_mapping_type());
            };
            let (_, arguments_head) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENTS,
                "native SWRL builtin atom has no arguments list",
                "native SWRL builtin atom has more than one arguments list",
                session,
            )?;
            let arguments = expressions.decode_raw_collection(arguments_head, session)?;
            let mut nodes = reserved_vec(arguments.items.len(), session)?;
            for (position, argument) in arguments.items.iter().enumerate() {
                let cell = arguments.cells.get(position).ok_or_else(|| {
                    NativeError::protocol("native SWRL argument list cell ledger is incomplete")
                })?;
                let argument_index = rdf_list_first_index(cell, triples, session)?;
                nodes.push(decode_swrl_data_argument(
                    argument_index,
                    *argument,
                    triples,
                    consumed,
                    expressions,
                    session,
                )?);
            }
            consume_collection_indexes(arguments.consumed, consumed, session)?;
            build_node(
                145,
                [
                    Field::Node(iri_node(predicate, session)?),
                    Field::Sequence(nodes),
                ],
                session,
            )?
        }
        SwrlAtomKind::SameIndividual | SwrlAtomKind::DifferentIndividuals => {
            ensure_subject_predicates(
                subject,
                &[RDF_TYPE, SWRL_ARGUMENT_1, SWRL_ARGUMENT_2],
                triples,
                session,
            )?;
            let (_, first) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_1,
                "native SWRL individual atom has no first argument",
                "native SWRL individual atom has more than one first argument",
                session,
            )?;
            let (_, second) = required_metadata_edge(
                triples,
                subject,
                SWRL_ARGUMENT_2,
                "native SWRL individual atom has no second argument",
                "native SWRL individual atom has more than one second argument",
                session,
            )?;
            let tag = if kind == SwrlAtomKind::SameIndividual {
                146
            } else {
                147
            };
            build_node(
                tag,
                [
                    Field::Node(decode_swrl_individual_argument(
                        first,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                    Field::Node(decode_swrl_individual_argument(
                        second,
                        triples,
                        consumed,
                        expressions,
                        session,
                    )?),
                ],
                session,
            )?
        }
    };
    consume_subject_indexes(subject, triples, consumed, session)?;
    Ok(node)
}

fn decode_swrl_individual_argument<'view, 'graph>(
    value: ListTerm<'graph>,
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    if let Some(variable) = decode_swrl_variable(value, triples, consumed, session)? {
        return Ok(variable);
    }
    expressions.decode_individual(value, session)
}

fn decode_swrl_data_argument<'view, 'graph>(
    triple_index: usize,
    value: ListTerm<'graph>,
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    if let Some(variable) = decode_swrl_variable(value, triples, consumed, session)? {
        return Ok(variable);
    }
    if !matches!(value, ListTerm::Literal(_)) {
        return Err(rdf_mapping_type());
    }
    expressions.decode_literal(triple_index, session)
}

fn decode_swrl_variable<'graph>(
    value: ListTerm<'graph>,
    triples: &[ListTriple<'graph>],
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<Option<Node>> {
    let resource = match value {
        ListTerm::Iri(value) => ListResource::Iri(value),
        ListTerm::Blank(value) => ListResource::Blank(value),
        ListTerm::Literal(_) => return Ok(None),
    };
    let mut type_indexes = Vec::new();
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if triple.subject == resource
            && triple.predicate == RDF_TYPE
            && triple.object == ListTerm::Iri(SWRL_VARIABLE)
        {
            reserve_vec_item(&mut type_indexes, session)?;
            type_indexes.push(index);
        }
    }
    if type_indexes.is_empty() {
        return Ok(None);
    }
    let ListResource::Iri(value) = resource else {
        return Err(rdf_mapping_type());
    };
    for index in type_indexes {
        let entry = consumed.get_mut(index).ok_or_else(|| {
            NativeError::protocol("native SWRL variable type index exceeds graph")
        })?;
        *entry = true;
    }
    Ok(Some(build_node(
        140,
        [Field::Node(iri_node(value, session)?)],
        session,
    )?))
}

fn list_term_resource(value: ListTerm<'_>) -> Option<ListResource<'_>> {
    match value {
        ListTerm::Iri(value) => Some(ListResource::Iri(value)),
        ListTerm::Blank(value) => Some(ListResource::Blank(value)),
        ListTerm::Literal(_) => None,
    }
}

fn ensure_subject_predicates<'graph>(
    subject: ListResource<'graph>,
    allowed: &[&str],
    triples: &[ListTriple<'graph>],
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for triple in triples {
        session.step(1)?;
        if triple.subject == subject && !allowed.contains(&triple.predicate) {
            return Err(mapping_incomplete());
        }
    }
    Ok(())
}

fn rdf_list_first_index(
    cell: &str,
    triples: &[ListTriple<'_>],
    session: &mut Session<'_>,
) -> NativeResult<usize> {
    let mut selected = None;
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if triple.subject == ListResource::Blank(cell)
            && triple.predicate == RDF_FIRST
            && selected.replace(index).is_some()
        {
            return Err(rdf_mapping_cardinality(
                "native SWRL argument list has more than one rdf:first edge",
            ));
        }
    }
    selected
        .ok_or_else(|| rdf_mapping_cardinality("native SWRL argument list has no rdf:first edge"))
}

#[allow(clippy::too_many_arguments)]
fn annotations_on_structural_node<'view, 'graph>(
    subject: ListResource<'graph>,
    metadata: &[&str],
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    session: &mut Session<'_>,
) -> NativeResult<Vec<Node>> {
    if triples.len() != source_triples.len() {
        return Err(NativeError::protocol(
            "native RDF graph views have different lengths",
        ));
    }
    let mut annotations = Vec::new();
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if triple.subject != subject || metadata.contains(&triple.predicate) {
            continue;
        }
        let nested =
            reifications.nested_annotations_for(index, source_triples, expressions, session)?;
        let annotation = annotation_node(index, triple, expressions, nested, session)?;
        enforce_usize(
            annotations.len().saturating_add(1),
            session.limits().value(LimitKey::MaxAnnotations),
            "native RDF structural-node annotations exceed max_annotations",
        )?;
        reserve_vec_item(&mut annotations, session)?;
        annotations.push(annotation);
    }
    canonical_set(annotations, 0, None)
}

fn consume_subject_indexes<'graph>(
    subject: ListResource<'graph>,
    triples: &[ListTriple<'graph>],
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if triple.subject == subject {
            let value = consumed.get_mut(index).ok_or_else(|| {
                NativeError::protocol("native RDF consumed ledger is shorter than graph")
            })?;
            *value = true;
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_negative_property_assertions<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (type_index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[type_index]
            || triple.predicate != RDF_TYPE
            || triple.object != ListTerm::Iri(OWL_NEGATIVE_PROPERTY_ASSERTION)
        {
            continue;
        }
        let (_source_index, source) = required_metadata_edge(
            triples,
            triple.subject,
            OWL_SOURCE_INDIVIDUAL,
            "native negative property assertion has no source individual",
            "native negative property assertion has more than one source individual",
            session,
        )?;
        let (_property_index, property) = required_metadata_edge(
            triples,
            triple.subject,
            OWL_ASSERTION_PROPERTY,
            "native negative property assertion has no assertion property",
            "native negative property assertion has more than one assertion property",
            session,
        )?;
        let target_individual = metadata_edge(
            triples,
            triple.subject,
            OWL_TARGET_INDIVIDUAL,
            "native negative property assertion has more than one individual target",
            session,
        )?;
        let target_value = metadata_edge(
            triples,
            triple.subject,
            OWL_TARGET_VALUE,
            "native negative property assertion has more than one value target",
            session,
        )?;
        if target_individual.is_some() == target_value.is_some() {
            return Err(rdf_mapping_cardinality(
                "native negative property assertion requires exactly one target",
            ));
        }
        let source = match source {
            ListTerm::Iri(value) => ListTerm::Iri(value),
            ListTerm::Blank(value) => ListTerm::Blank(value),
            ListTerm::Literal(_) => return Err(rdf_mapping_type()),
        };
        let source = expressions.decode_individual(source, session)?;
        let annotations = annotations_on_structural_node(
            triple.subject,
            &[
                RDF_TYPE,
                OWL_SOURCE_INDIVIDUAL,
                OWL_ASSERTION_PROPERTY,
                OWL_TARGET_INDIVIDUAL,
                OWL_TARGET_VALUE,
            ],
            triples,
            source_triples,
            expressions,
            reifications,
            session,
        )?;
        let (axiom, property_consumed) = if let Some((_target_index, target)) = target_individual {
            let target = match target {
                ListTerm::Iri(value) => ListTerm::Iri(value),
                ListTerm::Blank(value) => ListTerm::Blank(value),
                ListTerm::Literal(_) => return Err(rdf_mapping_type()),
            };
            let property = match property {
                ListTerm::Iri(value) => ListTerm::Iri(value),
                ListTerm::Blank(value) => ListTerm::Blank(value),
                ListTerm::Literal(_) => return Err(rdf_mapping_type()),
            };
            let DecodedPropertyExpression {
                node: property,
                consumed: property_consumed,
            } = expressions.decode_object_property_term(property, session)?;
            (
                build_node(
                    114,
                    [
                        Field::Node(property),
                        Field::Node(source),
                        Field::Node(expressions.decode_individual(target, session)?),
                        Field::Set(annotations),
                    ],
                    session,
                )?,
                property_consumed,
            )
        } else {
            let (target_index, target) = target_value
                .ok_or_else(|| NativeError::protocol("native negative target ledger is empty"))?;
            if !matches!(target, ListTerm::Literal(_)) {
                return Err(rdf_mapping_type());
            }
            let ListTerm::Iri(property) = property else {
                return Err(rdf_mapping_type());
            };
            (
                build_node(
                    116,
                    [
                        Field::Node(named_entity("data_property", property, session)?),
                        Field::Node(source),
                        Field::Node(expressions.decode_literal(target_index, session)?),
                        Field::Set(annotations),
                    ],
                    session,
                )?,
                Vec::new(),
            )
        };
        consume_collection_indexes(property_consumed, consumed, session)?;
        consume_subject_indexes(triple.subject, triples, consumed, session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn required_metadata_edge<'graph>(
    triples: &[ListTriple<'graph>],
    subject: ListResource<'graph>,
    predicate: &str,
    missing: &'static str,
    multiple: &'static str,
    session: &mut Session<'_>,
) -> NativeResult<(usize, ListTerm<'graph>)> {
    metadata_edge(triples, subject, predicate, multiple, session)?
        .ok_or_else(|| rdf_mapping_cardinality(missing))
}

fn metadata_edge<'graph>(
    triples: &[ListTriple<'graph>],
    subject: ListResource<'graph>,
    predicate: &str,
    multiple: &'static str,
    session: &mut Session<'_>,
) -> NativeResult<Option<(usize, ListTerm<'graph>)>> {
    let mut selected = None;
    for (index, candidate) in triples.iter().enumerate() {
        session.step(1)?;
        if candidate.subject != subject || candidate.predicate != predicate {
            continue;
        }
        if selected.replace((index, candidate.object)).is_some() {
            return Err(rdf_mapping_cardinality(multiple));
        }
    }
    Ok(selected)
}

#[allow(clippy::too_many_arguments)]
fn map_all_different<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (type_index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[type_index]
            || triple.predicate != RDF_TYPE
            || triple.object != ListTerm::Iri(OWL_ALL_DIFFERENT)
        {
            continue;
        }
        let members = metadata_edge(
            triples,
            triple.subject,
            OWL_MEMBERS,
            "native owl:AllDifferent has more than one members list",
            session,
        )?;
        let distinct_members = metadata_edge(
            triples,
            triple.subject,
            OWL_DISTINCT_MEMBERS,
            "native owl:AllDifferent has more than one distinctMembers list",
            session,
        )?;
        let head = match (members, distinct_members) {
            (Some((_, head)), None) | (None, Some((_, head))) => head,
            (None, None) => {
                return Err(rdf_mapping_cardinality(
                    "native owl:AllDifferent has no members list",
                ))
            }
            (Some(_), Some(_)) => {
                return Err(rdf_mapping_cardinality(
                    "native owl:AllDifferent has both members and distinctMembers lists",
                ))
            }
        };
        let DecodedIndividualCollection {
            individuals,
            consumed: collection_consumed,
        } = expressions.decode_individual_collection(head, session)?;
        let individuals = canonical_set(individuals, 2, None)?;
        let annotations = annotations_on_structural_node(
            triple.subject,
            &[RDF_TYPE, OWL_MEMBERS, OWL_DISTINCT_MEMBERS],
            triples,
            source_triples,
            expressions,
            reifications,
            session,
        )?;
        let axiom = build_node(
            111,
            [Field::Set(individuals), Field::Set(annotations)],
            session,
        )?;
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consume_subject_indexes(triple.subject, triples, consumed, session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_all_disjoint_collections<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (type_index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[type_index] || triple.predicate != RDF_TYPE {
            continue;
        }
        let kind = match triple.object {
            ListTerm::Iri(OWL_ALL_DISJOINT_CLASSES) => "classes",
            ListTerm::Iri(OWL_ALL_DISJOINT_PROPERTIES) => "properties",
            ListTerm::Iri(_) | ListTerm::Blank(_) | ListTerm::Literal(_) => continue,
        };
        let (_members_index, head) = collection_head(
            triples,
            triple.subject,
            OWL_MEMBERS,
            "native all-disjoint axiom has no members list",
            "native all-disjoint axiom has more than one members list",
            session,
        )?;
        let annotations = annotations_on_structural_node(
            triple.subject,
            &[RDF_TYPE, OWL_MEMBERS],
            triples,
            source_triples,
            expressions,
            reifications,
            session,
        )?;
        let (axiom, collection_consumed) = if kind == "classes" {
            let DecodedClassCollection {
                expressions: raw_expressions,
                consumed,
            } = expressions.decode_class_collection(head, session)?;
            let raw_length = raw_expressions.len();
            let mut expressions_set = canonical_set(raw_expressions, 1, None)?;
            let axiom = if raw_length >= 2 && expressions_set.len() == 1 {
                build_node(
                    61,
                    [
                        Field::Node(expressions_set.pop().ok_or_else(|| {
                            NativeError::protocol("native RDF all-disjoint class ledger is empty")
                        })?),
                        Field::Node(named_entity("class", OWL_NOTHING, session)?),
                        Field::Set(annotations),
                    ],
                    session,
                )?
            } else {
                let expressions_set = canonical_set(expressions_set, 2, None)?;
                build_node(
                    63,
                    [Field::Set(expressions_set), Field::Set(annotations)],
                    session,
                )?
            };
            (axiom, consumed)
        } else {
            let DecodedPropertyCollection {
                properties,
                consumed,
                data_properties,
            } = expressions.decode_property_collection(head, session)?;
            let properties = canonical_set(properties, 2, None)?;
            let tag = if data_properties { 92 } else { 72 };
            (
                build_node(
                    tag,
                    [Field::Set(properties), Field::Set(annotations)],
                    session,
                )?,
                consumed,
            )
        };
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consume_subject_indexes(triple.subject, triples, consumed, session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_property_chains<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_PROPERTY_CHAIN_AXIOM {
            continue;
        }
        let DecodedPropertyExpression {
            node: super_property,
            consumed: super_consumed,
        } = expressions.decode_object_property_term(
            ClassTerm::from_resource(triple.subject).as_term(),
            session,
        )?;
        let DecodedPropertyCollection {
            properties,
            consumed: collection_consumed,
            data_properties: _,
        } = expressions.decode_object_property_collection(triple.object, session)?;
        if properties.len() < 2 {
            return Err(rdf_mapping_cardinality(
                "native object property chain has fewer than two members",
            ));
        }
        let source_triple = source_triples.get(index).ok_or_else(|| {
            NativeError::protocol("native property-chain index exceeds source graph")
        })?;
        let annotations = reifications.annotations_for(source_triple, source_triples, session)?;
        let chain = build_node(11, [Field::Sequence(properties)], session)?;
        let axiom = build_node(
            70,
            [
                Field::Node(chain),
                Field::Node(super_property),
                Field::Set(annotations),
            ],
            session,
        )?;
        consume_collection_indexes(super_consumed, consumed, session)?;
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consumed[index] = true;
        reifications.claim(source_triple, source_triples)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_has_keys<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_HAS_KEY {
            continue;
        }
        let class_expression = decode_class_expression(
            expressions,
            ClassTerm::from_resource(triple.subject).as_term(),
            consumed,
            session,
        )?;
        let DecodedKeyCollection {
            object_properties,
            data_properties,
            consumed: collection_consumed,
        } = expressions.decode_key_collection(triple.object, session)?;
        if object_properties.is_empty() && data_properties.is_empty() {
            return Err(rdf_mapping_cardinality(
                "native owl:hasKey has no property members",
            ));
        }
        let source_triple = source_triples
            .get(index)
            .ok_or_else(|| NativeError::protocol("native has-key index exceeds source graph"))?;
        let annotations = reifications.annotations_for(source_triple, source_triples, session)?;
        let object_properties = canonical_set(object_properties, 0, None)?;
        let data_properties = canonical_set(data_properties, 0, None)?;
        let axiom = build_node(
            101,
            [
                Field::Node(class_expression),
                Field::Set(object_properties),
                Field::Set(data_properties),
                Field::Set(annotations),
            ],
            session,
        )?;
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consumed[index] = true;
        reifications.claim(source_triple, source_triples)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_disjoint_unions<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_DISJOINT_UNION_OF {
            continue;
        }
        let ListResource::Iri(defined_class) = triple.subject else {
            return Err(rdf_mapping_type());
        };
        let DecodedClassCollection {
            expressions: members,
            consumed: collection_consumed,
        } = expressions.decode_class_collection(triple.object, session)?;
        let members = canonical_set(members, 2, None)?;
        let source_triple = source_triples.get(index).ok_or_else(|| {
            NativeError::protocol("native disjoint-union index exceeds source graph")
        })?;
        let annotations = reifications.annotations_for(source_triple, source_triples, session)?;
        let axiom = build_node(
            64,
            [
                Field::Node(named_entity("class", defined_class, session)?),
                Field::Set(members),
                Field::Set(annotations),
            ],
            session,
        )?;
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consumed[index] = true;
        reifications.claim(source_triple, source_triples)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_owl1_compatibility_class_axioms<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] {
            continue;
        }
        let ListResource::Iri(class) = triple.subject else {
            continue;
        };
        if !has_kind(kinds, class, "class") {
            continue;
        }
        let decoded = match triple.predicate {
            OWL_COMPLEMENT_OF => {
                let operand =
                    decode_class_expression(expressions, triple.object, consumed, session)?;
                DecodedClassExpression {
                    node: build_node(32, [Field::Node(operand)], session)?,
                    consumed: Vec::new(),
                }
            }
            OWL_UNION_OF => {
                expressions.decode_compatibility_class_boolean(triple.object, 31, session)?
            }
            OWL_INTERSECTION_OF => {
                expressions.decode_compatibility_class_boolean(triple.object, 30, session)?
            }
            OWL_ONE_OF => expressions.decode_compatibility_named_one_of(triple.object, session)?,
            _ => continue,
        };
        consume_collection_indexes(decoded.consumed, consumed, session)?;
        let mut members = reserved_vec(2, session)?;
        members.push(named_entity("class", class, session)?);
        members.push(decoded.node);
        let members = canonical_set(members, 2, None)?;
        let source_triple = source_triples.get(index).ok_or_else(|| {
            NativeError::protocol("native OWL 1 compatibility index exceeds source graph")
        })?;
        let annotations = reifications.annotations_for(source_triple, source_triples, session)?;
        let axiom = build_node(62, [Field::Set(members), Field::Set(annotations)], session)?;
        consumed[index] = true;
        reifications.claim(source_triple, source_triples)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_datatype_definitions<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_EQUIVALENT_CLASS {
            continue;
        }
        let ListResource::Iri(datatype) = triple.subject else {
            continue;
        };
        if !has_kind(kinds, datatype, "datatype") {
            continue;
        }
        let DecodedDataRange {
            node: data_range,
            consumed: range_consumed,
        } = expressions.decode_data_term(triple.object, session)?;
        let source_triple = source_triples.get(index).ok_or_else(|| {
            NativeError::protocol("native datatype-definition index exceeds source graph")
        })?;
        let annotations = reifications.annotations_for(source_triple, source_triples, session)?;
        let axiom = build_node(
            100,
            [
                Field::Node(named_entity("datatype", datatype, session)?),
                Field::Node(data_range),
                Field::Set(annotations),
            ],
            session,
        )?;
        consume_collection_indexes(range_consumed, consumed, session)?;
        consumed[index] = true;
        reifications.claim(source_triple, source_triples)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn collection_head<'graph>(
    triples: &[ListTriple<'graph>],
    subject: ListResource<'graph>,
    predicate: &str,
    missing: &'static str,
    multiple: &'static str,
    session: &mut Session<'_>,
) -> NativeResult<(usize, ListTerm<'graph>)> {
    let mut selected = None;
    for (index, candidate) in triples.iter().enumerate() {
        session.step(1)?;
        if candidate.subject != subject || candidate.predicate != predicate {
            continue;
        }
        if selected.replace((index, candidate.object)).is_some() {
            return Err(rdf_mapping_cardinality(multiple));
        }
    }
    selected.ok_or_else(|| rdf_mapping_cardinality(missing))
}

fn consume_collection_indexes(
    indexes: Vec<usize>,
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.step(
        u64::try_from(indexes.len())
            .map_err(|_| NativeError::limit("native RDF consumed work exceeds u64"))?,
    )?;
    for index in indexes {
        let value = consumed
            .get_mut(index)
            .ok_or_else(|| NativeError::protocol("native RDF consumed index exceeds graph"))?;
        *value = true;
    }
    Ok(())
}

fn component_annotations(
    indexes: &[usize],
    triples: &[Triple],
    reifications: &mut AxiomAnnotationLedger,
    session: &mut Session<'_>,
) -> NativeResult<Vec<Node>> {
    let mut annotations = Vec::new();
    for index in indexes {
        session.step(1)?;
        let triple = triples.get(*index).ok_or_else(|| {
            NativeError::protocol("native RDF component edge index exceeds graph")
        })?;
        let edge_annotations = reifications.annotations_for(triple, triples, session)?;
        for annotation in edge_annotations {
            enforce_usize(
                annotations.len().saturating_add(1),
                session.limits().value(LimitKey::MaxAnnotations),
                "native RDF component annotations exceed max_annotations",
            )?;
            reserve_vec_item(&mut annotations, session)?;
            annotations.push(annotation);
        }
        reifications.claim(triple, triples)?;
    }
    canonical_set(annotations, 0, None)
}

#[allow(clippy::too_many_arguments)]
fn map_equivalent_class_components<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    mut occurrence_anchors: Option<&mut Vec<ComponentOccurrenceAnchor<'graph>>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for start in 0..triples.len() {
        if consumed[start] {
            continue;
        }
        let Some((left, right)) = class_equivalent_edge(&triples[start]) else {
            continue;
        };
        if !class_equivalence_subject_supported(left, kinds) {
            continue;
        }
        let mut members = Vec::new();
        let mut edge_indexes = Vec::new();
        add_class_member(&mut members, left, session)?;
        add_class_member(&mut members, right, session)?;
        reserve_vec_item(&mut edge_indexes, session)?;
        edge_indexes.push(start);
        consumed[start] = true;
        loop {
            let mut changed = false;
            for (index, triple) in triples.iter().enumerate() {
                session.step(1)?;
                if consumed[index] {
                    continue;
                }
                let Some((edge_left, edge_right)) = class_equivalent_edge(triple) else {
                    continue;
                };
                if !class_equivalence_subject_supported(edge_left, kinds) {
                    continue;
                }
                if members.contains(&edge_left) || members.contains(&edge_right) {
                    add_class_member(&mut members, edge_left, session)?;
                    add_class_member(&mut members, edge_right, session)?;
                    reserve_vec_item(&mut edge_indexes, session)?;
                    edge_indexes.push(index);
                    consumed[index] = true;
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }

        if let Some(anchors) = occurrence_anchors.as_deref_mut() {
            push_component_occurrence_anchor(
                anchors,
                0,
                members.iter().copied().map(class_term_anchor),
                session,
            )?;
        }
        let mut nodes = reserved_vec(members.len(), session)?;
        for member in members {
            nodes.push(decode_class_expression(
                expressions,
                member.as_term(),
                consumed,
                session,
            )?);
        }
        session.finish()?;
        let nodes = canonical_set(nodes, 2, None)?;
        let annotations =
            component_annotations(&edge_indexes, source_triples, reifications, session)?;
        let axiom = build_node(62, [Field::Set(nodes), Field::Set(annotations)], session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn class_equivalent_edge<'graph>(
    triple: &ListTriple<'graph>,
) -> Option<(ClassTerm<'graph>, ClassTerm<'graph>)> {
    if triple.predicate != OWL_EQUIVALENT_CLASS {
        return None;
    }
    Some((
        ClassTerm::from_resource(triple.subject),
        ClassTerm::from_term(triple.object)?,
    ))
}

fn class_equivalence_subject_supported(value: ClassTerm<'_>, kinds: &[KindRecord<'_>]) -> bool {
    match value {
        ClassTerm::Iri(value) => !has_kind(kinds, value, "datatype"),
        ClassTerm::Blank(_) => true,
    }
}

fn add_class_member<'graph>(
    members: &mut Vec<ClassTerm<'graph>>,
    value: ClassTerm<'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.step(
        u64::try_from(members.len())
            .map_err(|_| NativeError::limit("native RDF class component work exceeds u64"))?,
    )?;
    if !members.contains(&value) {
        reserve_vec_item(members, session)?;
        members.push(value);
    }
    Ok(())
}

fn decode_class_expression<'view, 'graph>(
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    value: ListTerm<'graph>,
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    let decoded = expressions.decode_term(value, session)?;
    consume_decoded_expression(decoded, consumed, session)
}

fn consume_decoded_expression(
    decoded: DecodedClassExpression,
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    session.step(
        u64::try_from(decoded.consumed.len())
            .map_err(|_| NativeError::limit("native RDF consumed work exceeds u64"))?,
    )?;
    for index in decoded.consumed {
        let value = consumed
            .get_mut(index)
            .ok_or_else(|| NativeError::protocol("native RDF consumed index exceeds graph"))?;
        *value = true;
    }
    Ok(decoded.node)
}

fn class_expression_axiom<'view, 'graph>(
    triple: &ListTriple<'graph>,
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    consumed: &mut [bool],
    annotations: &[Node],
    session: &mut Session<'_>,
) -> NativeResult<Option<Node>> {
    let subject = ClassTerm::from_resource(triple.subject);
    let object = ClassTerm::from_term(triple.object);
    let axiom = match triple.predicate {
        RDFS_SUB_CLASS_OF => {
            let Some(object) = object else {
                return Ok(None);
            };
            build_node(
                61,
                [
                    Field::Node(decode_class_expression(
                        expressions,
                        subject.as_term(),
                        consumed,
                        session,
                    )?),
                    Field::Node(decode_class_expression(
                        expressions,
                        object.as_term(),
                        consumed,
                        session,
                    )?),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        OWL_DISJOINT_WITH => {
            let Some(object) = object else {
                return Ok(None);
            };
            let mut expressions_set = reserved_vec(2, session)?;
            expressions_set.push(decode_class_expression(
                expressions,
                subject.as_term(),
                consumed,
                session,
            )?);
            expressions_set.push(decode_class_expression(
                expressions,
                object.as_term(),
                consumed,
                session,
            )?);
            let mut expressions_set = canonical_set(expressions_set, 1, None)?;
            if expressions_set.len() == 1 {
                build_node(
                    61,
                    [
                        Field::Node(expressions_set.pop().ok_or_else(|| {
                            NativeError::protocol("native RDF disjoint ledger is empty")
                        })?),
                        Field::Node(named_entity("class", OWL_NOTHING, session)?),
                        Field::Set(cloned_annotations(annotations, session)?),
                    ],
                    session,
                )?
            } else {
                build_node(
                    63,
                    [
                        Field::Set(expressions_set),
                        Field::Set(cloned_annotations(annotations, session)?),
                    ],
                    session,
                )?
            }
        }
        RDFS_SUB_PROPERTY_OF => {
            let Some(object) = object else {
                return Ok(None);
            };
            if matches!(triple.subject, ListResource::Iri(_)) && matches!(object, ClassTerm::Iri(_))
            {
                return Ok(None);
            }
            let DecodedPropertyExpression {
                node: sub_property,
                consumed: sub_consumed,
            } = expressions.decode_object_property_term(
                ClassTerm::from_resource(triple.subject).as_term(),
                session,
            )?;
            consume_collection_indexes(sub_consumed, consumed, session)?;
            let DecodedPropertyExpression {
                node: super_property,
                consumed: super_consumed,
            } = expressions.decode_object_property_term(object.as_term(), session)?;
            consume_collection_indexes(super_consumed, consumed, session)?;
            build_node(
                70,
                [
                    Field::Node(sub_property),
                    Field::Node(super_property),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        OWL_INVERSE_OF => {
            let (ListResource::Iri(first_iri), Some(second_term)) = (triple.subject, object) else {
                return Ok(None);
            };
            let DecodedPropertyExpression {
                node: mut second,
                consumed: property_consumed,
            } = expressions.decode_object_property_term(second_term.as_term(), session)?;
            consume_collection_indexes(property_consumed, consumed, session)?;
            let mut first = named_entity("object_property", first_iri, session)?;
            if second.as_bytes() < first.as_bytes() {
                std::mem::swap(&mut first, &mut second);
            }
            build_node(
                73,
                [
                    Field::Node(first),
                    Field::Node(second),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        OWL_PROPERTY_DISJOINT_WITH => {
            let Some(object) = object else {
                return Ok(None);
            };
            if matches!(triple.subject, ListResource::Iri(_)) && matches!(object, ClassTerm::Iri(_))
            {
                return Ok(None);
            }
            let DecodedPropertyExpression {
                node: first,
                consumed: first_consumed,
            } = expressions.decode_object_property_term(
                ClassTerm::from_resource(triple.subject).as_term(),
                session,
            )?;
            consume_collection_indexes(first_consumed, consumed, session)?;
            let DecodedPropertyExpression {
                node: second,
                consumed: second_consumed,
            } = expressions.decode_object_property_term(object.as_term(), session)?;
            consume_collection_indexes(second_consumed, consumed, session)?;
            let mut properties = reserved_vec(2, session)?;
            properties.push(first);
            properties.push(second);
            let properties = canonical_set(properties, 2, None)?;
            build_node(
                72,
                [
                    Field::Set(properties),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        RDFS_DOMAIN | RDFS_RANGE => {
            let Some(object) = object else {
                return Ok(None);
            };
            let (property, tag) = match triple.subject {
                ListResource::Iri(property) => {
                    if is_annotation_property(property, kinds) {
                        return Ok(None);
                    }
                    if triple.predicate == RDFS_RANGE && has_kind(kinds, property, "data_property")
                    {
                        let DecodedDataRange {
                            node: data_range,
                            consumed: range_consumed,
                        } = expressions.decode_data_term(object.as_term(), session)?;
                        consume_collection_indexes(range_consumed, consumed, session)?;
                        return Ok(Some(build_node(
                            94,
                            [
                                Field::Node(named_entity("data_property", property, session)?),
                                Field::Node(data_range),
                                Field::Set(cloned_annotations(annotations, session)?),
                            ],
                            session,
                        )?));
                    }
                    let (tag, property_kind) = if has_kind(kinds, property, "data_property") {
                        (93, "data_property")
                    } else if triple.predicate == RDFS_DOMAIN {
                        (74, "object_property")
                    } else {
                        (75, "object_property")
                    };
                    (named_entity(property_kind, property, session)?, tag)
                }
                ListResource::Blank(_) => {
                    let DecodedPropertyExpression {
                        node: property,
                        consumed: property_consumed,
                    } = expressions.decode_object_property_term(
                        ClassTerm::from_resource(triple.subject).as_term(),
                        session,
                    )?;
                    consume_collection_indexes(property_consumed, consumed, session)?;
                    let tag = if triple.predicate == RDFS_DOMAIN {
                        74
                    } else {
                        75
                    };
                    (property, tag)
                }
            };
            build_node(
                tag,
                [
                    Field::Node(property),
                    Field::Node(decode_class_expression(
                        expressions,
                        object.as_term(),
                        consumed,
                        session,
                    )?),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        RDF_TYPE => {
            let Some(class) = object else {
                return Ok(None);
            };
            if let ClassTerm::Iri(characteristic) = class {
                if let Some(tag) = characteristic_tag(characteristic, false) {
                    let ListResource::Blank(_) = triple.subject else {
                        return Ok(None);
                    };
                    let DecodedPropertyExpression {
                        node: property,
                        consumed: property_consumed,
                    } = expressions.decode_object_property_term(
                        ClassTerm::from_resource(triple.subject).as_term(),
                        session,
                    )?;
                    consume_collection_indexes(property_consumed, consumed, session)?;
                    return Ok(Some(build_node(
                        tag,
                        [
                            Field::Node(property),
                            Field::Set(cloned_annotations(annotations, session)?),
                        ],
                        session,
                    )?));
                }
            }
            if matches!(class, ClassTerm::Iri(value) if is_structural_type(value)) {
                return Ok(None);
            }
            build_node(
                112,
                [
                    Field::Node(decode_class_expression(
                        expressions,
                        class.as_term(),
                        consumed,
                        session,
                    )?),
                    Field::Node(expressions.decode_individual(
                        ClassTerm::from_resource(triple.subject).as_term(),
                        session,
                    )?),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        _ => return Ok(None),
    };
    Ok(Some(axiom))
}

#[allow(clippy::too_many_arguments)]
fn map_equivalent_property_components<'view, 'graph>(
    predicate: &str,
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    mut occurrence_anchors: Option<&mut Vec<ComponentOccurrenceAnchor<'graph>>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for start in 0..triples.len() {
        if consumed[start] {
            continue;
        }
        let Some((left, right)) = property_equivalent_edge(&triples[start], predicate) else {
            continue;
        };
        if !equivalent_property_member_supported(left, kinds)
            || !equivalent_property_member_supported(right, kinds)
        {
            continue;
        }
        let mut members = Vec::new();
        let mut edge_indexes = Vec::new();
        add_property_member(&mut members, left, session)?;
        add_property_member(&mut members, right, session)?;
        reserve_vec_item(&mut edge_indexes, session)?;
        edge_indexes.push(start);
        consumed[start] = true;
        loop {
            let mut changed = false;
            for (index, triple) in triples.iter().enumerate() {
                session.step(1)?;
                if consumed[index] {
                    continue;
                }
                let Some((edge_left, edge_right)) = property_equivalent_edge(triple, predicate)
                else {
                    continue;
                };
                if !equivalent_property_member_supported(edge_left, kinds)
                    || !equivalent_property_member_supported(edge_right, kinds)
                {
                    continue;
                }
                if members.contains(&edge_left) || members.contains(&edge_right) {
                    add_property_member(&mut members, edge_left, session)?;
                    add_property_member(&mut members, edge_right, session)?;
                    reserve_vec_item(&mut edge_indexes, session)?;
                    edge_indexes.push(index);
                    consumed[index] = true;
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }
        if let Some(anchors) = occurrence_anchors.as_deref_mut() {
            push_component_occurrence_anchor(
                anchors,
                1,
                members.iter().copied().map(class_term_anchor),
                session,
            )?;
        }
        let data_properties = members.iter().all(
            |value| matches!(value, ClassTerm::Iri(iri) if has_kind(kinds, iri, "data_property")),
        );
        let tag = if data_properties { 91 } else { 71 };
        let mut nodes = reserved_vec(members.len(), session)?;
        for member in members {
            if data_properties {
                let ClassTerm::Iri(value) = member else {
                    return Err(NativeError::protocol(
                        "native data property component contains a blank expression",
                    ));
                };
                nodes.push(named_entity("data_property", value, session)?);
            } else {
                let DecodedPropertyExpression {
                    node,
                    consumed: property_consumed,
                } = expressions.decode_object_property_term(member.as_term(), session)?;
                consume_collection_indexes(property_consumed, consumed, session)?;
                nodes.push(node);
            }
        }
        let nodes = canonical_set(nodes, 2, None)?;
        let annotations =
            component_annotations(&edge_indexes, source_triples, reifications, session)?;
        let axiom = build_node(tag, [Field::Set(nodes), Field::Set(annotations)], session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn property_equivalent_edge<'graph>(
    triple: &ListTriple<'graph>,
    predicate: &str,
) -> Option<(ClassTerm<'graph>, ClassTerm<'graph>)> {
    if triple.predicate != predicate {
        return None;
    }
    Some((
        ClassTerm::from_resource(triple.subject),
        ClassTerm::from_term(triple.object)?,
    ))
}

fn equivalent_property_member_supported(value: ClassTerm<'_>, kinds: &[KindRecord<'_>]) -> bool {
    match value {
        ClassTerm::Iri(value) => !has_kind(kinds, value, "annotation_property"),
        ClassTerm::Blank(_) => true,
    }
}

fn add_property_member<'graph>(
    members: &mut Vec<ClassTerm<'graph>>,
    value: ClassTerm<'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.step(
        u64::try_from(members.len())
            .map_err(|_| NativeError::limit("native RDF property component work exceeds u64"))?,
    )?;
    if !members.contains(&value) {
        reserve_vec_item(members, session)?;
        members.push(value);
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_same_individual_components<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    source_triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    reifications: &mut AxiomAnnotationLedger,
    axioms: &mut Vec<Vec<u8>>,
    mut occurrence_anchors: Option<&mut Vec<ComponentOccurrenceAnchor<'graph>>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for start in 0..triples.len() {
        if consumed[start] {
            continue;
        }
        let Some((left, right)) = individual_edge(&triples[start], OWL_SAME_AS) else {
            continue;
        };
        let mut members = Vec::new();
        let mut edge_indexes = Vec::new();
        add_individual_member(&mut members, left, session)?;
        add_individual_member(&mut members, right, session)?;
        reserve_vec_item(&mut edge_indexes, session)?;
        edge_indexes.push(start);
        consumed[start] = true;
        loop {
            let mut changed = false;
            for (index, triple) in triples.iter().enumerate() {
                session.step(1)?;
                if consumed[index] {
                    continue;
                }
                let Some((edge_left, edge_right)) = individual_edge(triple, OWL_SAME_AS) else {
                    continue;
                };
                if members.contains(&edge_left) || members.contains(&edge_right) {
                    add_individual_member(&mut members, edge_left, session)?;
                    add_individual_member(&mut members, edge_right, session)?;
                    reserve_vec_item(&mut edge_indexes, session)?;
                    edge_indexes.push(index);
                    consumed[index] = true;
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }

        if let Some(anchors) = occurrence_anchors.as_deref_mut() {
            push_component_occurrence_anchor(
                anchors,
                2,
                members.iter().copied().map(individual_term_anchor),
                session,
            )?;
        }
        let mut nodes = reserved_vec(members.len(), session)?;
        for member in members {
            nodes.push(expressions.decode_individual(member.as_term(), session)?);
        }
        session.finish()?;
        let nodes = canonical_set(nodes, 2, None)?;
        let annotations =
            component_annotations(&edge_indexes, source_triples, reifications, session)?;
        let axiom = build_node(110, [Field::Set(nodes), Field::Set(annotations)], session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn individual_edge<'graph>(
    triple: &ListTriple<'graph>,
    predicate: &str,
) -> Option<(IndividualTerm<'graph>, IndividualTerm<'graph>)> {
    if triple.predicate != predicate {
        return None;
    }
    Some((
        IndividualTerm::from_resource(triple.subject),
        IndividualTerm::from_term(triple.object)?,
    ))
}

fn add_individual_member<'graph>(
    members: &mut Vec<IndividualTerm<'graph>>,
    value: IndividualTerm<'graph>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session
        .step(u64::try_from(members.len()).map_err(|_| {
            NativeError::limit("native RDF individual component work exceeds u64")
        })?)?;
    if !members.contains(&value) {
        reserve_vec_item(members, session)?;
        members.push(value);
    }
    Ok(())
}

fn collect_axiom_annotations<'view, 'graph>(
    triples: &'graph [Triple],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<AxiomAnnotationLedger> {
    let nested_records = collect_nested_annotation_records(triples, consumed, session)?;
    validate_nested_annotation_records(&nested_records, triples, session)?;
    let mut ledger = AxiomAnnotationLedger {
        records: Vec::new(),
        nested_records,
    };
    for (type_index, type_triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed.get(type_index).copied().ok_or_else(|| {
            NativeError::protocol("native RDF consumed ledger is shorter than graph")
        })? || type_triple.predicate != RDF_TYPE
            || !matches!(&type_triple.object, Term::Iri(value) if value == OWL_AXIOM)
        {
            continue;
        }
        let reification = &type_triple.subject;
        let main_index = reification_main_index(reification, triples, session)?;

        let mut annotations = Vec::new();
        for (index, triple) in triples.iter().enumerate() {
            session.step(1)?;
            if &triple.subject != reification || is_reification_metadata(&triple.predicate) {
                continue;
            }
            let annotation = build_node(
                5,
                [
                    Field::Node(named_entity(
                        "annotation_property",
                        &triple.predicate,
                        session,
                    )?),
                    Field::Node(annotation_value(index, triple, expressions, session)?),
                    Field::Set(ledger.nested_annotations_for(
                        index,
                        triples,
                        expressions,
                        session,
                    )?),
                ],
                session,
            )?;
            enforce_usize(
                annotations.len().saturating_add(1),
                session.limits().value(LimitKey::MaxAnnotations),
                "native RDF axiom annotations exceed max_annotations",
            )?;
            session.reserve_bytes(annotation.as_bytes().len())?;
            reserve_vec_item(&mut annotations, session)?;
            annotations.push(annotation);
        }
        let annotations = canonical_set(annotations, 0, None)?;
        reserve_vec_item(&mut ledger.records, session)?;
        ledger.records.push(AxiomAnnotationRecord {
            main_index,
            annotations,
            claimed: false,
        });
        consume_reification_node(reification, triples, consumed, session)?;
    }
    Ok(ledger)
}

fn collect_nested_annotation_records(
    triples: &[Triple],
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<Vec<NestedAnnotationRecord>> {
    let mut records = Vec::new();
    for (type_index, type_triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed.get(type_index).copied().ok_or_else(|| {
            NativeError::protocol("native RDF consumed ledger is shorter than graph")
        })? || type_triple.predicate != RDF_TYPE
            || !matches!(&type_triple.object, Term::Iri(value) if value == OWL_ANNOTATION)
        {
            continue;
        }
        let reification = &type_triple.subject;
        if triples.iter().any(|triple| {
            triple.subject == *reification
                && triple.predicate == RDF_TYPE
                && matches!(&triple.object, Term::Iri(value) if value == OWL_AXIOM)
        }) {
            return Err(rdf_axiom_reification(
                "native RDF reification node cannot be both owl:Annotation and owl:Axiom",
            ));
        }
        let main_index = reification_main_index(reification, triples, session)?;
        reserve_vec_item(&mut records, session)?;
        records.push(NestedAnnotationRecord {
            type_index,
            main_index,
            claimed: false,
        });
        consume_reification_node(reification, triples, consumed, session)?;
    }
    Ok(records)
}

fn validate_nested_annotation_records(
    records: &[NestedAnnotationRecord],
    triples: &[Triple],
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let mut stack = Vec::new();
    for record in records {
        validate_nested_annotation_main(record.main_index, records, triples, &mut stack, session)?;
    }
    Ok(())
}

fn validate_nested_annotation_main(
    main_index: usize,
    records: &[NestedAnnotationRecord],
    triples: &[Triple],
    stack: &mut Vec<usize>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let main = triples.get(main_index).ok_or_else(|| {
        NativeError::protocol("native nested annotation main index exceeds graph")
    })?;
    if stack.iter().any(|index| {
        triples
            .get(*index)
            .is_some_and(|candidate| candidate == main)
    }) {
        return Err(rdf_axiom_reification(
            "native RDF annotation reification contains a cycle",
        ));
    }
    let mut matching = Vec::new();
    for record in records {
        session.step(1)?;
        let record_main = triples.get(record.main_index).ok_or_else(|| {
            NativeError::protocol("native nested annotation main index exceeds graph")
        })?;
        if record_main == main {
            reserve_vec_item(&mut matching, session)?;
            matching.push(record.type_index);
        }
    }
    if matching.is_empty() {
        return Ok(());
    }
    enforce_usize(
        stack.len().saturating_add(1),
        session.limits().value(LimitKey::MaxNestingDepth),
        "native RDF annotation nesting exceeds max_nesting_depth",
    )?;
    reserve_vec_item(stack, session)?;
    stack.push(main_index);
    for type_index in matching {
        let reification = &triples
            .get(type_index)
            .ok_or_else(|| {
                NativeError::protocol("native annotation reification index exceeds graph")
            })?
            .subject;
        for (index, triple) in triples.iter().enumerate() {
            session.step(1)?;
            if &triple.subject != reification || is_reification_metadata(&triple.predicate) {
                continue;
            }
            validate_nested_annotation_main(index, records, triples, stack, session)?;
        }
    }
    stack
        .pop()
        .ok_or_else(|| NativeError::protocol("native RDF annotation validation stack is empty"))?;
    Ok(())
}

fn nested_annotations<'view, 'graph>(
    main_index: usize,
    records: &mut [NestedAnnotationRecord],
    triples: &'graph [Triple],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    stack: &mut Vec<usize>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<Node>> {
    let main = triples.get(main_index).ok_or_else(|| {
        NativeError::protocol("native nested annotation main index exceeds graph")
    })?;
    let has_records = records.iter().any(|record| {
        triples
            .get(record.main_index)
            .is_some_and(|candidate| candidate == main)
    });
    if !has_records {
        return Ok(Vec::new());
    }
    if stack.iter().any(|index| {
        triples
            .get(*index)
            .is_some_and(|candidate| candidate == main)
    }) {
        return Err(rdf_axiom_reification(
            "native RDF annotation reification contains a cycle",
        ));
    }
    enforce_usize(
        stack.len().saturating_add(1),
        session.limits().value(LimitKey::MaxNestingDepth),
        "native RDF annotation nesting exceeds max_nesting_depth",
    )?;
    reserve_vec_item(stack, session)?;
    stack.push(main_index);

    let mut annotations = Vec::new();
    for record_index in 0..records.len() {
        session.step(1)?;
        let matches = {
            let record_main = triples
                .get(records[record_index].main_index)
                .ok_or_else(|| {
                    NativeError::protocol("native nested annotation main index exceeds graph")
                })?;
            record_main == main
        };
        if !matches {
            continue;
        }
        records[record_index].claimed = true;
        let type_index = records[record_index].type_index;
        let reification = &triples
            .get(type_index)
            .ok_or_else(|| {
                NativeError::protocol("native annotation reification index exceeds graph")
            })?
            .subject;
        for (index, triple) in triples.iter().enumerate() {
            session.step(1)?;
            if &triple.subject != reification || is_reification_metadata(&triple.predicate) {
                continue;
            }
            let nested = nested_annotations(index, records, triples, expressions, stack, session)?;
            let annotation = build_node(
                5,
                [
                    Field::Node(named_entity(
                        "annotation_property",
                        &triple.predicate,
                        session,
                    )?),
                    Field::Node(annotation_value(index, triple, expressions, session)?),
                    Field::Set(nested),
                ],
                session,
            )?;
            enforce_usize(
                annotations.len().saturating_add(1),
                session.limits().value(LimitKey::MaxAnnotations),
                "native RDF nested annotations exceed max_annotations",
            )?;
            reserve_vec_item(&mut annotations, session)?;
            annotations.push(annotation);
        }
    }
    stack
        .pop()
        .ok_or_else(|| NativeError::protocol("native RDF annotation recursion stack is empty"))?;
    canonical_set(annotations, 0, None)
}

fn reification_main_index(
    reification: &Resource,
    triples: &[Triple],
    session: &mut Session<'_>,
) -> NativeResult<usize> {
    let (_, source) = unique_reification_term(reification, OWL_ANNOTATED_SOURCE, triples, session)?;
    let (_, property) =
        unique_reification_term(reification, OWL_ANNOTATED_PROPERTY, triples, session)?;
    let (_, target) = unique_reification_term(reification, OWL_ANNOTATED_TARGET, triples, session)?;
    if !matches!(source, Term::Iri(_) | Term::Blank(_)) {
        return Err(rdf_axiom_reification(
            "native RDF annotatedSource must be an IRI or blank node",
        ));
    }
    let Term::Iri(property) = property else {
        return Err(rdf_axiom_reification(
            "native RDF annotatedProperty must be an IRI",
        ));
    };
    let mut main_index = None;
    for (index, candidate) in triples.iter().enumerate() {
        session.step(1)?;
        if resource_matches_term(&candidate.subject, source)
            && candidate.predicate == *property
            && candidate.object == *target
        {
            main_index.get_or_insert(index);
        }
    }
    main_index.ok_or_else(|| rdf_axiom_reification("native RDF reification main triple is absent"))
}

fn consume_reification_node(
    reification: &Resource,
    triples: &[Triple],
    consumed: &mut [bool],
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if &triple.subject == reification {
            let value = consumed.get_mut(index).ok_or_else(|| {
                NativeError::protocol("native RDF consumed ledger is shorter than graph")
            })?;
            *value = true;
        }
    }
    Ok(())
}

fn unique_reification_term<'a>(
    reification: &Resource,
    predicate: &str,
    triples: &'a [Triple],
    session: &mut Session<'_>,
) -> NativeResult<(usize, &'a Term)> {
    let mut value = None;
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if &triple.subject != reification || triple.predicate != predicate {
            continue;
        }
        if value.replace((index, &triple.object)).is_some() {
            return Err(rdf_axiom_reification(
                "native RDF reification metadata must have cardinality one",
            ));
        }
    }
    value.ok_or_else(|| {
        rdf_axiom_reification(
            "native RDF reification requires source, property, and target metadata",
        )
    })
}

fn resource_matches_term(resource: &Resource, term: &Term) -> bool {
    match (resource, term) {
        (Resource::Iri(left), Term::Iri(right)) | (Resource::Blank(left), Term::Blank(right)) => {
            left == right
        }
        (Resource::Iri(_) | Resource::Blank(_), Term::Literal { .. })
        | (Resource::Iri(_), Term::Blank(_))
        | (Resource::Blank(_), Term::Iri(_)) => false,
    }
}

fn is_reification_metadata(predicate: &str) -> bool {
    matches!(
        predicate,
        RDF_TYPE | OWL_ANNOTATED_SOURCE | OWL_ANNOTATED_PROPERTY | OWL_ANNOTATED_TARGET
    )
}

fn annotation_axiom<'view, 'graph>(
    index: usize,
    triple: &'graph Triple,
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    annotations: &[Node],
    session: &mut Session<'_>,
) -> NativeResult<Option<Node>> {
    if triple.predicate == RDFS_SUB_PROPERTY_OF {
        let (Resource::Iri(sub_property), Term::Iri(super_property)) =
            (&triple.subject, &triple.object)
        else {
            return Ok(None);
        };
        if is_annotation_property(sub_property, kinds)
            || is_annotation_property(super_property, kinds)
        {
            return Ok(Some(build_node(
                121,
                [
                    Field::Node(named_entity("annotation_property", sub_property, session)?),
                    Field::Node(named_entity(
                        "annotation_property",
                        super_property,
                        session,
                    )?),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?));
        }
        return Ok(None);
    }
    if matches!(triple.predicate.as_str(), RDFS_DOMAIN | RDFS_RANGE) {
        let Resource::Iri(property) = &triple.subject else {
            return Ok(None);
        };
        if !is_annotation_property(property, kinds) {
            return Ok(None);
        }
        let target = match &triple.object {
            Term::Iri(value) => iri_node(value, session)?,
            Term::Blank(_) => return Err(rdf_mapping_type()),
            Term::Literal { .. } => return Ok(None),
        };
        let tag = if triple.predicate == RDFS_DOMAIN {
            122
        } else {
            123
        };
        return Ok(Some(build_node(
            tag,
            [
                Field::Node(named_entity("annotation_property", property, session)?),
                Field::Node(target),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?));
    }
    if triple.predicate == RDF_TYPE && !matches!(triple.object, Term::Literal { .. }) {
        return Ok(None);
    }
    if !is_annotation_property(&triple.predicate, kinds) {
        return Ok(None);
    }
    let subject = annotation_subject(&triple.subject, expressions, session)?;
    let value = annotation_value(index, triple, expressions, session)?;
    Ok(Some(build_node(
        120,
        [
            Field::Node(named_entity(
                "annotation_property",
                &triple.predicate,
                session,
            )?),
            Field::Node(subject),
            Field::Node(value),
            Field::Set(cloned_annotations(annotations, session)?),
        ],
        session,
    )?))
}

fn annotation_node<'view, 'graph>(
    index: usize,
    triple: &ListTriple<'graph>,
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    nested: Vec<Node>,
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    build_node(
        5,
        [
            Field::Node(named_entity(
                "annotation_property",
                triple.predicate,
                session,
            )?),
            Field::Node(annotation_list_value(
                index,
                triple.object,
                expressions,
                session,
            )?),
            Field::Set(nested),
        ],
        session,
    )
}

fn annotation_subject<'view, 'graph>(
    value: &'graph Resource,
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    match value {
        Resource::Iri(value) => iri_node(value, session),
        Resource::Blank(value) => expressions.decode_individual(ListTerm::Blank(value), session),
    }
}

fn annotation_value<'view, 'graph>(
    index: usize,
    triple: &'graph Triple,
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    let value = match &triple.object {
        Term::Iri(value) => ListTerm::Iri(value),
        Term::Blank(value) => ListTerm::Blank(value),
        Term::Literal { lexical, .. } => ListTerm::Literal(lexical),
    };
    annotation_list_value(index, value, expressions, session)
}

fn annotation_list_value<'view, 'graph>(
    index: usize,
    value: ListTerm<'graph>,
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    match value {
        ListTerm::Iri(value) => iri_node(value, session),
        ListTerm::Blank(value) => expressions.decode_individual(ListTerm::Blank(value), session),
        ListTerm::Literal(_) => expressions.decode_literal(index, session),
    }
}

fn iri_node(value: &str, session: &mut Session<'_>) -> NativeResult<Node> {
    super::check_iri(
        value,
        session,
        "native RDF annotation IRI exceeds max_iri_bytes",
    )?;
    iri(owned_text(value, session)?)
}

fn assertion_axiom<'view, 'graph>(
    index: usize,
    triple: &'graph Triple,
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    annotations: &[Node],
    session: &mut Session<'_>,
) -> NativeResult<Option<Node>> {
    if triple.predicate == OWL_DIFFERENT_FROM {
        let object = match &triple.object {
            Term::Iri(value) => ListTerm::Iri(value),
            Term::Blank(value) => ListTerm::Blank(value),
            Term::Literal { .. } => return Ok(None),
        };
        let subject = match &triple.subject {
            Resource::Iri(value) => ListTerm::Iri(value),
            Resource::Blank(value) => ListTerm::Blank(value),
        };
        let mut individuals = reserved_vec(2, session)?;
        individuals.push(expressions.decode_individual(subject, session)?);
        individuals.push(expressions.decode_individual(object, session)?);
        let individuals = canonical_set(individuals, 2, None)?;
        return Ok(Some(build_node(
            111,
            [
                Field::Set(individuals),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?));
    }
    if is_annotation_property(&triple.predicate, kinds)
        || is_assertion_structural_predicate(&triple.predicate)
    {
        return Ok(None);
    }
    let subject = match &triple.subject {
        Resource::Iri(value) => ListTerm::Iri(value),
        Resource::Blank(value) => ListTerm::Blank(value),
    };
    let axiom = match &triple.object {
        Term::Literal { .. } if has_kind(kinds, &triple.predicate, "data_property") => build_node(
            115,
            [
                Field::Node(named_entity("data_property", &triple.predicate, session)?),
                Field::Node(expressions.decode_individual(subject, session)?),
                Field::Node(expressions.decode_literal(index, session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        Term::Iri(value) if has_kind(kinds, &triple.predicate, "object_property") => build_node(
            113,
            [
                Field::Node(named_entity("object_property", &triple.predicate, session)?),
                Field::Node(expressions.decode_individual(subject, session)?),
                Field::Node(expressions.decode_individual(ListTerm::Iri(value), session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        Term::Blank(value) if has_kind(kinds, &triple.predicate, "object_property") => build_node(
            113,
            [
                Field::Node(named_entity("object_property", &triple.predicate, session)?),
                Field::Node(expressions.decode_individual(subject, session)?),
                Field::Node(expressions.decode_individual(ListTerm::Blank(value), session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        Term::Iri(_) | Term::Blank(_) | Term::Literal { .. } => return Ok(None),
    };
    Ok(Some(axiom))
}

fn is_annotation_property(value: &str, kinds: &[KindRecord<'_>]) -> bool {
    has_kind(kinds, value, "annotation_property")
        || matches!(
            value,
            "http://www.w3.org/2000/01/rdf-schema#label"
                | "http://www.w3.org/2000/01/rdf-schema#comment"
                | "http://www.w3.org/2000/01/rdf-schema#seeAlso"
                | "http://www.w3.org/2000/01/rdf-schema#isDefinedBy"
                | "http://www.w3.org/2002/07/owl#deprecated"
                | "http://www.w3.org/2002/07/owl#versionInfo"
                | "http://www.w3.org/2002/07/owl#priorVersion"
                | "http://www.w3.org/2002/07/owl#backwardCompatibleWith"
                | "http://www.w3.org/2002/07/owl#incompatibleWith"
        )
}

fn is_assertion_structural_predicate(value: &str) -> bool {
    if value.starts_with(SWRL) {
        return true;
    }
    if matches!(
        value,
        RDF_TYPE
            | RDF_FIRST
            | RDF_REST
            | RDFS_SUB_CLASS_OF
            | RDFS_SUB_PROPERTY_OF
            | RDFS_DOMAIN
            | RDFS_RANGE
    ) {
        return true;
    }
    value.strip_prefix(OWL).is_some_and(|local| {
        matches!(
            local,
            "imports"
                | "versionIRI"
                | "intersectionOf"
                | "unionOf"
                | "complementOf"
                | "oneOf"
                | "datatypeComplementOf"
                | "onDatatype"
                | "withRestrictions"
                | "onProperty"
                | "onProperties"
                | "someValuesFrom"
                | "allValuesFrom"
                | "hasValue"
                | "hasSelf"
                | "minCardinality"
                | "maxCardinality"
                | "cardinality"
                | "minQualifiedCardinality"
                | "maxQualifiedCardinality"
                | "qualifiedCardinality"
                | "onClass"
                | "onDataRange"
                | "equivalentClass"
                | "disjointWith"
                | "disjointUnionOf"
                | "equivalentProperty"
                | "propertyDisjointWith"
                | "inverseOf"
                | "propertyChainAxiom"
                | "hasKey"
                | "sameAs"
                | "differentFrom"
                | "members"
                | "distinctMembers"
                | "sourceIndividual"
                | "assertionProperty"
                | "targetIndividual"
                | "targetValue"
                | "annotatedSource"
                | "annotatedProperty"
                | "annotatedTarget"
        )
    })
}

fn named_axiom(
    triple: &Triple,
    kinds: &[KindRecord<'_>],
    annotations: &[Node],
    session: &mut Session<'_>,
) -> NativeResult<Option<Node>> {
    let (Resource::Iri(subject), Term::Iri(object)) = (&triple.subject, &triple.object) else {
        return Ok(None);
    };
    let axiom = match triple.predicate.as_str() {
        RDFS_SUB_CLASS_OF => build_node(
            61,
            [
                Field::Node(named_entity("class", subject, session)?),
                Field::Node(named_entity("class", object, session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        OWL_DISJOINT_WITH if subject == object => build_node(
            61,
            [
                Field::Node(named_entity("class", subject, session)?),
                Field::Node(named_entity("class", OWL_NOTHING, session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        OWL_DISJOINT_WITH => build_node(
            63,
            [
                Field::Set(named_set("class", &[subject, object], session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        RDFS_SUB_PROPERTY_OF
            if has_kind(kinds, subject, "annotation_property")
                || has_kind(kinds, object, "annotation_property") =>
        {
            return Ok(None);
        }
        RDFS_SUB_PROPERTY_OF
            if has_kind(kinds, subject, "data_property")
                || has_kind(kinds, object, "data_property") =>
        {
            build_binary_named_axiom(90, "data_property", subject, object, annotations, session)?
        }
        RDFS_SUB_PROPERTY_OF => {
            build_binary_named_axiom(70, "object_property", subject, object, annotations, session)?
        }
        OWL_PROPERTY_DISJOINT_WITH if has_kind(kinds, subject, "data_property") => build_node(
            92,
            [
                Field::Set(named_set("data_property", &[subject, object], session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        OWL_PROPERTY_DISJOINT_WITH => build_node(
            72,
            [
                Field::Set(named_set("object_property", &[subject, object], session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        OWL_INVERSE_OF => {
            let mut first = named_entity("object_property", subject, session)?;
            let mut second = named_entity("object_property", object, session)?;
            if second.as_bytes() < first.as_bytes() {
                std::mem::swap(&mut first, &mut second);
            }
            build_node(
                73,
                [
                    Field::Node(first),
                    Field::Node(second),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        RDFS_DOMAIN | RDFS_RANGE if has_kind(kinds, subject, "annotation_property") => {
            return Ok(None);
        }
        RDFS_DOMAIN if has_kind(kinds, subject, "data_property") => build_node(
            93,
            [
                Field::Node(named_entity("data_property", subject, session)?),
                Field::Node(named_entity("class", object, session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        RDFS_RANGE if has_kind(kinds, subject, "data_property") => build_node(
            94,
            [
                Field::Node(named_entity("data_property", subject, session)?),
                Field::Node(named_entity("datatype", object, session)?),
                Field::Set(cloned_annotations(annotations, session)?),
            ],
            session,
        )?,
        RDFS_DOMAIN => {
            build_binary_named_axiom(74, "object_property", subject, object, annotations, session)?
        }
        RDFS_RANGE => {
            build_binary_named_axiom(75, "object_property", subject, object, annotations, session)?
        }
        RDF_TYPE
            if matches!(
                object.as_str(),
                OWL_DEPRECATED_CLASS | OWL_DEPRECATED_PROPERTY
            ) =>
        {
            let value = crate::canonical::literal(
                owned_text("true", session)?,
                named_entity("datatype", XSD_BOOLEAN, session)?,
                None,
            )?;
            build_node(
                120,
                [
                    Field::Node(named_entity(
                        "annotation_property",
                        OWL_DEPRECATED,
                        session,
                    )?),
                    Field::Node(iri_node(subject, session)?),
                    Field::Node(value),
                    Field::Set(cloned_annotations(annotations, session)?),
                ],
                session,
            )?
        }
        RDF_TYPE if has_kind(kinds, subject, "annotation_property") => return Ok(None),
        RDF_TYPE => {
            if let Some(tag) = characteristic_tag(object, has_kind(kinds, subject, "data_property"))
            {
                let kind = if tag == 95 {
                    "data_property"
                } else {
                    "object_property"
                };
                build_node(
                    tag,
                    [
                        Field::Node(named_entity(kind, subject, session)?),
                        Field::Set(cloned_annotations(annotations, session)?),
                    ],
                    session,
                )?
            } else if !is_structural_type(object) {
                build_node(
                    112,
                    [
                        Field::Node(named_entity("class", object, session)?),
                        Field::Node(named_entity("named_individual", subject, session)?),
                        Field::Set(cloned_annotations(annotations, session)?),
                    ],
                    session,
                )?
            } else {
                return Ok(None);
            }
        }
        _ => return Ok(None),
    };
    Ok(Some(axiom))
}

fn characteristic_tag(value: &str, data_property: bool) -> Option<u64> {
    Some(match value {
        OWL_FUNCTIONAL_PROPERTY if data_property => 95,
        OWL_FUNCTIONAL_PROPERTY => 76,
        OWL_INVERSE_FUNCTIONAL_PROPERTY => 77,
        "http://www.w3.org/2002/07/owl#ReflexiveProperty" => 78,
        "http://www.w3.org/2002/07/owl#IrreflexiveProperty" => 79,
        OWL_SYMMETRIC_PROPERTY => 80,
        "http://www.w3.org/2002/07/owl#AsymmetricProperty" => 81,
        OWL_TRANSITIVE_PROPERTY => 82,
        _ => return None,
    })
}

fn is_structural_type(value: &str) -> bool {
    value.starts_with(OWL)
        || matches!(
            value,
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#List"
                | RDF_PROPERTY
                | RDFS_CLASS
                | RDFS_DATATYPE
        )
}

fn build_binary_named_axiom(
    tag: u64,
    first_kind: &'static str,
    first: &str,
    second: &str,
    annotations: &[Node],
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    let second_kind = match tag {
        74 | 75 => "class",
        _ => first_kind,
    };
    build_node(
        tag,
        [
            Field::Node(named_entity(first_kind, first, session)?),
            Field::Node(named_entity(second_kind, second, session)?),
            Field::Set(cloned_annotations(annotations, session)?),
        ],
        session,
    )
}

fn cloned_annotations(annotations: &[Node], session: &mut Session<'_>) -> NativeResult<Vec<Node>> {
    let mut output = reserved_vec(annotations.len(), session)?;
    for annotation in annotations {
        session.reserve_bytes(annotation.as_bytes().len())?;
        output.push(annotation.clone());
    }
    Ok(output)
}

fn named_set(
    kind: &'static str,
    values: &[&str],
    session: &mut Session<'_>,
) -> NativeResult<Vec<Node>> {
    let mut nodes = reserved_vec(values.len(), session)?;
    for value in values {
        nodes.push(named_entity(kind, value, session)?);
    }
    canonical_set(nodes, 2, None)
}

fn named_entity(kind: &'static str, value: &str, session: &mut Session<'_>) -> NativeResult<Node> {
    super::check_iri(
        value,
        session,
        "native RDF named-node IRI exceeds max_iri_bytes",
    )?;
    entity(kind, iri(owned_text(value, session)?)?)
}

fn build_node<const N: usize>(
    tag: u64,
    fields: [Field; N],
    session: &mut Session<'_>,
) -> NativeResult<Node> {
    let mut values = reserved_vec(N, session)?;
    values.extend(fields);
    Node::build(tag, values)
}

fn push_axiom(
    axiom: Node,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.reserve_bytes(axiom.as_bytes().len())?;
    let mut encoded = Vec::new();
    encoded
        .try_reserve_exact(axiom.as_bytes().len())
        .map_err(|_| NativeError::limit("native RDF axiom allocation failed"))?;
    encoded.extend_from_slice(axiom.as_bytes());
    reserve_vec_item(axioms, session)?;
    axioms.push(encoded);
    Ok(())
}

fn push_extension(
    extension: Node,
    extensions: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.reserve_bytes(extension.as_bytes().len())?;
    let mut encoded = Vec::new();
    encoded
        .try_reserve_exact(extension.as_bytes().len())
        .map_err(|_| NativeError::limit("native RDF extension allocation failed"))?;
    encoded.extend_from_slice(extension.as_bytes());
    reserve_vec_item(extensions, session)?;
    extensions.push(encoded);
    Ok(())
}

fn capture_occurrences_since(
    rows: &[Vec<u8>],
    start: usize,
    collection: u8,
    occurrences: &mut Vec<CanonicalOccurrence>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let selected = rows.get(start..).ok_or_else(|| {
        NativeError::protocol("native RDF occurrence capture starts outside its root table")
    })?;
    occurrences
        .try_reserve_exact(selected.len())
        .map_err(|_| NativeError::limit("native RDF occurrence allocation failed"))?;
    for row in selected {
        session.reserve_bytes(row.len())?;
        occurrences.push(CanonicalOccurrence {
            collection,
            row: row.clone(),
        });
    }
    Ok(())
}

fn swrl_rule_occurrence_anchors(
    triples: &[Triple],
    session: &mut Session<'_>,
) -> NativeResult<Vec<usize>> {
    let mut anchors = Vec::new();
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if triple.predicate == RDF_TYPE
            && matches!(&triple.object, Term::Iri(value) if value == SWRL_IMP)
        {
            reserve_vec_item(&mut anchors, session)?;
            anchors.push(index);
        }
    }
    Ok(anchors)
}

fn special_occurrence_anchors(
    triples: &[Triple],
    session: &mut Session<'_>,
) -> NativeResult<Vec<usize>> {
    let mut anchors = Vec::new();
    for selected in [
        &[OWL_NEGATIVE_PROPERTY_ASSERTION][..],
        &[OWL_ALL_DIFFERENT][..],
        &[OWL_ALL_DISJOINT_CLASSES, OWL_ALL_DISJOINT_PROPERTIES][..],
    ] {
        for (index, triple) in triples.iter().enumerate() {
            session.step(1)?;
            if triple.predicate == RDF_TYPE
                && matches!(&triple.object, Term::Iri(value) if selected.contains(&value.as_str()))
            {
                reserve_vec_item(&mut anchors, session)?;
                anchors.push(index);
            }
        }
    }
    Ok(anchors)
}

fn pre_simple_occurrence_anchors(
    triples: &[Triple],
    consumed: &[bool],
    kinds: &[KindRecord<'_>],
    session: &mut Session<'_>,
) -> NativeResult<Vec<usize>> {
    if triples.len() != consumed.len() {
        return Err(NativeError::protocol(
            "native RDF simple occurrence ledger diverges from graph",
        ));
    }
    let mut anchors = Vec::new();
    for phase in 0_u8..5 {
        for (index, triple) in triples.iter().enumerate() {
            session.step(1)?;
            if consumed[index] {
                continue;
            }
            let selected = match phase {
                0 => triple.predicate == OWL_PROPERTY_CHAIN_AXIOM,
                1 => triple.predicate == OWL_HAS_KEY,
                2 => triple.predicate == OWL_DISJOINT_UNION_OF,
                3 => {
                    matches!(
                        triple.predicate.as_str(),
                        OWL_COMPLEMENT_OF | OWL_UNION_OF | OWL_INTERSECTION_OF | OWL_ONE_OF
                    ) && matches!(
                        &triple.subject,
                        Resource::Iri(value) if has_kind(kinds, value, "class")
                    )
                }
                4 => {
                    triple.predicate == OWL_EQUIVALENT_CLASS
                        && matches!(
                            &triple.subject,
                            Resource::Iri(value) if has_kind(kinds, value, "datatype")
                        )
                }
                _ => {
                    return Err(NativeError::protocol(
                        "native RDF simple occurrence phase is invalid",
                    ))
                }
            };
            if selected {
                reserve_vec_item(&mut anchors, session)?;
                anchors.push(index);
            }
        }
    }
    Ok(anchors)
}

fn order_occurrences_by_anchors(
    occurrences: &mut Vec<CanonicalOccurrence>,
    anchors: &[usize],
    triples: &[Triple],
    mismatch: &'static str,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    if occurrences.len() != anchors.len() {
        return Err(NativeError::protocol(mismatch));
    }
    if anchors.iter().any(|index| *index >= triples.len()) {
        return Err(NativeError::protocol(
            "native RDF occurrence anchor exceeds graph",
        ));
    }
    let mut keyed = reserved_vec(occurrences.len(), session)?;
    for (anchor, occurrence) in anchors.iter().copied().zip(occurrences.drain(..)) {
        keyed.push((anchor, occurrence));
    }
    keyed.sort_unstable_by(|left, right| {
        python_triple_cmp(&triples[left.0], &triples[right.0]).then_with(|| left.0.cmp(&right.0))
    });
    occurrences.extend(keyed.into_iter().map(|(_anchor, occurrence)| occurrence));
    Ok(())
}

fn push_component_occurrence_anchor<'graph>(
    anchors: &mut Vec<ComponentOccurrenceAnchor<'graph>>,
    phase: u8,
    members: impl IntoIterator<Item = PythonResourceAnchor<'graph>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let member = members
        .into_iter()
        .min_by(python_resource_anchor_cmp)
        .ok_or_else(|| NativeError::protocol("native RDF equivalence component is empty"))?;
    reserve_vec_item(anchors, session)?;
    anchors.push(ComponentOccurrenceAnchor { phase, member });
    Ok(())
}

fn order_occurrences_by_component_anchors(
    occurrences: &mut Vec<CanonicalOccurrence>,
    anchors: &[ComponentOccurrenceAnchor<'_>],
    session: &mut Session<'_>,
) -> NativeResult<()> {
    if occurrences.len() != anchors.len() {
        return Err(NativeError::protocol(
            "native RDF component anchors diverge from mapped roots",
        ));
    }
    let mut keyed = reserved_vec(occurrences.len(), session)?;
    for (anchor, occurrence) in anchors.iter().copied().zip(occurrences.drain(..)) {
        keyed.push((anchor, occurrence));
    }
    keyed.sort_unstable_by(|left, right| {
        left.0
            .phase
            .cmp(&right.0.phase)
            .then_with(|| python_resource_anchor_cmp(&left.0.member, &right.0.member))
            .then_with(|| left.1.row.cmp(&right.1.row))
    });
    occurrences.extend(keyed.into_iter().map(|(_anchor, occurrence)| occurrence));
    Ok(())
}

fn class_term_anchor(value: ClassTerm<'_>) -> PythonResourceAnchor<'_> {
    match value {
        ClassTerm::Iri(value) => PythonResourceAnchor::Iri(value),
        ClassTerm::Blank(value) => PythonResourceAnchor::Blank(value),
    }
}

fn individual_term_anchor(value: IndividualTerm<'_>) -> PythonResourceAnchor<'_> {
    match value {
        IndividualTerm::Iri(value) => PythonResourceAnchor::Iri(value),
        IndividualTerm::Blank(value) => PythonResourceAnchor::Blank(value),
    }
}

fn python_resource_anchor_cmp(
    left: &PythonResourceAnchor<'_>,
    right: &PythonResourceAnchor<'_>,
) -> Ordering {
    match (left, right) {
        (PythonResourceAnchor::Blank(left), PythonResourceAnchor::Blank(right))
        | (PythonResourceAnchor::Iri(left), PythonResourceAnchor::Iri(right)) => left.cmp(right),
        (PythonResourceAnchor::Blank(_), PythonResourceAnchor::Iri(_)) => Ordering::Less,
        (PythonResourceAnchor::Iri(_), PythonResourceAnchor::Blank(_)) => Ordering::Greater,
    }
}

fn python_triple_cmp(left: &Triple, right: &Triple) -> Ordering {
    python_resource_cmp(&left.subject, &right.subject)
        .then_with(|| left.predicate.cmp(&right.predicate))
        .then_with(|| python_term_cmp(&left.object, &right.object))
}

#[derive(Clone, Copy)]
struct PythonOrderedTriple<'a>(&'a Triple);

impl PartialEq for PythonOrderedTriple<'_> {
    fn eq(&self, other: &Self) -> bool {
        python_triple_cmp(self.0, other.0) == Ordering::Equal
    }
}

impl Eq for PythonOrderedTriple<'_> {}

impl PartialOrd for PythonOrderedTriple<'_> {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for PythonOrderedTriple<'_> {
    fn cmp(&self, other: &Self) -> Ordering {
        python_triple_cmp(self.0, other.0)
    }
}

fn python_resource_cmp(left: &Resource, right: &Resource) -> Ordering {
    match (left, right) {
        (Resource::Blank(left), Resource::Blank(right))
        | (Resource::Iri(left), Resource::Iri(right)) => left.cmp(right),
        (Resource::Blank(_), Resource::Iri(_)) => Ordering::Less,
        (Resource::Iri(_), Resource::Blank(_)) => Ordering::Greater,
    }
}

fn python_term_cmp(left: &Term, right: &Term) -> Ordering {
    match (left, right) {
        (Term::Blank(left), Term::Blank(right)) | (Term::Iri(left), Term::Iri(right)) => {
            left.cmp(right)
        }
        (
            Term::Literal {
                lexical: left_lexical,
                datatype: left_datatype,
                language: left_language,
            },
            Term::Literal {
                lexical: right_lexical,
                datatype: right_datatype,
                language: right_language,
            },
        ) => left_lexical
            .cmp(right_lexical)
            .then_with(|| {
                left_language
                    .as_deref()
                    .unwrap_or("")
                    .cmp(right_language.as_deref().unwrap_or(""))
            })
            .then_with(|| {
                left_datatype
                    .as_deref()
                    .unwrap_or("")
                    .cmp(right_datatype.as_deref().unwrap_or(""))
            }),
        (Term::Blank(_), Term::Iri(_) | Term::Literal { .. }) => Ordering::Less,
        (Term::Iri(_), Term::Blank(_)) => Ordering::Greater,
        (Term::Iri(_), Term::Literal { .. }) => Ordering::Less,
        (Term::Literal { .. }, Term::Blank(_) | Term::Iri(_)) => Ordering::Greater,
    }
}

fn partial_mapping_evidence(
    triples: &[Triple],
    consumed: &[bool],
    unconsumed_count: usize,
    session: &mut Session<'_>,
) -> NativeResult<Vec<RdfTripleEvidence>> {
    if triples.len() != consumed.len() {
        return Err(NativeError::protocol(
            "native RDF partial-mapping ledger diverges from graph",
        ));
    }
    let maximum =
        usize::try_from(session.limits().value(LimitKey::MaxDiagnostics)).unwrap_or(usize::MAX);
    let mut selected = BinaryHeap::from(reserved_temporary_vec(
        unconsumed_count.min(maximum),
        session,
    )?);
    let mut observed_unconsumed = 0_usize;
    for (triple, retained) in triples.iter().zip(consumed) {
        session.step(1)?;
        if *retained {
            continue;
        }
        observed_unconsumed = observed_unconsumed
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF unconsumed count overflow"))?;
        if observed_unconsumed > unconsumed_count {
            return Err(NativeError::protocol(
                "native RDF unconsumed count diverges from its ledger",
            ));
        }
        if selected.len() < maximum {
            selected.push(PythonOrderedTriple(triple));
        } else if selected
            .peek()
            .is_some_and(|largest| python_triple_cmp(triple, largest.0) == Ordering::Less)
        {
            selected.pop();
            selected.push(PythonOrderedTriple(triple));
        }
    }
    if observed_unconsumed != unconsumed_count {
        return Err(NativeError::protocol(
            "native RDF unconsumed count diverges from its ledger",
        ));
    }

    let mut result = reserved_vec(selected.len(), session)?;
    for PythonOrderedTriple(triple) in selected.into_sorted_vec() {
        let (object, object_requires_repr) = rdf_term_evidence(&triple.object, session)?;
        result.push(RdfTripleEvidence {
            subject: rdf_resource_evidence(&triple.subject, session)?,
            predicate: owned_text(&triple.predicate, session)?,
            object,
            object_requires_repr,
        });
    }
    Ok(result)
}

fn rdf_resource_evidence(value: &Resource, session: &mut Session<'_>) -> NativeResult<String> {
    match value {
        Resource::Iri(value) => framed_rdf_evidence('<', value, '>', session),
        Resource::Blank(value) => prefixed_rdf_evidence("_:", value, session),
    }
}

fn rdf_term_evidence(value: &Term, session: &mut Session<'_>) -> NativeResult<(String, bool)> {
    match value {
        Term::Iri(value) => Ok((framed_rdf_evidence('<', value, '>', session)?, false)),
        Term::Blank(value) => Ok((prefixed_rdf_evidence("_:", value, session)?, false)),
        Term::Literal { lexical, .. } => {
            reserve_python_repr_capacity(lexical, session)?;
            Ok((owned_text(lexical, session)?, true))
        }
    }
}

fn framed_rdf_evidence(
    prefix: char,
    value: &str,
    suffix: char,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    let capacity = value
        .len()
        .checked_add(prefix.len_utf8())
        .and_then(|size| size.checked_add(suffix.len_utf8()))
        .ok_or_else(|| NativeError::limit("native RDF evidence text size overflow"))?;
    session.reserve_bytes(capacity)?;
    let mut output = String::new();
    output
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native RDF evidence text allocation failed"))?;
    output.push(prefix);
    output.push_str(value);
    output.push(suffix);
    Ok(output)
}

fn prefixed_rdf_evidence(
    prefix: &str,
    value: &str,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    let capacity = value
        .len()
        .checked_add(prefix.len())
        .ok_or_else(|| NativeError::limit("native RDF evidence text size overflow"))?;
    session.reserve_bytes(capacity)?;
    let mut output = String::new();
    output
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native RDF evidence text allocation failed"))?;
    output.push_str(prefix);
    output.push_str(value);
    Ok(output)
}

fn reserve_python_repr_capacity(value: &str, session: &mut Session<'_>) -> NativeResult<()> {
    let maximum = value.chars().try_fold(2_usize, |total, character| {
        let escaped = match u32::from(character) {
            0..=0xff => 4,
            0x100..=0xffff => 6,
            _ => 10,
        };
        total
            .checked_add(escaped)
            .ok_or_else(|| NativeError::limit("native RDF literal evidence size overflow"))
    })?;
    session.reserve_bytes(maximum)
}

fn push_annotation(
    annotation: Node,
    annotations: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.reserve_bytes(annotation.as_bytes().len())?;
    let mut encoded = Vec::new();
    encoded
        .try_reserve_exact(annotation.as_bytes().len())
        .map_err(|_| NativeError::limit("native RDF annotation allocation failed"))?;
    encoded.extend_from_slice(annotation.as_bytes());
    reserve_vec_item(annotations, session)?;
    annotations.push(encoded);
    Ok(())
}

fn reserved_vec<T>(count: usize, session: &mut Session<'_>) -> NativeResult<Vec<T>> {
    let bytes = count
        .checked_mul(std::mem::size_of::<T>())
        .ok_or_else(|| NativeError::limit("native RDF allocation accounting overflow"))?;
    session.reserve_bytes(bytes)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(count)
        .map_err(|_| NativeError::limit("native RDF allocation failed"))?;
    Ok(output)
}

fn reserved_temporary_vec<T>(count: usize, session: &mut Session<'_>) -> NativeResult<Vec<T>> {
    let bytes = count
        .checked_mul(std::mem::size_of::<T>())
        .ok_or_else(|| NativeError::limit("native RDF temporary allocation overflow"))?;
    session.reserve_temporary_bytes(bytes)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(count)
        .map_err(|_| NativeError::limit("native RDF temporary allocation failed"))?;
    Ok(output)
}

fn sort_iris(values: &mut [String]) {
    values.sort_unstable_by(|left, right| {
        let (left_key, left_size) = varint_key(left.len());
        let (right_key, right_size) = varint_key(right.len());
        left_key[..left_size]
            .cmp(&right_key[..right_size])
            .then_with(|| left.as_bytes().cmp(right.as_bytes()))
    });
}

fn varint_key(value: usize) -> ([u8; 10], usize) {
    let mut output = [0_u8; 10];
    let mut value = value as u64;
    let mut size = 0_usize;
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        output[size] = byte | if value == 0 { 0 } else { 0x80 };
        size += 1;
        if value == 0 {
            return (output, size);
        }
    }
}

fn declaration_kind(value: &str) -> Option<&'static str> {
    Some(match value {
        OWL_CLASS => "class",
        RDFS_DATATYPE => "datatype",
        OWL_OBJECT_PROPERTY => "object_property",
        OWL_DATATYPE_PROPERTY => "data_property",
        OWL_ANNOTATION_PROPERTY | OWL_ONTOLOGY_PROPERTY => "annotation_property",
        "http://www.w3.org/2002/07/owl#NamedIndividual" => "named_individual",
        _ => return None,
    })
}

fn inferred_declaration_kind(value: &str) -> Option<&'static str> {
    matches!(
        value,
        OWL_INVERSE_FUNCTIONAL_PROPERTY | OWL_SYMMETRIC_PROPERTY | OWL_TRANSITIVE_PROPERTY
    )
    .then_some("object_property")
}

fn has_explicit_declaration(triples: &[Triple], subject: &str, kind: &str) -> bool {
    triples.iter().any(|triple| {
        matches!(&triple.subject, Resource::Iri(value) if value == subject)
            && triple.predicate == RDF_TYPE
            && matches!(&triple.object, Term::Iri(value) if declaration_kind(value) == Some(kind))
    })
}

#[derive(Clone, Copy, Debug)]
struct IriReference<'a> {
    scheme: Option<&'a str>,
    authority: Option<&'a str>,
    path: &'a str,
    query: Option<&'a str>,
    fragment: Option<&'a str>,
}

fn resolve_iri(value: &str, base: Option<&str>, session: &mut Session<'_>) -> NativeResult<String> {
    enforce_resolved_iri_size(value.len(), session)?;
    let reference = parse_iri_reference(value)?;
    let parsed_base = match (reference.scheme, base) {
        (Some(_), _) => None,
        (None, Some(base)) => {
            let parsed = parse_iri_reference(base).map_err(|_| invalid_base_iri())?;
            if parsed.scheme.is_none() {
                return Err(invalid_base_iri());
            }
            Some(parsed)
        }
        (None, None) => {
            return Err(NativeError::new(
                "NATIVE_RDFXML_RELATIVE_IRI_NO_BASE",
                "native RDF/XML relative IRI requires an absolute base",
            ));
        }
    };

    let (scheme, authority, path, query) = if let Some(scheme) = reference.scheme {
        (
            scheme,
            reference.authority,
            remove_dot_segments(reference.path, session)?,
            reference.query,
        )
    } else {
        let base = parsed_base.ok_or_else(invalid_base_iri)?;
        let scheme = base.scheme.ok_or_else(invalid_base_iri)?;
        if reference.authority.is_some() {
            (
                scheme,
                reference.authority,
                remove_dot_segments(reference.path, session)?,
                reference.query,
            )
        } else if reference.path.is_empty() {
            (
                scheme,
                base.authority,
                owned_text(base.path, session)?,
                reference.query.or(base.query),
            )
        } else if reference.path.starts_with('/') {
            (
                scheme,
                base.authority,
                remove_dot_segments(reference.path, session)?,
                reference.query,
            )
        } else {
            let merged = merge_paths(base, reference.path, session)?;
            (
                scheme,
                base.authority,
                remove_dot_segments(&merged, session)?,
                reference.query,
            )
        }
    };
    let resolved = serialize_iri(scheme, authority, &path, query, reference.fragment, session)?;
    super::check_iri(
        &resolved,
        session,
        "native RDF/XML IRI exceeds max_iri_bytes",
    )?;
    Ok(resolved)
}

fn parse_iri_reference(value: &str) -> NativeResult<IriReference<'_>> {
    let (without_fragment, fragment) = value
        .split_once('#')
        .map_or((value, None), |(head, tail)| (head, Some(tail)));
    let (hierarchical, query) = without_fragment
        .split_once('?')
        .map_or((without_fragment, None), |(head, tail)| (head, Some(tail)));
    let first_slash = hierarchical.find('/').unwrap_or(hierarchical.len());
    let colon = hierarchical.find(':');
    let (scheme, remainder) = match colon {
        Some(colon) if colon < first_slash && valid_scheme(&hierarchical[..colon]) => {
            (Some(&hierarchical[..colon]), &hierarchical[colon + 1..])
        }
        Some(colon) if colon < first_slash => {
            return Err(NativeError::new(
                "NATIVE_RDFXML_IRI_REFERENCE",
                "native RDF/XML IRI reference has an invalid scheme",
            ));
        }
        _ => (None, hierarchical),
    };
    let (authority, path) = if let Some(remainder) = remainder.strip_prefix("//") {
        let end = remainder.find('/').unwrap_or(remainder.len());
        (Some(&remainder[..end]), &remainder[end..])
    } else {
        (None, remainder)
    };
    Ok(IriReference {
        scheme,
        authority,
        path,
        query,
        fragment,
    })
}

fn valid_scheme(value: &str) -> bool {
    !value.is_empty()
        && value.as_bytes()[0].is_ascii_alphabetic()
        && value.as_bytes()[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'+' | b'-' | b'.'))
}

fn merge_paths(
    base: IriReference<'_>,
    reference_path: &str,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    let prefix = if base.authority.is_some() && base.path.is_empty() {
        "/"
    } else {
        base.path
            .rfind('/')
            .map_or("", |position| &base.path[..=position])
    };
    let size = prefix
        .len()
        .checked_add(reference_path.len())
        .ok_or_else(|| NativeError::limit("native RDF/XML merged path size overflow"))?;
    enforce_resolved_iri_size(size, session)?;
    prefixed_text(prefix, reference_path, session)
}

fn remove_dot_segments(path: &str, session: &mut Session<'_>) -> NativeResult<String> {
    enforce_resolved_iri_size(path.len(), session)?;
    session.reserve_bytes(path.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(path.len())
        .map_err(|_| NativeError::limit("native RDF/XML path allocation failed"))?;
    let mut input = path;
    while !input.is_empty() {
        if let Some(remainder) = input.strip_prefix("../") {
            input = remainder;
        } else if let Some(remainder) = input.strip_prefix("./") {
            input = remainder;
        } else if input.starts_with("/./") {
            input = &input[2..];
        } else if input == "/." {
            input = "/";
        } else if input.starts_with("/../") {
            input = &input[3..];
            remove_last_path_segment(&mut output);
        } else if input == "/.." {
            input = "/";
            remove_last_path_segment(&mut output);
        } else if matches!(input, "." | "..") {
            input = "";
        } else {
            let end = if let Some(remainder) = input.strip_prefix('/') {
                remainder
                    .find('/')
                    .map_or(input.len(), |position| position + 1)
            } else {
                input.find('/').unwrap_or(input.len())
            };
            output.push_str(&input[..end]);
            input = &input[end..];
        }
    }
    Ok(output)
}

fn remove_last_path_segment(value: &mut String) {
    value.truncate(value.rfind('/').unwrap_or(0));
}

fn serialize_iri(
    scheme: &str,
    authority: Option<&str>,
    path: &str,
    query: Option<&str>,
    fragment: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    let mut size = scheme
        .len()
        .checked_add(1)
        .and_then(|value| value.checked_add(path.len()))
        .ok_or_else(|| NativeError::limit("native RDF/XML resolved IRI size overflow"))?;
    if let Some(authority) = authority {
        size = size
            .checked_add(2)
            .and_then(|value| value.checked_add(authority.len()))
            .ok_or_else(|| NativeError::limit("native RDF/XML resolved IRI size overflow"))?;
    }
    for value in [query, fragment].into_iter().flatten() {
        size = size
            .checked_add(1)
            .and_then(|size| size.checked_add(value.len()))
            .ok_or_else(|| NativeError::limit("native RDF/XML resolved IRI size overflow"))?;
    }
    enforce_resolved_iri_size(size, session)?;
    session.reserve_bytes(size)?;
    let mut output = String::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF/XML resolved IRI allocation failed"))?;
    output.push_str(scheme);
    output.push(':');
    if let Some(authority) = authority {
        output.push_str("//");
        output.push_str(authority);
    }
    output.push_str(path);
    if let Some(query) = query {
        output.push('?');
        output.push_str(query);
    }
    if let Some(fragment) = fragment {
        output.push('#');
        output.push_str(fragment);
    }
    Ok(output)
}

fn enforce_resolved_iri_size(size: usize, session: &Session<'_>) -> NativeResult<()> {
    if u64::try_from(size).map_or(true, |size| {
        size > session.limits().value(LimitKey::MaxIriBytes)
    }) {
        Err(NativeError::limit(
            "native RDF/XML resolved IRI exceeds max_iri_bytes",
        ))
    } else {
        Ok(())
    }
}

fn invalid_base_iri() -> NativeError {
    NativeError::new(
        "NATIVE_RDFXML_INVALID_BASE_IRI",
        "native RDF/XML base IRI is not an absolute RFC 3986 IRI",
    )
}

#[derive(Clone, Copy)]
enum XmlValueKind {
    Text,
    Attribute,
}

fn decode_references(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    decode_xml_value(value, XmlValueKind::Text, session)
}

fn decode_attribute_references(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    decode_xml_value(value, XmlValueKind::Attribute, session)
}

fn decode_xml_value(
    value: &str,
    kind: XmlValueKind,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native XML reference allocation failed"))?;
    let mut cursor = 0;
    while let Some(relative) = value[cursor..].find('&') {
        let start = cursor + relative;
        append_normalized_xml_characters(&mut output, &value[cursor..start], kind)?;
        let end = value[start + 1..]
            .find(';')
            .map(|offset| start + 1 + offset)
            .ok_or_else(xml_syntax)?;
        let reference = &value[start + 1..end];
        match reference {
            "amp" => output.push('&'),
            "lt" => output.push('<'),
            "gt" => output.push('>'),
            "apos" => output.push('\''),
            "quot" => output.push('"'),
            _ if reference.starts_with("#x") => {
                let value = u32::from_str_radix(&reference[2..], 16).map_err(|_| xml_syntax())?;
                output.push(xml_character(value)?);
            }
            _ if reference.starts_with('#') => {
                let value = reference[1..].parse::<u32>().map_err(|_| xml_syntax())?;
                output.push(xml_character(value)?);
            }
            _ if is_xml_name(reference) => return Err(xml_forbidden()),
            _ => return Err(xml_syntax()),
        }
        cursor = end + 1;
    }
    append_normalized_xml_characters(&mut output, &value[cursor..], kind)?;
    Ok(output)
}

fn normalize_xml_characters(
    value: &str,
    kind: XmlValueKind,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native XML value allocation failed"))?;
    append_normalized_xml_characters(&mut output, value, kind)?;
    Ok(output)
}

fn append_normalized_xml_characters(
    output: &mut String,
    value: &str,
    kind: XmlValueKind,
) -> NativeResult<()> {
    let mut characters = value.chars().peekable();
    while let Some(character) = characters.next() {
        if !is_xml_character(character as u32) {
            return Err(xml_syntax());
        }
        match character {
            '\r' => {
                if characters.peek() == Some(&'\n') {
                    characters.next();
                }
                output.push(match kind {
                    XmlValueKind::Text => '\n',
                    XmlValueKind::Attribute => ' ',
                });
            }
            '\n' | '\t' if matches!(kind, XmlValueKind::Attribute) => output.push(' '),
            _ => output.push(character),
        }
    }
    Ok(())
}

fn validate_xml_characters(value: &str) -> NativeResult<()> {
    if value
        .chars()
        .all(|character| is_xml_character(character as u32))
    {
        Ok(())
    } else {
        Err(xml_syntax())
    }
}

fn xml_character(value: u32) -> NativeResult<char> {
    let character = char::from_u32(value).ok_or_else(xml_syntax)?;
    if !is_xml_character(value) {
        return Err(xml_syntax());
    }
    Ok(character)
}

fn is_xml_character(value: u32) -> bool {
    matches!(value, 0x09 | 0x0a | 0x0d | 0x20..=0xd7ff | 0xe000..=0xfffd | 0x10000..=0x10ffff)
}

fn scan_name(text: &str, start: usize) -> NativeResult<usize> {
    let suffix = text.get(start..).ok_or_else(xml_syntax)?;
    let mut characters = suffix.char_indices();
    let (_, first) = characters.next().ok_or_else(xml_syntax)?;
    if !is_xml_name_start(first) {
        return Err(xml_syntax());
    }
    let mut end = start
        .checked_add(first.len_utf8())
        .ok_or_else(|| NativeError::limit("native XML name offset overflow"))?;
    for (offset, character) in characters {
        if !is_xml_name_character(character) {
            break;
        }
        end = start
            .checked_add(offset)
            .and_then(|value| value.checked_add(character.len_utf8()))
            .ok_or_else(|| NativeError::limit("native XML name offset overflow"))?;
    }
    Ok(end)
}

fn is_xml_name_start(value: char) -> bool {
    matches!(
        value,
        ':' | 'A'..='Z'
            | '_'
            | 'a'..='z'
            | '\u{00c0}'..='\u{00d6}'
            | '\u{00d8}'..='\u{00f6}'
            | '\u{00f8}'..='\u{02ff}'
            | '\u{0370}'..='\u{037d}'
            | '\u{037f}'..='\u{1fff}'
            | '\u{200c}'..='\u{200d}'
            | '\u{2070}'..='\u{218f}'
            | '\u{2c00}'..='\u{2fef}'
            | '\u{3001}'..='\u{d7ff}'
            | '\u{f900}'..='\u{fdcf}'
            | '\u{fdf0}'..='\u{fffd}'
            | '\u{10000}'..='\u{effff}'
    )
}

fn is_xml_name_character(value: char) -> bool {
    is_xml_name_start(value)
        || matches!(
            value,
            '-' | '.' | '0'..='9' | '\u{00b7}' | '\u{0300}'..='\u{036f}' | '\u{203f}'..='\u{2040}'
        )
}

fn is_xml_name(value: &str) -> bool {
    let mut characters = value.chars();
    characters.next().is_some_and(is_xml_name_start) && characters.all(is_xml_name_character)
}

fn is_xml_ncname(value: &str) -> bool {
    let mut characters = value.chars();
    characters
        .next()
        .is_some_and(|character| character != ':' && is_xml_name_start(character))
        && characters.all(|character| character != ':' && is_xml_name_character(character))
}

fn bounded_find(
    bytes: &[u8],
    start: usize,
    end: usize,
    marker: &[u8],
    session: &mut Session<'_>,
) -> NativeResult<Option<usize>> {
    if marker.is_empty() || start > end || end > bytes.len() {
        return Err(xml_syntax());
    }
    session.finish()?;
    let Some(last_start) = end.checked_sub(marker.len()) else {
        return Ok(None);
    };
    let mut cursor = start;
    while cursor <= last_start {
        let batch_end = cursor.saturating_add(64 * 1024).min(last_start + 1);
        for position in cursor..batch_end {
            if bytes[position..].starts_with(marker) {
                return Ok(Some(position));
            }
        }
        cursor = batch_end;
        session.finish()?;
    }
    Ok(None)
}

fn skip_space(bytes: &[u8], cursor: &mut usize) {
    while bytes
        .get(*cursor)
        .is_some_and(|value| matches!(*value, b' ' | b'\t' | b'\r' | b'\n'))
    {
        *cursor += 1;
    }
}

fn validate_xml_declaration(
    declaration: &str,
    source_encoding: XmlSourceEncoding,
) -> NativeResult<()> {
    validate_xml_characters(declaration)?;
    let bytes = declaration.as_bytes();
    if !declaration.starts_with("xml") || !bytes.get(3).is_some_and(|value| is_xml_space(*value)) {
        return Err(xml_syntax());
    }
    let mut cursor = 3;
    skip_space(bytes, &mut cursor);
    let (name, version) = xml_declaration_attribute(declaration, &mut cursor)?;
    if name != "version" || version != "1.0" {
        return Err(xml_syntax());
    }
    let mut encoding_seen = false;
    let mut standalone_seen = false;
    while cursor < bytes.len() {
        if !is_xml_space(bytes[cursor]) {
            return Err(xml_syntax());
        }
        skip_space(bytes, &mut cursor);
        if cursor == bytes.len() {
            break;
        }
        let (name, value) = xml_declaration_attribute(declaration, &mut cursor)?;
        match name {
            "encoding" if !encoding_seen && !standalone_seen => {
                encoding_seen = true;
                let compatible = match source_encoding {
                    XmlSourceEncoding::Utf8 => ["utf-8", "utf8", "us-ascii"]
                        .iter()
                        .any(|encoding| value.eq_ignore_ascii_case(encoding)),
                    XmlSourceEncoding::Utf16Le => ["utf-16", "utf-16le"]
                        .iter()
                        .any(|encoding| value.eq_ignore_ascii_case(encoding)),
                    XmlSourceEncoding::Utf16Be => ["utf-16", "utf-16be"]
                        .iter()
                        .any(|encoding| value.eq_ignore_ascii_case(encoding)),
                };
                if !compatible {
                    return Err(xml_forbidden());
                }
            }
            "standalone" if !standalone_seen => {
                standalone_seen = true;
                if !matches!(value, "yes" | "no") {
                    return Err(xml_syntax());
                }
            }
            _ => return Err(xml_syntax()),
        }
    }
    Ok(())
}

fn xml_declaration_attribute<'a>(
    declaration: &'a str,
    cursor: &mut usize,
) -> NativeResult<(&'a str, &'a str)> {
    let bytes = declaration.as_bytes();
    let name_start = *cursor;
    while bytes.get(*cursor).is_some_and(u8::is_ascii_alphabetic) {
        *cursor += 1;
    }
    if *cursor == name_start {
        return Err(xml_syntax());
    }
    let name = &declaration[name_start..*cursor];
    skip_space(bytes, cursor);
    if bytes.get(*cursor) != Some(&b'=') {
        return Err(xml_syntax());
    }
    *cursor += 1;
    skip_space(bytes, cursor);
    let quote = *bytes.get(*cursor).ok_or_else(xml_syntax)?;
    if !matches!(quote, b'\'' | b'"') {
        return Err(xml_syntax());
    }
    *cursor += 1;
    let value_start = *cursor;
    while bytes.get(*cursor).is_some_and(|value| *value != quote) {
        if matches!(bytes[*cursor], b'<' | b'&') {
            return Err(xml_syntax());
        }
        *cursor += 1;
    }
    if bytes.get(*cursor) != Some(&quote) {
        return Err(xml_syntax());
    }
    let value = &declaration[value_start..*cursor];
    *cursor += 1;
    Ok((name, value))
}

fn is_xml_space(value: u8) -> bool {
    matches!(value, b' ' | b'\t' | b'\r' | b'\n')
}

fn clone_resource(value: &Resource, session: &mut Session<'_>) -> NativeResult<Resource> {
    match value {
        Resource::Iri(value) => owned_text(value, session).map(Resource::Iri),
        Resource::Blank(value) => owned_text(value, session).map(Resource::Blank),
    }
}

fn clone_term(value: &Term, session: &mut Session<'_>) -> NativeResult<Term> {
    match value {
        Term::Iri(value) => owned_text(value, session).map(Term::Iri),
        Term::Blank(value) => owned_text(value, session).map(Term::Blank),
        Term::Literal {
            lexical,
            datatype,
            language,
        } => Ok(Term::Literal {
            lexical: owned_text(lexical, session)?,
            datatype: datatype
                .as_deref()
                .map(|value| owned_text(value, session))
                .transpose()?,
            language: language
                .as_deref()
                .map(|value| owned_text(value, session))
                .transpose()?,
        }),
    }
}

fn owned_ascii_lowercase(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    let mut output = owned_text(value, session)?;
    output.make_ascii_lowercase();
    Ok(output)
}

fn prefixed_text(prefix: &str, value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    let size = prefix
        .len()
        .checked_add(value.len())
        .ok_or_else(|| NativeError::limit("native XML token size overflow"))?;
    session.reserve_bytes(size)?;
    let mut output = String::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native XML token allocation failed"))?;
    output.push_str(prefix);
    output.push_str(value);
    Ok(output)
}

fn generated_blank(value: u64, session: &mut Session<'_>) -> NativeResult<String> {
    use std::fmt::Write;

    let digits = if value == 0 {
        1
    } else {
        usize::try_from(value.ilog10())
            .map_err(|_| NativeError::limit("native RDF blank identifier size overflow"))?
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF blank identifier size overflow"))?
    };
    let size = GENERATED_BLANK_PREFIX
        .len()
        .checked_add(digits)
        .ok_or_else(|| NativeError::limit("native RDF blank identifier size overflow"))?;
    session.reserve_bytes(size)?;
    let mut output = String::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF blank identifier allocation failed"))?;
    output.push_str(GENERATED_BLANK_PREFIX);
    write!(&mut output, "{value}")
        .map_err(|_| NativeError::protocol("native RDF blank identifier formatting failed"))?;
    Ok(output)
}

fn rdf_membership_property(value: u64, session: &mut Session<'_>) -> NativeResult<String> {
    use std::fmt::Write;

    let digits = if value == 0 {
        1
    } else {
        usize::try_from(value.ilog10())
            .map_err(|_| NativeError::limit("native RDF membership IRI size overflow"))?
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native RDF membership IRI size overflow"))?
    };
    let size = RDF
        .len()
        .checked_add(1)
        .and_then(|prefix| prefix.checked_add(digits))
        .ok_or_else(|| NativeError::limit("native RDF membership IRI size overflow"))?;
    enforce_usize(
        size,
        session.limits().value(LimitKey::MaxIriBytes),
        "native RDF membership IRI exceeds max_iri_bytes",
    )?;
    session.reserve_bytes(size)?;
    let mut output = String::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF membership IRI allocation failed"))?;
    output.push_str(RDF);
    write!(&mut output, "_{value}")
        .map_err(|_| NativeError::protocol("native RDF membership IRI formatting failed"))?;
    Ok(output)
}

fn element_tree_namespace_prefix(iri: &str) -> Option<&'static str> {
    match iri {
        "http://www.w3.org/1999/xhtml" => Some("html"),
        RDF => Some("rdf"),
        "http://schemas.xmlsoap.org/wsdl/" => Some("wsdl"),
        "http://www.w3.org/2001/XMLSchema" => Some("xs"),
        "http://www.w3.org/2001/XMLSchema-instance" => Some("xsi"),
        "http://purl.org/dc/elements/1.1/" => Some("dc"),
        _ => None,
    }
}

fn numbered_xml_prefix(value: usize, session: &mut Session<'_>) -> NativeResult<String> {
    use std::fmt::Write;

    let value = u64::try_from(value)
        .map_err(|_| NativeError::limit("native XML literal namespace count exceeds u64"))?;
    let digits = if value == 0 {
        1
    } else {
        usize::try_from(value.ilog10())
            .map_err(|_| NativeError::limit("native XML literal prefix size overflow"))?
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native XML literal prefix size overflow"))?
    };
    let size = 2_usize
        .checked_add(digits)
        .ok_or_else(|| NativeError::limit("native XML literal prefix size overflow"))?;
    session.reserve_bytes(size)?;
    let mut output = String::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native XML literal prefix allocation failed"))?;
    write!(&mut output, "ns{value}")
        .map_err(|_| NativeError::protocol("native XML literal prefix formatting failed"))?;
    Ok(output)
}

fn expanded_name_matches(expanded: &str, namespace: &str, local: &str) -> bool {
    expanded
        .len()
        .checked_sub(namespace.len())
        .is_some_and(|length| length == local.len() && expanded.starts_with(namespace))
        && expanded.ends_with(local)
}

fn is_core_syntax_iri(value: &str) -> bool {
    matches!(
        value,
        RDF_RDF | RDF_ID | RDF_ABOUT | RDF_PARSE_TYPE | RDF_RESOURCE | RDF_NODE_ID | RDF_DATATYPE
    )
}

fn is_old_syntax_iri(value: &str) -> bool {
    matches!(value, RDF_ABOUT_EACH | RDF_ABOUT_EACH_PREFIX | RDF_BAG_ID)
}

fn is_node_element_iri(value: &str) -> bool {
    !is_core_syntax_iri(value) && value != RDF_LI && !is_old_syntax_iri(value)
}

fn is_property_element_iri(value: &str) -> bool {
    !is_core_syntax_iri(value) && value != RDF_DESCRIPTION && !is_old_syntax_iri(value)
}

fn is_property_attribute_iri(value: &str) -> bool {
    !is_core_syntax_iri(value)
        && !matches!(value, RDF_DESCRIPTION | RDF_LI)
        && !is_old_syntax_iri(value)
        && !value.starts_with(XML)
}

fn is_forbidden_rdf_property_attribute_iri(value: &str) -> bool {
    is_core_syntax_iri(value)
        || matches!(value, RDF_DESCRIPTION | RDF_LI)
        || is_old_syntax_iri(value)
}

fn legacy_unqualified_rdf_attribute(value: &str) -> Option<(usize, &'static str)> {
    match value {
        "ID" => Some((0, RDF_ID)),
        "about" => Some((1, RDF_ABOUT)),
        "resource" => Some((2, RDF_RESOURCE)),
        "parseType" => Some((3, RDF_PARSE_TYPE)),
        "type" => Some((4, RDF_TYPE)),
        _ => None,
    }
}

fn owned_text(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native XML token allocation failed"))?;
    output.push_str(value);
    Ok(output)
}

fn reserve_vec_item<T>(values: &mut Vec<T>, session: &mut Session<'_>) -> NativeResult<()> {
    if values.len() == values.capacity() {
        session.reserve_bytes(std::mem::size_of::<T>())?;
        values
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native XML table allocation failed"))?;
    }
    Ok(())
}

fn reserve_temporary_vec_item<T>(
    values: &mut Vec<T>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    if values.len() == values.capacity() {
        session.reserve_temporary_bytes(std::mem::size_of::<T>())?;
        values
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native RDF temporary allocation failed"))?;
    }
    Ok(())
}

fn enforce_usize(value: usize, maximum: u64, message: &'static str) -> NativeResult<()> {
    enforce_u64(
        u64::try_from(value).map_err(|_| NativeError::limit(message))?,
        maximum,
        message,
    )
}

fn enforce_u64(value: u64, maximum: u64, message: &'static str) -> NativeResult<()> {
    if value > maximum {
        Err(NativeError::limit(message))
    } else {
        Ok(())
    }
}

fn xml_syntax() -> NativeError {
    NativeError::new("NATIVE_RDFXML_SYNTAX", "native RDF/XML source is malformed")
}

fn xml_forbidden() -> NativeError {
    NativeError::new(
        "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        "native XML forbidden construct is disabled",
    )
}

fn mapping_incomplete() -> NativeError {
    NativeError::new(
        "NATIVE_RDF_MAPPING_INCOMPLETE",
        "native first-slice RDF mapping does not consume this construct",
    )
}

fn rdf_mapping_type() -> NativeError {
    NativeError::new(
        "NATIVE_RDF_MAPPING_TYPE",
        "native RDF mapping term has the wrong structural type",
    )
}

fn rdf_mapping_cardinality(message: &'static str) -> NativeError {
    NativeError::new("NATIVE_RDF_MAPPING_CARDINALITY", message)
}

fn rdf_axiom_reification(message: &'static str) -> NativeError {
    NativeError::new("NATIVE_RDF_AXIOM_REIFICATION", message)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::{Cancellation, Guard};
    use crate::canonical::literal;
    use crate::limits::{Limits, CONFIG_BYTES, CONFIG_MAGIC, CONFIG_SCHEMA};
    use std::time::Duration;

    fn mapped(source: &[u8], document_iri: Option<&str>) -> NativeResult<CanonicalDocument> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len())?;
        parse_and_map(source, document_iri, &mut session)
    }

    fn mapped_partial(
        source: &[u8],
        document_iri: Option<&str>,
    ) -> NativeResult<CanonicalDocument> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len())?;
        Ok(parse_and_map_timed(source, document_iri, true, true, false, false, &mut session)?.0)
    }

    fn resolved(reference: &str, base: Option<&str>) -> NativeResult<String> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0)?;
        resolve_iri(reference, base, &mut session)
    }

    fn graph_with_limits(source: &str, limits: &Limits) -> NativeResult<Vec<Triple>> {
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, limits, source.len())?;
        Ok(
            GraphParser::new(source, None, XmlSourceEncoding::Utf8, false, &mut session)?
                .parse()?
                .triples,
        )
    }

    fn graph(source: &str) -> NativeResult<Vec<Triple>> {
        graph_with_limits(source, &Limits::default())
    }

    #[test]
    fn retained_source_prefixes_track_rebindings_and_undeclarations() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" \
             xmlns:e=\"urn:first:\" xmlns:xml=\"{XML}\" xmlns=\"urn:default:\">\
             <owl:Class xmlns:e=\"urn:second:\" xmlns=\"\" rdf:about=\"urn:C\"/>\
             </rdf:RDF>"
        );
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("bounded session");
        let prefixes = GraphParser::new(&source, None, XmlSourceEncoding::Utf8, true, &mut session)
            .expect("graph parser")
            .parse()
            .expect("RDF/XML graph")
            .source_prefixes;

        assert_eq!(
            prefixes,
            vec![
                ("e".to_owned(), "urn:second:".to_owned()),
                ("owl".to_owned(), OWL.to_owned()),
                ("rdf".to_owned(), RDF.to_owned()),
                ("xml".to_owned(), XML.to_owned()),
            ],
        );
    }

    #[test]
    fn retained_source_blank_labels_are_explicit_unique_and_skip_xml_literals() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:e:\">\
             <rdf:Description rdf:nodeID=\"lexical-z\">\
             <owl:sameAs rdf:nodeID=\"lexical-a\"/></rdf:Description>\
             <rdf:Description rdf:nodeID=\"lexical-a\"/>\
             <rdf:Description><owl:sameAs rdf:nodeID=\"generated-1\"/></rdf:Description>\
             <rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Literal\">\
             <e:value rdf:nodeID=\"literal-only\"/></e:p></rdf:Description>\
             </rdf:RDF>"
        );
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("bounded session");
        let parsed = GraphParser::new(&source, None, XmlSourceEncoding::Utf8, true, &mut session)
            .expect("graph parser")
            .parse()
            .expect("RDF/XML graph");

        assert_eq!(
            parsed.source_blank_labels,
            vec![
                "generated-1".to_owned(),
                "lexical-a".to_owned(),
                "lexical-z".to_owned(),
            ],
        );
        assert!(contains_edge(
            &parsed.triples,
            generated_resource(1),
            OWL_SAME_AS,
            blank_resource("generated-1").into(),
        ));

        let mut disabled_guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut disabled_session =
            Session::new(&mut disabled_guard, &limits, source.len()).expect("bounded session");
        let disabled = GraphParser::new(
            &source,
            None,
            XmlSourceEncoding::Utf8,
            false,
            &mut disabled_session,
        )
        .expect("graph parser")
        .parse()
        .expect("RDF/XML graph")
        .source_blank_labels;
        assert!(disabled.is_empty());
    }

    fn utf16_bytes(source: &str, little_endian: bool, bom: bool) -> Vec<u8> {
        let mut output = Vec::with_capacity(source.len().saturating_mul(2).saturating_add(2));
        if bom {
            output.extend_from_slice(if little_endian {
                &[0xff, 0xfe]
            } else {
                &[0xfe, 0xff]
            });
        }
        for code_unit in source.encode_utf16() {
            let bytes = if little_endian {
                code_unit.to_le_bytes()
            } else {
                code_unit.to_be_bytes()
            };
            output.extend_from_slice(&bytes);
        }
        output
    }

    fn limits_with(key: LimitKey, value: u64) -> Limits {
        let mut encoded = vec![0_u8; CONFIG_BYTES];
        encoded[..8].copy_from_slice(CONFIG_MAGIC);
        encoded[8..10].copy_from_slice(&CONFIG_SCHEMA.to_le_bytes());
        for index in 0..37 {
            let configured = if matches!(index, 13 | 14) {
                0
            } else {
                1_000_000_000_u64
            };
            encoded[16 + index * 8..24 + index * 8].copy_from_slice(&configured.to_le_bytes());
        }
        let offset = 16 + key as usize * 8;
        encoded[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
        Limits::decode(&encoded).expect("test limits")
    }

    fn iri_resource(value: &str) -> Resource {
        Resource::Iri(value.to_owned())
    }

    fn blank_resource(value: &str) -> Resource {
        Resource::Blank(value.to_owned())
    }

    fn generated_resource(value: u64) -> Resource {
        Resource::Blank(format!("{GENERATED_BLANK_PREFIX}{value}"))
    }

    fn contains_edge(graph: &[Triple], subject: Resource, predicate: &str, object: Term) -> bool {
        graph.contains(&Triple {
            subject,
            predicate: predicate.to_owned(),
            object,
        })
    }

    fn assert_statement_reification(
        graph: &[Triple],
        statement: &str,
        subject: Resource,
        predicate: &str,
        object: Term,
    ) {
        assert!(contains_edge(
            graph,
            subject.clone(),
            predicate,
            object.clone(),
        ));
        assert!(contains_edge(
            graph,
            iri_resource(statement),
            RDF_TYPE,
            iri_resource(RDF_STATEMENT).into(),
        ));
        assert!(contains_edge(
            graph,
            iri_resource(statement),
            RDF_SUBJECT,
            subject.into(),
        ));
        assert!(contains_edge(
            graph,
            iri_resource(statement),
            RDF_PREDICATE,
            iri_resource(predicate).into(),
        ));
        assert!(contains_edge(
            graph,
            iri_resource(statement),
            RDF_OBJECT,
            object,
        ));
    }

    #[test]
    fn parse_type_collection_emits_empty_single_and_multi_member_chains() {
        let empty = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Collection\"/></rdf:Description></rdf:RDF>"
        ))
        .expect("empty collection");
        assert_eq!(empty.len(), 1);
        assert!(contains_edge(
            &empty,
            iri_resource("urn:s"),
            "urn:e:p",
            Term::Iri(RDF_NIL.to_owned()),
        ));

        let single = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:a\"/></e:p></rdf:Description></rdf:RDF>"
        ))
        .expect("single collection");
        assert_eq!(single.len(), 3);
        assert!(contains_edge(
            &single,
            iri_resource("urn:s"),
            "urn:e:p",
            generated_resource(1).into(),
        ));
        assert!(contains_edge(
            &single,
            generated_resource(1),
            RDF_FIRST,
            iri_resource("urn:a").into(),
        ));
        assert!(contains_edge(
            &single,
            generated_resource(1),
            RDF_REST,
            Term::Iri(RDF_NIL.to_owned()),
        ));

        let multiple = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:a\"/><rdf:Description rdf:about=\"urn:b\"/></e:p></rdf:Description></rdf:RDF>"
        ))
        .expect("multi collection");
        assert_eq!(multiple.len(), 5);
        assert!(contains_edge(
            &multiple,
            generated_resource(1),
            RDF_REST,
            generated_resource(2).into(),
        ));
        assert!(contains_edge(
            &multiple,
            generated_resource(2),
            RDF_FIRST,
            iri_resource("urn:b").into(),
        ));
        assert!(contains_edge(
            &multiple,
            generated_resource(2),
            RDF_REST,
            Term::Iri(RDF_NIL.to_owned()),
        ));
    }

    #[test]
    fn parse_type_resource_streams_nested_properties_into_one_implicit_node() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Resource\"><e:q rdf:resource=\"urn:o\"/><e:label xml:lang=\"EN\">value</e:label></e:p></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("parseType Resource graph");

        assert_eq!(parsed.len(), 3);
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            "urn:e:p",
            generated_resource(1).into(),
        ));
        assert!(contains_edge(
            &parsed,
            generated_resource(1),
            "urn:e:q",
            iri_resource("urn:o").into(),
        ));
        assert!(contains_edge(
            &parsed,
            generated_resource(1),
            "urn:e:label",
            Term::Literal {
                lexical: "value".to_owned(),
                datatype: None,
                language: Some("en".to_owned()),
            },
        ));

        let empty = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Resource\"/></rdf:Description></rdf:RDF>"
        ))
        .expect("empty parseType Resource graph");
        assert_eq!(empty.len(), 1);
        assert!(contains_edge(
            &empty,
            iri_resource("urn:s"),
            "urn:e:p",
            generated_resource(1).into(),
        ));
    }

    #[test]
    fn parse_type_literal_streams_markup_free_xml_literal_content() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:lang=\"EN\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Literal\">a &amp; <![CDATA[b < c]]></e:p><e:q rdf:parseType=\"Literal\"></e:q></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("markup-free XML literals");

        assert_eq!(parsed.len(), 2);
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            "urn:e:p",
            Term::Literal {
                lexical: "a & b < c".to_owned(),
                datatype: Some(RDF_XML_LITERAL.to_owned()),
                language: None,
            },
        ));
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            "urn:e:q",
            Term::Literal {
                lexical: String::new(),
                datatype: Some(RDF_XML_LITERAL.to_owned()),
                language: None,
            },
        ));
    }

    #[test]
    fn parse_type_literal_serializes_nested_markup_like_element_tree() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xmlns:x=\"urn:x:\" xmlns:y=\"urn:y:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Literal\">root<x:box z=\"2\" a=\"1\" xml:base=\"../\"><y:item x:attr=\"&quot;\">hi &amp;</y:item>tail&lt;</x:box>between<x:empty/>suffix</e:p></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("nested XML literal");
        assert_eq!(parsed.len(), 1);
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            "urn:e:p",
            Term::Literal {
                lexical: "root<ns0:box xmlns:ns0=\"urn:x:\" xmlns:ns1=\"urn:y:\" z=\"2\" a=\"1\" xml:base=\"../\"><ns1:item ns0:attr=\"&quot;\">hi &amp;</ns1:item>tail&lt;</ns0:box>between<ns0:empty xmlns:ns0=\"urn:x:\"></ns0:empty>suffix".to_owned(),
                datatype: Some(RDF_XML_LITERAL.to_owned()),
                language: None,
            },
        ));
    }

    #[test]
    fn parse_type_other_uses_xml_literal_semantics() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xmlns:x=\"urn:x:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Other\" rdf:ID=\"statement\">root<x:value a=\"1\">text</x:value>tail</e:p></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("parseType Other XML literal");
        assert_eq!(parsed.len(), 5);
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#statement",
            iri_resource("urn:s"),
            "urn:e:p",
            Term::Literal {
                lexical: "root<ns0:value xmlns:ns0=\"urn:x:\" a=\"1\">text</ns0:value>tail"
                    .to_owned(),
                datatype: Some(RDF_XML_LITERAL.to_owned()),
                language: None,
            },
        );
    }

    #[test]
    fn property_element_ids_reify_literal_resource_and_child_statements() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:about=\"urn:s\"><e:text rdf:ID=\"literal\" xml:lang=\"EN\">value</e:text><e:typed rdf:ID=\"typed\" rdf:datatype=\"urn:datatype\">7</e:typed><e:resource rdf:ID=\"resource\" rdf:resource=\"urn:o\"/><e:child rdf:ID=\"child\"><rdf:Description rdf:about=\"urn:c\"/></e:child><e:described rdf:ID=\"described\" e:q=\"attribute\"/></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("reified property statements");

        assert_eq!(parsed.len(), 26);
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#literal",
            iri_resource("urn:s"),
            "urn:e:text",
            Term::Literal {
                lexical: "value".to_owned(),
                datatype: None,
                language: Some("en".to_owned()),
            },
        );
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#typed",
            iri_resource("urn:s"),
            "urn:e:typed",
            Term::Literal {
                lexical: "7".to_owned(),
                datatype: Some("urn:datatype".to_owned()),
                language: None,
            },
        );
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#resource",
            iri_resource("urn:s"),
            "urn:e:resource",
            iri_resource("urn:o").into(),
        );
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#child",
            iri_resource("urn:s"),
            "urn:e:child",
            iri_resource("urn:c").into(),
        );
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#described",
            iri_resource("urn:s"),
            "urn:e:described",
            generated_resource(1).into(),
        );
        assert!(contains_edge(
            &parsed,
            generated_resource(1),
            "urn:e:q",
            Term::Literal {
                lexical: "attribute".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));
    }

    #[test]
    fn property_element_ids_reify_every_parse_type_object() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xmlns:x=\"urn:x:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:about=\"urn:s\"><e:resource rdf:ID=\"resource\" rdf:parseType=\"Resource\"><e:q rdf:resource=\"urn:q\"/></e:resource><e:collection rdf:ID=\"collection\" rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:a\"/><rdf:Description rdf:about=\"urn:b\"/></e:collection><e:empty rdf:ID=\"empty\" rdf:parseType=\"Collection\"/><e:xml rdf:ID=\"xml\" rdf:parseType=\"Literal\"><x:box>value</x:box></e:xml></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("reified parseType statements");

        assert_eq!(parsed.len(), 25);
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#resource",
            iri_resource("urn:s"),
            "urn:e:resource",
            generated_resource(1).into(),
        );
        assert!(contains_edge(
            &parsed,
            generated_resource(1),
            "urn:e:q",
            iri_resource("urn:q").into(),
        ));
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#collection",
            iri_resource("urn:s"),
            "urn:e:collection",
            generated_resource(2).into(),
        );
        assert!(contains_edge(
            &parsed,
            generated_resource(2),
            RDF_REST,
            generated_resource(3).into(),
        ));
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#empty",
            iri_resource("urn:s"),
            "urn:e:empty",
            iri_resource(RDF_NIL).into(),
        );
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#xml",
            iri_resource("urn:s"),
            "urn:e:xml",
            Term::Literal {
                lexical: "<ns0:box xmlns:ns0=\"urn:x:\">value</ns0:box>".to_owned(),
                datatype: Some(RDF_XML_LITERAL.to_owned()),
                language: None,
            },
        );
    }

    #[test]
    fn property_element_ids_validate_values_limits_and_strict_mapping() {
        for value in ["", "1statement", "bad:name", "bad name"] {
            let invalid = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:ID=\"{value}\">value</e:p></rdf:Description></rdf:RDF>"
            );
            assert_eq!(graph(&invalid).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }

        let unicode = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:ID=\"déclaration\">value</e:p></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&unicode).expect("Unicode rdf:ID NCName");
        assert_statement_reification(
            &parsed,
            "http://example.test/doc#déclaration",
            iri_resource("urn:s"),
            "urn:e:p",
            Term::Literal {
                lexical: "value".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        );

        for duplicate in [
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:ID=\"statement\">one</e:p><e:q rdf:ID=\"statement\">two</e:q></rdf:Description></rdf:RDF>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:ID=\"statement\"><e:p rdf:ID=\"statement\">value</e:p></rdf:Description></rdf:RDF>"
            ),
        ] {
            assert_eq!(
                graph(&duplicate).unwrap_err().code,
                "NATIVE_RDFXML_SYNTAX"
            );
        }

        let distinct_bases = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/\"><rdf:Description rdf:about=\"urn:s\"><e:p xml:base=\"a/\" rdf:ID=\"statement\">one</e:p><e:q xml:base=\"b/\" rdf:ID=\"statement\">two</e:q></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&distinct_bases).expect("same rdf:ID under distinct bases");
        assert_eq!(parsed.len(), 10);

        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:ID=\"statement\">value</e:p></rdf:Description></rdf:RDF>"
        );
        let limits = limits_with(LimitKey::MaxTriples, 4);
        assert_eq!(
            graph_with_limits(&source, &limits).unwrap_err().code,
            "NATIVE_WIRE_LIMIT"
        );
        assert_eq!(
            mapped(source.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE"
        );
    }

    #[test]
    fn node_property_attributes_emit_resolved_types_and_language_literals() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:lang=\"FR\"><rdf:Description xml:base=\"http://example.test/base/\" rdf:about=\"subject\" rdf:type=\"../Class\" e:label=\"bonjour\"/></rdf:RDF>"
        );
        let parsed = graph(&source).expect("node property attributes");

        assert_eq!(parsed.len(), 2);
        assert!(contains_edge(
            &parsed,
            iri_resource("http://example.test/base/subject"),
            RDF_TYPE,
            iri_resource("http://example.test/Class").into(),
        ));
        assert!(contains_edge(
            &parsed,
            iri_resource("http://example.test/base/subject"),
            "urn:e:label",
            Term::Literal {
                lexical: "bonjour".to_owned(),
                datatype: None,
                language: Some("fr".to_owned()),
            },
        ));

        let reset = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:lang=\"fr\"><rdf:Description rdf:about=\"urn:s\" xml:lang=\"\" e:label=\"plain\"><e:text>element</e:text></rdf:Description></rdf:RDF>"
        ))
        .expect("empty language reset");
        assert!(contains_edge(
            &reset,
            iri_resource("urn:s"),
            "urn:e:label",
            Term::Literal {
                lexical: "plain".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));
        assert!(contains_edge(
            &reset,
            iri_resource("urn:s"),
            "urn:e:text",
            Term::Literal {
                lexical: "element".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));
    }

    #[test]
    fn reserved_xml_attributes_are_ignored_outside_xml_literals() {
        let baseline = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:label>value</e:label><e:empty/><e:xml rdf:parseType=\"Literal\"><e:mark xml:trace=\"literal\"/></e:xml></rdf:Description></rdf:RDF>"
        );
        let decorated = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xmlns:XmLmeta=\"urn:xml-metadata:\" xmlns:XmLrdf=\"{RDF}\" xmlns:XML=\"urn:xml-uppercase:\" xml:trace=\"root\" xmlroot=\"root\" XmLmeta:trace=\"root\" XML:trace=\"root\"><rdf:Description rdf:about=\"urn:s\" xml:trace=\"node\" XMLnode=\"node\" XmLmeta:trace=\"node\"><e:label xml:trace=\"property\" xmlnewthing=\"property\" XmLmeta:trace=\"property\">value</e:label><e:empty XmLrdf:resource=\"urn:wrong\"/><e:xml rdf:parseType=\"Literal\" xml:trace=\"outer\" XmlOuter=\"outer\" XmLmeta:trace=\"outer\"><e:mark xml:trace=\"literal\"/></e:xml></rdf:Description></rdf:RDF>"
        );
        let expected = graph(&baseline).expect("baseline XML attributes");
        let parsed = graph(&decorated).expect("unrecognized XML attributes");

        assert_eq!(parsed, expected);
        assert!(parsed.iter().any(|triple| {
            matches!(
                &triple.object,
                Term::Literal { lexical, .. }
                    if lexical.contains("xml:trace=\"literal\"")
            )
        }));

        for invalid in [
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" e:trace=\"root\"/>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Resource\" e:trace=\"property\"/></rdf:Description></rdf:RDF>"
            ),
        ] {
            assert_eq!(
                graph(&invalid).unwrap_err().code,
                "NATIVE_RDFXML_SYNTAX"
            );
        }
    }

    #[test]
    fn node_property_attributes_reject_reserved_syntax_terms() {
        for local in [
            "RDF",
            "parseType",
            "resource",
            "datatype",
            "Description",
            "li",
            "aboutEach",
            "aboutEachPrefix",
            "bagID",
        ] {
            let source = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\"><rdf:Description rdf:about=\"urn:s\" rdf:{local}=\"value\"/></rdf:RDF>"
            );
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
    }

    #[test]
    fn property_element_attributes_reject_reserved_syntax_terms() {
        for local in [
            "RDF",
            "about",
            "Description",
            "li",
            "aboutEach",
            "aboutEachPrefix",
            "bagID",
        ] {
            for object_attribute in ["", "rdf:resource=\"urn:o\" "] {
                let source = format!(
                    "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p {object_attribute}rdf:{local}=\"value\"/></rdf:Description></rdf:RDF>"
                );
                assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
            }
        }
    }

    #[test]
    fn legacy_unqualified_rdf_attributes_match_qualified_spelling() {
        fn source(prefix: &str) -> String {
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\" xmlns:owl=\"http://www.w3.org/2002/07/owl#\" xml:base=\"urn:legacy\"><owl:Class {prefix}about=\"urn:C\"><rdfs:subClassOf {prefix}resource=\"urn:D\"/><owl:equivalentClass><owl:Class><owl:intersectionOf {prefix}parseType=\"Collection\"><owl:Class {prefix}about=\"urn:D\"/><owl:Class {prefix}about=\"urn:E\"/></owl:intersectionOf></owl:Class></owl:equivalentClass></owl:Class><owl:Class {prefix}ID=\"F\"/><rdf:Description {prefix}about=\"urn:G\" {prefix}type=\"http://www.w3.org/2002/07/owl#Class\"/></rdf:RDF>"
            )
        }

        let qualified = graph(&source("rdf:")).expect("qualified RDF attributes");
        let legacy = graph(&source("")).expect("legacy unqualified RDF attributes");

        assert_eq!(legacy, qualified);
        assert_eq!(legacy.len(), 13);

        let reserved_alias = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"http://www.w3.org/2002/07/owl#\" xmlns:XmLrdf=\"{RDF}\"><owl:Class about=\"urn:C\" XmLrdf:about=\"urn:ignored\"/></rdf:RDF>"
        );
        assert_eq!(
            graph(&reserved_alias).expect("legacy RDF attribute with ignored reserved alias"),
            graph(&format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"http://www.w3.org/2002/07/owl#\"><owl:Class rdf:about=\"urn:C\"/></rdf:RDF>"
            ))
            .expect("qualified RDF attribute"),
        );
    }

    #[test]
    fn qualified_and_legacy_attribute_aliases_are_duplicates() {
        let elements = [
            "<owl:Class rdf:about=\"urn:C\" about=\"urn:D\"/>",
            "<owl:Class rdf:ID=\"C\" ID=\"D\"/>",
            "<rdf:Description rdf:about=\"urn:C\" rdf:type=\"http://www.w3.org/2002/07/owl#Class\" type=\"http://www.w3.org/2002/07/owl#Class\"/>",
            "<owl:Class rdf:about=\"urn:C\"><rdfs:subClassOf rdf:resource=\"urn:D\" resource=\"urn:E\"/></owl:Class>",
            "<owl:Class rdf:about=\"urn:C\"><owl:intersectionOf rdf:parseType=\"Collection\" parseType=\"Collection\"/></owl:Class>",
        ];
        for element in elements {
            let source = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\" xmlns:owl=\"http://www.w3.org/2002/07/owl#\" xml:base=\"urn:legacy\">{element}</rdf:RDF>"
            );
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
    }

    #[test]
    fn unqualified_attributes_remain_distinct_inside_xml_literals() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"http://www.w3.org/2002/07/owl#\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:Ontology rdf:about=\"urn:o\"><rdfs:comment rdf:parseType=\"Literal\"><mark about=\"legacy\" rdf:about=\"qualified\"/></rdfs:comment></owl:Ontology></rdf:RDF>"
        );
        let parsed = graph(&source).expect("unqualified XML literal attributes");

        assert!(parsed.iter().any(|triple| {
            matches!(
                &triple.object,
                Term::Literal { lexical, datatype: Some(datatype), .. }
                    if datatype == RDF_XML_LITERAL
                        && lexical.contains("about=\"legacy\"")
                        && lexical.contains("rdf:about=\"qualified\"")
            )
        }));
    }

    #[test]
    fn other_unqualified_attributes_remain_forbidden() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"http://www.w3.org/2002/07/owl#\"><owl:Class rdf:about=\"urn:C\" label=\"value\"/></rdf:RDF>"
        );
        assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
    }

    #[test]
    fn unicode_qnames_expand_in_elements_and_attributes() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:π=\"urn:unicode:\"><rdf:Description rdf:about=\"urn:s\" π:qualité=\"élevée\"><π:étiquette xml:lang=\"FR\">café</π:étiquette></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("Unicode XML qualified names");

        assert_eq!(parsed.len(), 2);
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            "urn:unicode:qualité",
            Term::Literal {
                lexical: "élevée".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            "urn:unicode:étiquette",
            Term::Literal {
                lexical: "café".to_owned(),
                datatype: None,
                language: Some("fr".to_owned()),
            },
        ));
    }

    #[test]
    fn xml_text_cdata_and_attributes_apply_xml_10_normalization() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:rdfs=\"{rdfs}\" xmlns:x=\"urn:x:\"><rdf:Description rdf:about=\"urn:s\" rdfs:label=\"a\tb\r\nc&#10;d&#9;e\"><rdfs:comment>one\r\n<![CDATA[two\rthree]]>&#13;four</rdfs:comment><rdfs:comment rdf:parseType=\"Literal\">one\r\n<x:a v=\"a\tb\r\nc&#10;d&#9;e\">two\rthree&#13;four</x:a>tail\r\n</rdfs:comment></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("normalized XML values");

        assert_eq!(parsed.len(), 3);
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            &format!("{rdfs}label"),
            Term::Literal {
                lexical: "a b c\nd\te".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            &format!("{rdfs}comment"),
            Term::Literal {
                lexical: "one\ntwo\nthree\rfour".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            &format!("{rdfs}comment"),
            Term::Literal {
                lexical: "one\n<ns0:a xmlns:ns0=\"urn:x:\" v=\"a b c&#10;d&#09;e\">two\nthree\rfour</ns0:a>tail\n".to_owned(),
                datatype: Some(RDF_XML_LITERAL.to_owned()),
                language: None,
            },
        ));
    }

    #[test]
    fn malformed_qnames_and_invalid_rdf_ids_fail_as_syntax() {
        for source in [
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:bad:name/></rdf:Description></rdf:RDF>"
            ),
            format!("<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e:bad=\"urn:e:\"/>"),
            format!("<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:1bad=\"urn:e:\"/>"),
            format!("<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><e:1node/></rdf:RDF>"),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description e:1property=\"value\"/></rdf:RDF>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:nodeID=\"\"/></rdf:Description></rdf:RDF>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:nodeID=\"bad:name\"/></rdf:Description></rdf:RDF>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xml:base=\"http://example.test/doc\"><rdf:Description rdf:ID=\"1node\"/></rdf:RDF>"
            ),
        ] {
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
    }

    #[test]
    fn forbidden_raw_xml_characters_and_cdata_close_text_fail_as_syntax() {
        for source in [
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p>bad\u{1}</e:p></rdf:Description></rdf:RDF>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p><![CDATA[bad\u{1}]]></e:p></rdf:Description></rdf:RDF>"
            ),
            format!("<rdf:RDF xmlns:rdf=\"{RDF}\"><!--bad\u{1}--></rdf:RDF>"),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p>bad]]></e:p></rdf:Description></rdf:RDF>"
            ),
        ] {
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
    }

    #[test]
    fn empty_property_attributes_describe_resolved_and_implicit_objects() {
        let resolved = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" xml:base=\"http://example.test/base/\" xml:lang=\"EN\"><rdf:Description rdf:about=\"subject\"><e:p rdf:resource=\"target\" rdf:type=\"../Class\" e:label=\"value\"/></rdf:Description></rdf:RDF>"
        ))
        .expect("empty property attributes with an IRI object");
        assert_eq!(resolved.len(), 3);
        assert!(contains_edge(
            &resolved,
            iri_resource("http://example.test/base/subject"),
            "urn:e:p",
            iri_resource("http://example.test/base/target").into(),
        ));
        assert!(contains_edge(
            &resolved,
            iri_resource("http://example.test/base/target"),
            RDF_TYPE,
            iri_resource("http://example.test/Class").into(),
        ));
        assert!(contains_edge(
            &resolved,
            iri_resource("http://example.test/base/target"),
            "urn:e:label",
            Term::Literal {
                lexical: "value".to_owned(),
                datatype: None,
                language: Some("en".to_owned()),
            },
        ));

        let implicit = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p e:q=\"value\"></e:p></rdf:Description></rdf:RDF>"
        ))
        .expect("empty property attributes with an implicit blank object");
        assert_eq!(implicit.len(), 2);
        assert!(contains_edge(
            &implicit,
            iri_resource("urn:s"),
            "urn:e:p",
            generated_resource(1).into(),
        ));
        assert!(contains_edge(
            &implicit,
            generated_resource(1),
            "urn:e:q",
            Term::Literal {
                lexical: "value".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));

        let named = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:nodeID=\"target\" e:q=\"value\"/></rdf:Description></rdf:RDF>"
        ))
        .expect("empty property attributes with a named blank object");
        assert!(contains_edge(
            &named,
            iri_resource("urn:s"),
            "urn:e:p",
            blank_resource("target").into(),
        ));
        assert!(contains_edge(
            &named,
            blank_resource("target"),
            "urn:e:q",
            Term::Literal {
                lexical: "value".to_owned(),
                datatype: Some(XSD_STRING.to_owned()),
                language: None,
            },
        ));
    }

    #[test]
    fn empty_property_attributes_fail_closed_for_unretained_forms() {
        for (source, expected) in [
            (
                format!(
                    "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:resource=\"urn:o\" e:q=\"value\"><rdf:Description/></e:p></rdf:Description></rdf:RDF>"
                ),
                "NATIVE_RDFXML_SYNTAX",
            ),
            (
                format!(
                    "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:resource=\"urn:o\" rdf:about=\"urn:invalid\"/></rdf:Description></rdf:RDF>"
                ),
                "NATIVE_RDFXML_SYNTAX",
            ),
            (
                format!(
                    "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:resource=\"urn:o\" rdf:RDF=\"invalid\"/></rdf:Description></rdf:RDF>"
                ),
                "NATIVE_RDFXML_SYNTAX",
            ),
        ] {
            assert_eq!(graph(&source).unwrap_err().code, expected);
        }
    }

    #[test]
    fn datatype_empty_properties_keep_literal_semantics_with_legacy_attributes() {
        let parsed = graph(&format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:datatype=\"urn:datatype\" e:q=\"ignored\"/></rdf:Description></rdf:RDF>"
        ))
        .expect("datatyped empty property with a legacy property attribute");

        assert_eq!(parsed.len(), 1);
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            "urn:e:p",
            Term::Literal {
                lexical: String::new(),
                datatype: Some("urn:datatype".to_owned()),
                language: None,
            },
        ));
    }

    #[test]
    fn rdf_li_expands_in_each_node_scope_in_document_order() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><rdf:li rdf:resource=\"urn:a\"/><e:p rdf:parseType=\"Resource\"><rdf:li rdf:resource=\"urn:b\"/><rdf:li rdf:resource=\"urn:c\"/></e:p><rdf:li rdf:resource=\"urn:d\"/></rdf:Description></rdf:RDF>"
        );
        let parsed = graph(&source).expect("rdf:li graph");

        assert_eq!(parsed.len(), 5);
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            &format!("{RDF}_1"),
            iri_resource("urn:a").into(),
        ));
        assert!(contains_edge(
            &parsed,
            iri_resource("urn:s"),
            &format!("{RDF}_2"),
            iri_resource("urn:d").into(),
        ));
        assert!(contains_edge(
            &parsed,
            generated_resource(1),
            &format!("{RDF}_1"),
            iri_resource("urn:b").into(),
        ));
        assert!(contains_edge(
            &parsed,
            generated_resource(1),
            &format!("{RDF}_2"),
            iri_resource("urn:c").into(),
        ));
    }

    #[test]
    fn rdf_element_grammar_rejects_reserved_roles_and_root_attributes() {
        for (source, expected) in [
            (
                format!(
                    "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\" e:ignored=\"value\"/>"
                ),
                "NATIVE_RDFXML_SYNTAX",
            ),
            (
                format!("<rdf:RDF xmlns:rdf=\"{RDF}\"><rdf:about/></rdf:RDF>"),
                "NATIVE_RDFXML_SYNTAX",
            ),
            (
                format!(
                    "<rdf:RDF xmlns:rdf=\"{RDF}\"><rdf:Description rdf:about=\"urn:s\"><rdf:Description rdf:resource=\"urn:o\"/></rdf:Description></rdf:RDF>"
                ),
                "NATIVE_RDFXML_SYNTAX",
            ),
            (
                format!(
                    "<rdf:RDF xmlns:rdf=\"{RDF}\"><rdf:Description rdf:about=\"urn:s\"><rdf:bagID rdf:resource=\"urn:o\"/></rdf:Description></rdf:RDF>"
                ),
                "NATIVE_RDFXML_SYNTAX",
            ),
        ] {
            assert_eq!(graph(&source).unwrap_err().code, expected);
        }
    }

    #[test]
    fn rdf_li_counter_overflow_fails_before_graph_mutation() {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut parser = GraphParser::new("", None, XmlSourceEncoding::Utf8, false, &mut session)
            .expect("parser");
        parser.frames.push(Frame {
            raw_name: "rdf:Description".to_owned(),
            namespace_start: parser.namespaces.len(),
            base: None,
            language: None,
            role: FrameRole::Node {
                subject: iri_resource("urn:s"),
                next_li: u64::MAX,
            },
        });

        assert_eq!(
            parser.next_li_property().unwrap_err().code,
            "NATIVE_WIRE_LIMIT"
        );
        assert!(parser.triples.is_empty());
        assert!(matches!(
            parser.frames.last().map(|frame| &frame.role),
            Some(FrameRole::Node {
                next_li: u64::MAX,
                ..
            })
        ));
    }

    #[test]
    fn parse_type_collection_rejects_conflicts_text_and_preflights_length() {
        for source in [
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Collection\" rdf:resource=\"urn:o\"/></rdf:Description></rdf:RDF>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Collection\">not-whitespace</e:p></rdf:Description></rdf:RDF>"
            ),
            format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Collection\" e:ignored=\"value\"/></rdf:Description></rdf:RDF>"
            ),
        ] {
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
        let limits = Limits::default();
        let maximum = limits.value(LimitKey::MaxRdfListLength);
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut parser = GraphParser::new("", None, XmlSourceEncoding::Utf8, false, &mut session)
            .expect("parser");
        parser.frames.push(Frame {
            raw_name: "e:p".to_owned(),
            namespace_start: parser.namespaces.len(),
            base: None,
            language: None,
            role: FrameRole::Collection {
                subject: iri_resource("urn:s"),
                predicate: "urn:e:p".to_owned(),
                head: None,
                tail: None,
                member_count: maximum,
                reification: None,
            },
        });
        assert_eq!(
            parser.check_collection_member_limit().unwrap_err().code,
            "NATIVE_WIRE_LIMIT"
        );
        assert_eq!(parser.blank_counter, 0);
        assert!(parser.triples.is_empty());
    }

    fn class_node(value: &str) -> Node {
        entity("class", iri(value.to_owned()).expect("IRI node")).expect("class node")
    }

    fn named_individual_node(value: &str) -> Node {
        entity(
            "named_individual",
            iri(value.to_owned()).expect("individual IRI"),
        )
        .expect("named individual")
    }

    fn individual_set_axiom(tag: u64, individuals: Vec<Node>) -> Node {
        Node::build(
            tag,
            vec![
                Field::Set(canonical_set(individuals, 2, None).expect("individual set")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("individual axiom")
    }

    fn boolean_node(tag: u64, values: &[&str]) -> Node {
        let operands = values.iter().map(|value| class_node(value)).collect();
        Node::build(
            tag,
            vec![Field::Set(
                canonical_set(operands, 2, Some(tag)).expect("boolean operands"),
            )],
        )
        .expect("boolean node")
    }

    #[test]
    fn boolean_class_expressions_map_in_axiom_class_positions() {
        let subclass_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:Class rdf:about=\"urn:A\"><rdfs:subClassOf><rdf:Description><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:B\"/><rdf:Description rdf:about=\"urn:C\"/></owl:intersectionOf></rdf:Description></rdfs:subClassOf></owl:Class></rdf:RDF>"
        );
        let subclass = mapped(subclass_source.as_bytes(), None).expect("boolean subclass");
        let expected_subclass = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(boolean_node(30, &["urn:B", "urn:C"])),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(
            subclass
                .axioms
                .iter()
                .any(|value| value == expected_subclass.as_bytes()),
            "subclass expression bytes must match the canonical model",
        );
        assert_eq!(
            subclass.mapping.total_triples,
            subclass.mapping.consumed_triples
        );

        let domain_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:ObjectProperty rdf:about=\"urn:p\"><rdfs:domain><rdf:Description><owl:unionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:unionOf></rdf:Description></rdfs:domain></owl:ObjectProperty></rdf:RDF>"
        );
        let domain = mapped(domain_source.as_bytes(), None).expect("boolean domain");
        let expected_domain = Node::build(
            74,
            vec![
                Field::Node(
                    entity(
                        "object_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("property node"),
                ),
                Field::Node(boolean_node(31, &["urn:A", "urn:B"])),
                Field::Set(Vec::new()),
            ],
        )
        .expect("domain node");
        assert!(
            domain
                .axioms
                .iter()
                .any(|value| value == expected_domain.as_bytes()),
            "domain expression bytes must match the canonical model",
        );
        assert_eq!(
            domain.mapping.total_triples,
            domain.mapping.consumed_triples
        );

        let assertion_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:about=\"urn:i\"><rdf:type><rdf:Description><owl:unionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:unionOf></rdf:Description></rdf:type></rdf:Description></rdf:RDF>"
        );
        let assertion = mapped(assertion_source.as_bytes(), None).expect("boolean assertion");
        let expected_assertion = Node::build(
            112,
            vec![
                Field::Node(boolean_node(31, &["urn:A", "urn:B"])),
                Field::Node(
                    entity(
                        "named_individual",
                        iri("urn:i".to_owned()).expect("individual IRI"),
                    )
                    .expect("individual node"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("assertion node");
        assert!(
            assertion
                .axioms
                .iter()
                .any(|value| value == expected_assertion.as_bytes()),
            "class assertion bytes must match the canonical model",
        );
        assert_eq!(
            assertion.mapping.total_triples,
            assertion.mapping.consumed_triples,
        );

        let equivalent_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:about=\"urn:A\"><owl:equivalentClass><rdf:Description><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:B\"/><rdf:Description rdf:about=\"urn:C\"/></owl:intersectionOf></rdf:Description></owl:equivalentClass></rdf:Description></rdf:RDF>"
        );
        let equivalent = mapped(equivalent_source.as_bytes(), None).expect("boolean equivalence");
        let expected_equivalent = Node::build(
            62,
            vec![
                Field::Set(
                    canonical_set(
                        vec![class_node("urn:A"), boolean_node(30, &["urn:B", "urn:C"])],
                        2,
                        None,
                    )
                    .expect("equivalent members"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("equivalent node");
        assert!(
            equivalent
                .axioms
                .iter()
                .any(|value| value == expected_equivalent.as_bytes()),
            "equivalent expression bytes must match the canonical model",
        );
        assert_eq!(
            equivalent.mapping.total_triples,
            equivalent.mapping.consumed_triples,
        );
    }

    #[test]
    fn owl1_named_class_constructor_axioms_map_to_equivalence() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:Complement\"><owl:complementOf rdf:resource=\"urn:A\"/></owl:Class><owl:Class rdf:about=\"urn:Union\"><owl:unionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:unionOf></owl:Class><owl:Class rdf:about=\"urn:Intersection\"><owl:intersectionOf rdf:resource=\"{RDF_NIL}\"/></owl:Class><owl:Class rdf:about=\"urn:Enum\"><owl:oneOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:i\"/></owl:oneOf></owl:Class></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("OWL 1 named class constructors");
        let complement = Node::build(32, vec![Field::Node(class_node("urn:A"))])
            .expect("compatibility complement");
        let enumeration = Node::build(33, vec![Field::Set(vec![named_individual_node("urn:i")])])
            .expect("compatibility enumeration");
        for (class, expression) in [
            ("urn:Complement", complement),
            ("urn:Union", boolean_node(31, &["urn:A", "urn:B"])),
            (
                "urn:Intersection",
                class_node("http://www.w3.org/2002/07/owl#Thing"),
            ),
            ("urn:Enum", enumeration),
        ] {
            let expected = Node::build(
                62,
                vec![
                    Field::Set(
                        canonical_set(vec![class_node(class), expression], 2, None)
                            .expect("compatibility equivalent classes"),
                    ),
                    Field::Set(Vec::new()),
                ],
            )
            .expect("compatibility axiom");
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let anonymous_enumeration = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:Enum\"><owl:oneOf rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"anonymous\"/></owl:oneOf></owl:Class></rdf:RDF>"
        );
        assert_eq!(
            mapped(anonymous_enumeration.as_bytes(), None)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
    }

    #[test]
    fn owl1_declarations_redundant_types_and_deprecation_map_exactly() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"><rdf:type rdf:resource=\"{RDFS_CLASS}\"/></owl:Class><owl:ObjectProperty rdf:about=\"urn:p\"><rdf:type rdf:resource=\"{RDF_PROPERTY}\"/></owl:ObjectProperty><owl:OntologyProperty rdf:about=\"urn:ap\"><rdf:type rdf:resource=\"{RDF_PROPERTY}\"/></owl:OntologyProperty><rdf:Description rdf:about=\"urn:inverse\"><rdf:type rdf:resource=\"{OWL_INVERSE_FUNCTIONAL_PROPERTY}\"/><rdf:type rdf:resource=\"{RDF_PROPERTY}\"/></rdf:Description><rdf:Description rdf:about=\"urn:symmetric\"><rdf:type rdf:resource=\"{OWL_SYMMETRIC_PROPERTY}\"/><rdf:type rdf:resource=\"{RDF_PROPERTY}\"/></rdf:Description><rdf:Description rdf:about=\"urn:transitive\"><rdf:type rdf:resource=\"{OWL_TRANSITIVE_PROPERTY}\"/><rdf:type rdf:resource=\"{RDF_PROPERTY}\"/></rdf:Description><owl:Class rdf:about=\"urn:Old\"><rdf:type rdf:resource=\"{OWL_DEPRECATED_CLASS}\"/></owl:Class></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("OWL 1 declarations");
        let declaration = |kind: &'static str, value: &str| {
            Node::build(
                60,
                vec![
                    Field::Node(
                        entity(kind, iri(value.to_owned()).expect("declaration IRI"))
                            .expect("declared entity"),
                    ),
                    Field::Set(Vec::new()),
                ],
            )
            .expect("declaration")
        };
        for expected in [
            declaration("class", "urn:C"),
            declaration("object_property", "urn:p"),
            declaration("annotation_property", "urn:ap"),
            declaration("object_property", "urn:inverse"),
            declaration("object_property", "urn:symmetric"),
            declaration("object_property", "urn:transitive"),
            declaration("class", "urn:Old"),
        ] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        for (tag, property) in [
            (77, "urn:inverse"),
            (80, "urn:symmetric"),
            (82, "urn:transitive"),
        ] {
            let expected = Node::build(
                tag,
                vec![
                    Field::Node(
                        entity(
                            "object_property",
                            iri(property.to_owned()).expect("property IRI"),
                        )
                        .expect("object property"),
                    ),
                    Field::Set(Vec::new()),
                ],
            )
            .expect("property characteristic");
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        let deprecated = Node::build(
            120,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri(OWL_DEPRECATED.to_owned()).expect("deprecated IRI"),
                    )
                    .expect("deprecated property"),
                ),
                Field::Node(iri("urn:Old".to_owned()).expect("deprecated subject")),
                Field::Node(
                    literal(
                        "true".to_owned(),
                        entity(
                            "datatype",
                            iri(XSD_BOOLEAN.to_owned()).expect("boolean IRI"),
                        )
                        .expect("boolean datatype"),
                        None,
                    )
                    .expect("deprecated value"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("deprecated annotation assertion");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == deprecated.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
        assert_eq!(document.mapping.total_triples, 14);

        for unsupported in [RDFS_CLASS, RDF_PROPERTY] {
            let source = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\"><rdf:Description rdf:about=\"urn:x\"><rdf:type rdf:resource=\"{unsupported}\"/></rdf:Description></rdf:RDF>"
            );
            assert_eq!(
                mapped(source.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }
        let wrong_kind = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:NamedIndividual rdf:about=\"urn:x\"><rdf:type rdf:resource=\"{RDF_PROPERTY}\"/></owl:NamedIndividual></rdf:RDF>"
        );
        assert_eq!(
            mapped(wrong_kind.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
    }

    #[test]
    fn owl1_empty_data_range_crosses_the_rdfxml_mapper() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\" xmlns:owl=\"{OWL}\"><owl:DatatypeProperty rdf:about=\"urn:d\"/><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:d\"/><owl:allValuesFrom><owl:DataRange><rdf:type rdf:resource=\"{RDFS_CLASS}\"/><owl:oneOf rdf:resource=\"{RDF_NIL}\"/></owl:DataRange></owl:allValuesFrom></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("OWL 1 empty data range");
        let complement = Node::build(
            23,
            vec![Field::Node(
                entity(
                    "datatype",
                    iri("http://www.w3.org/2000/01/rdf-schema#Literal".to_owned())
                        .expect("literal IRI"),
                )
                .expect("literal datatype"),
            )],
        )
        .expect("literal complement");
        let restriction = Node::build(
            42,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:d".to_owned()).expect("property IRI"),
                )
                .expect("data property")]),
                Field::Node(complement),
            ],
        )
        .expect("data all-values restriction");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("OWL 1 data-range subclass");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
    }

    #[test]
    fn complement_and_object_enumeration_map_in_axiom_class_positions() {
        let complement_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><rdf:Description><owl:complementOf rdf:resource=\"urn:B\"/></rdf:Description></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let complement = mapped(complement_source.as_bytes(), None).expect("object complement");
        let complement_expression =
            Node::build(32, vec![Field::Node(class_node("urn:B"))]).expect("complement node");
        let expected_subclass = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(complement_expression),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(complement
            .axioms
            .iter()
            .any(|value| value == expected_subclass.as_bytes()),);
        assert_eq!(
            complement.mapping.total_triples,
            complement.mapping.consumed_triples,
        );

        let one_of_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:about=\"urn:x\"><rdf:type><rdf:Description><owl:oneOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:i\"/><rdf:Description rdf:nodeID=\"anonymous\"/></owl:oneOf></rdf:Description></rdf:type></rdf:Description></rdf:RDF>"
        );
        let one_of = mapped(one_of_source.as_bytes(), None).expect("object enumeration");
        let individuals = canonical_set(
            vec![
                entity(
                    "named_individual",
                    iri("urn:i".to_owned()).expect("individual IRI"),
                )
                .expect("named individual"),
                crate::canonical::anonymous("anonymous").expect("anonymous individual"),
            ],
            1,
            None,
        )
        .expect("individual set");
        let enumeration = Node::build(33, vec![Field::Set(individuals)]).expect("one-of node");
        let expected_assertion = Node::build(
            112,
            vec![
                Field::Node(enumeration),
                Field::Node(
                    entity(
                        "named_individual",
                        iri("urn:x".to_owned()).expect("individual IRI"),
                    )
                    .expect("named individual"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("class assertion");
        assert!(one_of
            .axioms
            .iter()
            .any(|value| value == expected_assertion.as_bytes()),);
        assert_eq!(
            one_of.mapping.total_triples,
            one_of.mapping.consumed_triples
        );
    }

    #[test]
    fn detached_class_complement_requires_exact_expression_shape() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"/><owl:Class rdf:nodeID=\"complement\"><owl:complementOf rdf:resource=\"urn:C\"/></owl:Class></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("detached class complement");
        assert_eq!(document.axioms.len(), 1);
        assert_eq!(document.mapping.total_triples, 3);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"/><rdf:Description rdf:nodeID=\"complement\"><owl:complementOf rdf:resource=\"urn:C\"/></rdf:Description></rdf:RDF>"
        );
        let undeclared = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"complement\"><owl:complementOf rdf:resource=\"urn:undeclared\"/></owl:Class></rdf:RDF>"
        );
        let anonymous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"/><owl:Class rdf:nodeID=\"complement\"><owl:complementOf rdf:nodeID=\"anonymous\"/></owl:Class></rdf:RDF>"
        );
        let cyclic = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"/><owl:Class rdf:nodeID=\"left\"><owl:complementOf rdf:nodeID=\"right\"/></owl:Class><owl:Class rdf:nodeID=\"right\"><owl:complementOf rdf:nodeID=\"left\"/></owl:Class></rdf:RDF>"
        );
        for incomplete in [markerless, undeclared, anonymous, cyclic] {
            assert_eq!(
                mapped(incomplete.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        let ambiguous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"/><owl:Class rdf:about=\"urn:D\"/><owl:Class rdf:nodeID=\"complement\"><owl:complementOf rdf:resource=\"urn:C\"/><owl:complementOf rdf:resource=\"urn:D\"/></owl:Class></rdf:RDF>"
        );
        assert_eq!(
            mapped(ambiguous.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn detached_empty_class_boolean_requires_exact_expression_shape() {
        for predicate in [OWL_INTERSECTION_OF, OWL_UNION_OF] {
            let local_name = predicate
                .strip_prefix(OWL)
                .expect("class-boolean predicate uses the OWL namespace");
            let source = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"expression\"><owl:{local_name} rdf:resource=\"{RDF_NIL}\"/></owl:Class></rdf:RDF>"
            );
            let document = mapped(source.as_bytes(), None).expect("detached empty class boolean");
            assert!(document.axioms.is_empty());
            assert_eq!(document.mapping.total_triples, 2);
            assert_eq!(
                document.mapping.total_triples,
                document.mapping.consumed_triples,
            );
        }

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:nodeID=\"expression\"><owl:intersectionOf rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        let restriction = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"expression\"><rdf:type rdf:resource=\"{OWL_RESTRICTION}\"/><owl:intersectionOf rdf:resource=\"{RDF_NIL}\"/></owl:Class></rdf:RDF>"
        );
        let markerless_duplicate = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:nodeID=\"expression\"><owl:intersectionOf rdf:resource=\"{RDF_NIL}\"/><owl:intersectionOf rdf:resource=\"urn:not-a-list\"/></rdf:Description></rdf:RDF>"
        );
        for incomplete in [markerless, restriction, markerless_duplicate] {
            assert_eq!(
                mapped(incomplete.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        let conflict = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"expression\"><owl:intersectionOf rdf:resource=\"{RDF_NIL}\"/><owl:unionOf rdf:resource=\"{RDF_NIL}\"/></owl:Class></rdf:RDF>"
        );
        assert_eq!(
            mapped(conflict.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );

        let duplicate = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"expression\"><owl:intersectionOf rdf:resource=\"{RDF_NIL}\"/><owl:intersectionOf rdf:resource=\"urn:not-a-list\"/></owl:Class></rdf:RDF>"
        );
        assert_eq!(
            mapped(duplicate.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn detached_named_class_boolean_requires_established_named_operands() {
        for local_name in ["intersectionOf", "unionOf"] {
            let singleton = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:nodeID=\"expression\"><owl:{local_name} rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/></owl:{local_name}></owl:Class></rdf:RDF>"
            );
            let singleton =
                mapped(singleton.as_bytes(), None).expect("detached singleton class boolean");
            assert_eq!(singleton.axioms.len(), 1);
            assert_eq!(singleton.mapping.total_triples, 5);
            assert_eq!(
                singleton.mapping.total_triples,
                singleton.mapping.consumed_triples,
            );

            let binary = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:about=\"urn:B\"/><owl:Class rdf:nodeID=\"expression\"><owl:{local_name} rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:{local_name}></owl:Class></rdf:RDF>"
            );
            let binary = mapped(binary.as_bytes(), None).expect("detached binary class boolean");
            assert_eq!(binary.axioms.len(), 2);
            assert_eq!(binary.mapping.total_triples, 8);
            assert_eq!(
                binary.mapping.total_triples,
                binary.mapping.consumed_triples,
            );

            let named = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"><owl:{local_name} rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:{local_name}></owl:Class><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:about=\"urn:B\"/></rdf:RDF>"
            );
            let named = mapped(named.as_bytes(), None).expect("named binary class boolean");
            assert_eq!(named.axioms.len(), 4);
            assert_eq!(named.mapping.total_triples, 8);
            assert_eq!(named.mapping.total_triples, named.mapping.consumed_triples,);
        }

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><rdf:Description rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/></owl:intersectionOf></rdf:Description></rdf:RDF>"
        );
        let undeclared = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:undeclared\"/></owl:intersectionOf></owl:Class></rdf:RDF>"
        );
        let forked = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:about=\"urn:B\"/><owl:Class rdf:nodeID=\"expression\"><owl:intersectionOf rdf:nodeID=\"values\"/></owl:Class><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:A\"/><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        let cyclic = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:nodeID=\"expression\"><owl:intersectionOf rdf:nodeID=\"values\"/></owl:Class><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"values\"/></rdf:Description></rdf:RDF>"
        );
        let restriction = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:nodeID=\"expression\"><rdf:type rdf:resource=\"{OWL_RESTRICTION}\"/><owl:intersectionOf rdf:nodeID=\"values\"/></owl:Class><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        for incomplete in [markerless, undeclared, forked, cyclic, restriction] {
            assert_eq!(
                mapped(incomplete.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        let conflict = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/></owl:intersectionOf><owl:complementOf rdf:resource=\"urn:A\"/></owl:Class></rdf:RDF>"
        );
        let shared_tail = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:about=\"urn:B\"/><owl:Class rdf:nodeID=\"left-expression\"><owl:intersectionOf rdf:nodeID=\"left\"/></owl:Class><owl:Class rdf:nodeID=\"right-expression\"><owl:unionOf rdf:nodeID=\"right\"/></owl:Class><rdf:Description rdf:nodeID=\"left\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:nodeID=\"tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"tail\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        for unsupported in [conflict, shared_tail] {
            assert_eq!(
                mapped(unsupported.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }

        let duplicate = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"/><owl:Class rdf:nodeID=\"expression\"><owl:intersectionOf rdf:nodeID=\"left\"/><owl:intersectionOf rdf:nodeID=\"right\"/></owl:Class><rdf:Description rdf:nodeID=\"left\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(duplicate.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn detached_named_data_boolean_requires_established_named_operands() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        for local_name in ["intersectionOf", "unionOf"] {
            let binary = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:{local_name} rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:{local_name}></rdfs:Datatype></rdf:RDF>"
            );
            let binary = mapped(binary.as_bytes(), None).expect("detached binary data boolean");
            assert_eq!(binary.axioms.len(), 2);
            assert_eq!(binary.mapping.total_triples, 8);
            assert_eq!(
                binary.mapping.total_triples,
                binary.mapping.consumed_triples,
            );

            let duplicate_operand = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:{local_name} rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:A\"/></owl:{local_name}></rdfs:Datatype></rdf:RDF>"
            );
            let duplicate_operand =
                mapped(duplicate_operand.as_bytes(), None).expect("duplicate data operand");
            assert_eq!(duplicate_operand.axioms.len(), 1);
            assert_eq!(duplicate_operand.mapping.total_triples, 7);
            assert_eq!(
                duplicate_operand.mapping.total_triples,
                duplicate_operand.mapping.consumed_triples,
            );
        }

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdf:Description rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:intersectionOf></rdf:Description></rdf:RDF>"
        );
        let named = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:about=\"urn:expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:intersectionOf></rdfs:Datatype></rdf:RDF>"
        );
        let empty = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:resource=\"{RDF_NIL}\"/></rdfs:Datatype></rdf:RDF>"
        );
        let singleton = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/></owl:intersectionOf></rdfs:Datatype></rdf:RDF>"
        );
        let undeclared = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:undeclared\"/></owl:intersectionOf></rdfs:Datatype></rdf:RDF>"
        );
        let anonymous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:nodeID=\"anonymous\"/></owl:intersectionOf></rdfs:Datatype></rdf:RDF>"
        );
        let literal = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:nodeID=\"head\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"head\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"tail\"><rdf:first>literal</rdf:first><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        let forked = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:A\"/><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        let cyclic = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"values\"/></rdf:Description></rdf:RDF>"
        );
        for incomplete in [
            markerless, named, empty, singleton, undeclared, anonymous, literal, forked, cyclic,
        ] {
            assert_eq!(
                mapped(incomplete.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        let mixed_marker = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:nodeID=\"expression\"><rdf:type rdf:resource=\"{OWL}DataRange\"/><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:intersectionOf></rdfs:Datatype></rdf:RDF>"
        );
        let conflict = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:intersectionOf><owl:datatypeComplementOf rdf:resource=\"urn:A\"/></rdfs:Datatype></rdf:RDF>"
        );
        let shared_tail = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:nodeID=\"left-expression\"><owl:intersectionOf rdf:nodeID=\"left\"/></rdfs:Datatype><rdfs:Datatype rdf:nodeID=\"right-expression\"><owl:unionOf rdf:nodeID=\"right\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"left\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:nodeID=\"tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"tail\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        for unsupported in [mixed_marker, conflict, shared_tail] {
            assert_eq!(
                mapped(unsupported.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }

        let duplicate_constructor = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:A\"/><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:intersectionOf rdf:nodeID=\"left\"/><owl:intersectionOf rdf:nodeID=\"right\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"left\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"left-tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"left-tail\"><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"right-tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"right-tail\"><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(duplicate_constructor.as_bytes(), None)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn detached_datatype_restriction_requires_established_facets() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let xsd = "http://www.w3.org/2001/XMLSchema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive rdf:datatype=\"{xsd}integer\">1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("detached datatype restriction");
        assert_eq!(document.axioms.len(), 1);
        assert_eq!(document.mapping.total_triples, 7);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let duplicate_facet = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let duplicate_facet =
            mapped(duplicate_facet.as_bytes(), None).expect("repeated facet member");
        assert_eq!(duplicate_facet.axioms.len(), 1);
        assert_eq!(duplicate_facet.mapping.total_triples, 9);
        assert_eq!(
            duplicate_facet.mapping.total_triples,
            duplicate_facet.mapping.consumed_triples,
        );

        let builtin = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"{xsd}integer\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let builtin = mapped(builtin.as_bytes(), None).expect("built-in datatype restriction");
        assert!(builtin.axioms.is_empty());
        assert_eq!(builtin.mapping.total_triples, 6);
        assert_eq!(
            builtin.mapping.total_triples,
            builtin.mapping.consumed_triples,
        );

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdf:Description rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdf:Description><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let undeclared = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:undeclared\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let empty = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:resource=\"{RDF_NIL}\"/></rdfs:Datatype></rdf:RDF>"
        );
        let missing_literal = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive rdf:resource=\"urn:value\"/></rdf:Description></rdf:RDF>"
        );
        let forked = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:nodeID=\"left\"/><rdf:first rdf:nodeID=\"right\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"left\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description><rdf:Description rdf:nodeID=\"right\"><xsd:maxExclusive>10</xsd:maxExclusive></rdf:Description></rdf:RDF>"
        );
        let cyclic = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:nodeID=\"facet\"/><rdf:rest rdf:nodeID=\"values\"/></rdf:Description><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        for incomplete in [
            markerless,
            undeclared,
            empty,
            missing_literal,
            forked,
            cyclic,
        ] {
            assert_eq!(
                mapped(incomplete.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        for near_builtin in [
            format!("{xsd}duration"),
            format!("{xsd}anyType"),
            format!("{RDF}langString"),
            format!("{OWL}realNumber"),
            RDFS_DATATYPE.to_owned(),
        ] {
            let source = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"{near_builtin}\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
            );
            assert_eq!(
                mapped(source.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        let duplicate_base = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:about=\"urn:E\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:onDatatype rdf:resource=\"urn:E\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let duplicate_list = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:nodeID=\"left\"/><owl:withRestrictions rdf:nodeID=\"right\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"left\"><rdf:first rdf:nodeID=\"left-facet\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first rdf:nodeID=\"right-facet\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"left-facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description><rdf:Description rdf:nodeID=\"right-facet\"><xsd:maxExclusive>10</xsd:maxExclusive></rdf:Description></rdf:RDF>"
        );
        for cardinality in [duplicate_base, duplicate_list] {
            assert_eq!(
                mapped(cardinality.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_CARDINALITY",
            );
        }

        let mixed_marker = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><rdf:type rdf:resource=\"{OWL}DataRange\"/><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let conflict = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions><owl:datatypeComplementOf rdf:resource=\"urn:D\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let multiple_values = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:parseType=\"Collection\"><rdf:Description rdf:nodeID=\"facet\"/></owl:withRestrictions></rdfs:Datatype><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive>1</xsd:minInclusive><xsd:maxExclusive>10</xsd:maxExclusive></rdf:Description></rdf:RDF>"
        );
        let ambiguous_role = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:xsd=\"{xsd}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"expression\"><owl:onDatatype rdf:resource=\"urn:D\"/><owl:withRestrictions rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:nodeID=\"values\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/><xsd:minInclusive>1</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        for unsupported in [mixed_marker, conflict, multiple_values, ambiguous_role] {
            assert_eq!(
                mapped(unsupported.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }
    }

    #[test]
    fn detached_named_list_precheck_is_bounded_and_cancellable() {
        let graph = [
            ListTriple {
                subject: ListResource::Blank("head"),
                predicate: RDF_FIRST,
                object: ListTerm::Iri("urn:A"),
            },
            ListTriple {
                subject: ListResource::Blank("head"),
                predicate: RDF_REST,
                object: ListTerm::Iri(RDF_NIL),
            },
        ];
        let kinds = [KindRecord {
            iri: "urn:A",
            kind: "class",
        }];
        let datatype_kinds = [KindRecord {
            iri: "urn:A",
            kind: "datatype",
        }];

        let limits = Limits::default();
        let mut semantic_guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut semantic_session =
            Session::new(&mut semantic_guard, &limits, 0).expect("bounded session");
        assert!(established_named_list(
            &graph,
            ListTerm::Blank("head"),
            &kinds,
            "class",
            1,
            &mut semantic_session,
        )
        .expect("class singleton precheck"));
        assert!(!established_named_list(
            &graph,
            ListTerm::Blank("head"),
            &kinds,
            "datatype",
            1,
            &mut semantic_session,
        )
        .expect("entity-kind mismatch"));
        assert!(!established_named_list(
            &graph,
            ListTerm::Blank("head"),
            &datatype_kinds,
            "datatype",
            2,
            &mut semantic_session,
        )
        .expect("data singleton arity"));

        let temporary_limits = limits_with(LimitKey::MaxTemporaryBytes, 1);
        let mut temporary_guard = Guard::new(
            Cancellation::with_duration(None),
            temporary_limits.deadline,
            temporary_limits.cancellation_stride,
        );
        let mut temporary_session =
            Session::new(&mut temporary_guard, &temporary_limits, 0).expect("bounded session");
        assert_eq!(
            established_named_list(
                &graph,
                ListTerm::Blank("head"),
                &kinds,
                "class",
                1,
                &mut temporary_session,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT",
        );

        let work_limits = limits_with(LimitKey::MaxCanonicalWork, 1);
        let mut work_guard = Guard::new(
            Cancellation::with_duration(None),
            work_limits.deadline,
            work_limits.cancellation_stride,
        );
        let mut work_session =
            Session::new(&mut work_guard, &work_limits, 0).expect("bounded session");
        assert_eq!(
            established_named_list(
                &graph,
                ListTerm::Blank("head"),
                &kinds,
                "class",
                1,
                &mut work_session,
            )
            .unwrap_err()
            .code,
            "NATIVE_WIRE_LIMIT",
        );

        let limits = Limits::default();
        let mut cancelled_guard = Guard::new(
            Cancellation::with_duration(Some(Duration::ZERO)),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut cancelled_session =
            Session::new(&mut cancelled_guard, &limits, 0).expect("bounded session");
        assert_eq!(
            established_named_list(
                &graph,
                ListTerm::Blank("head"),
                &kinds,
                "class",
                1,
                &mut cancelled_session,
            )
            .unwrap_err()
            .code,
            "NATIVE_DEADLINE",
        );
    }

    #[test]
    fn detached_facet_list_precheck_is_bounded_and_cancellable() {
        let graph = [
            ListTriple {
                subject: ListResource::Blank("head"),
                predicate: RDF_FIRST,
                object: ListTerm::Blank("facet"),
            },
            ListTriple {
                subject: ListResource::Blank("head"),
                predicate: RDF_REST,
                object: ListTerm::Iri(RDF_NIL),
            },
            ListTriple {
                subject: ListResource::Blank("facet"),
                predicate: "urn:minInclusive",
                object: ListTerm::Literal("1"),
            },
        ];

        let limits = Limits::default();
        let mut semantic_guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut semantic_session =
            Session::new(&mut semantic_guard, &limits, 0).expect("bounded session");
        assert!(
            established_facet_list(&graph, ListTerm::Blank("head"), &mut semantic_session,)
                .expect("facet-list precheck")
        );

        let temporary_limits = limits_with(LimitKey::MaxTemporaryBytes, 1);
        let mut temporary_guard = Guard::new(
            Cancellation::with_duration(None),
            temporary_limits.deadline,
            temporary_limits.cancellation_stride,
        );
        let mut temporary_session =
            Session::new(&mut temporary_guard, &temporary_limits, 0).expect("bounded session");
        assert_eq!(
            established_facet_list(&graph, ListTerm::Blank("head"), &mut temporary_session,)
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT",
        );

        let work_limits = limits_with(LimitKey::MaxCanonicalWork, 1);
        let mut work_guard = Guard::new(
            Cancellation::with_duration(None),
            work_limits.deadline,
            work_limits.cancellation_stride,
        );
        let mut work_session =
            Session::new(&mut work_guard, &work_limits, 0).expect("bounded session");
        assert_eq!(
            established_facet_list(&graph, ListTerm::Blank("head"), &mut work_session)
                .unwrap_err()
                .code,
            "NATIVE_WIRE_LIMIT",
        );

        let limits = Limits::default();
        let mut cancelled_guard = Guard::new(
            Cancellation::with_duration(Some(Duration::ZERO)),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut cancelled_session =
            Session::new(&mut cancelled_guard, &limits, 0).expect("bounded session");
        assert_eq!(
            established_facet_list(&graph, ListTerm::Blank("head"), &mut cancelled_session)
                .unwrap_err()
                .code,
            "NATIVE_DEADLINE",
        );
    }

    #[test]
    fn detached_object_enumeration_requires_exact_expression_shape() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></owl:Class><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:i\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("detached object enumeration");
        assert!(document.axioms.is_empty());
        assert_eq!(document.mapping.total_triples, 4);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let empty = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"range\"><owl:oneOf rdf:resource=\"{RDF}nil\"/></owl:Class></rdf:RDF>"
        );
        let empty = mapped(empty.as_bytes(), None).expect("empty detached object enumeration");
        assert!(empty.axioms.is_empty());
        assert_eq!(empty.mapping.total_triples, 2);
        assert_eq!(empty.mapping.total_triples, empty.mapping.consumed_triples);

        let named_subject = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"><owl:oneOf rdf:nodeID=\"values\"/></owl:Class><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:i\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let named_subject =
            mapped(named_subject.as_bytes(), None).expect("named object enumeration");
        assert_eq!(named_subject.axioms.len(), 2);
        assert_eq!(
            named_subject.mapping.total_triples,
            named_subject.mapping.consumed_triples,
        );

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></rdf:Description><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:i\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(markerless.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );

        let literal = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></owl:Class><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let conflict = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/><owl:complementOf rdf:resource=\"urn:C\"/></owl:Class><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:i\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        for unsupported in [literal, conflict] {
            assert_eq!(
                mapped(unsupported.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }

        let ambiguous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"left\"/><owl:oneOf rdf:nodeID=\"right\"/></owl:Class><rdf:Description rdf:nodeID=\"left\"><rdf:first rdf:resource=\"urn:left\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first rdf:resource=\"urn:right\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(ambiguous.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn detached_datatype_complement_requires_exact_expression_shape() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"complement\"><owl:datatypeComplementOf rdf:resource=\"urn:D\"/></rdfs:Datatype></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("detached datatype complement");
        assert_eq!(document.axioms.len(), 1);
        assert_eq!(document.mapping.total_triples, 3);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdf:Description rdf:nodeID=\"complement\"><owl:datatypeComplementOf rdf:resource=\"urn:D\"/></rdf:Description></rdf:RDF>"
        );
        let named_subject = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:about=\"urn:complement\"><owl:datatypeComplementOf rdf:resource=\"urn:D\"/></rdfs:Datatype></rdf:RDF>"
        );
        let undeclared = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"complement\"><owl:datatypeComplementOf rdf:resource=\"urn:undeclared\"/></rdfs:Datatype></rdf:RDF>"
        );
        let anonymous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"complement\"><owl:datatypeComplementOf rdf:nodeID=\"anonymous\"/></rdfs:Datatype></rdf:RDF>"
        );
        let cyclic = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"left\"><owl:datatypeComplementOf rdf:nodeID=\"right\"/></rdfs:Datatype><rdfs:Datatype rdf:nodeID=\"right\"><owl:datatypeComplementOf rdf:nodeID=\"left\"/></rdfs:Datatype></rdf:RDF>"
        );
        for incomplete in [markerless, named_subject, undeclared, anonymous, cyclic] {
            assert_eq!(
                mapped(incomplete.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        let ambiguous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:C\"/><rdfs:Datatype rdf:about=\"urn:D\"/><rdfs:Datatype rdf:nodeID=\"complement\"><owl:datatypeComplementOf rdf:resource=\"urn:C\"/><owl:datatypeComplementOf rdf:resource=\"urn:D\"/></rdfs:Datatype></rdf:RDF>"
        );
        assert_eq!(
            mapped(ambiguous.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn detached_data_enumeration_requires_exact_expression_shape() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("detached data enumeration");
        assert!(document.axioms.is_empty());
        assert_eq!(document.mapping.total_triples, 4);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let markerless = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdf:Description rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></rdf:Description><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let named_subject = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:range\"><owl:oneOf rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        for incomplete in [markerless, named_subject] {
            assert_eq!(
                mapped(incomplete.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
        }

        let empty = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"range\"><owl:oneOf rdf:resource=\"{RDF}nil\"/></rdfs:Datatype></rdf:RDF>"
        );
        let nonliteral = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:value\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let conflicting_markers = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"range\"><rdf:type rdf:resource=\"{OWL}DataRange\"/><owl:oneOf rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        for unsupported in [empty, nonliteral, conflicting_markers] {
            assert_eq!(
                mapped(unsupported.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }

        let ambiguous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"left\"/><owl:oneOf rdf:nodeID=\"right\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"left\"><rdf:first>left</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first>right</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(ambiguous.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn detached_owl1_data_enumeration_requires_exact_expression_shape() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DataRange rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></owl:DataRange><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("detached OWL 1 data enumeration");
        assert!(document.axioms.is_empty());
        assert_eq!(document.mapping.total_triples, 4);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let empty = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DataRange rdf:nodeID=\"range\"><owl:oneOf rdf:resource=\"{RDF}nil\"/></owl:DataRange></rdf:RDF>"
        );
        let empty = mapped(empty.as_bytes(), None).expect("empty detached OWL 1 data enumeration");
        assert!(empty.axioms.is_empty());
        assert_eq!(empty.mapping.total_triples, 2);
        assert_eq!(empty.mapping.total_triples, empty.mapping.consumed_triples);

        let named_subject = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DataRange rdf:about=\"urn:range\"><owl:oneOf rdf:nodeID=\"values\"/></owl:DataRange><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(named_subject.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );

        let nonliteral = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DataRange rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"values\"/></owl:DataRange><rdf:Description rdf:nodeID=\"values\"><rdf:first rdf:resource=\"urn:value\"/><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        let other_constructor = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DataRange rdf:nodeID=\"range\"><owl:intersectionOf rdf:resource=\"{RDF}nil\"/></owl:DataRange></rdf:RDF>"
        );
        for unsupported in [nonliteral, other_constructor] {
            assert_eq!(
                mapped(unsupported.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_UNSUPPORTED",
            );
        }

        let conflicting_markers = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:nodeID=\"range\"><rdf:type rdf:resource=\"{OWL}DataRange\"/><owl:oneOf rdf:nodeID=\"values\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"values\"><rdf:first>one</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(conflicting_markers.as_bytes(), None)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );

        let ambiguous = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DataRange rdf:nodeID=\"range\"><owl:oneOf rdf:nodeID=\"left\"/><owl:oneOf rdf:nodeID=\"right\"/></owl:DataRange><rdf:Description rdf:nodeID=\"left\"><rdf:first>left</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description><rdf:Description rdf:nodeID=\"right\"><rdf:first>right</rdf:first><rdf:rest rdf:resource=\"{RDF}nil\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(ambiguous.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn named_object_quantified_restrictions_map_with_nested_fillers() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:someValuesFrom><rdf:Description><owl:unionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:B\"/><rdf:Description rdf:about=\"urn:C\"/></owl:unionOf></rdf:Description></owl:someValuesFrom></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("nested object restriction");
        let restriction = Node::build(
            34,
            vec![
                Field::Node(
                    entity(
                        "object_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("object property"),
                ),
                Field::Node(boolean_node(31, &["urn:B", "urn:C"])),
            ],
        )
        .expect("restriction node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()),);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
        let declared_data = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:DatatypeProperty rdf:about=\"urn:p\"/><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:allValuesFrom rdf:resource=\"urn:B\"/></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let declared_data =
            mapped(declared_data.as_bytes(), None).expect("quantified data restriction");
        let restriction = Node::build(
            42,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:p".to_owned()).expect("property IRI"),
                )
                .expect("data property")]),
                Field::Node(
                    entity("datatype", iri("urn:B".to_owned()).expect("datatype IRI"))
                        .expect("datatype"),
                ),
            ],
        )
        .expect("data restriction node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(declared_data
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            declared_data.mapping.total_triples,
            declared_data.mapping.consumed_triples,
        );
    }

    #[test]
    fn inverse_object_property_maps_inside_restriction() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty><rdf:Description><owl:inverseOf rdf:resource=\"urn:p\"/></rdf:Description></owl:onProperty><owl:someValuesFrom rdf:resource=\"urn:B\"/></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("inverse property restriction");
        let inverse = Node::build(
            10,
            vec![Field::Node(
                entity(
                    "object_property",
                    iri("urn:p".to_owned()).expect("property IRI"),
                )
                .expect("object property"),
            )],
        )
        .expect("inverse property");
        let restriction = Node::build(
            34,
            vec![Field::Node(inverse), Field::Node(class_node("urn:B"))],
        )
        .expect("object restriction");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
    }

    #[test]
    fn inverse_property_axioms_accept_inverse_object_expressions() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:ObjectProperty rdf:about=\"urn:p\"><owl:inverseOf><rdf:Description><owl:inverseOf rdf:resource=\"urn:q\"/></rdf:Description></owl:inverseOf></owl:ObjectProperty></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("inverse properties");
        let property = |value: &str| {
            entity(
                "object_property",
                iri(value.to_owned()).expect("property IRI"),
            )
            .expect("object property")
        };
        let mut first = property("urn:p");
        let mut second =
            Node::build(10, vec![Field::Node(property("urn:q"))]).expect("inverse expression");
        if second.as_bytes() < first.as_bytes() {
            std::mem::swap(&mut first, &mut second);
        }
        let expected = Node::build(
            73,
            vec![
                Field::Node(first),
                Field::Node(second),
                Field::Set(Vec::new()),
            ],
        )
        .expect("inverse properties axiom");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let malformed = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:ObjectProperty rdf:about=\"urn:p\"><owl:inverseOf><rdf:Description/></owl:inverseOf></owl:ObjectProperty></rdf:RDF>"
        );
        assert_eq!(
            mapped(malformed.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
        let unowned_expression = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><owl:inverseOf rdf:resource=\"urn:q\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(unowned_expression.as_bytes(), None)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
        let detached_expression = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:ObjectProperty rdf:about=\"urn:q\"/><rdf:Description rdf:nodeID=\"inverse\"><owl:inverseOf rdf:resource=\"urn:q\"/></rdf:Description></rdf:RDF>"
        );
        let detached =
            mapped(detached_expression.as_bytes(), None).expect("detached inverse expression");
        assert_eq!(detached.axioms.len(), 1);
        assert_eq!(detached.mapping.total_triples, 2);
        assert_eq!(
            detached.mapping.total_triples,
            detached.mapping.consumed_triples,
        );

        let anonymous_target = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:ObjectProperty rdf:about=\"urn:q\"/><rdf:Description rdf:nodeID=\"inverse\"><owl:inverseOf rdf:nodeID=\"anonymous\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(anonymous_target.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
        let ambiguous_expression = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:ObjectProperty rdf:about=\"urn:q\"/><owl:ObjectProperty rdf:about=\"urn:r\"/><rdf:Description rdf:nodeID=\"inverse\"><owl:inverseOf rdf:resource=\"urn:q\"/><owl:inverseOf rdf:resource=\"urn:r\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(ambiguous_expression.as_bytes(), None)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn inverse_properties_map_in_domain_range_and_disjoint_positions() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:e=\"urn:\"><rdf:Description rdf:nodeID=\"domain\"><owl:inverseOf rdf:resource=\"urn:p\"/><rdfs:domain rdf:resource=\"urn:C\"/></rdf:Description><rdf:Description rdf:nodeID=\"range\"><owl:inverseOf rdf:resource=\"urn:q\"/><rdfs:range rdf:resource=\"urn:D\"/></rdf:Description><rdf:Description rdf:nodeID=\"disjoint\"><owl:inverseOf rdf:resource=\"urn:r\"/><owl:propertyDisjointWith rdf:resource=\"urn:s\"/></rdf:Description><rdf:Description rdf:nodeID=\"sub\"><owl:inverseOf rdf:resource=\"urn:sub\"/><rdfs:subPropertyOf><rdf:Description><owl:inverseOf rdf:resource=\"urn:super\"/></rdf:Description></rdfs:subPropertyOf></rdf:Description><rdf:Description rdf:nodeID=\"functional\"><owl:inverseOf rdf:resource=\"urn:f\"/><rdf:type rdf:resource=\"{OWL}FunctionalProperty\"/></rdf:Description><owl:Axiom rdf:nodeID=\"domain-axiom\"><owl:annotatedSource rdf:nodeID=\"domain\"/><owl:annotatedProperty rdf:resource=\"{RDFS_DOMAIN}\"/><owl:annotatedTarget rdf:resource=\"urn:C\"/><e:note rdf:resource=\"urn:value\"/></owl:Axiom></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("inverse property positions");
        let property = |value: &str| {
            entity(
                "object_property",
                iri(value.to_owned()).expect("property IRI"),
            )
            .expect("object property")
        };
        let inverse = |value: &str| {
            Node::build(10, vec![Field::Node(property(value))]).expect("inverse property")
        };
        let annotation = Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:note".to_owned()).expect("annotation property IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri("urn:value".to_owned()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("domain annotation");
        let domain = Node::build(
            74,
            vec![
                Field::Node(inverse("urn:p")),
                Field::Node(class_node("urn:C")),
                Field::Set(vec![annotation]),
            ],
        )
        .expect("inverse domain");
        let range = Node::build(
            75,
            vec![
                Field::Node(inverse("urn:q")),
                Field::Node(class_node("urn:D")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("inverse range");
        let disjoint = Node::build(
            72,
            vec![
                Field::Set(
                    canonical_set(vec![inverse("urn:r"), property("urn:s")], 2, None)
                        .expect("disjoint properties"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("inverse disjoint properties");
        let sub_property = Node::build(
            70,
            vec![
                Field::Node(inverse("urn:sub")),
                Field::Node(inverse("urn:super")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("inverse sub-property axiom");
        let functional = Node::build(
            76,
            vec![Field::Node(inverse("urn:f")), Field::Set(Vec::new())],
        )
        .expect("functional inverse property");
        for expected in [domain, range, disjoint, sub_property, functional] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
    }

    #[test]
    fn structural_data_ranges_map_inside_restrictions() {
        let union_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:DatatypeProperty rdf:about=\"urn:p\"/><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:someValuesFrom><rdfs:Datatype><owl:unionOf rdf:parseType=\"Collection\"><rdfs:Datatype rdf:about=\"urn:B\"/><rdfs:Datatype rdf:about=\"urn:C\"/></owl:unionOf></rdfs:Datatype></owl:someValuesFrom></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let union = mapped(union_source.as_bytes(), None).expect("boolean data range");
        let ranges = canonical_set(
            vec![
                entity("datatype", iri("urn:B".to_owned()).expect("datatype IRI"))
                    .expect("datatype"),
                entity("datatype", iri("urn:C".to_owned()).expect("datatype IRI"))
                    .expect("datatype"),
            ],
            2,
            Some(22),
        )
        .expect("data ranges");
        let data_range = Node::build(22, vec![Field::Set(ranges)]).expect("data union");
        let restriction = Node::build(
            41,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:p".to_owned()).expect("property IRI"),
                )
                .expect("data property")]),
                Field::Node(data_range),
            ],
        )
        .expect("data restriction");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(union
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(union.mapping.total_triples, union.mapping.consumed_triples);

        let one_of_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:DatatypeProperty rdf:about=\"urn:p\"/><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:allValuesFrom rdf:nodeID=\"e\"/></owl:Restriction></rdfs:subClassOf></rdf:Description><rdfs:Datatype rdf:nodeID=\"e\"><owl:oneOf rdf:nodeID=\"h\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"h\"><rdf:first rdf:datatype=\"http://www.w3.org/2001/XMLSchema#integer\">007</rdf:first><rdf:rest rdf:nodeID=\"t\"/></rdf:Description><rdf:Description rdf:nodeID=\"t\"><rdf:first xml:lang=\"EN-gb\">colour</rdf:first><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        let one_of = mapped(one_of_source.as_bytes(), None).expect("data enumeration");
        let values = canonical_set(
            vec![
                literal(
                    "007".to_owned(),
                    entity(
                        "datatype",
                        iri("http://www.w3.org/2001/XMLSchema#integer".to_owned())
                            .expect("datatype IRI"),
                    )
                    .expect("datatype"),
                    None,
                )
                .expect("typed literal"),
                literal(
                    "colour".to_owned(),
                    entity(
                        "datatype",
                        iri("http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral".to_owned())
                            .expect("datatype IRI"),
                    )
                    .expect("datatype"),
                    Some("en-gb".to_owned()),
                )
                .expect("language literal"),
            ],
            1,
            None,
        )
        .expect("literal set");
        let data_range = Node::build(24, vec![Field::Set(values)]).expect("data enumeration");
        let restriction = Node::build(
            42,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:p".to_owned()).expect("property IRI"),
                )
                .expect("data property")]),
                Field::Node(data_range),
            ],
        )
        .expect("data restriction");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(one_of
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            one_of.mapping.total_triples,
            one_of.mapping.consumed_triples
        );

        let restriction_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\" xmlns:xsd=\"http://www.w3.org/2001/XMLSchema#\"><owl:DatatypeProperty rdf:about=\"urn:p\"/><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:someValuesFrom rdf:nodeID=\"e\"/></owl:Restriction></rdfs:subClassOf></rdf:Description><rdfs:Datatype rdf:nodeID=\"e\"><owl:onDatatype rdf:resource=\"urn:Datatype\"/><owl:withRestrictions rdf:nodeID=\"h\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"h\"><rdf:first rdf:nodeID=\"facet\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"facet\"><xsd:minInclusive rdf:datatype=\"http://www.w3.org/2001/XMLSchema#integer\">007</xsd:minInclusive></rdf:Description></rdf:RDF>"
        );
        let restriction_document =
            mapped(restriction_source.as_bytes(), None).expect("datatype restriction");
        let facet_value = literal(
            "007".to_owned(),
            entity(
                "datatype",
                iri("http://www.w3.org/2001/XMLSchema#integer".to_owned()).expect("datatype IRI"),
            )
            .expect("datatype"),
            None,
        )
        .expect("facet literal");
        let facet = Node::build(
            20,
            vec![
                Field::Node(
                    iri("http://www.w3.org/2001/XMLSchema#minInclusive".to_owned())
                        .expect("facet IRI"),
                ),
                Field::Node(facet_value),
            ],
        )
        .expect("facet restriction");
        let data_range = Node::build(
            25,
            vec![
                Field::Node(
                    entity(
                        "datatype",
                        iri("urn:Datatype".to_owned()).expect("datatype IRI"),
                    )
                    .expect("datatype"),
                ),
                Field::Set(vec![facet]),
            ],
        )
        .expect("datatype restriction");
        let restriction = Node::build(
            41,
            vec![
                Field::Sequence(vec![entity(
                    "data_property",
                    iri("urn:p".to_owned()).expect("property IRI"),
                )
                .expect("data property")]),
                Field::Node(data_range),
            ],
        )
        .expect("data restriction");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(restriction_document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            restriction_document.mapping.total_triples,
            restriction_document.mapping.consumed_triples,
        );
    }

    #[test]
    fn object_value_and_self_restrictions_map_exactly() {
        let has_value_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:hasValue rdf:resource=\"urn:i\"/></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let has_value =
            mapped(has_value_source.as_bytes(), None).expect("object value restriction");
        let restriction = Node::build(
            36,
            vec![
                Field::Node(
                    entity(
                        "object_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("object property"),
                ),
                Field::Node(
                    entity(
                        "named_individual",
                        iri("urn:i".to_owned()).expect("individual IRI"),
                    )
                    .expect("named individual"),
                ),
            ],
        )
        .expect("value restriction node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(has_value
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            has_value.mapping.total_triples,
            has_value.mapping.consumed_triples,
        );

        let has_self_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:hasSelf>TrUe</owl:hasSelf></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let has_self = mapped(has_self_source.as_bytes(), None).expect("self restriction");
        let restriction = Node::build(
            37,
            vec![Field::Node(
                entity(
                    "object_property",
                    iri("urn:p".to_owned()).expect("property IRI"),
                )
                .expect("object property"),
            )],
        )
        .expect("self restriction node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(has_self
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            has_self.mapping.total_triples,
            has_self.mapping.consumed_triples,
        );

        let invalid_self = has_self_source.replace(">TrUe<", ">false<");
        assert_eq!(
            mapped(invalid_self.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
    }

    #[test]
    fn data_value_restrictions_preserve_literal_identity() {
        let typed_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:hasValue rdf:datatype=\"http://www.w3.org/2001/XMLSchema#integer\">007</owl:hasValue></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let typed = mapped(typed_source.as_bytes(), None).expect("typed data value restriction");
        let value = literal(
            "007".to_owned(),
            entity(
                "datatype",
                iri("http://www.w3.org/2001/XMLSchema#integer".to_owned()).expect("datatype IRI"),
            )
            .expect("datatype"),
            None,
        )
        .expect("typed literal");
        let restriction = Node::build(
            43,
            vec![
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(value),
            ],
        )
        .expect("data value node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(typed
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(typed.mapping.total_triples, typed.mapping.consumed_triples);
        let inherited_language = typed_source.replacen("<rdf:RDF ", "<rdf:RDF xml:lang=\"fr\" ", 1);
        let inherited_language = mapped(inherited_language.as_bytes(), None)
            .expect("explicit datatype overrides inherited language");
        assert!(inherited_language
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));

        let language_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:hasValue xml:lang=\"EN-gb\">colour</owl:hasValue></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let language =
            mapped(language_source.as_bytes(), None).expect("language data value restriction");
        let value = literal(
            "colour".to_owned(),
            entity(
                "datatype",
                iri("http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral".to_owned())
                    .expect("datatype IRI"),
            )
            .expect("datatype"),
            Some("en-gb".to_owned()),
        )
        .expect("language literal");
        let restriction = Node::build(
            43,
            vec![
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(value),
            ],
        )
        .expect("data value node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(language
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            language.mapping.total_triples,
            language.mapping.consumed_triples,
        );

        let plain_source = typed_source
            .replace(
                "http://www.w3.org/2001/XMLSchema#integer",
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral",
            )
            .replace(">007<", ">legacy@<");
        let plain = mapped(plain_source.as_bytes(), None).expect("legacy plain literal");
        let value = literal(
            "legacy".to_owned(),
            entity(
                "datatype",
                iri("http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral".to_owned())
                    .expect("datatype IRI"),
            )
            .expect("datatype"),
            None,
        )
        .expect("plain literal");
        let restriction = Node::build(
            43,
            vec![
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(value),
            ],
        )
        .expect("data value node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(plain
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));

        let invalid_language = language_source.replace("EN-gb", "not_valid");
        assert_eq!(
            mapped(invalid_language.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
        let invalid_datatype = typed_source.replace(
            "http://www.w3.org/2001/XMLSchema#integer",
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString",
        );
        assert_eq!(
            mapped(invalid_datatype.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
    }

    #[test]
    fn declared_object_and_data_property_assertions_map_exactly() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:ObjectProperty rdf:about=\"urn:p\"/><owl:DatatypeProperty rdf:about=\"urn:d\"/><rdf:Description rdf:about=\"urn:s\"><e:p rdf:resource=\"urn:o\"/><e:d rdf:datatype=\"http://www.w3.org/2001/XMLSchema#integer\">007</e:d></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("property assertions");
        let object_assertion = Node::build(
            113,
            vec![
                Field::Node(
                    entity(
                        "object_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("object property"),
                ),
                Field::Node(
                    entity(
                        "named_individual",
                        iri("urn:s".to_owned()).expect("individual IRI"),
                    )
                    .expect("subject"),
                ),
                Field::Node(
                    entity(
                        "named_individual",
                        iri("urn:o".to_owned()).expect("individual IRI"),
                    )
                    .expect("object"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("object assertion");
        let data_assertion = Node::build(
            115,
            vec![
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:d".to_owned()).expect("property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(
                    entity(
                        "named_individual",
                        iri("urn:s".to_owned()).expect("individual IRI"),
                    )
                    .expect("subject"),
                ),
                Field::Node(
                    literal(
                        "007".to_owned(),
                        entity(
                            "datatype",
                            iri("http://www.w3.org/2001/XMLSchema#integer".to_owned())
                                .expect("datatype IRI"),
                        )
                        .expect("datatype"),
                        None,
                    )
                    .expect("literal"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("data assertion");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == object_assertion.as_bytes()));
        assert!(document
            .axioms
            .iter()
            .any(|value| value == data_assertion.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let blank_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:ObjectProperty rdf:about=\"urn:p\"/><rdf:Description rdf:nodeID=\"subject\"><e:p rdf:nodeID=\"object\"/></rdf:Description></rdf:RDF>"
        );
        let blank = mapped(blank_source.as_bytes(), None).expect("anonymous assertion");
        let expected = Node::build(
            113,
            vec![
                Field::Node(
                    entity(
                        "object_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("object property"),
                ),
                Field::Node(crate::canonical::anonymous("subject").expect("subject")),
                Field::Node(crate::canonical::anonymous("object").expect("object")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("anonymous assertion");
        assert!(blank
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));

        let wrong_kind = source.replace("<owl:DatatypeProperty rdf:about=\"urn:d\"/>", "");
        assert_eq!(
            mapped(wrong_kind.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );

        let annotation_overlap = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:AnnotationProperty rdf:about=\"urn:a\"/><owl:DatatypeProperty rdf:about=\"urn:a\"/><rdf:Description rdf:about=\"urn:s\"><e:a>note</e:a></rdf:Description></rdf:RDF>"
        );
        let annotation_overlap =
            mapped(annotation_overlap.as_bytes(), None).expect("annotation precedence");
        let annotation = Node::build(
            120,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:a".to_owned()).expect("property IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri("urn:s".to_owned()).expect("annotation subject")),
                Field::Node(
                    literal(
                        "note".to_owned(),
                        entity(
                            "datatype",
                            iri(XSD_STRING.to_owned()).expect("datatype IRI"),
                        )
                        .expect("datatype"),
                        None,
                    )
                    .expect("annotation value"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("annotation assertion");
        assert!(annotation_overlap
            .axioms
            .iter()
            .any(|value| value == annotation.as_bytes()));

        let structural_overlap = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DatatypeProperty rdf:about=\"{OWL}sameAs\"/><rdf:Description rdf:about=\"urn:s\"><owl:sameAs>not-an-assertion</owl:sameAs></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(structural_overlap.as_bytes(), None)
                .unwrap_err()
                .code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
    }

    #[test]
    fn negative_property_assertions_map_object_and_data_targets() {
        let xsd_integer = "http://www.w3.org/2001/XMLSchema#integer";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:NegativePropertyAssertion rdf:nodeID=\"object-axiom\"><owl:sourceIndividual rdf:resource=\"urn:s\"/><owl:assertionProperty><rdf:Description><owl:inverseOf rdf:resource=\"urn:p\"/></rdf:Description></owl:assertionProperty><owl:targetIndividual rdf:nodeID=\"target\"/></owl:NegativePropertyAssertion><owl:NegativePropertyAssertion rdf:nodeID=\"data-axiom\"><owl:sourceIndividual rdf:nodeID=\"source\"/><owl:assertionProperty rdf:resource=\"urn:d\"/><owl:targetValue rdf:datatype=\"{xsd_integer}\">007</owl:targetValue></owl:NegativePropertyAssertion></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("negative assertions");
        let property = |kind: &'static str, value: &str| {
            entity(kind, iri(value.to_owned()).expect("property IRI")).expect("property")
        };
        let inverse = Node::build(10, vec![Field::Node(property("object_property", "urn:p"))])
            .expect("inverse property");
        let object = Node::build(
            114,
            vec![
                Field::Node(inverse),
                Field::Node(named_individual_node("urn:s")),
                Field::Node(crate::canonical::anonymous("target").expect("anonymous individual")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("negative object assertion");
        let data = Node::build(
            116,
            vec![
                Field::Node(property("data_property", "urn:d")),
                Field::Node(crate::canonical::anonymous("source").expect("anonymous individual")),
                Field::Node(
                    literal(
                        "007".to_owned(),
                        entity(
                            "datatype",
                            iri(xsd_integer.to_owned()).expect("datatype IRI"),
                        )
                        .expect("datatype"),
                        None,
                    )
                    .expect("literal"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("negative data assertion");
        for expected in [object, data] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        for targets in [
            "",
            "<owl:targetIndividual rdf:resource=\"urn:t\"/><owl:targetValue>value</owl:targetValue>",
        ] {
            let invalid = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:NegativePropertyAssertion><owl:sourceIndividual rdf:resource=\"urn:s\"/><owl:assertionProperty rdf:resource=\"urn:p\"/>{targets}</owl:NegativePropertyAssertion></rdf:RDF>"
            );
            assert_eq!(
                mapped(invalid.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_CARDINALITY",
            );
        }

        let annotated = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:NegativePropertyAssertion rdf:nodeID=\"negative\"><owl:sourceIndividual rdf:resource=\"urn:s\"/><owl:assertionProperty rdf:resource=\"urn:p\"/><owl:targetIndividual rdf:resource=\"urn:t\"/><e:note rdf:resource=\"urn:annotation\"/></owl:NegativePropertyAssertion><owl:Annotation rdf:nodeID=\"nested\"><owl:annotatedSource rdf:nodeID=\"negative\"/><owl:annotatedProperty rdf:resource=\"urn:note\"/><owl:annotatedTarget rdf:resource=\"urn:annotation\"/><e:detail rdf:resource=\"urn:nested\"/></owl:Annotation></rdf:RDF>"
        );
        let annotated = mapped(annotated.as_bytes(), None).expect("annotated negative assertion");
        let annotation_property = |value: &str| {
            entity(
                "annotation_property",
                iri(value.to_owned()).expect("annotation property IRI"),
            )
            .expect("annotation property")
        };
        let detail = Node::build(
            5,
            vec![
                Field::Node(annotation_property("urn:detail")),
                Field::Node(iri("urn:nested".to_owned()).expect("nested value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("nested annotation");
        let note = Node::build(
            5,
            vec![
                Field::Node(annotation_property("urn:note")),
                Field::Node(iri("urn:annotation".to_owned()).expect("annotation value")),
                Field::Set(vec![detail]),
            ],
        )
        .expect("negative assertion annotation");
        let expected = Node::build(
            114,
            vec![
                Field::Node(property("object_property", "urn:p")),
                Field::Node(named_individual_node("urn:s")),
                Field::Node(named_individual_node("urn:t")),
                Field::Set(vec![note]),
            ],
        )
        .expect("annotated negative assertion");
        assert_eq!(annotated.axioms, [expected.as_bytes().to_vec()]);
        assert_eq!(
            annotated.mapping.total_triples,
            annotated.mapping.consumed_triples,
        );
    }

    #[test]
    fn same_and_different_individual_axioms_map_exactly() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:NamedIndividual rdf:about=\"urn:a\"><owl:sameAs rdf:resource=\"urn:b\"/><owl:differentFrom rdf:nodeID=\"other\"/></owl:NamedIndividual><rdf:Description rdf:about=\"urn:b\"><owl:sameAs rdf:nodeID=\"anonymous\"/></rdf:Description><rdf:Description rdf:about=\"urn:x\"><owl:sameAs rdf:resource=\"urn:y\"/></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("individual axioms");
        let same_connected = individual_set_axiom(
            110,
            vec![
                named_individual_node("urn:a"),
                named_individual_node("urn:b"),
                crate::canonical::anonymous("anonymous").expect("anonymous individual"),
            ],
        );
        let same_pair = individual_set_axiom(
            110,
            vec![
                named_individual_node("urn:x"),
                named_individual_node("urn:y"),
            ],
        );
        let different = individual_set_axiom(
            111,
            vec![
                named_individual_node("urn:a"),
                crate::canonical::anonymous("other").expect("anonymous individual"),
            ],
        );
        for expected in [same_connected, same_pair, different] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        for predicate in ["sameAs", "differentFrom"] {
            let literal = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:about=\"urn:a\"><owl:{predicate}>literal</owl:{predicate}></rdf:Description></rdf:RDF>"
            );
            assert_eq!(
                mapped(literal.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_INCOMPLETE",
            );
            let reflexive = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:about=\"urn:a\"><owl:{predicate} rdf:resource=\"urn:a\"/></rdf:Description></rdf:RDF>"
            );
            assert!(mapped(reflexive.as_bytes(), None).is_err());
        }
    }

    #[test]
    fn class_assertions_accept_anonymous_individuals_and_class_expressions() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:nodeID=\"named-class\"><rdf:type rdf:resource=\"urn:C\"/></rdf:Description><rdf:Description rdf:nodeID=\"expression-class\"><rdf:type><owl:Class><owl:unionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:unionOf></owl:Class></rdf:type></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("anonymous class assertions");
        let named = Node::build(
            112,
            vec![
                Field::Node(class_node("urn:C")),
                Field::Node(
                    crate::canonical::anonymous("named-class").expect("anonymous individual"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("named class assertion");
        let expression = Node::build(
            112,
            vec![
                Field::Node(boolean_node(31, &["urn:A", "urn:B"])),
                Field::Node(
                    crate::canonical::anonymous("expression-class").expect("anonymous individual"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("class-expression assertion");
        for expected in [named, expression] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let structural = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:nodeID=\"anonymous\"><rdf:type rdf:resource=\"{OWL}NamedIndividual\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(structural.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
    }

    #[test]
    fn component_axioms_merge_reified_edge_annotations() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:Class rdf:about=\"urn:A\"><owl:equivalentClass rdf:resource=\"urn:B\"/></owl:Class><owl:Class rdf:about=\"urn:B\"><owl:equivalentClass rdf:resource=\"urn:C\"/></owl:Class><owl:DatatypeProperty rdf:about=\"urn:d\"><owl:equivalentProperty rdf:resource=\"urn:e\"/></owl:DatatypeProperty><owl:DatatypeProperty rdf:about=\"urn:e\"/><rdf:Description rdf:about=\"urn:i\"><owl:sameAs rdf:resource=\"urn:j\"/></rdf:Description><rdf:Description rdf:about=\"urn:j\"><owl:sameAs rdf:resource=\"urn:k\"/></rdf:Description><owl:Axiom rdf:nodeID=\"class-one\"><owl:annotatedSource rdf:resource=\"urn:A\"/><owl:annotatedProperty rdf:resource=\"{OWL_EQUIVALENT_CLASS}\"/><owl:annotatedTarget rdf:resource=\"urn:B\"/><e:note rdf:resource=\"urn:class-one\"/></owl:Axiom><owl:Axiom rdf:nodeID=\"class-two\"><owl:annotatedSource rdf:resource=\"urn:B\"/><owl:annotatedProperty rdf:resource=\"{OWL_EQUIVALENT_CLASS}\"/><owl:annotatedTarget rdf:resource=\"urn:C\"/><e:note rdf:resource=\"urn:class-two\"/></owl:Axiom><owl:Axiom rdf:nodeID=\"property\"><owl:annotatedSource rdf:resource=\"urn:d\"/><owl:annotatedProperty rdf:resource=\"{OWL_EQUIVALENT_PROPERTY}\"/><owl:annotatedTarget rdf:resource=\"urn:e\"/><e:note rdf:resource=\"urn:property\"/></owl:Axiom><owl:Axiom rdf:nodeID=\"same-one\"><owl:annotatedSource rdf:resource=\"urn:i\"/><owl:annotatedProperty rdf:resource=\"{OWL_SAME_AS}\"/><owl:annotatedTarget rdf:resource=\"urn:j\"/><e:note rdf:resource=\"urn:same-one\"/></owl:Axiom><owl:Axiom rdf:nodeID=\"same-two\"><owl:annotatedSource rdf:resource=\"urn:j\"/><owl:annotatedProperty rdf:resource=\"{OWL_SAME_AS}\"/><owl:annotatedTarget rdf:resource=\"urn:k\"/><e:note rdf:resource=\"urn:same-two\"/></owl:Axiom></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("annotated components");
        let property = |kind: &'static str, value: &str| {
            entity(kind, iri(value.to_owned()).expect("entity IRI")).expect("entity")
        };
        let annotation = |value: &str| {
            Node::build(
                5,
                vec![
                    Field::Node(property("annotation_property", "urn:note")),
                    Field::Node(iri(value.to_owned()).expect("annotation value")),
                    Field::Set(Vec::new()),
                ],
            )
            .expect("edge annotation")
        };
        let equivalent_classes = Node::build(
            62,
            vec![
                Field::Set(
                    canonical_set(
                        vec![
                            class_node("urn:A"),
                            class_node("urn:B"),
                            class_node("urn:C"),
                        ],
                        2,
                        None,
                    )
                    .expect("equivalent classes"),
                ),
                Field::Set(
                    canonical_set(
                        vec![annotation("urn:class-one"), annotation("urn:class-two")],
                        0,
                        None,
                    )
                    .expect("class annotations"),
                ),
            ],
        )
        .expect("annotated equivalent classes");
        let equivalent_data = Node::build(
            91,
            vec![
                Field::Set(
                    canonical_set(
                        vec![
                            property("data_property", "urn:d"),
                            property("data_property", "urn:e"),
                        ],
                        2,
                        None,
                    )
                    .expect("equivalent data properties"),
                ),
                Field::Set(vec![annotation("urn:property")]),
            ],
        )
        .expect("annotated equivalent data properties");
        let same = Node::build(
            110,
            vec![
                Field::Set(
                    canonical_set(
                        vec![
                            named_individual_node("urn:i"),
                            named_individual_node("urn:j"),
                            named_individual_node("urn:k"),
                        ],
                        2,
                        None,
                    )
                    .expect("same individuals"),
                ),
                Field::Set(
                    canonical_set(
                        vec![annotation("urn:same-one"), annotation("urn:same-two")],
                        0,
                        None,
                    )
                    .expect("same annotations"),
                ),
            ],
        )
        .expect("annotated same individuals");
        for expected in [equivalent_classes, equivalent_data, same] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let inverse_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><rdf:Description rdf:nodeID=\"inverse\"><owl:inverseOf rdf:resource=\"urn:p\"/><owl:equivalentProperty rdf:resource=\"urn:q\"/></rdf:Description><rdf:Description rdf:about=\"urn:q\"><owl:equivalentProperty rdf:resource=\"urn:r\"/></rdf:Description><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:nodeID=\"inverse\"/><owl:annotatedProperty rdf:resource=\"{OWL_EQUIVALENT_PROPERTY}\"/><owl:annotatedTarget rdf:resource=\"urn:q\"/><e:note rdf:resource=\"urn:inverse\"/></owl:Axiom></rdf:RDF>"
        );
        let inverse_document =
            mapped(inverse_source.as_bytes(), None).expect("inverse equivalent properties");
        let inverse = Node::build(10, vec![Field::Node(property("object_property", "urn:p"))])
            .expect("inverse property");
        let expected = Node::build(
            71,
            vec![
                Field::Set(
                    canonical_set(
                        vec![
                            inverse,
                            property("object_property", "urn:q"),
                            property("object_property", "urn:r"),
                        ],
                        2,
                        None,
                    )
                    .expect("equivalent object properties"),
                ),
                Field::Set(vec![annotation("urn:inverse")]),
            ],
        )
        .expect("annotated inverse property component");
        assert_eq!(inverse_document.axioms, [expected.as_bytes().to_vec()]);
        assert_eq!(
            inverse_document.mapping.total_triples,
            inverse_document.mapping.consumed_triples,
        );
    }

    #[test]
    fn all_different_collection_maps_exactly_and_validates_cardinality() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><rdf:Description rdf:nodeID=\"axiom\"><rdf:type rdf:resource=\"{OWL}AllDifferent\"/><owl:distinctMembers rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:a\"/><rdf:Description rdf:nodeID=\"anonymous\"/><rdf:Description rdf:about=\"urn:b\"/></owl:distinctMembers><e:note rdf:resource=\"urn:value\"/></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("AllDifferent");
        let individuals = canonical_set(
            vec![
                named_individual_node("urn:a"),
                crate::canonical::anonymous("anonymous").expect("anonymous individual"),
                named_individual_node("urn:b"),
            ],
            2,
            None,
        )
        .expect("different individuals");
        let annotation = Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:note".to_owned()).expect("annotation property IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri("urn:value".to_owned()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("all-different annotation");
        let expected = Node::build(
            111,
            vec![Field::Set(individuals), Field::Set(vec![annotation])],
        )
        .expect("annotated all-different axiom");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
        let modern_source = source.replace("distinctMembers", "members");
        let modern = mapped(modern_source.as_bytes(), None).expect("OWL 2 AllDifferent members");
        assert_eq!(modern.axioms, document.axioms);
        assert_eq!(
            modern.mapping.total_triples,
            modern.mapping.consumed_triples
        );

        for members in [
            "",
            "<rdf:Description rdf:about=\"urn:a\"/>",
            "<rdf:Description rdf:about=\"urn:a\"/><rdf:Description rdf:about=\"urn:a\"/>",
        ] {
            let invalid = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description rdf:nodeID=\"axiom\"><rdf:type rdf:resource=\"{OWL}AllDifferent\"/><owl:distinctMembers rdf:parseType=\"Collection\">{members}</owl:distinctMembers></rdf:Description></rdf:RDF>"
            );
            assert!(mapped(invalid.as_bytes(), None).is_err());
        }

        let missing = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><rdf:type rdf:resource=\"{OWL}AllDifferent\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(missing.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
        let multiple = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><rdf:type rdf:resource=\"{OWL}AllDifferent\"/><owl:distinctMembers rdf:resource=\"{RDF_NIL}\"/><owl:distinctMembers rdf:nodeID=\"other-list\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(multiple.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
        let conflicting = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><rdf:type rdf:resource=\"{OWL}AllDifferent\"/><owl:members rdf:resource=\"{RDF_NIL}\"/><owl:distinctMembers rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(conflicting.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn all_disjoint_collections_map_classes_and_properties_exactly() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:DatatypeProperty rdf:about=\"urn:d\"/><owl:DatatypeProperty rdf:about=\"urn:e\"/><owl:ObjectProperty rdf:about=\"urn:p\"/><owl:ObjectProperty rdf:about=\"urn:q\"/><rdf:Description rdf:nodeID=\"classes\"><rdf:type rdf:resource=\"{OWL}AllDisjointClasses\"/><owl:members rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description><owl:unionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:B\"/><rdf:Description rdf:about=\"urn:C\"/></owl:unionOf></rdf:Description></owl:members><e:note rdf:resource=\"urn:value\"/></rdf:Description><rdf:Description rdf:nodeID=\"data\"><rdf:type rdf:resource=\"{OWL}AllDisjointProperties\"/><owl:members rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:d\"/><rdf:Description rdf:about=\"urn:e\"/></owl:members></rdf:Description><rdf:Description rdf:nodeID=\"objects\"><rdf:type rdf:resource=\"{OWL}AllDisjointProperties\"/><owl:members rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:p\"/><rdf:Description><owl:inverseOf rdf:resource=\"urn:q\"/></rdf:Description></owl:members></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("all-disjoint collections");
        let annotation = Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:note".to_owned()).expect("annotation property IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri("urn:value".to_owned()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("all-disjoint annotation");
        let disjoint_classes = Node::build(
            63,
            vec![
                Field::Set(
                    canonical_set(
                        vec![class_node("urn:A"), boolean_node(31, &["urn:B", "urn:C"])],
                        2,
                        None,
                    )
                    .expect("disjoint classes"),
                ),
                Field::Set(vec![annotation]),
            ],
        )
        .expect("disjoint classes axiom");
        let disjoint_data = Node::build(
            92,
            vec![
                Field::Set(
                    canonical_set(
                        vec![
                            entity(
                                "data_property",
                                iri("urn:d".to_owned()).expect("property IRI"),
                            )
                            .expect("data property"),
                            entity(
                                "data_property",
                                iri("urn:e".to_owned()).expect("property IRI"),
                            )
                            .expect("data property"),
                        ],
                        2,
                        None,
                    )
                    .expect("data properties"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("disjoint data properties");
        let inverse = Node::build(
            10,
            vec![Field::Node(
                entity(
                    "object_property",
                    iri("urn:q".to_owned()).expect("property IRI"),
                )
                .expect("object property"),
            )],
        )
        .expect("inverse property");
        let disjoint_objects = Node::build(
            72,
            vec![
                Field::Set(
                    canonical_set(
                        vec![
                            entity(
                                "object_property",
                                iri("urn:p".to_owned()).expect("property IRI"),
                            )
                            .expect("object property"),
                            inverse,
                        ],
                        2,
                        None,
                    )
                    .expect("object properties"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("disjoint object properties");
        for expected in [disjoint_classes, disjoint_data, disjoint_objects] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let duplicate_class = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><rdf:type rdf:resource=\"{OWL}AllDisjointClasses\"/><owl:members rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:A\"/></owl:members></rdf:Description></rdf:RDF>"
        );
        let duplicate = mapped(duplicate_class.as_bytes(), None).expect("duplicate class members");
        let expected_duplicate = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(class_node(OWL_NOTHING)),
                Field::Set(Vec::new()),
            ],
        )
        .expect("self-disjoint class");
        assert!(duplicate
            .axioms
            .iter()
            .any(|value| value == expected_duplicate.as_bytes()));

        for kind in [OWL_ALL_DISJOINT_CLASSES, OWL_ALL_DISJOINT_PROPERTIES] {
            let single = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><rdf:type rdf:resource=\"{kind}\"/><owl:members rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:only\"/></owl:members></rdf:Description></rdf:RDF>"
            );
            assert!(mapped(single.as_bytes(), None).is_err());
        }
    }

    #[test]
    fn object_property_chains_preserve_order_and_inverse_members() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:ObjectProperty rdf:about=\"urn:super\"><owl:propertyChainAxiom rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:p\"/><rdf:Description><owl:inverseOf rdf:resource=\"urn:q\"/></rdf:Description><rdf:Description rdf:about=\"urn:p\"/></owl:propertyChainAxiom></owl:ObjectProperty></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("property chain");
        let property = |value: &str| {
            entity(
                "object_property",
                iri(value.to_owned()).expect("property IRI"),
            )
            .expect("object property")
        };
        let inverse = Node::build(10, vec![Field::Node(property("urn:q"))])
            .expect("inverse property expression");
        let chain = Node::build(
            11,
            vec![Field::Sequence(vec![
                property("urn:p"),
                inverse,
                property("urn:p"),
            ])],
        )
        .expect("object property chain");
        let expected = Node::build(
            70,
            vec![
                Field::Node(chain),
                Field::Node(property("urn:super")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("sub-property axiom");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        for members in ["", "<rdf:Description rdf:about=\"urn:only\"/>"] {
            let short = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:ObjectProperty rdf:about=\"urn:super\"><owl:propertyChainAxiom rdf:parseType=\"Collection\">{members}</owl:propertyChainAxiom></owl:ObjectProperty></rdf:RDF>"
            );
            assert_eq!(
                mapped(short.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDF_MAPPING_CARDINALITY",
            );
        }

        let inverse_super = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><owl:inverseOf rdf:resource=\"urn:super\"/><owl:propertyChainAxiom rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:p\"/><rdf:Description rdf:about=\"urn:q\"/></owl:propertyChainAxiom></rdf:Description></rdf:RDF>"
        );
        let inverse_document = mapped(inverse_super.as_bytes(), None).expect("inverse chain super");
        let inverse_super = Node::build(10, vec![Field::Node(property("urn:super"))])
            .expect("inverse super-property expression");
        let inverse_chain = Node::build(
            11,
            vec![Field::Sequence(vec![property("urn:p"), property("urn:q")])],
        )
        .expect("inverse super-property chain");
        let inverse_expected = Node::build(
            70,
            vec![
                Field::Node(inverse_chain),
                Field::Node(inverse_super),
                Field::Set(Vec::new()),
            ],
        )
        .expect("inverse super-property axiom");
        assert!(inverse_document
            .axioms
            .iter()
            .any(|value| value == inverse_expected.as_bytes()));
        assert_eq!(
            inverse_document.mapping.total_triples,
            inverse_document.mapping.consumed_triples,
        );
    }

    #[test]
    fn has_key_splits_object_and_data_property_members() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DatatypeProperty rdf:about=\"urn:d\"/><rdf:Description><owl:intersectionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:intersectionOf><owl:hasKey rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:p\"/><rdf:Description><owl:inverseOf rdf:resource=\"urn:q\"/></rdf:Description><rdf:Description rdf:about=\"urn:d\"/></owl:hasKey></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("HasKey");
        let property = |kind: &'static str, value: &str| {
            entity(kind, iri(value.to_owned()).expect("property IRI")).expect("property")
        };
        let inverse = Node::build(10, vec![Field::Node(property("object_property", "urn:q"))])
            .expect("inverse property");
        let expected = Node::build(
            101,
            vec![
                Field::Node(boolean_node(30, &["urn:A", "urn:B"])),
                Field::Set(
                    canonical_set(vec![property("object_property", "urn:p"), inverse], 0, None)
                        .expect("object keys"),
                ),
                Field::Set(
                    canonical_set(vec![property("data_property", "urn:d")], 0, None)
                        .expect("data keys"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("HasKey axiom");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let duplicate_data = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:DatatypeProperty rdf:about=\"urn:d\"/><owl:Class rdf:about=\"urn:A\"><owl:hasKey rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:d\"/><rdf:Description rdf:about=\"urn:d\"/></owl:hasKey></owl:Class></rdf:RDF>"
        );
        let duplicate = mapped(duplicate_data.as_bytes(), None).expect("duplicate data key");
        let expected_duplicate = Node::build(
            101,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Set(Vec::new()),
                Field::Set(vec![property("data_property", "urn:d")]),
                Field::Set(Vec::new()),
            ],
        )
        .expect("deduplicated data key");
        assert!(duplicate
            .axioms
            .iter()
            .any(|value| value == expected_duplicate.as_bytes()));

        let empty = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:A\"><owl:hasKey rdf:parseType=\"Collection\"/></owl:Class></rdf:RDF>"
        );
        assert_eq!(
            mapped(empty.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
    }

    #[test]
    fn disjoint_union_maps_named_class_and_nested_members() {
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:Defined\"><owl:disjointUnionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description><owl:complementOf rdf:resource=\"urn:B\"/></rdf:Description></owl:disjointUnionOf></owl:Class></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("DisjointUnion");
        let complement =
            Node::build(32, vec![Field::Node(class_node("urn:B"))]).expect("class complement");
        let expected = Node::build(
            64,
            vec![
                Field::Node(class_node("urn:Defined")),
                Field::Set(
                    canonical_set(vec![class_node("urn:A"), complement], 2, None)
                        .expect("disjoint union members"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("DisjointUnion axiom");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        for members in [
            "",
            "<rdf:Description rdf:about=\"urn:A\"/>",
            "<rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:A\"/>",
        ] {
            let invalid = format!(
                "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:Defined\"><owl:disjointUnionOf rdf:parseType=\"Collection\">{members}</owl:disjointUnionOf></owl:Class></rdf:RDF>"
            );
            assert!(mapped(invalid.as_bytes(), None).is_err());
        }

        let blank_class = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><owl:disjointUnionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:disjointUnionOf></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(blank_class.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_TYPE",
        );
    }

    #[test]
    fn datatype_definitions_preserve_direction_and_structural_ranges() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:D\"><owl:equivalentClass><rdfs:Datatype><owl:unionOf rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:A\"/><rdf:Description rdf:about=\"urn:B\"/></owl:unionOf></rdfs:Datatype></owl:equivalentClass></rdfs:Datatype><rdfs:Datatype rdf:about=\"urn:E\"><owl:equivalentClass rdf:resource=\"urn:Base\"/></rdfs:Datatype></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("datatype definitions");
        let datatype = |value: &str| {
            entity("datatype", iri(value.to_owned()).expect("datatype IRI")).expect("datatype")
        };
        let union = Node::build(
            22,
            vec![Field::Set(
                canonical_set(vec![datatype("urn:A"), datatype("urn:B")], 2, Some(22))
                    .expect("data union operands"),
            )],
        )
        .expect("data union");
        let structural = Node::build(
            100,
            vec![
                Field::Node(datatype("urn:D")),
                Field::Node(union),
                Field::Set(Vec::new()),
            ],
        )
        .expect("structural datatype definition");
        let named = Node::build(
            100,
            vec![
                Field::Node(datatype("urn:E")),
                Field::Node(datatype("urn:Base")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("named datatype definition");
        for expected in [structural, named] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let reverse = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><owl:Class rdf:about=\"urn:C\"><owl:equivalentClass rdf:resource=\"urn:D\"/></owl:Class><rdfs:Datatype rdf:about=\"urn:D\"/></rdf:RDF>"
        );
        let reverse_document = mapped(reverse.as_bytes(), None).expect("reverse orientation");
        let reverse_axiom = Node::build(
            62,
            vec![
                Field::Set(
                    canonical_set(vec![class_node("urn:C"), class_node("urn:D")], 2, None)
                        .expect("equivalent classes"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("equivalent classes axiom");
        assert!(reverse_document
            .axioms
            .iter()
            .any(|value| value == reverse_axiom.as_bytes()));

        let literal = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdfs:Datatype rdf:about=\"urn:D\"><owl:equivalentClass>not-a-range</owl:equivalentClass></rdfs:Datatype></rdf:RDF>"
        );
        assert_eq!(
            mapped(literal.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
    }

    #[test]
    fn reified_annotations_attach_to_list_backed_axioms() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:e=\"urn:\"><owl:ObjectProperty rdf:about=\"urn:super\"><owl:propertyChainAxiom rdf:nodeID=\"chain\"/></owl:ObjectProperty><owl:DatatypeProperty rdf:about=\"urn:d\"/><owl:Class rdf:about=\"urn:Keyed\"><owl:hasKey rdf:nodeID=\"keys\"/></owl:Class><owl:Class rdf:about=\"urn:Defined\"><owl:disjointUnionOf rdf:nodeID=\"union\"/></owl:Class><rdfs:Datatype rdf:about=\"urn:D\"><owl:equivalentClass rdf:resource=\"urn:Base\"/></rdfs:Datatype><rdf:Description rdf:nodeID=\"chain\"><rdf:first rdf:resource=\"urn:p\"/><rdf:rest rdf:nodeID=\"chain-tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"chain-tail\"><rdf:first rdf:resource=\"urn:q\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"keys\"><rdf:first rdf:resource=\"urn:d\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><rdf:Description rdf:nodeID=\"union\"><rdf:first rdf:resource=\"urn:A\"/><rdf:rest rdf:nodeID=\"union-tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"union-tail\"><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description><owl:Axiom rdf:nodeID=\"chain-axiom\"><owl:annotatedSource rdf:resource=\"urn:super\"/><owl:annotatedProperty rdf:resource=\"{OWL_PROPERTY_CHAIN_AXIOM}\"/><owl:annotatedTarget rdf:nodeID=\"chain\"/><e:note rdf:resource=\"urn:chain-note\"/></owl:Axiom><owl:Axiom rdf:nodeID=\"key-axiom\"><owl:annotatedSource rdf:resource=\"urn:Keyed\"/><owl:annotatedProperty rdf:resource=\"{OWL_HAS_KEY}\"/><owl:annotatedTarget rdf:nodeID=\"keys\"/><e:note rdf:resource=\"urn:key-note\"/></owl:Axiom><owl:Axiom rdf:nodeID=\"union-axiom\"><owl:annotatedSource rdf:resource=\"urn:Defined\"/><owl:annotatedProperty rdf:resource=\"{OWL_DISJOINT_UNION_OF}\"/><owl:annotatedTarget rdf:nodeID=\"union\"/><e:note rdf:resource=\"urn:union-note\"/></owl:Axiom><owl:Axiom rdf:nodeID=\"datatype-axiom\"><owl:annotatedSource rdf:resource=\"urn:D\"/><owl:annotatedProperty rdf:resource=\"{OWL_EQUIVALENT_CLASS}\"/><owl:annotatedTarget rdf:resource=\"urn:Base\"/><e:note rdf:resource=\"urn:datatype-note\"/></owl:Axiom></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("annotated list-backed axioms");
        let property = |kind: &'static str, value: &str| {
            entity(kind, iri(value.to_owned()).expect("property IRI")).expect("property")
        };
        let annotation = |value: &str| {
            Node::build(
                5,
                vec![
                    Field::Node(property("annotation_property", "urn:note")),
                    Field::Node(iri(value.to_owned()).expect("annotation value")),
                    Field::Set(Vec::new()),
                ],
            )
            .expect("axiom annotation")
        };
        let chain = Node::build(
            11,
            vec![Field::Sequence(vec![
                property("object_property", "urn:p"),
                property("object_property", "urn:q"),
            ])],
        )
        .expect("property chain");
        let chain_axiom = Node::build(
            70,
            vec![
                Field::Node(chain),
                Field::Node(property("object_property", "urn:super")),
                Field::Set(vec![annotation("urn:chain-note")]),
            ],
        )
        .expect("annotated property chain");
        let key_axiom = Node::build(
            101,
            vec![
                Field::Node(class_node("urn:Keyed")),
                Field::Set(Vec::new()),
                Field::Set(vec![property("data_property", "urn:d")]),
                Field::Set(vec![annotation("urn:key-note")]),
            ],
        )
        .expect("annotated key");
        let union_axiom = Node::build(
            64,
            vec![
                Field::Node(class_node("urn:Defined")),
                Field::Set(
                    canonical_set(vec![class_node("urn:A"), class_node("urn:B")], 2, None)
                        .expect("disjoint union members"),
                ),
                Field::Set(vec![annotation("urn:union-note")]),
            ],
        )
        .expect("annotated disjoint union");
        let datatype_axiom = Node::build(
            100,
            vec![
                Field::Node(property("datatype", "urn:D")),
                Field::Node(property("datatype", "urn:Base")),
                Field::Set(vec![annotation("urn:datatype-note")]),
            ],
        )
        .expect("annotated datatype definition");
        for expected in [chain_axiom, key_axiom, union_axiom, datatype_axiom] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
    }

    #[test]
    fn structural_data_property_ranges_map_exactly() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><owl:DatatypeProperty rdf:about=\"urn:d\"><rdfs:range><rdfs:Datatype><owl:datatypeComplementOf rdf:resource=\"urn:Base\"/></rdfs:Datatype></rdfs:range></owl:DatatypeProperty></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("structural data range");
        let datatype = |value: &str| {
            entity("datatype", iri(value.to_owned()).expect("datatype IRI")).expect("datatype")
        };
        let complement =
            Node::build(23, vec![Field::Node(datatype("urn:Base"))]).expect("data complement");
        let expected = Node::build(
            94,
            vec![
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:d".to_owned()).expect("property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(complement),
                Field::Set(Vec::new()),
            ],
        )
        .expect("data property range");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let literal = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><owl:DatatypeProperty rdf:about=\"urn:d\"><rdfs:range>not-a-range</rdfs:range></owl:DatatypeProperty></rdf:RDF>"
        );
        assert_eq!(
            mapped(literal.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
    }

    #[test]
    fn object_cardinalities_map_defaults_qualifiers_and_wide_integers() {
        let unqualified_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:minCardinality>0002</owl:minCardinality></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let unqualified =
            mapped(unqualified_source.as_bytes(), None).expect("unqualified cardinality");
        let restriction = Node::build(
            38,
            vec![
                Field::Integer("2".to_owned()),
                Field::Node(
                    entity(
                        "object_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("object property"),
                ),
                Field::Node(class_node("http://www.w3.org/2002/07/owl#Thing")),
            ],
        )
        .expect("minimum cardinality node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(unqualified
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            unqualified.mapping.total_triples,
            unqualified.mapping.consumed_triples,
        );

        let qualified_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf><owl:Restriction><owl:onProperty rdf:resource=\"urn:p\"/><owl:qualifiedCardinality>18446744073709551616</owl:qualifiedCardinality><owl:onClass rdf:resource=\"urn:B\"/></owl:Restriction></rdfs:subClassOf></rdf:Description></rdf:RDF>"
        );
        let qualified = mapped(qualified_source.as_bytes(), None).expect("qualified cardinality");
        let restriction = Node::build(
            40,
            vec![
                Field::Integer("18446744073709551616".to_owned()),
                Field::Node(
                    entity(
                        "object_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("object property"),
                ),
                Field::Node(class_node("urn:B")),
            ],
        )
        .expect("exact cardinality node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(qualified
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            qualified.mapping.total_triples,
            qualified.mapping.consumed_triples,
        );

        let negative = unqualified_source.replace(">0002<", ">-1<");
        assert_eq!(
            mapped(negative.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
        let data_property = unqualified_source.replace(
            "<rdf:Description rdf:about=\"urn:A\">",
            "<owl:DatatypeProperty rdf:about=\"urn:p\"/><rdf:Description rdf:about=\"urn:A\">",
        );
        let data_property =
            mapped(data_property.as_bytes(), None).expect("unqualified data cardinality");
        let restriction = Node::build(
            44,
            vec![
                Field::Integer("2".to_owned()),
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:p".to_owned()).expect("property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(
                    entity(
                        "datatype",
                        iri("http://www.w3.org/2000/01/rdf-schema#Literal".to_owned())
                            .expect("datatype IRI"),
                    )
                    .expect("datatype"),
                ),
            ],
        )
        .expect("data cardinality node");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(restriction),
                Field::Set(Vec::new()),
            ],
        )
        .expect("subclass node");
        assert!(data_property
            .axioms
            .iter()
            .any(|value| value == expected.as_bytes()));
        assert_eq!(
            data_property.mapping.total_triples,
            data_property.mapping.consumed_triples,
        );
    }

    #[test]
    fn malformed_collection_reached_through_class_mapping_fails_closed() {
        let cyclic = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf rdf:nodeID=\"e\"/></rdf:Description><rdf:Description rdf:nodeID=\"e\"><owl:intersectionOf rdf:nodeID=\"h\"/></rdf:Description><rdf:Description rdf:nodeID=\"h\"><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:nodeID=\"h\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(cyclic.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );

        let forked = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf rdf:nodeID=\"e\"/></rdf:Description><rdf:Description rdf:nodeID=\"e\"><owl:unionOf rdf:nodeID=\"h\"/></rdf:Description><rdf:Description rdf:nodeID=\"h\"><rdf:first rdf:resource=\"urn:B\"/><rdf:first rdf:resource=\"urn:C\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(forked.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );

        let shared_tail = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf rdf:nodeID=\"e1\"/></rdf:Description><rdf:Description rdf:about=\"urn:D\"><rdfs:subClassOf rdf:nodeID=\"e2\"/></rdf:Description><rdf:Description rdf:nodeID=\"e1\"><owl:intersectionOf rdf:nodeID=\"h1\"/></rdf:Description><rdf:Description rdf:nodeID=\"e2\"><owl:unionOf rdf:nodeID=\"h2\"/></rdf:Description><rdf:Description rdf:nodeID=\"h1\"><rdf:first rdf:resource=\"urn:B\"/><rdf:rest rdf:nodeID=\"tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"h2\"><rdf:first rdf:resource=\"urn:C\"/><rdf:rest rdf:nodeID=\"tail\"/></rdf:Description><rdf:Description rdf:nodeID=\"tail\"><rdf:first rdf:resource=\"urn:E\"/><rdf:rest rdf:resource=\"{RDF_NIL}\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(shared_tail.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_UNSUPPORTED",
        );
    }

    #[test]
    fn header_import_and_declaration_mapping_is_deterministic() {
        let source = br#"<?xml version='1.0' encoding='UTF-8'?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:owl="http://www.w3.org/2002/07/owl#">
 <owl:Ontology rdf:about="urn:o">
  <owl:versionIRI rdf:resource="urn:v"/>
  <owl:imports rdf:resource="urn:z"/>
  <owl:imports rdf:resource="urn:a"/>
 </owl:Ontology>
 <owl:Class rdf:about="urn:C"/>
</rdf:RDF>"#;
        let document = mapped(source, None).expect("mapped RDF/XML");
        assert_eq!(document.ontology_iri.as_deref(), Some("urn:o"));
        assert_eq!(document.version_iri.as_deref(), Some("urn:v"));
        assert_eq!(document.imports, ["urn:a", "urn:z"]);
        assert_eq!(document.axioms.len(), 1);
        assert_eq!(document.mapping.total_triples, 5);
        assert_eq!(document.mapping.consumed_triples, 5);
    }

    #[test]
    fn ontology_and_annotation_axioms_map_exact_canonical_nodes() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let xsd_integer = "http://www.w3.org/2001/XMLSchema#integer";
        let plain_literal = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:e=\"urn:\"><owl:Ontology rdf:about=\"urn:o\"><rdfs:label xml:lang=\"EN\">Ontology</rdfs:label></owl:Ontology><owl:AnnotationProperty rdf:about=\"urn:sub\"><rdfs:subPropertyOf rdf:resource=\"urn:super\"/><rdfs:domain rdf:resource=\"urn:Domain\"/><rdfs:range rdf:resource=\"urn:Range\"/></owl:AnnotationProperty><owl:AnnotationProperty rdf:about=\"urn:super\"/><rdf:Description rdf:about=\"urn:subject\"><e:sub rdf:datatype=\"{xsd_integer}\">007</e:sub></rdf:Description><rdf:Description rdf:nodeID=\"anonymous-subject\"><rdfs:comment rdf:resource=\"urn:value\"/></rdf:Description></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("annotations");
        let annotation_property = |value: &str| {
            entity(
                "annotation_property",
                iri(value.to_owned()).expect("annotation property IRI"),
            )
            .expect("annotation property")
        };
        let ontology_annotation = Node::build(
            5,
            vec![
                Field::Node(annotation_property(&format!("{rdfs}label"))),
                Field::Node(
                    literal(
                        "Ontology".to_owned(),
                        entity(
                            "datatype",
                            iri(plain_literal.to_owned()).expect("datatype IRI"),
                        )
                        .expect("datatype"),
                        Some("en".to_owned()),
                    )
                    .expect("annotation literal"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("ontology annotation");
        assert_eq!(
            document.ontology_annotations,
            [ontology_annotation.as_bytes().to_vec()],
        );

        let literal_assertion = Node::build(
            120,
            vec![
                Field::Node(annotation_property("urn:sub")),
                Field::Node(iri("urn:subject".to_owned()).expect("annotation subject")),
                Field::Node(
                    literal(
                        "007".to_owned(),
                        entity(
                            "datatype",
                            iri(xsd_integer.to_owned()).expect("datatype IRI"),
                        )
                        .expect("datatype"),
                        None,
                    )
                    .expect("annotation literal"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("literal annotation assertion");
        let anonymous_assertion = Node::build(
            120,
            vec![
                Field::Node(annotation_property(&format!("{rdfs}comment"))),
                Field::Node(
                    crate::canonical::anonymous("anonymous-subject").expect("anonymous subject"),
                ),
                Field::Node(iri("urn:value".to_owned()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("anonymous annotation assertion");
        let sub_property = Node::build(
            121,
            vec![
                Field::Node(annotation_property("urn:sub")),
                Field::Node(annotation_property("urn:super")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("sub annotation property");
        let domain = Node::build(
            122,
            vec![
                Field::Node(annotation_property("urn:sub")),
                Field::Node(iri("urn:Domain".to_owned()).expect("domain IRI")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("annotation domain");
        let range = Node::build(
            123,
            vec![
                Field::Node(annotation_property("urn:sub")),
                Field::Node(iri("urn:Range".to_owned()).expect("range IRI")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("annotation range");
        for expected in [
            literal_assertion,
            anonymous_assertion,
            sub_property,
            domain,
            range,
        ] {
            assert!(document
                .axioms
                .iter()
                .any(|value| value == expected.as_bytes()));
        }
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let blank_domain = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><owl:AnnotationProperty rdf:about=\"urn:a\"><rdfs:domain rdf:nodeID=\"blank\"/></owl:AnnotationProperty></rdf:RDF>"
        );
        assert_eq!(
            mapped(blank_domain.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_TYPE",
        );
    }

    #[test]
    fn axiom_reification_attaches_exact_canonical_annotations() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let xsd_integer = "http://www.w3.org/2001/XMLSchema#integer";
        let plain_literal = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
        let subclass = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf rdf:resource=\"urn:B\"/></rdf:Description><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:resource=\"urn:A\"/><owl:annotatedProperty rdf:resource=\"{RDFS_SUB_CLASS_OF}\"/><owl:annotatedTarget rdf:resource=\"urn:B\"/><rdfs:comment xml:lang=\"EN\">note</rdfs:comment></owl:Axiom></rdf:RDF>"
        );
        let document = mapped(subclass.as_bytes(), None).expect("annotated subclass axiom");
        let comment = Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri(format!("{rdfs}comment")).expect("comment IRI"),
                    )
                    .expect("comment property"),
                ),
                Field::Node(
                    literal(
                        "note".to_owned(),
                        entity(
                            "datatype",
                            iri(plain_literal.to_owned()).expect("plain literal IRI"),
                        )
                        .expect("plain literal datatype"),
                        Some("en".to_owned()),
                    )
                    .expect("comment literal"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("comment annotation");
        let expected_subclass = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(class_node("urn:B")),
                Field::Set(vec![comment]),
            ],
        )
        .expect("annotated subclass");
        assert_eq!(document.axioms, [expected_subclass.as_bytes().to_vec()]);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let data_assertion = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:DatatypeProperty rdf:about=\"urn:d\"/><rdf:Description rdf:about=\"urn:s\"><e:d rdf:datatype=\"{xsd_integer}\">007</e:d></rdf:Description><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:resource=\"urn:s\"/><owl:annotatedProperty rdf:resource=\"urn:d\"/><owl:annotatedTarget rdf:datatype=\"{xsd_integer}\">007</owl:annotatedTarget><e:note rdf:resource=\"urn:value\"/></owl:Axiom></rdf:RDF>"
        );
        let document = mapped(data_assertion.as_bytes(), None).expect("annotated data assertion");
        let annotation = Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:note".to_owned()).expect("annotation property IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri("urn:value".to_owned()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("axiom annotation");
        let expected_assertion = Node::build(
            115,
            vec![
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:d".to_owned()).expect("data property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(named_individual_node("urn:s")),
                Field::Node(
                    literal(
                        "007".to_owned(),
                        entity(
                            "datatype",
                            iri(xsd_integer.to_owned()).expect("integer datatype IRI"),
                        )
                        .expect("integer datatype"),
                        None,
                    )
                    .expect("assertion literal"),
                ),
                Field::Set(vec![annotation]),
            ],
        )
        .expect("annotated data assertion");
        assert!(document
            .axioms
            .iter()
            .any(|value| value == expected_assertion.as_bytes()));
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
    }

    #[test]
    fn malformed_or_unclaimed_axiom_reification_fails_closed() {
        let missing_main = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:resource=\"urn:A\"/><owl:annotatedProperty rdf:resource=\"{RDFS_SUB_CLASS_OF}\"/><owl:annotatedTarget rdf:resource=\"urn:B\"/></owl:Axiom></rdf:RDF>"
        );
        assert_eq!(
            mapped(missing_main.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_AXIOM_REIFICATION",
        );

        let duplicate_source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf rdf:resource=\"urn:B\"/></rdf:Description><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:resource=\"urn:A\"/><owl:annotatedSource rdf:resource=\"urn:C\"/><owl:annotatedProperty rdf:resource=\"{RDFS_SUB_CLASS_OF}\"/><owl:annotatedTarget rdf:resource=\"urn:B\"/></owl:Axiom></rdf:RDF>"
        );
        assert_eq!(
            mapped(duplicate_source.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_AXIOM_REIFICATION",
        );

        let annotated_declaration = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:Class rdf:about=\"urn:C\"/><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:resource=\"urn:C\"/><owl:annotatedProperty rdf:resource=\"{RDF_TYPE}\"/><owl:annotatedTarget rdf:resource=\"{OWL}Class\"/><e:note rdf:resource=\"urn:value\"/></owl:Axiom></rdf:RDF>"
        );
        let document =
            mapped(annotated_declaration.as_bytes(), None).expect("annotated declaration");
        let annotation = Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:note".to_owned()).expect("annotation property IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri("urn:value".to_owned()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("declaration annotation");
        let expected = Node::build(
            60,
            vec![
                Field::Node(class_node("urn:C")),
                Field::Set(vec![annotation]),
            ],
        )
        .expect("annotated declaration");
        assert_eq!(document.axioms, [expected.as_bytes().to_vec()]);

        let nested = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Annotation rdf:nodeID=\"annotation\"><owl:annotatedSource rdf:resource=\"urn:s\"/><owl:annotatedProperty rdf:resource=\"urn:p\"/><owl:annotatedTarget rdf:resource=\"urn:o\"/></owl:Annotation></rdf:RDF>"
        );
        assert_eq!(
            mapped(nested.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_AXIOM_REIFICATION",
        );

        let cyclic = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:Annotation rdf:nodeID=\"a\"><owl:annotatedSource rdf:nodeID=\"b\"/><owl:annotatedProperty rdf:resource=\"urn:p\"/><owl:annotatedTarget rdf:resource=\"urn:o\"/><e:q rdf:resource=\"urn:x\"/></owl:Annotation><owl:Annotation rdf:nodeID=\"b\"><owl:annotatedSource rdf:nodeID=\"a\"/><owl:annotatedProperty rdf:resource=\"urn:q\"/><owl:annotatedTarget rdf:resource=\"urn:x\"/><e:p rdf:resource=\"urn:o\"/></owl:Annotation></rdf:RDF>"
        );
        assert_eq!(
            mapped(cyclic.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_AXIOM_REIFICATION",
        );
    }

    #[test]
    fn nested_annotation_reification_maps_recursively() {
        let rdfs = "http://www.w3.org/2000/01/rdf-schema#";
        let xsd_integer = "http://www.w3.org/2001/XMLSchema#integer";
        let plain_literal = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"{rdfs}\" xmlns:e=\"urn:\"><rdf:Description rdf:about=\"urn:A\"><rdfs:subClassOf rdf:resource=\"urn:B\"/></rdf:Description><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:resource=\"urn:A\"/><owl:annotatedProperty rdf:resource=\"{RDFS_SUB_CLASS_OF}\"/><owl:annotatedTarget rdf:resource=\"urn:B\"/><e:note rdf:resource=\"urn:value\"/></owl:Axiom><owl:Annotation rdf:nodeID=\"annotation\"><owl:annotatedSource rdf:nodeID=\"axiom\"/><owl:annotatedProperty rdf:resource=\"urn:note\"/><owl:annotatedTarget rdf:resource=\"urn:value\"/><e:provenance rdf:datatype=\"{xsd_integer}\">007</e:provenance></owl:Annotation><owl:Annotation rdf:nodeID=\"deep\"><owl:annotatedSource rdf:nodeID=\"annotation\"/><owl:annotatedProperty rdf:resource=\"urn:provenance\"/><owl:annotatedTarget rdf:datatype=\"{xsd_integer}\">007</owl:annotatedTarget><e:detail xml:lang=\"EN\">deep</e:detail></owl:Annotation></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("nested axiom annotations");
        let annotation_property = |value: &str| {
            entity(
                "annotation_property",
                iri(value.to_owned()).expect("annotation property IRI"),
            )
            .expect("annotation property")
        };
        let detail = Node::build(
            5,
            vec![
                Field::Node(annotation_property("urn:detail")),
                Field::Node(
                    literal(
                        "deep".to_owned(),
                        entity(
                            "datatype",
                            iri(plain_literal.to_owned()).expect("plain literal IRI"),
                        )
                        .expect("plain literal datatype"),
                        Some("en".to_owned()),
                    )
                    .expect("detail literal"),
                ),
                Field::Set(Vec::new()),
            ],
        )
        .expect("detail annotation");
        let provenance = Node::build(
            5,
            vec![
                Field::Node(annotation_property("urn:provenance")),
                Field::Node(
                    literal(
                        "007".to_owned(),
                        entity(
                            "datatype",
                            iri(xsd_integer.to_owned()).expect("integer datatype IRI"),
                        )
                        .expect("integer datatype"),
                        None,
                    )
                    .expect("provenance literal"),
                ),
                Field::Set(vec![detail]),
            ],
        )
        .expect("provenance annotation");
        let note = Node::build(
            5,
            vec![
                Field::Node(annotation_property("urn:note")),
                Field::Node(iri("urn:value".to_owned()).expect("note value")),
                Field::Set(vec![provenance]),
            ],
        )
        .expect("note annotation");
        let expected = Node::build(
            61,
            vec![
                Field::Node(class_node("urn:A")),
                Field::Node(class_node("urn:B")),
                Field::Set(vec![note]),
            ],
        )
        .expect("nested annotated subclass");
        assert_eq!(document.axioms, [expected.as_bytes().to_vec()]);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let ontology = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:e=\"urn:\"><owl:Ontology rdf:about=\"urn:o\"><e:note rdf:resource=\"urn:value\"/></owl:Ontology><owl:AnnotationProperty rdf:about=\"urn:note\"/><owl:Annotation rdf:nodeID=\"annotation\"><owl:annotatedSource rdf:resource=\"urn:o\"/><owl:annotatedProperty rdf:resource=\"urn:note\"/><owl:annotatedTarget rdf:resource=\"urn:value\"/><e:detail rdf:resource=\"urn:nested\"/></owl:Annotation></rdf:RDF>"
        );
        let document = mapped(ontology.as_bytes(), None).expect("nested ontology annotation");
        let nested = Node::build(
            5,
            vec![
                Field::Node(annotation_property("urn:detail")),
                Field::Node(iri("urn:nested".to_owned()).expect("nested value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("nested ontology annotation");
        let expected = Node::build(
            5,
            vec![
                Field::Node(annotation_property("urn:note")),
                Field::Node(iri("urn:value".to_owned()).expect("ontology value")),
                Field::Set(vec![nested]),
            ],
        )
        .expect("ontology annotation");
        assert_eq!(
            document.ontology_annotations,
            [expected.as_bytes().to_vec()],
        );
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );
    }

    #[test]
    fn swrl_rules_map_all_atom_kinds_to_extensions() {
        let xsd_integer = "http://www.w3.org/2001/XMLSchema#integer";
        let source = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:swrl=\"{SWRL}\" xmlns:e=\"urn:\"><swrl:Variable rdf:about=\"urn:x\"/><swrl:Variable rdf:about=\"urn:y\"/><swrl:Imp rdf:nodeID=\"rule\"><swrl:body rdf:parseType=\"Collection\"><swrl:ClassAtom><swrl:classPredicate rdf:resource=\"urn:C\"/><swrl:argument1 rdf:resource=\"urn:x\"/></swrl:ClassAtom><swrl:DataRangeAtom><swrl:dataRange rdf:resource=\"urn:D\"/><swrl:argument1 rdf:resource=\"urn:y\"/></swrl:DataRangeAtom><swrl:IndividualPropertyAtom><swrl:propertyPredicate><rdf:Description><owl:inverseOf rdf:resource=\"urn:p\"/></rdf:Description></swrl:propertyPredicate><swrl:argument1 rdf:resource=\"urn:x\"/><swrl:argument2 rdf:resource=\"urn:i\"/></swrl:IndividualPropertyAtom><swrl:DatavaluedPropertyAtom><swrl:propertyPredicate rdf:resource=\"urn:d\"/><swrl:argument1 rdf:resource=\"urn:x\"/><swrl:argument2 rdf:datatype=\"{xsd_integer}\">007</swrl:argument2></swrl:DatavaluedPropertyAtom><swrl:BuiltinAtom><swrl:builtin rdf:resource=\"urn:lessThan\"/><swrl:arguments rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:x\"/><rdf:Description rdf:about=\"urn:y\"/></swrl:arguments></swrl:BuiltinAtom><swrl:SameIndividualAtom><swrl:argument1 rdf:resource=\"urn:x\"/><swrl:argument2 rdf:resource=\"urn:i\"/></swrl:SameIndividualAtom></swrl:body><swrl:head rdf:parseType=\"Collection\"><swrl:DifferentIndividualsAtom><swrl:argument1 rdf:resource=\"urn:x\"/><swrl:argument2 rdf:resource=\"urn:j\"/></swrl:DifferentIndividualsAtom></swrl:head><e:note rdf:resource=\"urn:value\"/></swrl:Imp></rdf:RDF>"
        );
        let document = mapped(source.as_bytes(), None).expect("SWRL rule");
        let variable = |value: &str| {
            Node::build(
                140,
                vec![Field::Node(iri(value.to_owned()).expect("variable IRI"))],
            )
            .expect("SWRL variable")
        };
        let class_atom = Node::build(
            141,
            vec![
                Field::Node(class_node("urn:C")),
                Field::Node(variable("urn:x")),
            ],
        )
        .expect("class atom");
        let data_range_atom = Node::build(
            142,
            vec![
                Field::Node(
                    entity("datatype", iri("urn:D".to_owned()).expect("datatype IRI"))
                        .expect("datatype"),
                ),
                Field::Node(variable("urn:y")),
            ],
        )
        .expect("data-range atom");
        let inverse = Node::build(
            10,
            vec![Field::Node(
                entity(
                    "object_property",
                    iri("urn:p".to_owned()).expect("property IRI"),
                )
                .expect("object property"),
            )],
        )
        .expect("inverse property");
        let object_atom = Node::build(
            143,
            vec![
                Field::Node(inverse),
                Field::Node(variable("urn:x")),
                Field::Node(named_individual_node("urn:i")),
            ],
        )
        .expect("object-property atom");
        let data_atom = Node::build(
            144,
            vec![
                Field::Node(
                    entity(
                        "data_property",
                        iri("urn:d".to_owned()).expect("property IRI"),
                    )
                    .expect("data property"),
                ),
                Field::Node(variable("urn:x")),
                Field::Node(
                    literal(
                        "007".to_owned(),
                        entity(
                            "datatype",
                            iri(xsd_integer.to_owned()).expect("datatype IRI"),
                        )
                        .expect("datatype"),
                        None,
                    )
                    .expect("data argument"),
                ),
            ],
        )
        .expect("data-property atom");
        let builtin = Node::build(
            145,
            vec![
                Field::Node(iri("urn:lessThan".to_owned()).expect("builtin IRI")),
                Field::Sequence(vec![variable("urn:x"), variable("urn:y")]),
            ],
        )
        .expect("builtin atom");
        let same = Node::build(
            146,
            vec![
                Field::Node(variable("urn:x")),
                Field::Node(named_individual_node("urn:i")),
            ],
        )
        .expect("same-individual atom");
        let different = Node::build(
            147,
            vec![
                Field::Node(variable("urn:x")),
                Field::Node(named_individual_node("urn:j")),
            ],
        )
        .expect("different-individuals atom");
        let annotation = Node::build(
            5,
            vec![
                Field::Node(
                    entity(
                        "annotation_property",
                        iri("urn:note".to_owned()).expect("annotation property IRI"),
                    )
                    .expect("annotation property"),
                ),
                Field::Node(iri("urn:value".to_owned()).expect("annotation value")),
                Field::Set(Vec::new()),
            ],
        )
        .expect("rule annotation");
        let rule = Node::build(
            148,
            vec![
                Field::Set(
                    canonical_set(
                        vec![
                            class_atom,
                            data_range_atom,
                            object_atom,
                            data_atom,
                            builtin,
                            same,
                        ],
                        0,
                        None,
                    )
                    .expect("rule body"),
                ),
                Field::Set(vec![different]),
                Field::Set(vec![annotation]),
            ],
        )
        .expect("SWRL rule");
        assert!(document.axioms.is_empty());
        assert_eq!(document.extensions, [rule.as_bytes().to_vec()]);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples,
        );

        let missing_head = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:swrl=\"{SWRL}\"><swrl:Imp><swrl:body rdf:resource=\"{RDF_NIL}\"/></swrl:Imp></rdf:RDF>"
        );
        assert_eq!(
            mapped(missing_head.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_CARDINALITY",
        );
        let malformed_atom = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:swrl=\"{SWRL}\" xmlns:e=\"urn:\"><swrl:Imp><swrl:body rdf:parseType=\"Collection\"><swrl:ClassAtom><swrl:classPredicate rdf:resource=\"urn:C\"/><swrl:argument1 rdf:resource=\"urn:i\"/><e:extra rdf:resource=\"urn:x\"/></swrl:ClassAtom></swrl:body><swrl:head rdf:resource=\"{RDF_NIL}\"/></swrl:Imp></rdf:RDF>"
        );
        assert_eq!(
            mapped(malformed_atom.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
        let blank_variable = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:swrl=\"{SWRL}\"><swrl:Variable rdf:nodeID=\"x\"/><swrl:Imp><swrl:body rdf:parseType=\"Collection\"><swrl:ClassAtom><swrl:classPredicate rdf:resource=\"urn:C\"/><swrl:argument1 rdf:nodeID=\"x\"/></swrl:ClassAtom></swrl:body><swrl:head rdf:resource=\"{RDF_NIL}\"/></swrl:Imp></rdf:RDF>"
        );
        assert_eq!(
            mapped(blank_variable.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_TYPE",
        );
    }

    #[test]
    fn namespace_spelling_comments_and_description_type_have_equal_mapping() {
        let typed = br#"<r:RDF xmlns:r="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:o="http://www.w3.org/2002/07/owl#"><!--layout-->
 <r:Description r:about="urn:C"><r:type r:resource="http://www.w3.org/2002/07/owl#Class"/></r:Description>
</r:RDF>"#;
        let direct = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:owl="http://www.w3.org/2002/07/owl#"><owl:Class rdf:about="urn:C"/></rdf:RDF>"#;
        assert_eq!(
            mapped(typed, None).expect("description").axioms,
            mapped(direct, None).expect("typed node").axioms,
        );
    }

    #[test]
    fn xml_declarations_and_reserved_namespace_bindings_are_validated() {
        let valid = format!(
            "<?xml version = '1.0' encoding='UTF-8' standalone = \"yes\"?><rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:xml=\"{XML}\" xmlns=\"\"/>"
        );
        assert!(graph(&valid).expect("valid XML declaration").is_empty());

        for declaration in [
            "<?xml encoding='UTF-8'?>",
            "<?xml garbage?>",
            "<?xml version=''?>",
            "<?xml version='1.1'?>",
            "<?xml version='2.0'?>",
            "<?xml version='1.0' unknown='value'?>",
            "<?xml version='1.0' standalone='true'?>",
            "<?xml version='1.0' standalone='yes' encoding='UTF-8'?>",
        ] {
            let source = format!("{declaration}<rdf:RDF xmlns:rdf=\"{RDF}\"/>");
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
        let unsupported_encoding =
            format!("<?xml version='1.0' encoding='ISO-8859-1'?><rdf:RDF xmlns:rdf=\"{RDF}\"/>");
        assert_eq!(
            graph(&unsupported_encoding).unwrap_err().code,
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        );

        for declaration in [
            format!("xmlns:p=\"{XML}\""),
            format!("xmlns:p=\"{XMLNS}\""),
            format!("xmlns=\"{XML}\""),
            "xmlns:p=\"\"".to_owned(),
            "xmlns:xmlns=\"urn:invalid\"".to_owned(),
        ] {
            let source = format!("<rdf:RDF xmlns:rdf=\"{RDF}\" {declaration}/>");
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
    }

    #[test]
    fn utf16_sources_decode_with_bom_signature_and_declaration_parity() {
        let body = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:Class rdf:about=\"urn:C\"><rdfs:label>café 🙂</rdfs:label></owl:Class></rdf:RDF>"
        );
        let baseline = mapped(body.as_bytes(), None).expect("UTF-8 baseline");
        for (little_endian, bom) in [(true, true), (false, true), (true, false), (false, false)] {
            let encoded = utf16_bytes(&body, little_endian, bom);
            let observed = mapped(&encoded, None).expect("UTF-16 RDF/XML without declaration");
            assert_eq!(observed.axioms, baseline.axioms);
            assert_eq!(observed.mapping, baseline.mapping);
            assert_eq!(
                observed.decoded_codepoints,
                u64::try_from(body.chars().count()).expect("decoded length"),
            );
        }
        for (little_endian, bom, declaration_encoding) in [
            (true, true, "UTF-16"),
            (false, true, "UTF-16"),
            (true, false, "UTF-16LE"),
            (false, false, "UTF-16BE"),
        ] {
            let source = format!("<?xml version='1.0' encoding='{declaration_encoding}'?>{body}");
            let encoded = utf16_bytes(&source, little_endian, bom);
            let observed = mapped(&encoded, None).expect("UTF-16 RDF/XML");
            assert_eq!(observed.axioms, baseline.axioms);
            assert_eq!(observed.mapping, baseline.mapping);
            assert_eq!(
                observed.decoded_codepoints,
                u64::try_from(source.chars().count()).expect("decoded length"),
            );
        }

        let mismatch =
            format!("<?xml version='1.0' encoding='UTF-16BE'?><rdf:RDF xmlns:rdf=\"{RDF}\"/>");
        assert_eq!(
            mapped(&utf16_bytes(&mismatch, true, true), None)
                .unwrap_err()
                .code,
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        );
        let hostile = format!(
            "<!DOCTYPE rdf:RDF [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><rdf:RDF xmlns:rdf=\"{RDF}\"/>"
        );
        assert_eq!(
            mapped(&utf16_bytes(&hostile, true, true), None)
                .unwrap_err()
                .code,
            "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        );
        for invalid in [
            vec![0xff, 0xfe, b'<', 0x00, b'x'],
            vec![0xff, 0xfe, 0x00, 0xd8],
            vec![0xff, 0xfe, 0x00, 0x00],
        ] {
            assert_eq!(
                mapped(&invalid, None).unwrap_err().code,
                "NATIVE_FORMAT_ENCODING",
            );
        }
    }

    #[test]
    fn xml_comments_reject_double_hyphens_and_trailing_hyphens() {
        for source in [
            format!("<!--bad--comment--><rdf:RDF xmlns:rdf=\"{RDF}\"/>"),
            format!("<!--bad---><rdf:RDF xmlns:rdf=\"{RDF}\"/>"),
            format!("<rdf:RDF xmlns:rdf=\"{RDF}\"><!--bad---></rdf:RDF>"),
        ] {
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
    }

    #[test]
    fn processing_instructions_are_validated_and_map_to_no_rdf_events() {
        let with_instructions = format!(
            "<?audit before?><rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><?xml-stylesheet href=\"ignored.xsl\"?><owl:Class rdf:about=\"urn:C\"><rdfs:comment>a<?audit nested?>b</rdfs:comment></owl:Class></rdf:RDF><?audit after?>"
        );
        let without_instructions = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\" xmlns:rdfs=\"http://www.w3.org/2000/01/rdf-schema#\"><owl:Class rdf:about=\"urn:C\"><rdfs:comment>ab</rdfs:comment></owl:Class></rdf:RDF>"
        );
        assert_eq!(
            mapped(with_instructions.as_bytes(), None)
                .expect("RDF/XML with processing instructions")
                .axioms,
            mapped(without_instructions.as_bytes(), None)
                .expect("RDF/XML without processing instructions")
                .axioms,
        );

        for malformed in [
            "<??>",
            "<?1target?>",
            "<?target/data?>",
            "<?a:b?>",
            "<?XML version='1.0'?>",
            " <?xml version='1.0'?><rdf:RDF/>",
        ] {
            assert_eq!(graph(malformed).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
    }

    #[test]
    fn hostile_xml_is_rejected_before_publication() {
        let cases: &[(&[u8], &str)] = &[
            (br#"<!DOCTYPE rdf:RDF [<!ENTITY x SYSTEM 'file:///etc/passwd'>]><rdf:RDF/>"#, "NATIVE_XML_FORBIDDEN_CONSTRUCT"),
            (br#"<x:RDF xmlns:x="http://www.w3.org/1999/02/22-rdf-syntax-ns#">&external;</x:RDF>"#, "NATIVE_XML_FORBIDDEN_CONSTRUCT"),
            (br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description></rdf:RDF>"#, "NATIVE_RDFXML_SYNTAX"),
            (br#"<xi:include xmlns:xi="http://www.w3.org/2001/XInclude"/>"#, "NATIVE_XML_FORBIDDEN_CONSTRUCT"),
        ];
        for (source, code) in cases {
            assert_eq!(mapped(source, None).unwrap_err().code, *code);
        }
    }

    #[test]
    fn malformed_and_undefined_references_have_distinct_codes() {
        for reference in ["&external", "&amp", "&1bad;", "&bad name;", "&;"] {
            let source = format!("<rdf:RDF xmlns:rdf=\"{RDF}\">{reference}</rdf:RDF>");
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
        let malformed_attribute = format!("<rdf:RDF xmlns:rdf=\"{RDF}\" xml:lang=\"&external\"/>");
        assert_eq!(
            graph(&malformed_attribute).unwrap_err().code,
            "NATIVE_RDFXML_SYNTAX",
        );
        for reference in ["&external;", "&entité;"] {
            let source = format!("<rdf:RDF xmlns:rdf=\"{RDF}\">{reference}</rdf:RDF>");
            assert_eq!(
                graph(&source).unwrap_err().code,
                "NATIVE_XML_FORBIDDEN_CONSTRUCT",
            );
        }
    }

    #[test]
    fn duplicate_expanded_attributes_are_rejected_across_prefix_aliases() {
        for source in [
            br#"<rdf:RDF
              xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
              xmlns:a="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
              xmlns:b="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
              <rdf:Description a:about="urn:a" b:about="urn:b"/>
            </rdf:RDF>"#
                .as_slice(),
            br#"<rdf:RDF
              xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
              xmlns:XmLrdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
              <rdf:Description rdf:about="urn:a" XmLrdf:about="urn:b"/>
            </rdf:RDF>"#
                .as_slice(),
        ] {
            assert_eq!(
                mapped(source, None).unwrap_err().code,
                "NATIVE_RDFXML_SYNTAX",
            );
        }
    }

    #[test]
    fn incomplete_mapping_fails_closed_and_is_not_advertisable() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:ex="urn:example:"><rdf:Description rdf:about="urn:s"><ex:p>quote'"\&#xE9;&#xA0;&#x1F600;</ex:p></rdf:Description></rdf:RDF>"#;
        assert_eq!(
            mapped(source, None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
        let partial = mapped_partial(source, None).expect("explicit partial mapping");
        assert_eq!(partial.mapping.total_triples, 1);
        assert_eq!(partial.mapping.consumed_triples, 0);
        assert_eq!(
            partial.mapping.unconsumed,
            [RdfTripleEvidence {
                subject: "<urn:s>".to_owned(),
                predicate: "urn:example:p".to_owned(),
                object: "quote'\"\\é\u{a0}😀".to_owned(),
                object_requires_repr: true,
            }],
        );
    }

    #[test]
    fn partial_mapping_evidence_charges_the_complete_selection_scan() {
        let triples = (0..4)
            .map(|index| Triple {
                subject: Resource::Iri(format!("urn:subject:{index}")),
                predicate: "urn:example:predicate".to_owned(),
                object: Term::Iri("urn:example:object".to_owned()),
            })
            .collect::<Vec<_>>();
        let consumed = [true, true, true, false];
        let mut limits = Limits::default();
        limits.max_canonical_work = 3;
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let error = partial_mapping_evidence(&triples, &consumed, 1, &mut session)
            .expect_err("consumed-prefix scan must exhaust the work budget");
        assert_eq!(error.code, "NATIVE_WIRE_LIMIT");
        assert_eq!(error.message, "native operation exceeds max_canonical_work",);
    }

    #[test]
    fn rfc3986_relative_references_cover_queries_fragments_and_dot_segments() {
        let base = "http://a/b/c/d;p?q";
        for (reference, expected) in [
            ("", "http://a/b/c/d;p?q"),
            ("g", "http://a/b/c/g"),
            ("./g", "http://a/b/c/g"),
            ("g/", "http://a/b/c/g/"),
            ("/g", "http://a/g"),
            ("//g", "http://g"),
            ("?y", "http://a/b/c/d;p?y"),
            ("#s", "http://a/b/c/d;p?q#s"),
            ("g?y#s", "http://a/b/c/g?y#s"),
            (";x", "http://a/b/c/;x"),
            (".", "http://a/b/c/"),
            ("..", "http://a/b/"),
            ("../g", "http://a/b/g"),
            ("../../g", "http://a/g"),
        ] {
            assert_eq!(
                resolved(reference, Some(base)).expect("resolved RFC 3986 reference"),
                expected,
            );
        }
        assert_eq!(
            resolved("next", Some("urn:base")).expect("resolved opaque base"),
            "urn:next",
        );
    }

    #[test]
    fn nested_xml_base_is_resolved_before_named_mapping() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:owl="http://www.w3.org/2002/07/owl#" xml:base="../base/">
 <owl:Ontology rdf:about="./ontology">
  <owl:versionIRI xml:base="versions/" rdf:resource="../v?x#f"/>
 </owl:Ontology>
 <owl:Class xml:base="nested/" rdf:about="../C#x"/>
</rdf:RDF>"#;
        let document =
            mapped(source, Some("http://example.test/a/root.owl")).expect("nested relative bases");
        assert_eq!(
            document.ontology_iri.as_deref(),
            Some("http://example.test/base/ontology"),
        );
        assert_eq!(
            document.version_iri.as_deref(),
            Some("http://example.test/base/v?x#f"),
        );
        assert_eq!(document.axioms.len(), 1);
    }

    #[test]
    fn relative_reference_without_base_and_invalid_base_fail_closed() {
        assert_eq!(
            resolved("relative", None).unwrap_err().code,
            "NATIVE_RDFXML_RELATIVE_IRI_NO_BASE",
        );
        assert_eq!(
            resolved("relative", Some("not-an-absolute-base"))
                .unwrap_err()
                .code,
            "NATIVE_RDFXML_INVALID_BASE_IRI",
        );
        assert_eq!(
            resolved("1:invalid", Some("http://example.test/base"))
                .unwrap_err()
                .code,
            "NATIVE_RDFXML_IRI_REFERENCE",
        );
    }

    #[test]
    fn long_comment_cdata_and_processing_instruction_searches_checkpoint() {
        for source in [
            format!("<!--{}-->", "x".repeat(256 * 1024)),
            format!("<![CDATA[{}]]>", "x".repeat(256 * 1024)),
            format!("<?xml {}?>", " ".repeat(256 * 1024)),
        ] {
            let limits = Limits::default();
            let mut guard = Guard::new(
                Cancellation::with_duration(Some(Duration::ZERO)),
                limits.deadline,
                limits.cancellation_stride,
            );
            let mut session =
                Session::new(&mut guard, &limits, source.len()).expect("bounded session");
            assert_eq!(
                XmlStream::new(&source, XmlSourceEncoding::Utf8)
                    .next(&mut session)
                    .unwrap_err()
                    .code,
                "NATIVE_DEADLINE",
            );
        }
    }

    #[test]
    fn forbidden_xml_10_numeric_characters_are_rejected() {
        for reference in ["&#1;", "&#x8;", "&#11;", "&#x1f;"] {
            let source = format!("<rdf:RDF xmlns:rdf=\"{RDF}\">{reference}</rdf:RDF>");
            assert_eq!(
                mapped(source.as_bytes(), None).unwrap_err().code,
                "NATIVE_RDFXML_SYNTAX",
            );
        }
    }

    #[test]
    fn nesting_limit_is_applied_during_the_forward_scan() {
        let mut source = String::from(
            "<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\" xmlns:e=\"urn:e:\"><rdf:Description>",
        );
        for _ in 0..256 {
            source.push_str("<e:p><rdf:Description>");
        }
        source.push_str("<e:p>");
        assert_eq!(
            mapped(source.as_bytes(), None).unwrap_err().code,
            "NATIVE_WIRE_LIMIT",
        );
    }

    #[test]
    fn expanded_names_are_accounted_before_allocation() {
        let initial = "xml".len() + XML.len() + std::mem::size_of::<NamespaceBinding>();
        let expansion = XML.len() + "base".len();
        let mut limits = Limits::default();
        limits.max_memory_bytes =
            Some(u64::try_from(initial + expansion - 1).expect("test memory limit fits in u64"));
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("bounded session");
        let mut parser = GraphParser::new("", None, XmlSourceEncoding::Utf8, false, &mut session)
            .expect("parser prefix table");
        assert_eq!(
            parser.expand("xml:base", true).unwrap_err().code,
            "NATIVE_WIRE_LIMIT",
        );
    }

    #[test]
    fn reference_upper_bound_is_accounted_before_allocation() {
        let mut limits = Limits::default();
        limits.max_memory_bytes =
            Some(u64::try_from("&amp;".len() - 1).expect("test memory limit fits in u64"));
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("bounded session");
        assert_eq!(
            decode_references("&amp;", &mut session).unwrap_err().code,
            "NATIVE_WIRE_LIMIT",
        );
    }
}
