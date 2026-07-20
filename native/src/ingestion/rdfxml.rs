//! Forward-only UTF-8 RDF/XML tokenization and a closed OWL mapping slice.
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

use super::rdf_class_expressions::{
    DecodedClassCollection, DecodedClassExpression, DecodedDataRange, DecodedIndividualCollection,
    DecodedKeyCollection, DecodedPropertyCollection, DecodedPropertyExpression,
    RdfClassExpressionDecoder,
};
use super::rdf_lists::{RdfResource as ListResource, RdfTerm as ListTerm, RdfTriple as ListTriple};
use super::{CanonicalDocument, MappingEvidence};

const RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
const OWL: &str = "http://www.w3.org/2002/07/owl#";
const XML: &str = "http://www.w3.org/XML/1998/namespace";
const XINCLUDE: &str = "http://www.w3.org/2001/XInclude";

const RDF_RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#RDF";
const RDF_DESCRIPTION: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Description";
const RDF_TYPE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const RDF_FIRST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first";
const RDF_REST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest";
const RDF_NIL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil";
const OWL_ONTOLOGY: &str = "http://www.w3.org/2002/07/owl#Ontology";
const OWL_IMPORTS: &str = "http://www.w3.org/2002/07/owl#imports";
const OWL_VERSION_IRI: &str = "http://www.w3.org/2002/07/owl#versionIRI";
const XSD_STRING: &str = "http://www.w3.org/2001/XMLSchema#string";

const RDFS_SUB_CLASS_OF: &str = "http://www.w3.org/2000/01/rdf-schema#subClassOf";
const RDFS_SUB_PROPERTY_OF: &str = "http://www.w3.org/2000/01/rdf-schema#subPropertyOf";
const RDFS_DOMAIN: &str = "http://www.w3.org/2000/01/rdf-schema#domain";
const RDFS_RANGE: &str = "http://www.w3.org/2000/01/rdf-schema#range";
const OWL_EQUIVALENT_CLASS: &str = "http://www.w3.org/2002/07/owl#equivalentClass";
const OWL_DISJOINT_WITH: &str = "http://www.w3.org/2002/07/owl#disjointWith";
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
const OWL_ANNOTATED_SOURCE: &str = "http://www.w3.org/2002/07/owl#annotatedSource";
const OWL_ANNOTATED_PROPERTY: &str = "http://www.w3.org/2002/07/owl#annotatedProperty";
const OWL_ANNOTATED_TARGET: &str = "http://www.w3.org/2002/07/owl#annotatedTarget";
const OWL_NOTHING: &str = "http://www.w3.org/2002/07/owl#Nothing";

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

struct XmlStream<'a> {
    text: &'a str,
    offset: usize,
    line: u64,
    column: u64,
    xml_declaration_seen: bool,
}

