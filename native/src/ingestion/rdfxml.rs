//! Forward-only UTF-8 RDF/XML tokenization and the first closed OWL mapping slice.
//!
//! This intentionally unadvertised slice accepts ontology headers, imports,
//! named entity declarations, and the closed named-node axiom subset that does
//! not require RDF-list or blank-expression decoding. The event/token model is
//! not tied to that subset: later RDF/XML productions extend the graph sink and
//! RDF mapper rather than replacing the bounded XML scanner.

use crate::canonical::{canonical_set, entity, iri, Field, Node};
use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;

use super::{CanonicalDocument, MappingEvidence};

const RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
const OWL: &str = "http://www.w3.org/2002/07/owl#";
const XML: &str = "http://www.w3.org/XML/1998/namespace";
const XINCLUDE: &str = "http://www.w3.org/2001/XInclude";

const RDF_RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#RDF";
const RDF_DESCRIPTION: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#Description";
const RDF_TYPE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
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
const OWL_INVERSE_OF: &str = "http://www.w3.org/2002/07/owl#inverseOf";
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
            self.blank_counter = self
                .blank_counter
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native RDF blank counter overflow"))?;
            Resource::Blank(generated_blank(self.blank_counter, self.session)?)
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
        if usize::from(resource.is_some())
            + usize::from(node_id.is_some())
            + usize::from(parse_type.is_some())
            > 1
        {
            return Err(xml_syntax());
        }
        if parse_type.is_some() {
            return Err(mapping_incomplete());
        }
        let datatype = self
            .attribute(attributes, RDF, "datatype")?
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
        if let FrameRole::Property {
            subject,
            predicate,
            object_set,
            text,
            datatype,
            language,
        } = frame.role
        {
            if object_set {
                if !text.chars().all(char::is_whitespace) {
                    return Err(xml_syntax());
                }
            } else {
                let datatype = match datatype {
                    Some(value) => Some(value),
                    None if language.is_none() => Some(owned_text(XSD_STRING, self.session)?),
                    None => None,
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
    for (index, triple) in triples.iter().enumerate() {
        session.step(1)?;
        if triple.predicate != RDF_TYPE {
            continue;
        }
        let Term::Iri(object) = &triple.object else {
            continue;
        };
        let Some(kind) = declaration_kind(object) else {
            continue;
        };
        let Resource::Iri(subject) = &triple.subject else {
            return Err(rdf_mapping_type());
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
    map_equivalent_components(
        OWL_EQUIVALENT_CLASS,
        EquivalentKind::Class,
        &triples,
        &mut consumed,
        &kinds,
        &mut axioms,
        session,
    )?;
    map_equivalent_components(
        OWL_EQUIVALENT_PROPERTY,
        EquivalentKind::Property,
        &triples,
        &mut consumed,
        &kinds,
        &mut axioms,
        session,
    )?;
    for (index, triple) in triples.iter().enumerate() {
        if consumed[index] {
            continue;
        }
        session.step(1)?;
        if let Some(axiom) = named_axiom(triple, &kinds, session)? {
            push_axiom(axiom, &mut axioms, session)?;
            consumed[index] = true;
        }
    }
    axioms.sort_unstable();
    axioms.dedup();
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
        ontology_annotations: Vec::new(),
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
            ],
        },
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct KindRecord<'a> {
    iri: &'a str,
    kind: &'static str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EquivalentKind {
    Class,
    Property,
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

#[allow(clippy::too_many_arguments)]
fn map_equivalent_components(
    predicate: &str,
    equivalent_kind: EquivalentKind,
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
        if !equivalent_member_supported(equivalent_kind, left, kinds)
            || !equivalent_member_supported(equivalent_kind, right, kinds)
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
                if !equivalent_member_supported(equivalent_kind, edge_left, kinds)
                    || !equivalent_member_supported(equivalent_kind, edge_right, kinds)
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
        let (tag, entity_kind) = match equivalent_kind {
            EquivalentKind::Class => (62, "class"),
            EquivalentKind::Property
                if members
                    .iter()
                    .all(|value| has_kind(kinds, value, "data_property")) =>
            {
                (91, "data_property")
            }
            EquivalentKind::Property => (71, "object_property"),
        };
        let members = named_set(entity_kind, &members, session)?;
        let axiom = build_node(tag, [Field::Set(members), Field::Set(Vec::new())], session)?;
        push_axiom(axiom, axioms, session)?;
    }
    Ok(())
}

fn equivalent_member_supported(
    equivalent_kind: EquivalentKind,
    value: &str,
    kinds: &[KindRecord<'_>],
) -> bool {
    match equivalent_kind {
        EquivalentKind::Class => !has_kind(kinds, value, "datatype"),
        EquivalentKind::Property => !has_kind(kinds, value, "annotation_property"),
    }
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

fn named_axiom(
    triple: &Triple,
    kinds: &[KindRecord<'_>],
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
                Field::Set(Vec::new()),
            ],
            session,
        )?,
        OWL_DISJOINT_WITH if subject == object => build_node(
            61,
            [
                Field::Node(named_entity("class", subject, session)?),
                Field::Node(named_entity("class", OWL_NOTHING, session)?),
                Field::Set(Vec::new()),
            ],
            session,
        )?,
        OWL_DISJOINT_WITH => build_node(
            63,
            [
                Field::Set(named_set("class", &[subject, object], session)?),
                Field::Set(Vec::new()),
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
            build_binary_named_axiom(90, "data_property", subject, object, session)?
        }
        RDFS_SUB_PROPERTY_OF => {
            build_binary_named_axiom(70, "object_property", subject, object, session)?
        }
        OWL_PROPERTY_DISJOINT_WITH if has_kind(kinds, subject, "data_property") => build_node(
            92,
            [
                Field::Set(named_set("data_property", &[subject, object], session)?),
                Field::Set(Vec::new()),
            ],
            session,
        )?,
        OWL_PROPERTY_DISJOINT_WITH => build_node(
            72,
            [
                Field::Set(named_set("object_property", &[subject, object], session)?),
                Field::Set(Vec::new()),
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
                    Field::Set(Vec::new()),
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
                Field::Set(Vec::new()),
            ],
            session,
        )?,
        RDFS_RANGE if has_kind(kinds, subject, "data_property") => build_node(
            94,
            [
                Field::Node(named_entity("data_property", subject, session)?),
                Field::Node(named_entity("datatype", object, session)?),
                Field::Set(Vec::new()),
            ],
            session,
        )?,
        RDFS_DOMAIN => build_binary_named_axiom(74, "object_property", subject, object, session)?,
        RDFS_RANGE => build_binary_named_axiom(75, "object_property", subject, object, session)?,
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
                        Field::Set(Vec::new()),
                    ],
                    session,
                )?
            } else if !is_structural_type(object) {
                build_node(
                    112,
                    [
                        Field::Node(named_entity("class", object, session)?),
                        Field::Node(named_entity("named_individual", subject, session)?),
                        Field::Set(Vec::new()),
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
            Field::Set(Vec::new()),
        ],
        session,
    )
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::{Cancellation, Guard};
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