impl<'a> XmlStream<'a> {
    fn new(text: &'a str) -> Self {
        Self {
            text,
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
                let value = decode_references(&self.text[start..end], session)?;
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
                if bounded_find(self.text.as_bytes(), body_start, marker, b"--", session)?.is_some()
                {
                    return Err(xml_syntax());
                }
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
                let value = owned_text(&self.text[body_start..body_end], session)?;
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
                let target_end =
                    bounded_find_xml_space(body.as_bytes(), session)?.unwrap_or(body.len());
                let target = &body[..target_end];
                if target != "xml"
                    || start != 0
                    || self.xml_declaration_seen
                    || declaration_has_unsupported_encoding(body, session)?
                {
                    return Err(xml_forbidden());
                }
                self.xml_declaration_seen = true;
                self.advance(end, session)?;
                continue;
            }
            if self.starts_with(start, "<!") {
                return Err(xml_forbidden());
            }
            if self.starts_with(start, "</") {
                let mut cursor = start + 2;
                skip_space(self.text.as_bytes(), &mut cursor);
                let name_end = scan_name(self.text.as_bytes(), cursor)?;
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
        let name_end = scan_name(bytes, cursor)?;
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
            let attribute_end = scan_name(bytes, cursor)?;
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
            let value = decode_references(&self.text[value_start..cursor], session)?;
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

#[derive(Clone, Debug)]
struct NamespaceBinding {
    prefix: String,
    iri: String,
}

#[derive(Clone, Debug)]
enum FrameRole {
    Root,
    Node {
        subject: Resource,
    },
    Property {
        subject: Resource,
        predicate: String,
        object_set: bool,
        text: String,
        datatype: Option<String>,
        language: Option<String>,
    },
    Collection {
        subject: Resource,
        predicate: String,
        tail: Option<Resource>,
        member_count: u64,
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

struct GraphParser<'text, 'session, 'guard> {
    stream: XmlStream<'text>,
    session: &'session mut Session<'guard>,
    namespaces: Vec<NamespaceBinding>,
    frames: Vec<Frame>,
    triples: Vec<Triple>,
    document_base: Option<String>,
    blank_counter: u64,
    prefix_declarations: u64,
    root_closed: bool,
}

impl<'text, 'session, 'guard> GraphParser<'text, 'session, 'guard> {
    fn new(
        text: &'text str,
        document_iri: Option<&str>,
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
            stream: XmlStream::new(text),
            session,
            namespaces,
            frames: Vec::new(),
            triples: Vec::new(),
            document_base,
            blank_counter: 0,
            prefix_declarations: 0,
            root_closed: false,
        })
    }

    fn parse(mut self) -> NativeResult<Vec<Triple>> {
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
        Ok(self.triples)
    }

    fn start(&mut self, event: StartEvent) -> NativeResult<()> {
        if self.root_closed {
            return Err(xml_syntax());
        }
        let namespace_start = self.namespaces.len();
        for attribute in &event.attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                let prefix = attribute.name.strip_prefix("xmlns:").unwrap_or("");
                if prefix == "xmlns" || (prefix == "xml" && attribute.value != XML) {
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
                let binding = NamespaceBinding {
                    prefix: owned_text(prefix, self.session)?,
                    iri: owned_text(&attribute.value, self.session)?,
                };
                reserve_vec_item(&mut self.namespaces, self.session)?;
                self.namespaces.push(binding);
            }
        }
        self.validate_expanded_attribute_uniqueness(&event.attributes)?;
        let expanded_name = self.expand(&event.name, false)?;
        if expanded_name.starts_with(XINCLUDE) {
            return Err(xml_forbidden());
        }
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
                FrameRole::Root
            } else {
                self.node_role(&event.attributes, &expanded_name, base.as_deref(), None)?
            }
        } else {
            match self.frames.last().map(|frame| &frame.role) {
                Some(FrameRole::Root) => {
                    self.node_role(&event.attributes, &expanded_name, base.as_deref(), None)?
                }
                Some(FrameRole::Node { subject }) => {
                    let subject = clone_resource(subject, self.session)?;
                    self.property_role(
                        &event.attributes,
                        subject,
                        &expanded_name,
                        base.as_deref(),
                        language.as_deref(),
                    )?
                }
                Some(FrameRole::Property { object_set, .. }) if !*object_set => {
                    let role =
                        self.node_role(&event.attributes, &expanded_name, base.as_deref(), None)?;
                    let object = match &role {
                        FrameRole::Node { subject } => clone_resource(subject, self.session)?,
                        _ => return Err(xml_syntax()),
                    };
                    self.set_parent_object(object)?;
                    role
                }
                Some(FrameRole::Collection { .. }) => {
                    self.check_collection_member_limit()?;
                    let role =
                        self.node_role(&event.attributes, &expanded_name, base.as_deref(), None)?;
                    let member = match &role {
                        FrameRole::Node { subject } => clone_resource(subject, self.session)?,
                        _ => return Err(xml_syntax()),
                    };
                    self.append_collection_member(member)?;
                    role
                }
                _ => return Err(xml_syntax()),
            }
        };
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
            raw_name: event.name,
            namespace_start,
            base,
            language,
            role,
        });
        Ok(())
    }

    fn node_role(
        &mut self,
        attributes: &[Attribute],
        expanded_name: &str,
        base: Option<&str>,
        linked_subject: Option<Resource>,
    ) -> NativeResult<FrameRole> {
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
            if value.is_empty() {
                return Err(xml_syntax());
            }
            let fragment = prefixed_text("#", value, self.session)?;
            Resource::Iri(resolve_iri(&fragment, base, self.session)?)
        } else if let Some(value) = node_id {
            if value.is_empty() {
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
        self.reject_unknown_attributes(
            attributes,
            &[
                (RDF, "about"),
                (RDF, "ID"),
                (RDF, "nodeID"),
                (XML, "base"),
                (XML, "lang"),
            ],
        )?;
        Ok(FrameRole::Node { subject })
    }

    fn property_role(
        &mut self,
        attributes: &[Attribute],
        subject: Resource,
        predicate: &str,
        base: Option<&str>,
        language: Option<&str>,
    ) -> NativeResult<FrameRole> {
        let resource = self.attribute(attributes, RDF, "resource")?;
        let node_id = self.attribute(attributes, RDF, "nodeID")?;
        let parse_type = self.attribute(attributes, RDF, "parseType")?;
        let datatype_attribute = self.attribute(attributes, RDF, "datatype")?;
        if usize::from(resource.is_some())
            + usize::from(node_id.is_some())
            + usize::from(parse_type.is_some())
            + usize::from(datatype_attribute.is_some())
            > 1
        {
            return Err(xml_syntax());
        }
        if let Some(parse_type) = parse_type {
            if parse_type != "Collection" {
                return Err(mapping_incomplete());
            }
            if self.attribute(attributes, RDF, "ID")?.is_some() {
                return Err(mapping_incomplete());
            }
            self.reject_unknown_attributes(
                attributes,
                &[(RDF, "parseType"), (XML, "base"), (XML, "lang")],
            )?;
            return Ok(FrameRole::Collection {
                subject,
                predicate: owned_text(predicate, self.session)?,
                tail: None,
                member_count: 0,
            });
        }
        let datatype = datatype_attribute
            .map(|value| resolve_iri(value, base, self.session))
            .transpose()?;
        self.reject_unknown_attributes(
            attributes,
            &[
                (RDF, "resource"),
                (RDF, "nodeID"),
                (RDF, "parseType"),
                (RDF, "datatype"),
                (RDF, "ID"),
                (XML, "base"),
                (XML, "lang"),
            ],
        )?;
        let object = if let Some(value) = resource {
            Some(Resource::Iri(resolve_iri(value, base, self.session)?))
        } else {
            node_id
                .map(|value| owned_text(value, self.session).map(Resource::Blank))
                .transpose()?
        };
        let object_set = object.is_some();
        if let Some(object) = object {
            let triple_subject = clone_resource(&subject, self.session)?;
            let triple_predicate = owned_text(predicate, self.session)?;
            self.add(Triple {
                subject: triple_subject,
                predicate: triple_predicate,
                object: object.into(),
            })?;
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
        })
    }

    fn set_parent_object(&mut self, object: Resource) -> NativeResult<()> {
        let (subject, predicate) = match self.frames.last_mut().map(|frame| &mut frame.role) {
            Some(FrameRole::Property {
                subject,
                predicate,
                object_set,
                ..
            }) if !*object_set => {
                *object_set = true;
                (
                    clone_resource(subject, self.session)?,
                    owned_text(predicate, self.session)?,
                )
            }
            _ => return Err(xml_syntax()),
        };
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
        match tail {
            Some(tail) => self.add_resource_edge(tail, RDF_REST, linked_cell)?,
            None => self.add_resource_edge(subject, &predicate, linked_cell)?,
        }
        let first_subject = clone_resource(&cell, self.session)?;
        self.add_resource_edge(first_subject, RDF_FIRST, member)?;
        match self.frames.last_mut().map(|frame| &mut frame.role) {
            Some(FrameRole::Collection {
                tail, member_count, ..
            }) => {
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
        match self.frames.last_mut().map(|frame| &mut frame.role) {
            Some(FrameRole::Property {
                object_set: false,
                text,
                ..
            }) => {
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
            } => {
                if object_set {
                    if !text.chars().all(char::is_whitespace) {
                        return Err(xml_syntax());
                    }
                } else {
                    let (datatype, language) = match (datatype, language) {
                        (Some(value), _) => (Some(value), None),
                        (None, Some(value)) => (None, Some(value)),
                        (None, None) => (Some(owned_text(XSD_STRING, self.session)?), None),
                    };
                    self.add(Triple {
                        subject,
                        predicate,
                        object: Term::Literal {
                            lexical: text,
                            datatype,
                            language,
                        },
                    })?;
                }
            }
            FrameRole::Collection {
                subject,
                predicate,
                tail,
                ..
            } => {
                let nil = Resource::Iri(owned_text(RDF_NIL, self.session)?);
                match tail {
                    Some(tail) => self.add_resource_edge(tail, RDF_REST, nil)?,
                    None => self.add_resource_edge(subject, &predicate, nil)?,
                }
            }
            FrameRole::Root | FrameRole::Node { .. } => {}
        }
        self.namespaces.truncate(frame.namespace_start);
        if self.frames.is_empty() {
            self.root_closed = true;
        }
        Ok(())
    }

    fn expand(&mut self, raw: &str, attribute: bool) -> NativeResult<String> {
        let (prefix, local) = match raw.split_once(':') {
            Some((prefix, local)) if !prefix.is_empty() && !local.is_empty() => {
                (Some(prefix), local)
            }
            Some(_) => return Err(xml_syntax()),
            None => (None, raw),
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
            let expanded = self.expand(&attribute.name, true)?;
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
            let expanded = self.expand(&attribute.name, true)?;
            if !allowed
                .iter()
                .any(|(namespace, local)| expanded_name_matches(&expanded, namespace, local))
            {
                return Err(mapping_incomplete());
            }
        }
        Ok(())
    }

    fn validate_expanded_attribute_uniqueness(
        &mut self,
        attributes: &[Attribute],
    ) -> NativeResult<()> {
        let count = attributes
            .iter()
            .filter(|attribute| attribute.name != "xmlns" && !attribute.name.starts_with("xmlns:"))
            .count();
        let metadata = count
            .checked_mul(std::mem::size_of::<String>())
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
            expanded.push(self.expand(&attribute.name, true)?);
        }
        expanded.sort_unstable();
        if expanded.windows(2).any(|pair| pair[0] == pair[1]) {
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
    let (text, decoded_codepoints) = decode_utf8(source, session)?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(&text);
    let decoded_codepoints =
        decoded_codepoints.saturating_sub(u64::from(source.starts_with(&[0xef, 0xbb, 0xbf])));
    let triples = GraphParser::new(text, document_iri, session)?.parse()?;
    map_graph(triples, decoded_codepoints, session)
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
        let Some(kind) = declaration_kind(object) else {
            continue;
        };
        // A blank `rdf:type owl:Class` is an optional structural-expression
        // marker rather than an OWL Declaration axiom.  Its owning expression
        // decoder consumes the exact marker later.
        let Resource::Iri(subject) = &triple.subject else {
            continue;
        };
        super::check_iri(
            subject,
            session,
            "native RDF declaration IRI exceeds max_iri_bytes",
        )?;
        let declaration = build_node(
            60,
            [
                Field::Node(named_entity(kind, subject, session)?),
                Field::Set(Vec::new()),
            ],
            session,
        )?;
        push_axiom(declaration, &mut axioms, session)?;
        consumed[index] = true;
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
    map_property_chains(
        &list_graph,
        &mut consumed,
        &mut expressions,
        &mut axioms,
        session,
    )?;
    map_has_keys(
        &list_graph,
        &mut consumed,
        &mut expressions,
        &mut axioms,
        session,
    )?;
    map_disjoint_unions(
        &list_graph,
        &mut consumed,
        &mut expressions,
        &mut axioms,
        session,
    )?;
    map_datatype_definitions(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        &mut axioms,
        session,
    )?;
    map_equivalent_class_components(
        &list_graph,
        &mut consumed,
        &kinds,
        &mut expressions,
        &mut axioms,
        session,
    )?;
    map_equivalent_property_components(
        OWL_EQUIVALENT_PROPERTY,
        &triples,
        &mut consumed,
        &kinds,
        &mut axioms,
        session,
    )?;
    map_same_individual_components(
        &list_graph,
        &mut consumed,
        &mut expressions,
        &mut axioms,
        session,
    )?;
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
            consumed[index] = true;
            axiom_annotations.claim(triple, &triples)?;
        }
    }
    if axiom_annotations.has_unclaimed() {
        return Err(rdf_axiom_reification(
            "native owl:Axiom reification targets an unsupported axiom mapping",
        ));
    }
    axioms.sort_unstable();
    axioms.dedup();
    ontology_annotations.sort_unstable();
    ontology_annotations.dedup();
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
    let consumed_triples = consumed.iter().filter(|value| **value).count();
    if consumed_triples != triples.len() {
        return Err(mapping_incomplete());
    }
    Ok(CanonicalDocument {
        document_iri: None,
        ontology_iri,
        version_iri,
        imports,
        ontology_annotations,
        axioms,
        extensions: Vec::new(),
        source_sha256: [0; 32],
        byte_length: 0,
        decoded_codepoints,
        mapping: MappingEvidence {
            total_triples,
            consumed_triples: u64::try_from(consumed_triples)
                .map_err(|_| NativeError::limit("native consumed triple count exceeds u64"))?,
            rule_ids: &[
                "OWL2-RDF-REVERSE-HEADER",
                "OWL2-RDF-REVERSE-DECLARATION",
                "OWL2-RDF-REVERSE-NAMED-AXIOM",
                "OWL2-RDF-REVERSE-BOOLEAN-CLASS-EXPRESSION",
            ],
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
        let Some(kind) = declaration_kind(object) else {
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
        let (_distinct_index, head) = collection_head(
            triples,
            triple.subject,
            OWL_DISTINCT_MEMBERS,
            "native owl:AllDifferent has no distinctMembers list",
            "native owl:AllDifferent has more than one distinctMembers list",
            session,
        )?;
        let DecodedIndividualCollection {
            individuals,
            consumed: collection_consumed,
        } = expressions.decode_individual_collection(head, session)?;
        let individuals = canonical_set(individuals, 2, None)?;
        let annotations = annotations_on_structural_node(
            triple.subject,
            &[RDF_TYPE, OWL_DISTINCT_MEMBERS],
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

fn map_property_chains<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if consumed[index] || triple.predicate != OWL_PROPERTY_CHAIN_AXIOM {
            continue;
        }
        let ListResource::Iri(super_property) = triple.subject else {
            continue;
        };
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
        let chain = build_node(11, [Field::Sequence(properties)], session)?;
        let axiom = build_node(
            70,
            [
                Field::Node(chain),
                Field::Node(named_entity("object_property", super_property, session)?),
                Field::Set(Vec::new()),
            ],
            session,
        )?;
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consumed[index] = true;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn map_has_keys<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
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
        let object_properties = canonical_set(object_properties, 0, None)?;
        let data_properties = canonical_set(data_properties, 0, None)?;
        let axiom = build_node(
            101,
            [
                Field::Node(class_expression),
                Field::Set(object_properties),
                Field::Set(data_properties),
                Field::Set(Vec::new()),
            ],
            session,
        )?;
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consumed[index] = true;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn map_disjoint_unions<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
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
        let axiom = build_node(
            64,
            [
                Field::Node(named_entity("class", defined_class, session)?),
                Field::Set(members),
                Field::Set(Vec::new()),
            ],
            session,
        )?;
        consume_collection_indexes(collection_consumed, consumed, session)?;
        consumed[index] = true;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn map_datatype_definitions<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
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
        let axiom = build_node(
            100,
            [
                Field::Node(named_entity("datatype", datatype, session)?),
                Field::Node(data_range),
                Field::Set(Vec::new()),
            ],
            session,
        )?;
        consume_collection_indexes(range_consumed, consumed, session)?;
        consumed[index] = true;
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

#[allow(clippy::too_many_arguments)]
fn map_equivalent_class_components<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    kinds: &[KindRecord<'graph>],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    axioms: &mut Vec<Vec<u8>>,
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
        add_class_member(&mut members, left, session)?;
        add_class_member(&mut members, right, session)?;
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
                    consumed[index] = true;
                    changed = true;
                }
            }
            if !changed {
                break;
            }
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
        let axiom = build_node(62, [Field::Set(nodes), Field::Set(Vec::new())], session)?;
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
        RDFS_DOMAIN | RDFS_RANGE => {
            let (ListResource::Iri(property), Some(object)) = (triple.subject, object) else {
                return Ok(None);
            };
            if is_annotation_property(property, kinds) {
                return Ok(None);
            }
            if triple.predicate == RDFS_RANGE && has_kind(kinds, property, "data_property") {
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
            build_node(
                tag,
                [
                    Field::Node(named_entity(property_kind, property, session)?),
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
fn map_equivalent_property_components(
    predicate: &str,
    triples: &[Triple],
    consumed: &mut [bool],
    kinds: &[KindRecord<'_>],
    axioms: &mut Vec<Vec<u8>>,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    for start in 0..triples.len() {
        if consumed[start] {
            continue;
        }
        let Some((left, right)) = named_edge(&triples[start], predicate) else {
            continue;
        };
        if !equivalent_property_member_supported(left, kinds)
            || !equivalent_property_member_supported(right, kinds)
        {
            continue;
        }
        let mut members = Vec::new();
        add_member(&mut members, left, session)?;
        add_member(&mut members, right, session)?;
        consumed[start] = true;
        loop {
            let mut changed = false;
            for (index, triple) in triples.iter().enumerate() {
                session.step(1)?;
                if consumed[index] {
                    continue;
                }
                let Some((edge_left, edge_right)) = named_edge(triple, predicate) else {
                    continue;
                };
                if !equivalent_property_member_supported(edge_left, kinds)
                    || !equivalent_property_member_supported(edge_right, kinds)
                {
                    continue;
                }
                if members.contains(&edge_left) || members.contains(&edge_right) {
                    add_member(&mut members, edge_left, session)?;
                    add_member(&mut members, edge_right, session)?;
                    consumed[index] = true;
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }
        let (tag, entity_kind) = if members
            .iter()
            .all(|value| has_kind(kinds, value, "data_property"))
        {
            (91, "data_property")
        } else {
            (71, "object_property")
        };
        let members = named_set(entity_kind, &members, session)?;
        let axiom = build_node(tag, [Field::Set(members), Field::Set(Vec::new())], session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn equivalent_property_member_supported(value: &str, kinds: &[KindRecord<'_>]) -> bool {
    !has_kind(kinds, value, "annotation_property")
}

fn map_same_individual_components<'view, 'graph>(
    triples: &'view [ListTriple<'graph>],
    consumed: &mut [bool],
    expressions: &mut RdfClassExpressionDecoder<'view, 'graph>,
    axioms: &mut Vec<Vec<u8>>,
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
        add_individual_member(&mut members, left, session)?;
        add_individual_member(&mut members, right, session)?;
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
                    consumed[index] = true;
                    changed = true;
                }
            }
            if !changed {
                break;
            }
        }

        let mut nodes = reserved_vec(members.len(), session)?;
        for member in members {
            nodes.push(expressions.decode_individual(member.as_term(), session)?);
        }
        session.finish()?;
        let nodes = canonical_set(nodes, 2, None)?;
        let axiom = build_node(110, [Field::Set(nodes), Field::Set(Vec::new())], session)?;
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

fn named_edge<'a>(triple: &'a Triple, predicate: &str) -> Option<(&'a str, &'a str)> {
    if triple.predicate != predicate {
        return None;
    }
    let (Resource::Iri(left), Term::Iri(right)) = (&triple.subject, &triple.object) else {
        return None;
    };
    Some((left, right))
}

fn add_member<'a>(
    members: &mut Vec<&'a str>,
    value: &'a str,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    session.step(
        u64::try_from(members.len())
            .map_err(|_| NativeError::limit("native RDF component work exceeds u64"))?,
    )?;
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
        "http://www.w3.org/2002/07/owl#FunctionalProperty" if data_property => 95,
        "http://www.w3.org/2002/07/owl#FunctionalProperty" => 76,
        "http://www.w3.org/2002/07/owl#InverseFunctionalProperty" => 77,
        "http://www.w3.org/2002/07/owl#ReflexiveProperty" => 78,
        "http://www.w3.org/2002/07/owl#IrreflexiveProperty" => 79,
        "http://www.w3.org/2002/07/owl#SymmetricProperty" => 80,
        "http://www.w3.org/2002/07/owl#AsymmetricProperty" => 81,
        "http://www.w3.org/2002/07/owl#TransitiveProperty" => 82,
        _ => return None,
    })
}

fn is_structural_type(value: &str) -> bool {
    value.starts_with(OWL)
        || matches!(
            value,
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#List"
                | "http://www.w3.org/2000/01/rdf-schema#Datatype"
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
        "http://www.w3.org/2002/07/owl#Class" => "class",
        "http://www.w3.org/2000/01/rdf-schema#Datatype" => "datatype",
        "http://www.w3.org/2002/07/owl#ObjectProperty" => "object_property",
        "http://www.w3.org/2002/07/owl#DatatypeProperty" => "data_property",
        "http://www.w3.org/2002/07/owl#AnnotationProperty" => "annotation_property",
        "http://www.w3.org/2002/07/owl#NamedIndividual" => "named_individual",
        _ => return None,
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

fn decode_references(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    if !value.contains('&') {
        return owned_text(value, session);
    }
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native XML reference allocation failed"))?;
    let mut cursor = 0;
    while let Some(relative) = value[cursor..].find('&') {
        let start = cursor + relative;
        output.push_str(&value[cursor..start]);
        let end = value[start + 1..]
            .find(';')
            .map(|offset| start + 1 + offset)
            .ok_or_else(xml_forbidden)?;
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
            _ => return Err(xml_forbidden()),
        }
        cursor = end + 1;
    }
    output.push_str(&value[cursor..]);
    Ok(output)
}

fn xml_character(value: u32) -> NativeResult<char> {
    let character = char::from_u32(value).ok_or_else(xml_syntax)?;
    if !matches!(value, 0x09 | 0x0a | 0x0d | 0x20..=0xd7ff | 0xe000..=0xfffd | 0x10000..=0x10ffff) {
        return Err(xml_syntax());
    }
    Ok(character)
}

fn scan_name(bytes: &[u8], start: usize) -> NativeResult<usize> {
    let first = *bytes.get(start).ok_or_else(xml_syntax)?;
    if !(first.is_ascii_alphabetic() || matches!(first, b'_' | b':')) {
        return Err(xml_syntax());
    }
    let mut end = start + 1;
    while bytes.get(end).is_some_and(|value| {
        value.is_ascii_alphanumeric() || matches!(*value, b'_' | b':' | b'.' | b'-')
    }) {
        end += 1;
    }
    Ok(end)
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

fn bounded_find_ascii_case(
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
            if bytes[position..position + marker.len()].eq_ignore_ascii_case(marker) {
                return Ok(Some(position));
            }
        }
        cursor = batch_end;
        session.finish()?;
    }
    Ok(None)
}

fn bounded_skip_xml_space(
    bytes: &[u8],
    start: usize,
    session: &mut Session<'_>,
) -> NativeResult<usize> {
    if start > bytes.len() {
        return Err(xml_syntax());
    }
    session.finish()?;
    let mut cursor = start;
    while cursor < bytes.len() {
        let batch_end = cursor.saturating_add(64 * 1024).min(bytes.len());
        while cursor < batch_end && matches!(bytes[cursor], b' ' | b'\t' | b'\r' | b'\n') {
            cursor += 1;
        }
        if cursor < batch_end {
            return Ok(cursor);
        }
        session.finish()?;
    }
    Ok(cursor)
}

fn bounded_find_xml_space(bytes: &[u8], session: &mut Session<'_>) -> NativeResult<Option<usize>> {
    session.finish()?;
    for (batch, chunk) in bytes.chunks(64 * 1024).enumerate() {
        if let Some(position) = chunk
            .iter()
            .position(|value| matches!(*value, b' ' | b'\t' | b'\r' | b'\n'))
        {
            return batch
                .checked_mul(64 * 1024)
                .and_then(|offset| offset.checked_add(position))
                .map(Some)
                .ok_or_else(|| NativeError::limit("native XML scan offset overflow"));
        }
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

fn declaration_has_unsupported_encoding(
    declaration: &str,
    session: &mut Session<'_>,
) -> NativeResult<bool> {
    let bytes = declaration.as_bytes();
    let Some(position) = bounded_find_ascii_case(bytes, 0, bytes.len(), b"encoding", session)?
    else {
        return Ok(false);
    };
    let suffix_start = position + "encoding".len();
    let Some(equal) = bounded_find(bytes, suffix_start, bytes.len(), b"=", session)? else {
        return Ok(true);
    };
    let value_start = bounded_skip_xml_space(bytes, equal + 1, session)?;
    let Some(quote) = bytes.get(value_start).copied() else {
        return Ok(true);
    };
    if !matches!(quote, b'\'' | b'"') {
        return Ok(true);
    }
    let Some(end) = bounded_find(bytes, value_start + 1, bytes.len(), &[quote], session)? else {
        return Ok(true);
    };
    let value = &declaration[value_start + 1..end];
    Ok(!["utf-8", "utf8", "us-ascii"]
        .iter()
        .any(|encoding| value.eq_ignore_ascii_case(encoding)))
}

fn clone_resource(value: &Resource, session: &mut Session<'_>) -> NativeResult<Resource> {
    match value {
        Resource::Iri(value) => owned_text(value, session).map(Resource::Iri),
        Resource::Blank(value) => owned_text(value, session).map(Resource::Blank),
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
    let size = "generated-"
        .len()
        .checked_add(digits)
        .ok_or_else(|| NativeError::limit("native RDF blank identifier size overflow"))?;
    session.reserve_bytes(size)?;
    let mut output = String::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF blank identifier allocation failed"))?;
    write!(&mut output, "generated-{value}")
        .map_err(|_| NativeError::protocol("native RDF blank identifier formatting failed"))?;
    Ok(output)
}

fn expanded_name_matches(expanded: &str, namespace: &str, local: &str) -> bool {
    expanded
        .len()
        .checked_sub(namespace.len())
        .is_some_and(|length| length == local.len() && expanded.starts_with(namespace))
        && expanded.ends_with(local)
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
    use crate::limits::Limits;
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

    fn graph(source: &str) -> NativeResult<Vec<Triple>> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len())?;
        GraphParser::new(source, None, &mut session)?.parse()
    }

    fn iri_resource(value: &str) -> Resource {
        Resource::Iri(value.to_owned())
    }

    fn blank_resource(value: &str) -> Resource {
        Resource::Blank(value.to_owned())
    }

    fn contains_edge(graph: &[Triple], subject: Resource, predicate: &str, object: Term) -> bool {
        graph.contains(&Triple {
            subject,
            predicate: predicate.to_owned(),
            object,
        })
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
            blank_resource("generated-1").into(),
        ));
        assert!(contains_edge(
            &single,
            blank_resource("generated-1"),
            RDF_FIRST,
            iri_resource("urn:a").into(),
        ));
        assert!(contains_edge(
            &single,
            blank_resource("generated-1"),
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
            blank_resource("generated-1"),
            RDF_REST,
            blank_resource("generated-2").into(),
        ));
        assert!(contains_edge(
            &multiple,
            blank_resource("generated-2"),
            RDF_FIRST,
            iri_resource("urn:b").into(),
        ));
        assert!(contains_edge(
            &multiple,
            blank_resource("generated-2"),
            RDF_REST,
            Term::Iri(RDF_NIL.to_owned()),
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
        ] {
            assert_eq!(graph(&source).unwrap_err().code, "NATIVE_RDFXML_SYNTAX");
        }
        let unsupported = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:e=\"urn:e:\"><rdf:Description rdf:about=\"urn:s\"><e:p rdf:parseType=\"Resource\"/></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            graph(&unsupported).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE"
        );

        let limits = Limits::default();
        let maximum = limits.value(LimitKey::MaxRdfListLength);
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, 0).expect("session");
        let mut parser = GraphParser::new("", None, &mut session).expect("parser");
        parser.frames.push(Frame {
            raw_name: "e:p".to_owned(),
            namespace_start: parser.namespaces.len(),
            base: None,
            language: None,
            role: FrameRole::Collection {
                subject: iri_resource("urn:s"),
                predicate: "urn:e:p".to_owned(),
                tail: None,
                member_count: maximum,
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

        let blank_super = format!(
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><rdf:Description><owl:propertyChainAxiom rdf:parseType=\"Collection\"><rdf:Description rdf:about=\"urn:p\"/><rdf:Description rdf:about=\"urn:q\"/></owl:propertyChainAxiom></rdf:Description></rdf:RDF>"
        );
        assert_eq!(
            mapped(blank_super.as_bytes(), None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
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
            "<rdf:RDF xmlns:rdf=\"{RDF}\" xmlns:owl=\"{OWL}\"><owl:Class rdf:about=\"urn:C\"/><owl:Axiom rdf:nodeID=\"axiom\"><owl:annotatedSource rdf:resource=\"urn:C\"/><owl:annotatedProperty rdf:resource=\"{RDF_TYPE}\"/><owl:annotatedTarget rdf:resource=\"{OWL}Class\"/></owl:Axiom></rdf:RDF>"
        );
        assert_eq!(
            mapped(annotated_declaration.as_bytes(), None)
                .unwrap_err()
                .code,
            "NATIVE_RDF_AXIOM_REIFICATION",
        );

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
    fn duplicate_expanded_attributes_are_rejected_across_prefix_aliases() {
        let source = br#"<rdf:RDF
          xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns:a="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
          xmlns:b="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
          <rdf:Description a:about="urn:a" b:about="urn:b"/>
        </rdf:RDF>"#;
        assert_eq!(
            mapped(source, None).unwrap_err().code,
            "NATIVE_RDFXML_SYNTAX",
        );
    }

    #[test]
    fn incomplete_mapping_fails_closed_and_is_not_advertisable() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns:ex="urn:example:"><rdf:Description rdf:about="urn:s"><ex:p rdf:resource="urn:o"/></rdf:Description></rdf:RDF>"#;
        assert_eq!(
            mapped(source, None).unwrap_err().code,
            "NATIVE_RDF_MAPPING_INCOMPLETE",
        );
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
                XmlStream::new(&source).next(&mut session).unwrap_err().code,
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
        let mut parser = GraphParser::new("", None, &mut session).expect("parser prefix table");
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
