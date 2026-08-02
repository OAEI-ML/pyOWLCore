//! Bounded forward OWL/XML ingestion.
//!
//! This module is intentionally private until the native OWL/XML capability is
//! completed and advertised.  The XML envelope is consumed as a forward event
//! stream.  Completed subtrees are reduced immediately to canonical nodes, so
//! memory is bounded by the open element stack and the largest current
//! constructor rather than by the complete document tree.

use std::collections::BTreeMap;
use std::mem::size_of;

use crate::canonical::{anonymous, canonical_set, entity, iri, literal, Field, Node};
use crate::error::{NativeError, NativeResult};
use crate::hash::sha256;
use crate::limits::LimitKey;
use crate::session::Session;

use super::{CanonicalDocument, CanonicalOccurrence, MappingEvidence};

const OWL: &str = "http://www.w3.org/2002/07/owl#";
const RDF: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#";
const RDFS: &str = "http://www.w3.org/2000/01/rdf-schema#";
const XSD: &str = "http://www.w3.org/2001/XMLSchema#";
const XML: &str = "http://www.w3.org/XML/1998/namespace";
const XMLNS: &str = "http://www.w3.org/2000/xmlns/";
const RDF_PLAIN_LITERAL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";
const RDFS_LITERAL: &str = "http://www.w3.org/2000/01/rdf-schema#Literal";
const OWL_THING: &str = "http://www.w3.org/2002/07/owl#Thing";
const OWL_NOTHING: &str = "http://www.w3.org/2002/07/owl#Nothing";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum XmlSourceEncoding {
    Utf8,
    Utf16Le,
    Utf16Be,
}

#[derive(Clone, Debug)]
struct Attribute {
    name: String,
    value: String,
}

#[derive(Clone, Debug)]
struct StartEvent {
    name: String,
    attributes: Vec<Attribute>,
    empty: bool,
}

#[derive(Clone, Debug)]
enum XmlEvent {
    Start(StartEvent),
    End(String),
    Text(String),
}

struct XmlStream<'a> {
    text: &'a str,
    source_encoding: XmlSourceEncoding,
    offset: usize,
    xml_declaration_seen: bool,
}

impl<'a> XmlStream<'a> {
    fn new(text: &'a str, source_encoding: XmlSourceEncoding) -> Self {
        Self {
            text,
            source_encoding,
            offset: 0,
            xml_declaration_seen: false,
        }
    }

    fn next(&mut self, session: &mut Session<'_>) -> NativeResult<Option<XmlEvent>> {
        loop {
            if self.offset == self.text.len() {
                return Ok(None);
            }
            let start = self.offset;
            if self.byte(start) != Some(b'<') {
                let end = self.find_byte(start, b'<').unwrap_or(self.text.len());
                let raw = &self.text[start..end];
                if raw.contains("]]>") {
                    return Err(syntax());
                }
                let value = decode_references(raw, XmlValueKind::Text, session)?;
                self.advance(end, session)?;
                return Ok(Some(XmlEvent::Text(value)));
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
                .ok_or_else(syntax)?;
                let body = &self.text[body_start..marker];
                if body.ends_with('-')
                    || bounded_find(self.text.as_bytes(), body_start, marker, b"--", session)?
                        .is_some()
                {
                    return Err(syntax());
                }
                validate_xml_characters(body)?;
                self.advance(marker + 3, session)?;
                continue;
            }
            if self.starts_with(start, "<![CDATA[") {
                let body_start = start + 9;
                let marker = bounded_find(
                    self.text.as_bytes(),
                    body_start,
                    self.text.len(),
                    b"]]>",
                    session,
                )?
                .ok_or_else(syntax)?;
                let value = normalize_xml_characters(
                    &self.text[body_start..marker],
                    XmlValueKind::Text,
                    session,
                )?;
                self.advance(marker + 3, session)?;
                return Ok(Some(XmlEvent::Text(value)));
            }
            if self.starts_with(start, "<?") {
                let marker = bounded_find(
                    self.text.as_bytes(),
                    start + 2,
                    self.text.len(),
                    b"?>",
                    session,
                )?
                .ok_or_else(syntax)?;
                let body = &self.text[start + 2..marker];
                let target_end = scan_name(body, 0)?;
                if target_end != body.len()
                    && !body
                        .as_bytes()
                        .get(target_end)
                        .is_some_and(|value| is_xml_space(*value))
                {
                    return Err(syntax());
                }
                let target = &body[..target_end];
                if !is_xml_ncname(target) {
                    return Err(syntax());
                }
                if target == "xml" {
                    if start != 0 || self.xml_declaration_seen {
                        return Err(syntax());
                    }
                    validate_xml_declaration(body, self.source_encoding)?;
                    self.xml_declaration_seen = true;
                } else if target.eq_ignore_ascii_case("xml") {
                    return Err(syntax());
                } else {
                    validate_xml_characters(&body[target_end..])?;
                }
                self.advance(marker + 2, session)?;
                continue;
            }
            if self.starts_with(start, "<!") {
                return Err(forbidden());
            }
            if self.starts_with(start, "</") {
                let mut cursor = start + 2;
                skip_space(self.text.as_bytes(), &mut cursor);
                let name_end = scan_name(self.text, cursor)?;
                let name = owned_text(&self.text[cursor..name_end], session)?;
                cursor = name_end;
                skip_space(self.text.as_bytes(), &mut cursor);
                if self.byte(cursor) != Some(b'>') {
                    return Err(syntax());
                }
                self.advance(cursor + 1, session)?;
                return Ok(Some(XmlEvent::End(name)));
            }
            let event = self.start_event(start, session)?;
            self.advance(event.1, session)?;
            return Ok(Some(XmlEvent::Start(event.0)));
        }
    }

    fn start_event(
        &self,
        start: usize,
        session: &mut Session<'_>,
    ) -> NativeResult<(StartEvent, usize)> {
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
                None => return Err(syntax()),
            }
            let attribute_end = scan_name(self.text, cursor)?;
            let attribute_name = owned_text(&self.text[cursor..attribute_end], session)?;
            if attributes
                .iter()
                .any(|value: &Attribute| value.name == attribute_name)
            {
                return Err(syntax());
            }
            cursor = attribute_end;
            skip_space(bytes, &mut cursor);
            if bytes.get(cursor) != Some(&b'=') {
                return Err(syntax());
            }
            cursor += 1;
            skip_space(bytes, &mut cursor);
            let quote = *bytes.get(cursor).ok_or_else(syntax)?;
            if !matches!(quote, b'\'' | b'"') {
                return Err(syntax());
            }
            cursor += 1;
            let value_start = cursor;
            while bytes.get(cursor).is_some_and(|value| *value != quote) {
                if bytes[cursor] == b'<' {
                    return Err(syntax());
                }
                cursor += 1;
            }
            if bytes.get(cursor) != Some(&quote) {
                return Err(syntax());
            }
            let value = decode_references(
                &self.text[value_start..cursor],
                XmlValueKind::Attribute,
                session,
            )?;
            cursor += 1;
            reserve_vec_item(&mut attributes, session)?;
            attributes.push(Attribute {
                name: attribute_name,
                value,
            });
        }
        Ok((
            StartEvent {
                name,
                attributes,
                empty,
            },
            cursor,
        ))
    }

    fn advance(&mut self, end: usize, session: &mut Session<'_>) -> NativeResult<()> {
        let fragment = self.text.get(self.offset..end).ok_or_else(syntax)?;
        session.step(
            u64::try_from(fragment.chars().count())
                .map_err(|_| NativeError::limit("native OWL/XML work exceeds u64"))?,
        )?;
        self.offset = end;
        Ok(())
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
            .map(|relative| offset + relative)
    }
}

#[derive(Clone, Debug)]
struct NamespaceBinding {
    prefix: String,
    iri: String,
}

#[derive(Clone, Debug)]
struct ExpandedAttribute {
    namespace: Option<String>,
    local: String,
    value: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum NodeKind {
    Iri,
    Class,
    Datatype,
    ObjectProperty,
    ObjectInverse,
    ObjectPropertyChain,
    DataProperty,
    AnnotationProperty,
    NamedIndividual,
    AnonymousIndividual,
    Literal,
    Annotation,
    ClassExpression,
    DataRange,
    Facet,
}

#[derive(Clone, Debug)]
struct TypedNode {
    kind: NodeKind,
    node: Node,
}

#[derive(Clone, Debug)]
enum Reduced {
    Node(TypedNode),
    Axioms(Vec<Node>),
    Prefix { name: String, iri: String },
    Import(String),
}

#[derive(Clone, Debug)]
struct Frame {
    raw_name: String,
    local: String,
    attributes: Vec<ExpandedAttribute>,
    text: String,
    children: Vec<TypedNode>,
    namespace_mark: usize,
    root: bool,
}

struct Parser<'a, 'b> {
    source: &'a [u8],
    session: &'a mut Session<'b>,
    namespaces: Vec<NamespaceBinding>,
    source_prefixes: BTreeMap<String, String>,
    prefixes: BTreeMap<String, String>,
    frames: Vec<Frame>,
    document_iri: Option<String>,
    ontology_iri: Option<String>,
    version_iri: Option<String>,
    imports: Vec<String>,
    annotations: Vec<Vec<u8>>,
    axioms: Vec<Vec<u8>>,
    occurrences: Vec<CanonicalOccurrence>,
    language_spellings: Vec<String>,
    decoded_codepoints: u64,
    root_seen: bool,
    root_closed: bool,
    element_count: u64,
    root_member_count: u64,
    root_occurrence_count: u64,
    capture_occurrences: bool,
    preserve_source_map: bool,
    collect_language_spellings: bool,
}

impl<'a, 'b> Parser<'a, 'b> {
    #[allow(clippy::too_many_arguments)]
    fn new(
        source: &'a [u8],
        document_iri: Option<&str>,
        decoded_codepoints: u64,
        capture_occurrences: bool,
        preserve_source_map: bool,
        collect_language_spellings: bool,
        session: &'a mut Session<'b>,
    ) -> NativeResult<Self> {
        let mut prefixes = BTreeMap::new();
        for (prefix, value) in [("owl:", OWL), ("rdf:", RDF), ("rdfs:", RDFS), ("xsd:", XSD)] {
            prefixes.insert(owned_text(prefix, session)?, owned_text(value, session)?);
        }
        let document_iri = document_iri
            .map(|value| checked_iri(value, session))
            .transpose()?;
        Ok(Self {
            source,
            session,
            namespaces: Vec::new(),
            source_prefixes: BTreeMap::new(),
            prefixes,
            frames: Vec::new(),
            document_iri,
            ontology_iri: None,
            version_iri: None,
            imports: Vec::new(),
            annotations: Vec::new(),
            axioms: Vec::new(),
            occurrences: Vec::new(),
            language_spellings: Vec::new(),
            decoded_codepoints,
            root_seen: false,
            root_closed: false,
            element_count: 0,
            root_member_count: 0,
            root_occurrence_count: 0,
            capture_occurrences,
            preserve_source_map,
            collect_language_spellings,
        })
    }

    fn parse(mut self, text: &str, encoding: XmlSourceEncoding) -> NativeResult<CanonicalDocument> {
        let mut stream = XmlStream::new(text, encoding);
        while let Some(event) = stream.next(self.session)? {
            match event {
                XmlEvent::Start(event) => {
                    self.element_count = self.element_count.checked_add(1).ok_or_else(|| {
                        NativeError::limit("native OWL/XML element count exceeds u64")
                    })?;
                    if self.element_count > self.session.limits().value(LimitKey::MaxTerms) {
                        return Err(self.session.limits().resource_limit(
                            LimitKey::MaxTerms,
                            self.element_count,
                            "native OWL/XML element count exceeds max_terms",
                        ));
                    }
                    let empty = event.empty;
                    let raw_name = event.name.clone();
                    self.start(event)?;
                    if empty {
                        self.end(&raw_name)?;
                    }
                }
                XmlEvent::End(name) => self.end(&name)?,
                XmlEvent::Text(value) => self.text(value)?,
            }
        }
        if !self.root_seen || !self.root_closed || !self.frames.is_empty() {
            return Err(root_error());
        }
        self.session.finish()?;
        Ok(CanonicalDocument {
            document_iri: self.document_iri,
            ontology_iri: self.ontology_iri,
            version_iri: self.version_iri,
            imports: self.imports,
            ontology_annotations: self.annotations,
            axioms: self.axioms,
            extensions: Vec::new(),
            occurrences: self.occurrences,
            language_spellings: self.language_spellings,
            source_blank_labels: Vec::new(),
            source_prefixes: self.source_prefixes.into_iter().collect(),
            source_sha256: sha256(self.source),
            byte_length: u64::try_from(self.source.len())
                .map_err(|_| NativeError::limit("native OWL/XML source length exceeds u64"))?,
            decoded_codepoints: self.decoded_codepoints,
            mapping: MappingEvidence {
                total_triples: 0,
                consumed_triples: 0,
                occurrence_count: self.root_occurrence_count,
                rule_ids: &[],
                unconsumed: Vec::new(),
            },
        })
    }

    fn start(&mut self, event: StartEvent) -> NativeResult<()> {
        if self.root_closed {
            return Err(syntax());
        }
        let namespace_mark = self.namespaces.len();
        for attribute in &event.attributes {
            if attribute.name == "xmlns" {
                self.bind_namespace("", &attribute.value)?;
            } else if let Some(prefix) = attribute.name.strip_prefix("xmlns:") {
                if prefix.is_empty() || prefix.contains(':') {
                    return Err(syntax());
                }
                self.bind_namespace(prefix, &attribute.value)?;
            }
        }
        let root = self.frames.is_empty();
        let (prefix, local) = split_qname(&event.name)?;
        if (prefix.is_empty() || prefix.eq_ignore_ascii_case("xi"))
            && local.eq_ignore_ascii_case("include")
        {
            return Err(forbidden());
        }
        let local = owned_text(local, self.session)?;
        let namespace = match self.resolve_element_namespace(prefix) {
            Ok(value) => value,
            Err(_) if root => return Err(root_error()),
            Err(error) => return Err(error),
        };
        if root {
            if self.root_seen || namespace != OWL || local != "Ontology" {
                return Err(root_error());
            }
            self.root_seen = true;
        } else if namespace != OWL {
            return Err(syntax());
        } else if !self.root_seen {
            return Err(root_error());
        }

        let mut attributes = Vec::new();
        for attribute in event.attributes {
            if attribute.name == "xmlns" || attribute.name.starts_with("xmlns:") {
                continue;
            }
            let (attribute_prefix, attribute_local) = split_qname(&attribute.name)?;
            let attribute_namespace = if attribute_prefix.is_empty() {
                None
            } else {
                Some(self.resolve_attribute_namespace(attribute_prefix)?)
            };
            if attributes.iter().any(|existing: &ExpandedAttribute| {
                existing.namespace == attribute_namespace && existing.local == attribute_local
            }) {
                return Err(syntax());
            }
            reserve_vec_item(&mut attributes, self.session)?;
            attributes.push(ExpandedAttribute {
                namespace: attribute_namespace,
                local: owned_text(attribute_local, self.session)?,
                value: attribute.value,
            });
        }

        let depth = self
            .frames
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native OWL/XML depth overflow"))?;
        enforce(
            self.session,
            LimitKey::MaxNestingDepth,
            depth,
            "native OWL/XML exceeds max_nesting_depth",
        )?;
        let frame = Frame {
            raw_name: event.name,
            local,
            attributes,
            text: String::new(),
            children: Vec::new(),
            namespace_mark,
            root,
        };
        reserve_vec_item(&mut self.frames, self.session)?;
        self.frames.push(frame);

        if root {
            let frame = self.frames.last().ok_or_else(syntax)?;
            self.ontology_iri = optional_checked_attribute(frame, "ontologyIRI", self.session)?;
            self.version_iri = optional_checked_attribute(frame, "versionIRI", self.session)?;
        }
        Ok(())
    }

    fn bind_namespace(&mut self, prefix: &str, value: &str) -> NativeResult<()> {
        if prefix == "xmlns"
            || (prefix == "xml" && value != XML)
            || (prefix != "xml" && value == XML)
            || value == XMLNS
        {
            return Err(syntax());
        }
        if !prefix.is_empty() && value.is_empty() {
            return Err(syntax());
        }
        reserve_vec_item(&mut self.namespaces, self.session)?;
        self.namespaces.push(NamespaceBinding {
            prefix: owned_text(prefix, self.session)?,
            iri: owned_text(value, self.session)?,
        });
        if self.preserve_source_map {
            self.source_prefixes.insert(
                owned_text(prefix, self.session)?,
                owned_text(value, self.session)?,
            );
            enforce(
                self.session,
                LimitKey::MaxPrefixes,
                self.source_prefixes.len(),
                "native OWL/XML source prefix count exceeds max_prefixes",
            )?;
        }
        Ok(())
    }

    fn resolve_element_namespace(&self, prefix: &str) -> NativeResult<&str> {
        if prefix == "xml" {
            return Ok(XML);
        }
        self.namespaces
            .iter()
            .rev()
            .find(|binding| binding.prefix == prefix)
            .map(|binding| binding.iri.as_str())
            .ok_or_else(syntax)
    }

    fn resolve_attribute_namespace(&self, prefix: &str) -> NativeResult<String> {
        if prefix == "xml" {
            return Ok(XML.to_owned());
        }
        self.namespaces
            .iter()
            .rev()
            .find(|binding| binding.prefix == prefix)
            .map(|binding| binding.iri.clone())
            .ok_or_else(syntax)
    }

    fn text(&mut self, value: String) -> NativeResult<()> {
        let Some(frame) = self.frames.last_mut() else {
            if value.trim().is_empty() {
                return Ok(());
            }
            return Err(syntax());
        };
        let following = frame
            .text
            .len()
            .checked_add(value.len())
            .ok_or_else(|| NativeError::limit("native OWL/XML text size overflow"))?;
        if frame.local == "Literal" {
            enforce(
                self.session,
                LimitKey::MaxLiteralBytes,
                following,
                "native OWL/XML literal exceeds max_literal_bytes",
            )?;
        }
        self.session.reserve_bytes(value.len())?;
        frame
            .text
            .try_reserve_exact(value.len())
            .map_err(|_| NativeError::limit("native OWL/XML text allocation failed"))?;
        frame.text.push_str(&value);
        Ok(())
    }

    fn end(&mut self, raw_name: &str) -> NativeResult<()> {
        let frame = self.frames.pop().ok_or_else(syntax)?;
        if frame.raw_name != raw_name {
            return Err(syntax());
        }
        self.namespaces.truncate(frame.namespace_mark);
        if frame.root {
            if frame.local != "Ontology"
                || !frame.children.is_empty()
                || !frame.text.trim().is_empty()
            {
                return Err(syntax());
            }
            self.root_closed = true;
            return Ok(());
        }
        let reduced = self.reduce(frame)?;
        let parent = self.frames.last().ok_or_else(syntax)?;
        if parent.root {
            self.accept_root_child(reduced)
        } else {
            let Reduced::Node(node) = reduced else {
                return Err(syntax());
            };
            let parent = self.frames.last_mut().ok_or_else(syntax)?;
            let following = parent
                .children
                .len()
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native OWL/XML arity overflow"))?;
            enforce(
                self.session,
                LimitKey::MaxSequenceArity,
                following,
                "native OWL/XML collection exceeds max_sequence_arity",
            )?;
            reserve_vec_item(&mut parent.children, self.session)?;
            parent.children.push(node);
            Ok(())
        }
    }

    fn accept_root_child(&mut self, reduced: Reduced) -> NativeResult<()> {
        self.root_member_count = self
            .root_member_count
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native OWL/XML root member count exceeds u64"))?;
        if self.root_member_count > self.session.limits().value(LimitKey::MaxTerms) {
            return Err(self.session.limits().resource_limit(
                LimitKey::MaxTerms,
                self.root_member_count,
                "native OWL/XML root member count exceeds max_terms",
            ));
        }
        match reduced {
            Reduced::Prefix { name, iri } => {
                self.prefixes.insert(name, iri);
                enforce(
                    self.session,
                    LimitKey::MaxPrefixes,
                    self.prefixes.len(),
                    "native OWL/XML prefix count exceeds max_prefixes",
                )
            }
            Reduced::Import(value) => {
                reserve_vec_item(&mut self.imports, self.session)?;
                self.imports.push(value);
                Ok(())
            }
            Reduced::Node(TypedNode {
                kind: NodeKind::Annotation,
                node,
            }) => self.push_root(node, 0),
            Reduced::Axioms(nodes) => {
                for node in nodes {
                    self.push_root(node, 1)?;
                    enforce(
                        self.session,
                        LimitKey::MaxAxioms,
                        self.axioms.len(),
                        "native OWL/XML axiom count exceeds max_axioms",
                    )?;
                }
                Ok(())
            }
            _ => Err(syntax()),
        }
    }

    fn push_root(&mut self, node: Node, collection: u8) -> NativeResult<()> {
        self.root_occurrence_count = self
            .root_occurrence_count
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native OWL/XML occurrence count exceeds u64"))?;
        let row = node.into_bytes();
        if self.capture_occurrences {
            self.session.reserve_bytes(row.len())?;
            reserve_vec_item(&mut self.occurrences, self.session)?;
            self.occurrences.push(CanonicalOccurrence {
                collection,
                row: row.clone(),
            });
        }
        let rows = if collection == 0 {
            &mut self.annotations
        } else {
            &mut self.axioms
        };
        reserve_vec_item(rows, self.session)?;
        rows.push(row);
        Ok(())
    }

    fn reduce(&mut self, mut frame: Frame) -> NativeResult<Reduced> {
        if frame.local != "Literal"
            && frame.local != "IRI"
            && frame.local != "AbbreviatedIRI"
            && frame.local != "Import"
            && !frame.text.trim().is_empty()
        {
            return Err(syntax());
        }
        match frame.local.as_str() {
            "Prefix" => {
                exact_arity(&frame.children, 0)?;
                let name = required_attribute(&frame, "name")?;
                let value = required_attribute(&frame, "IRI")?;
                let value = checked_iri(value, self.session)?;
                Ok(Reduced::Prefix {
                    name: owned_text(name, self.session)?,
                    iri: value,
                })
            }
            "Import" => {
                exact_arity(&frame.children, 0)?;
                let value = checked_iri(frame.text.trim(), self.session)?;
                Ok(Reduced::Import(value))
            }
            "IRI" => {
                exact_arity(&frame.children, 0)?;
                let value = checked_iri(frame.text.trim(), self.session)?;
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::Iri,
                    node: iri(value)?,
                }))
            }
            "AbbreviatedIRI" => {
                exact_arity(&frame.children, 0)?;
                let value = self.expand_iri(frame.text.trim())?;
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::Iri,
                    node: iri(value)?,
                }))
            }
            "Class" | "Datatype" | "ObjectProperty" | "DataProperty" | "AnnotationProperty"
            | "NamedIndividual" => self.reduce_entity(&frame),
            "AnonymousIndividual" => {
                exact_arity(&frame.children, 0)?;
                let value = required_attribute(&frame, "nodeID")?;
                if value.is_empty() {
                    return Err(syntax());
                }
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::AnonymousIndividual,
                    node: anonymous(value)?,
                }))
            }
            "Literal" => {
                exact_arity(&frame.children, 0)?;
                enforce(
                    self.session,
                    LimitKey::MaxLiteralBytes,
                    frame.text.len(),
                    "native OWL/XML literal exceeds max_literal_bytes",
                )?;
                let language = optional_attribute(&frame, "lang")
                    .or_else(|| namespaced_attribute(&frame, XML, "lang"));
                let datatype = optional_attribute(&frame, "datatypeIRI");
                let (datatype, language) = if let Some(language) = language {
                    if !valid_language(language) {
                        return Err(syntax());
                    }
                    if self.collect_language_spellings {
                        self.session.reserve_bytes(size_of::<String>())?;
                        reserve_vec_item(&mut self.language_spellings, self.session)?;
                        self.language_spellings
                            .push(owned_text(language, self.session)?);
                    }
                    (
                        entity("datatype", iri(RDF_PLAIN_LITERAL.to_owned())?)?,
                        Some(owned_ascii_lowercase(language, self.session)?),
                    )
                } else {
                    let datatype = match datatype {
                        Some(value) => checked_iri(value, self.session)?,
                        None => RDF_PLAIN_LITERAL.to_owned(),
                    };
                    (entity("datatype", iri(datatype)?)?, None)
                };
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::Literal,
                    node: literal(frame.text, datatype, language)?,
                }))
            }
            "Annotation" => {
                let (annotations, mut children) =
                    take_leading_annotations(frame.children, self.session)?;
                exact_arity(&children, 2)?;
                let value = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_annotation_property)?;
                require_kind(&value, is_annotation_value)?;
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::Annotation,
                    node: Node::build(
                        5,
                        vec![
                            Field::Node(property.node),
                            Field::Node(value.node),
                            Field::Set(annotations),
                        ],
                    )?,
                }))
            }
            "ObjectInverseOf" => {
                exact_arity(&frame.children, 1)?;
                let child = frame.children.pop().ok_or_else(syntax)?;
                if child.kind != NodeKind::ObjectProperty {
                    return Err(syntax());
                }
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::ObjectInverse,
                    node: Node::build(10, vec![Field::Node(child.node)])?,
                }))
            }
            "ObjectPropertyChain" => {
                require_minimum(&frame.children, 2)?;
                require_all(&frame.children, is_object_property)?;
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::ObjectPropertyChain,
                    node: Node::build(11, vec![Field::Sequence(into_nodes(frame.children))])?,
                }))
            }
            "FacetRestriction" => {
                exact_arity(&frame.children, 1)?;
                let value = frame.children.pop().ok_or_else(syntax)?;
                require_kind(&value, is_literal)?;
                let facet = checked_iri(required_attribute(&frame, "facet")?, self.session)?;
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::Facet,
                    node: Node::build(20, vec![Field::Node(iri(facet)?), Field::Node(value.node)])?,
                }))
            }
            "ObjectIntersectionOf" | "ObjectUnionOf" => {
                require_minimum(&frame.children, 2)?;
                require_all(&frame.children, is_class_expression)?;
                let tag = if frame.local == "ObjectIntersectionOf" {
                    30
                } else {
                    31
                };
                let mut operands = canonical_set(into_nodes(frame.children), 1, Some(tag))?;
                let node = if operands.len() == 1 {
                    operands.pop().ok_or_else(syntax)?
                } else {
                    Node::build(tag, vec![Field::Set(operands)])?
                };
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::ClassExpression,
                    node,
                }))
            }
            "ObjectComplementOf" => {
                exact_arity(&frame.children, 1)?;
                let value = frame.children.pop().ok_or_else(syntax)?;
                require_kind(&value, is_class_expression)?;
                class_expression(32, vec![Field::Node(value.node)])
            }
            "ObjectOneOf" => {
                require_minimum(&frame.children, 1)?;
                require_all(&frame.children, is_individual)?;
                class_expression(
                    33,
                    vec![Field::Set(canonical_set(
                        into_nodes(frame.children),
                        1,
                        None,
                    )?)],
                )
            }
            "ObjectSomeValuesFrom" | "ObjectAllValuesFrom" => {
                exact_arity(&frame.children, 2)?;
                let mut children = frame.children;
                let filler = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_object_property)?;
                require_kind(&filler, is_class_expression)?;
                class_expression(
                    if frame.local == "ObjectSomeValuesFrom" {
                        34
                    } else {
                        35
                    },
                    vec![Field::Node(property.node), Field::Node(filler.node)],
                )
            }
            "ObjectHasValue" => {
                exact_arity(&frame.children, 2)?;
                let mut children = frame.children;
                let value = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_object_property)?;
                require_kind(&value, is_individual)?;
                class_expression(
                    36,
                    vec![Field::Node(property.node), Field::Node(value.node)],
                )
            }
            "ObjectHasSelf" => {
                exact_arity(&frame.children, 1)?;
                let property = frame.children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_object_property)?;
                class_expression(37, vec![Field::Node(property.node)])
            }
            "ObjectMinCardinality" | "ObjectMaxCardinality" | "ObjectExactCardinality" => {
                if !matches!(frame.children.len(), 1 | 2) {
                    return Err(syntax());
                }
                let cardinality = cardinality(&frame, self.session)?;
                let mut children = frame.children;
                let property = children.remove(0);
                require_kind(&property, is_object_property)?;
                let filler = match children.pop() {
                    Some(value) => {
                        require_kind(&value, is_class_expression)?;
                        value.node
                    }
                    None => entity("class", iri(OWL_THING.to_owned())?)?,
                };
                let tag = match frame.local.as_str() {
                    "ObjectMinCardinality" => 38,
                    "ObjectMaxCardinality" => 39,
                    _ => 40,
                };
                class_expression(
                    tag,
                    vec![
                        Field::Integer(cardinality),
                        Field::Node(property.node),
                        Field::Node(filler),
                    ],
                )
            }
            "DataSomeValuesFrom" | "DataAllValuesFrom" => {
                require_minimum(&frame.children, 2)?;
                let mut children = frame.children;
                let filler = children.pop().ok_or_else(syntax)?;
                require_kind(&filler, is_data_range)?;
                require_all(&children, is_data_property)?;
                class_expression(
                    if frame.local == "DataSomeValuesFrom" {
                        41
                    } else {
                        42
                    },
                    vec![
                        Field::Sequence(into_nodes(children)),
                        Field::Node(filler.node),
                    ],
                )
            }
            "DataHasValue" => {
                exact_arity(&frame.children, 2)?;
                let mut children = frame.children;
                let value = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_data_property)?;
                require_kind(&value, is_literal)?;
                class_expression(
                    43,
                    vec![Field::Node(property.node), Field::Node(value.node)],
                )
            }
            "DataMinCardinality" | "DataMaxCardinality" | "DataExactCardinality" => {
                if !matches!(frame.children.len(), 1 | 2) {
                    return Err(syntax());
                }
                let cardinality = cardinality(&frame, self.session)?;
                let mut children = frame.children;
                let property = children.remove(0);
                require_kind(&property, is_data_property)?;
                let filler = match children.pop() {
                    Some(value) => {
                        require_kind(&value, is_data_range)?;
                        value.node
                    }
                    None => entity("datatype", iri(RDFS_LITERAL.to_owned())?)?,
                };
                let tag = match frame.local.as_str() {
                    "DataMinCardinality" => 44,
                    "DataMaxCardinality" => 45,
                    _ => 46,
                };
                class_expression(
                    tag,
                    vec![
                        Field::Integer(cardinality),
                        Field::Node(property.node),
                        Field::Node(filler),
                    ],
                )
            }
            "DataIntersectionOf" | "DataUnionOf" => {
                require_minimum(&frame.children, 2)?;
                require_all(&frame.children, is_data_range)?;
                let tag = if frame.local == "DataIntersectionOf" {
                    21
                } else {
                    22
                };
                let mut operands = canonical_set(into_nodes(frame.children), 1, Some(tag))?;
                let node = if operands.len() == 1 {
                    operands.pop().ok_or_else(syntax)?
                } else {
                    Node::build(tag, vec![Field::Set(operands)])?
                };
                Ok(Reduced::Node(TypedNode {
                    kind: NodeKind::DataRange,
                    node,
                }))
            }
            "DataComplementOf" => {
                exact_arity(&frame.children, 1)?;
                let value = frame.children.pop().ok_or_else(syntax)?;
                require_kind(&value, is_data_range)?;
                data_range(23, vec![Field::Node(value.node)])
            }
            "DataOneOf" => {
                require_minimum(&frame.children, 1)?;
                require_all(&frame.children, is_literal)?;
                data_range(
                    24,
                    vec![Field::Set(canonical_set(
                        into_nodes(frame.children),
                        1,
                        None,
                    )?)],
                )
            }
            "DatatypeRestriction" => {
                require_minimum(&frame.children, 2)?;
                let mut children = frame.children;
                let datatype = children.remove(0);
                if datatype.kind != NodeKind::Datatype {
                    return Err(syntax());
                }
                require_all(&children, is_facet)?;
                data_range(
                    25,
                    vec![
                        Field::Node(datatype.node),
                        Field::Set(canonical_set(into_nodes(children), 1, None)?),
                    ],
                )
            }
            _ if axiom_tag(&frame.local).is_some() => self.reduce_axiom(frame),
            _ => Err(syntax()),
        }
    }

    fn reduce_entity(&mut self, frame: &Frame) -> NativeResult<Reduced> {
        exact_arity(&frame.children, 0)?;
        let direct = optional_attribute(frame, "IRI");
        let abbreviated = optional_attribute(frame, "abbreviatedIRI");
        let value = match (direct, abbreviated) {
            (Some(value), None) => checked_iri(value, self.session)?,
            (None, Some(value)) => self.expand_iri(value)?,
            _ => return Err(syntax()),
        };
        let (kind, canonical_kind) = match frame.local.as_str() {
            "Class" => (NodeKind::Class, "class"),
            "Datatype" => (NodeKind::Datatype, "datatype"),
            "ObjectProperty" => (NodeKind::ObjectProperty, "object_property"),
            "DataProperty" => (NodeKind::DataProperty, "data_property"),
            "AnnotationProperty" => (NodeKind::AnnotationProperty, "annotation_property"),
            "NamedIndividual" => (NodeKind::NamedIndividual, "named_individual"),
            _ => return Err(syntax()),
        };
        Ok(Reduced::Node(TypedNode {
            kind,
            node: entity(canonical_kind, iri(value)?)?,
        }))
    }

    fn expand_iri(&mut self, value: &str) -> NativeResult<String> {
        let (prefix, local) = value.split_once(':').ok_or_else(syntax)?;
        let mut key = owned_text(prefix, self.session)?;
        self.session.reserve_bytes(1)?;
        key.push(':');
        let base = self.prefixes.get(&key).ok_or_else(syntax)?;
        let length = base
            .len()
            .checked_add(local.len())
            .ok_or_else(|| NativeError::limit("native OWL/XML IRI size overflow"))?;
        self.session.reserve_bytes(length)?;
        let mut expanded = String::new();
        expanded
            .try_reserve_exact(length)
            .map_err(|_| NativeError::limit("native OWL/XML IRI allocation failed"))?;
        expanded.push_str(base);
        expanded.push_str(local);
        checked_iri(&expanded, self.session)
    }

    fn reduce_axiom(&mut self, frame: Frame) -> NativeResult<Reduced> {
        let tag = axiom_tag(&frame.local).ok_or_else(syntax)?;
        let (annotations, mut children) = take_leading_annotations(frame.children, self.session)?;
        if frame.local == "DisjointClasses" {
            require_minimum(&children, 2)?;
            require_all(&children, is_class_expression)?;
            return Ok(Reduced::Axioms(disjoint_class_axioms(
                into_nodes(children),
                annotations,
            )?));
        }
        let fields = match frame.local.as_str() {
            "Declaration" => {
                exact_arity(&children, 1)?;
                let value = children.pop().ok_or_else(syntax)?;
                require_kind(&value, is_declaration_entity)?;
                vec![Field::Node(value.node), Field::Set(annotations)]
            }
            "SubClassOf" => {
                exact_arity(&children, 2)?;
                require_all(&children, is_class_expression)?;
                let mut values = into_nodes(children);
                let second = values.pop().ok_or_else(syntax)?;
                let first = values.pop().ok_or_else(syntax)?;
                vec![
                    Field::Node(first),
                    Field::Node(second),
                    Field::Set(annotations),
                ]
            }
            "EquivalentClasses" => {
                require_minimum(&children, 2)?;
                require_all(&children, is_class_expression)?;
                vec![
                    Field::Set(canonical_set(into_nodes(children), 2, None)?),
                    Field::Set(annotations),
                ]
            }
            "DisjointUnion" => {
                require_minimum(&children, 3)?;
                let first = children.remove(0);
                if first.kind != NodeKind::Class {
                    return Err(syntax());
                }
                require_all(&children, is_class_expression)?;
                vec![
                    Field::Node(first.node),
                    Field::Set(canonical_set(into_nodes(children), 2, None)?),
                    Field::Set(annotations),
                ]
            }
            "SubObjectPropertyOf" => {
                exact_arity(&children, 2)?;
                let second = children.pop().ok_or_else(syntax)?;
                let first = children.pop().ok_or_else(syntax)?;
                require_kind(&first, is_sub_object_property)?;
                require_kind(&second, is_object_property)?;
                vec![
                    Field::Node(first.node),
                    Field::Node(second.node),
                    Field::Set(annotations),
                ]
            }
            "EquivalentObjectProperties" | "DisjointObjectProperties" => {
                require_minimum(&children, 2)?;
                require_all(&children, is_object_property)?;
                vec![
                    Field::Set(canonical_set(into_nodes(children), 2, None)?),
                    Field::Set(annotations),
                ]
            }
            "InverseObjectProperties" => {
                exact_arity(&children, 2)?;
                require_all(&children, is_object_property)?;
                let mut values = into_nodes(children);
                let mut second = values.pop().ok_or_else(syntax)?;
                let mut first = values.pop().ok_or_else(syntax)?;
                if second.as_bytes() < first.as_bytes() {
                    std::mem::swap(&mut first, &mut second);
                }
                vec![
                    Field::Node(first),
                    Field::Node(second),
                    Field::Set(annotations),
                ]
            }
            "ObjectPropertyDomain" | "ObjectPropertyRange" => {
                exact_arity(&children, 2)?;
                let expression = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_object_property)?;
                require_kind(&expression, is_class_expression)?;
                vec![
                    Field::Node(property.node),
                    Field::Node(expression.node),
                    Field::Set(annotations),
                ]
            }
            name if object_characteristic(name) => {
                exact_arity(&children, 1)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_object_property)?;
                vec![Field::Node(property.node), Field::Set(annotations)]
            }
            "SubDataPropertyOf" => {
                exact_arity(&children, 2)?;
                require_all(&children, is_data_property)?;
                let mut values = into_nodes(children);
                let second = values.pop().ok_or_else(syntax)?;
                let first = values.pop().ok_or_else(syntax)?;
                vec![
                    Field::Node(first),
                    Field::Node(second),
                    Field::Set(annotations),
                ]
            }
            "EquivalentDataProperties" | "DisjointDataProperties" => {
                require_minimum(&children, 2)?;
                require_all(&children, is_data_property)?;
                vec![
                    Field::Set(canonical_set(into_nodes(children), 2, None)?),
                    Field::Set(annotations),
                ]
            }
            "DataPropertyDomain" => {
                exact_arity(&children, 2)?;
                let expression = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_data_property)?;
                require_kind(&expression, is_class_expression)?;
                vec![
                    Field::Node(property.node),
                    Field::Node(expression.node),
                    Field::Set(annotations),
                ]
            }
            "DataPropertyRange" => {
                exact_arity(&children, 2)?;
                let range = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_data_property)?;
                require_kind(&range, is_data_range)?;
                vec![
                    Field::Node(property.node),
                    Field::Node(range.node),
                    Field::Set(annotations),
                ]
            }
            "FunctionalDataProperty" => {
                exact_arity(&children, 1)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_data_property)?;
                vec![Field::Node(property.node), Field::Set(annotations)]
            }
            "DatatypeDefinition" => {
                exact_arity(&children, 2)?;
                let range = children.pop().ok_or_else(syntax)?;
                let datatype = children.pop().ok_or_else(syntax)?;
                if datatype.kind != NodeKind::Datatype {
                    return Err(syntax());
                }
                require_kind(&range, is_data_range)?;
                vec![
                    Field::Node(datatype.node),
                    Field::Node(range.node),
                    Field::Set(annotations),
                ]
            }
            "HasKey" => {
                require_minimum(&children, 2)?;
                let expression = children.remove(0);
                require_kind(&expression, is_class_expression)?;
                let mut object_properties = Vec::new();
                let mut data_properties = Vec::new();
                for property in children {
                    if is_object_property(property.kind) {
                        reserve_vec_item(&mut object_properties, self.session)?;
                        object_properties.push(property.node);
                    } else if is_data_property(property.kind) {
                        reserve_vec_item(&mut data_properties, self.session)?;
                        data_properties.push(property.node);
                    } else {
                        return Err(syntax());
                    }
                }
                vec![
                    Field::Node(expression.node),
                    Field::Set(canonical_set(object_properties, 0, None)?),
                    Field::Set(canonical_set(data_properties, 0, None)?),
                    Field::Set(annotations),
                ]
            }
            "SameIndividual" | "DifferentIndividuals" => {
                require_minimum(&children, 2)?;
                require_all(&children, is_individual)?;
                vec![
                    Field::Set(canonical_set(into_nodes(children), 2, None)?),
                    Field::Set(annotations),
                ]
            }
            "ClassAssertion" => {
                exact_arity(&children, 2)?;
                let individual = children.pop().ok_or_else(syntax)?;
                let expression = children.pop().ok_or_else(syntax)?;
                require_kind(&expression, is_class_expression)?;
                require_kind(&individual, is_individual)?;
                vec![
                    Field::Node(expression.node),
                    Field::Node(individual.node),
                    Field::Set(annotations),
                ]
            }
            "ObjectPropertyAssertion" | "NegativeObjectPropertyAssertion" => {
                exact_arity(&children, 3)?;
                let target = children.pop().ok_or_else(syntax)?;
                let source = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_object_property)?;
                require_kind(&source, is_individual)?;
                require_kind(&target, is_individual)?;
                vec![
                    Field::Node(property.node),
                    Field::Node(source.node),
                    Field::Node(target.node),
                    Field::Set(annotations),
                ]
            }
            "DataPropertyAssertion" | "NegativeDataPropertyAssertion" => {
                exact_arity(&children, 3)?;
                let value = children.pop().ok_or_else(syntax)?;
                let source = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_data_property)?;
                require_kind(&source, is_individual)?;
                require_kind(&value, is_literal)?;
                vec![
                    Field::Node(property.node),
                    Field::Node(source.node),
                    Field::Node(value.node),
                    Field::Set(annotations),
                ]
            }
            "AnnotationAssertion" => {
                exact_arity(&children, 3)?;
                let value = children.pop().ok_or_else(syntax)?;
                let subject = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_annotation_property)?;
                require_kind(&subject, is_annotation_subject)?;
                require_kind(&value, is_annotation_value)?;
                vec![
                    Field::Node(property.node),
                    Field::Node(subject.node),
                    Field::Node(value.node),
                    Field::Set(annotations),
                ]
            }
            "SubAnnotationPropertyOf" => {
                exact_arity(&children, 2)?;
                require_all(&children, is_annotation_property)?;
                let mut values = into_nodes(children);
                let second = values.pop().ok_or_else(syntax)?;
                let first = values.pop().ok_or_else(syntax)?;
                vec![
                    Field::Node(first),
                    Field::Node(second),
                    Field::Set(annotations),
                ]
            }
            "AnnotationPropertyDomain" | "AnnotationPropertyRange" => {
                exact_arity(&children, 2)?;
                let value = children.pop().ok_or_else(syntax)?;
                let property = children.pop().ok_or_else(syntax)?;
                require_kind(&property, is_annotation_property)?;
                require_kind(&value, is_iri)?;
                vec![
                    Field::Node(property.node),
                    Field::Node(value.node),
                    Field::Set(annotations),
                ]
            }
            _ => return Err(syntax()),
        };
        Ok(Reduced::Axioms(vec![Node::build(tag, fields)?]))
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn parse_and_map(
    source: &[u8],
    document_iri: Option<&str>,
    capture_occurrences: bool,
    preserve_source_map: bool,
    collect_language_spellings: bool,
    session: &mut Session<'_>,
) -> NativeResult<CanonicalDocument> {
    check_source(source, session)?;
    let (text, mut decoded_codepoints, encoding) = decode_xml(source, session)?;
    let utf8_bom = encoding == XmlSourceEncoding::Utf8 && source.starts_with(&[0xef, 0xbb, 0xbf]);
    let text = if utf8_bom {
        text.strip_prefix('\u{feff}').ok_or_else(syntax)?
    } else {
        &text
    };
    decoded_codepoints = decoded_codepoints.saturating_sub(u64::from(utf8_bom));
    Parser::new(
        source,
        document_iri,
        decoded_codepoints,
        capture_occurrences,
        preserve_source_map,
        collect_language_spellings,
        session,
    )?
    .parse(text, encoding)
}

fn class_expression(tag: u64, fields: Vec<Field>) -> NativeResult<Reduced> {
    Ok(Reduced::Node(TypedNode {
        kind: NodeKind::ClassExpression,
        node: Node::build(tag, fields)?,
    }))
}

fn data_range(tag: u64, fields: Vec<Field>) -> NativeResult<Reduced> {
    Ok(Reduced::Node(TypedNode {
        kind: NodeKind::DataRange,
        node: Node::build(tag, fields)?,
    }))
}

fn into_nodes(values: Vec<TypedNode>) -> Vec<Node> {
    values.into_iter().map(|value| value.node).collect()
}

fn exact_arity(values: &[TypedNode], expected: usize) -> NativeResult<()> {
    if values.len() == expected {
        Ok(())
    } else {
        Err(syntax())
    }
}

fn require_minimum(values: &[TypedNode], minimum: usize) -> NativeResult<()> {
    if values.len() >= minimum {
        Ok(())
    } else {
        Err(syntax())
    }
}

fn require_kind(value: &TypedNode, predicate: fn(NodeKind) -> bool) -> NativeResult<()> {
    if predicate(value.kind) {
        Ok(())
    } else {
        Err(syntax())
    }
}

fn require_all(values: &[TypedNode], predicate: fn(NodeKind) -> bool) -> NativeResult<()> {
    if values.iter().all(|value| predicate(value.kind)) {
        Ok(())
    } else {
        Err(syntax())
    }
}

fn is_iri(kind: NodeKind) -> bool {
    kind == NodeKind::Iri
}

fn is_class_expression(kind: NodeKind) -> bool {
    matches!(kind, NodeKind::Class | NodeKind::ClassExpression)
}

fn is_data_range(kind: NodeKind) -> bool {
    matches!(kind, NodeKind::Datatype | NodeKind::DataRange)
}

fn is_object_property(kind: NodeKind) -> bool {
    matches!(kind, NodeKind::ObjectProperty | NodeKind::ObjectInverse)
}

fn is_sub_object_property(kind: NodeKind) -> bool {
    is_object_property(kind) || kind == NodeKind::ObjectPropertyChain
}

fn is_data_property(kind: NodeKind) -> bool {
    kind == NodeKind::DataProperty
}

fn is_annotation_property(kind: NodeKind) -> bool {
    kind == NodeKind::AnnotationProperty
}

fn is_individual(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::NamedIndividual | NodeKind::AnonymousIndividual
    )
}

fn is_literal(kind: NodeKind) -> bool {
    kind == NodeKind::Literal
}

fn is_facet(kind: NodeKind) -> bool {
    kind == NodeKind::Facet
}

fn is_annotation_subject(kind: NodeKind) -> bool {
    matches!(kind, NodeKind::Iri | NodeKind::AnonymousIndividual)
}

fn is_annotation_value(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::Iri | NodeKind::AnonymousIndividual | NodeKind::Literal
    )
}

fn is_declaration_entity(kind: NodeKind) -> bool {
    matches!(
        kind,
        NodeKind::Class
            | NodeKind::Datatype
            | NodeKind::ObjectProperty
            | NodeKind::DataProperty
            | NodeKind::AnnotationProperty
            | NodeKind::NamedIndividual
    )
}

fn take_leading_annotations(
    mut children: Vec<TypedNode>,
    session: &Session<'_>,
) -> NativeResult<(Vec<Node>, Vec<TypedNode>)> {
    let count = children
        .iter()
        .take_while(|value| value.kind == NodeKind::Annotation)
        .count();
    enforce(
        session,
        LimitKey::MaxAnnotations,
        count,
        "native OWL/XML annotation count exceeds max_annotations",
    )?;
    let remaining = children.split_off(count);
    let annotations = canonical_set(into_nodes(children), 0, None)?;
    Ok((annotations, remaining))
}

fn disjoint_class_axioms(
    mut expressions: Vec<Node>,
    annotations: Vec<Node>,
) -> NativeResult<Vec<Node>> {
    if expressions.len() < 2 {
        return Err(syntax());
    }
    expressions.sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    let mut repeated = Vec::new();
    for index in 1..expressions.len() {
        if expressions[index - 1].as_bytes() == expressions[index].as_bytes()
            && (index == 1 || expressions[index - 2].as_bytes() != expressions[index].as_bytes())
        {
            repeated.push(expressions[index].clone());
        }
    }
    expressions.dedup_by(|left, right| left.as_bytes() == right.as_bytes());
    let mut axioms = Vec::new();
    if expressions.len() >= 2 {
        axioms.push(Node::build(
            63,
            vec![Field::Set(expressions), Field::Set(annotations.clone())],
        )?);
    }
    if !repeated.is_empty() {
        let nothing = entity("class", iri(OWL_NOTHING.to_owned())?)?;
        for expression in repeated {
            axioms.push(Node::build(
                61,
                vec![
                    Field::Node(expression),
                    Field::Node(nothing.clone()),
                    Field::Set(annotations.clone()),
                ],
            )?);
        }
    }
    Ok(axioms)
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

fn optional_attribute<'a>(frame: &'a Frame, local: &str) -> Option<&'a str> {
    frame
        .attributes
        .iter()
        .find(|attribute| attribute.namespace.is_none() && attribute.local == local)
        .map(|attribute| attribute.value.as_str())
}

fn namespaced_attribute<'a>(frame: &'a Frame, namespace: &str, local: &str) -> Option<&'a str> {
    frame
        .attributes
        .iter()
        .find(|attribute| {
            attribute.namespace.as_deref() == Some(namespace) && attribute.local == local
        })
        .map(|attribute| attribute.value.as_str())
}

fn required_attribute<'a>(frame: &'a Frame, local: &str) -> NativeResult<&'a str> {
    optional_attribute(frame, local).ok_or_else(syntax)
}

fn optional_checked_attribute(
    frame: &Frame,
    local: &str,
    session: &mut Session<'_>,
) -> NativeResult<Option<String>> {
    optional_attribute(frame, local)
        .map(|value| checked_iri(value, session))
        .transpose()
}

fn cardinality(frame: &Frame, session: &mut Session<'_>) -> NativeResult<String> {
    let value = required_attribute(frame, "cardinality")?;
    if value.is_empty() || !value.bytes().all(|value| value.is_ascii_digit()) {
        return Err(syntax());
    }
    owned_text(value, session)
}

fn checked_iri(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    enforce(
        session,
        LimitKey::MaxIriBytes,
        value.len(),
        "native OWL/XML IRI exceeds max_iri_bytes",
    )?;
    crate::model::validate_iri(value).map_err(|error| {
        if error.code == "NATIVE_WIRE_CORRUPTION" {
            syntax()
        } else {
            error
        }
    })?;
    owned_text(value, session)
}

fn split_qname(value: &str) -> NativeResult<(&str, &str)> {
    match value.split_once(':') {
        Some((prefix, local))
            if !prefix.is_empty()
                && !local.is_empty()
                && !local.contains(':')
                && is_xml_ncname(prefix)
                && is_xml_ncname(local) =>
        {
            Ok((prefix, local))
        }
        None if is_xml_ncname(value) => Ok(("", value)),
        _ => Err(syntax()),
    }
}

fn check_source(source: &[u8], session: &Session<'_>) -> NativeResult<()> {
    let size = u64::try_from(source.len())
        .map_err(|_| NativeError::limit("native OWL/XML source length exceeds u64"))?;
    for (key, message) in [
        (
            LimitKey::MaxSourceBytes,
            "native OWL/XML source exceeds max_source_bytes",
        ),
        (
            LimitKey::MaxTotalSourceBytes,
            "native OWL/XML source exceeds max_total_source_bytes",
        ),
    ] {
        if size > session.limits().value(key) {
            return Err(session.limits().resource_limit(key, size, message));
        }
    }
    Ok(())
}

fn enforce(
    session: &Session<'_>,
    key: LimitKey,
    observed: usize,
    message: &'static str,
) -> NativeResult<()> {
    let observed = u64::try_from(observed)
        .map_err(|_| NativeError::limit("native OWL/XML observation exceeds u64"))?;
    if observed > session.limits().value(key) {
        Err(session.limits().resource_limit(key, observed, message))
    } else {
        Ok(())
    }
}

fn owned_text(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native OWL/XML token allocation failed"))?;
    output.push_str(value);
    Ok(output)
}

fn owned_ascii_lowercase(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    let mut output = owned_text(value, session)?;
    output.make_ascii_lowercase();
    Ok(output)
}

fn reserve_vec_item<T>(values: &mut Vec<T>, session: &mut Session<'_>) -> NativeResult<()> {
    if values.len() == values.capacity() {
        session.reserve_bytes(size_of::<T>())?;
        values
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native OWL/XML table allocation failed"))?;
    }
    Ok(())
}

fn syntax() -> NativeError {
    NativeError::new("NATIVE_OWLXML_SYNTAX", "native OWL/XML source is malformed")
}

fn root_error() -> NativeError {
    NativeError::new(
        "NATIVE_OWLXML_ROOT",
        "native OWL/XML root must be owl:Ontology",
    )
}

fn forbidden() -> NativeError {
    NativeError::new(
        "NATIVE_XML_FORBIDDEN_CONSTRUCT",
        "native XML forbidden construct is disabled",
    )
}

#[derive(Clone, Copy)]
enum XmlValueKind {
    Text,
    Attribute,
}

fn decode_references(
    value: &str,
    kind: XmlValueKind,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native OWL/XML reference allocation failed"))?;
    let mut cursor = 0;
    while let Some(relative) = value[cursor..].find('&') {
        let start = cursor + relative;
        append_normalized_xml_characters(&mut output, &value[cursor..start], kind)?;
        let end = value[start + 1..]
            .find(';')
            .map(|offset| start + 1 + offset)
            .ok_or_else(syntax)?;
        let reference = &value[start + 1..end];
        match reference {
            "amp" => output.push('&'),
            "lt" => output.push('<'),
            "gt" => output.push('>'),
            "apos" => output.push('\''),
            "quot" => output.push('"'),
            _ if reference.starts_with("#x") => {
                if reference.len() == 2 {
                    return Err(syntax());
                }
                let value = u32::from_str_radix(&reference[2..], 16).map_err(|_| syntax())?;
                output.push(xml_character(value)?);
            }
            _ if reference.starts_with('#') => {
                if reference.len() == 1 {
                    return Err(syntax());
                }
                let value = reference[1..].parse::<u32>().map_err(|_| syntax())?;
                output.push(xml_character(value)?);
            }
            _ if is_xml_name(reference) => return Err(forbidden()),
            _ => return Err(syntax()),
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
        .map_err(|_| NativeError::limit("native OWL/XML value allocation failed"))?;
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
            return Err(syntax());
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
        Err(syntax())
    }
}

fn xml_character(value: u32) -> NativeResult<char> {
    let character = char::from_u32(value).ok_or_else(syntax)?;
    if !is_xml_character(value) {
        return Err(syntax());
    }
    Ok(character)
}

fn is_xml_character(value: u32) -> bool {
    matches!(
        value,
        0x09 | 0x0a | 0x0d | 0x20..=0xd7ff | 0xe000..=0xfffd | 0x10000..=0x10ffff
    )
}

fn scan_name(text: &str, start: usize) -> NativeResult<usize> {
    let suffix = text.get(start..).ok_or_else(syntax)?;
    let mut characters = suffix.char_indices();
    let (_, first) = characters.next().ok_or_else(syntax)?;
    if !is_xml_name_start(first) {
        return Err(syntax());
    }
    let mut end = start
        .checked_add(first.len_utf8())
        .ok_or_else(|| NativeError::limit("native OWL/XML name offset overflow"))?;
    for (offset, character) in characters {
        if !is_xml_name_character(character) {
            break;
        }
        end = start
            .checked_add(offset)
            .and_then(|value| value.checked_add(character.len_utf8()))
            .ok_or_else(|| NativeError::limit("native OWL/XML name offset overflow"))?;
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
        return Err(syntax());
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
        session.step(
            u64::try_from(batch_end.saturating_sub(cursor))
                .map_err(|_| NativeError::limit("native OWL/XML scan work exceeds u64"))?,
        )?;
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

fn is_xml_space(value: u8) -> bool {
    matches!(value, b' ' | b'\t' | b'\r' | b'\n')
}

fn validate_xml_declaration(
    declaration: &str,
    source_encoding: XmlSourceEncoding,
) -> NativeResult<()> {
    validate_xml_characters(declaration)?;
    let bytes = declaration.as_bytes();
    if !declaration.starts_with("xml") || !bytes.get(3).is_some_and(|value| is_xml_space(*value)) {
        return Err(syntax());
    }
    let mut cursor = 3;
    skip_space(bytes, &mut cursor);
    let (name, version) = xml_declaration_attribute(declaration, &mut cursor)?;
    if name != "version" || version != "1.0" {
        return Err(syntax());
    }
    let mut encoding_seen = false;
    let mut standalone_seen = false;
    while cursor < bytes.len() {
        if !is_xml_space(bytes[cursor]) {
            return Err(syntax());
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
                    return Err(NativeError::new(
                        "NATIVE_FORMAT_ENCODING",
                        "native OWL/XML declaration conflicts with source encoding",
                    ));
                }
            }
            "standalone" if !standalone_seen => {
                standalone_seen = true;
                if !matches!(value, "yes" | "no") {
                    return Err(syntax());
                }
            }
            _ => return Err(syntax()),
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
        return Err(syntax());
    }
    let name = &declaration[name_start..*cursor];
    skip_space(bytes, cursor);
    if bytes.get(*cursor) != Some(&b'=') {
        return Err(syntax());
    }
    *cursor += 1;
    skip_space(bytes, cursor);
    let quote = *bytes.get(*cursor).ok_or_else(syntax)?;
    if !matches!(quote, b'\'' | b'"') {
        return Err(syntax());
    }
    *cursor += 1;
    let value_start = *cursor;
    while bytes.get(*cursor).is_some_and(|value| *value != quote) {
        if matches!(bytes[*cursor], b'<' | b'&') {
            return Err(syntax());
        }
        *cursor += 1;
    }
    if bytes.get(*cursor) != Some(&quote) {
        return Err(syntax());
    }
    let value = &declaration[value_start..*cursor];
    *cursor += 1;
    Ok((name, value))
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
            "native OWL/XML source uses unsupported UTF-32 encoding",
        ));
    }
    if let Some(content) = source.strip_prefix(&[0xff, 0xfe]) {
        let (text, decoded) = decode_utf16(content, true, session)?;
        return Ok((text, decoded, XmlSourceEncoding::Utf16Le));
    }
    if let Some(content) = source.strip_prefix(&[0xfe, 0xff]) {
        let (text, decoded) = decode_utf16(content, false, session)?;
        return Ok((text, decoded, XmlSourceEncoding::Utf16Be));
    }
    if source.starts_with(&[b'<', 0x00]) {
        let (text, decoded) = decode_utf16(source, true, session)?;
        return Ok((text, decoded, XmlSourceEncoding::Utf16Le));
    }
    if source.starts_with(&[0x00, b'<']) {
        let (text, decoded) = decode_utf16(source, false, session)?;
        return Ok((text, decoded, XmlSourceEncoding::Utf16Be));
    }
    let (text, decoded) = decode_utf8(source, session)?;
    Ok((text, decoded, XmlSourceEncoding::Utf8))
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
            "native OWL/XML source has a truncated UTF-16 code unit",
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
                "native OWL/XML source contains an invalid UTF-16 surrogate",
            )
        })?;
        output_bytes = output_bytes
            .checked_add(character.len_utf8())
            .ok_or_else(|| NativeError::limit("native OWL/XML decode allocation overflow"))?;
        codepoints = codepoints
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native decoded OWL/XML length overflow"))?;
        session.step(1)?;
    }
    session.reserve_bytes(output_bytes)?;
    let mut output = String::new();
    output
        .try_reserve_exact(output_bytes)
        .map_err(|_| NativeError::limit("native OWL/XML decode allocation failed"))?;
    for (index, decoded) in char::decode_utf16(code_units()).enumerate() {
        if index % (32 * 1024) == 0 {
            session.finish()?;
        }
        output.push(decoded.map_err(|_| {
            NativeError::new(
                "NATIVE_FORMAT_ENCODING",
                "native OWL/XML source contains an invalid UTF-16 surrogate",
            )
        })?);
    }
    session.finish()?;
    Ok((output, codepoints))
}

fn decode_utf8(source: &[u8], session: &mut Session<'_>) -> NativeResult<(String, u64)> {
    session.finish()?;
    session.reserve_bytes(source.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(source.len())
        .map_err(|_| NativeError::limit("native OWL/XML decode allocation failed"))?;
    let mut start = 0_usize;
    let mut codepoints = 0_u64;
    while start < source.len() {
        let mut end = start.saturating_add(64 * 1024).min(source.len());
        while end < source.len() && source[end] & 0xc0 == 0x80 {
            end = end.checked_sub(1).ok_or_else(|| {
                NativeError::new(
                    "NATIVE_FORMAT_ENCODING",
                    "native OWL/XML source is not valid UTF-8",
                )
            })?;
        }
        if end == start {
            return Err(NativeError::new(
                "NATIVE_FORMAT_ENCODING",
                "native OWL/XML source is not valid UTF-8",
            ));
        }
        let fragment = std::str::from_utf8(&source[start..end]).map_err(|_| {
            NativeError::new(
                "NATIVE_FORMAT_ENCODING",
                "native OWL/XML source is not valid UTF-8",
            )
        })?;
        let count = u64::try_from(fragment.chars().count())
            .map_err(|_| NativeError::limit("native decoded OWL/XML length exceeds u64"))?;
        codepoints = codepoints
            .checked_add(count)
            .ok_or_else(|| NativeError::limit("native decoded OWL/XML length overflow"))?;
        session.step(count)?;
        output.push_str(fragment);
        start = end;
    }
    session.finish()?;
    Ok((output, codepoints))
}

fn valid_language(value: &str) -> bool {
    if value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
        || value.starts_with('-')
        || value.ends_with('-')
        || value.contains("--")
    {
        return false;
    }
    let lowered = value.to_ascii_lowercase();
    const GRANDFATHERED: [&str; 26] = [
        "art-lojban",
        "cel-gaulish",
        "en-gb-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "no-bok",
        "no-nyn",
        "sgn-be-fr",
        "sgn-be-nl",
        "sgn-ch-de",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    ];
    if GRANDFATHERED.contains(&lowered.as_str()) || lowered.starts_with("x-") {
        return true;
    }
    let parts: Vec<&str> = lowered.split('-').collect();
    let first = parts[0];
    if !(2..=8).contains(&first.len()) || !first.bytes().all(|byte| byte.is_ascii_alphabetic()) {
        return false;
    }
    let mut index = 1;
    if matches!(first.len(), 2 | 3) {
        let mut extlangs = 0;
        while index < parts.len()
            && parts[index].len() == 3
            && parts[index].bytes().all(|byte| byte.is_ascii_alphabetic())
            && extlangs < 3
        {
            index += 1;
            extlangs += 1;
        }
    }
    if index < parts.len()
        && parts[index].len() == 4
        && parts[index].bytes().all(|byte| byte.is_ascii_alphabetic())
    {
        index += 1;
    }
    if index < parts.len()
        && ((parts[index].len() == 2
            && parts[index].bytes().all(|byte| byte.is_ascii_alphabetic()))
            || (parts[index].len() == 3 && parts[index].bytes().all(|byte| byte.is_ascii_digit())))
    {
        index += 1;
    }
    let mut variants = Vec::new();
    while index < parts.len()
        && ((5..=8).contains(&parts[index].len())
            || (parts[index].len() == 4
                && parts[index]
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_digit)))
    {
        if variants.contains(&parts[index]) {
            return false;
        }
        variants.push(parts[index]);
        index += 1;
    }
    let mut singletons = Vec::new();
    while index < parts.len() && parts[index].len() == 1 && parts[index] != "x" {
        if !parts[index]
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric())
            || singletons.contains(&parts[index])
        {
            return false;
        }
        singletons.push(parts[index]);
        index += 1;
        let start = index;
        while index < parts.len()
            && (2..=8).contains(&parts[index].len())
            && parts[index]
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric())
        {
            index += 1;
        }
        if index == start {
            return false;
        }
    }
    if index < parts.len() && parts[index] == "x" {
        index += 1;
        let start = index;
        while index < parts.len()
            && (1..=8).contains(&parts[index].len())
            && parts[index]
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric())
        {
            index += 1;
        }
        if index == start {
            return false;
        }
    }
    index == parts.len()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::{Cancellation, Guard};
    use crate::limits::{Limits, CONFIG_BYTES, CONFIG_MAGIC, CONFIG_SCHEMA};
    use crate::source::SourceRequest;
    use std::time::Duration;

    fn mapped_with(
        source: &[u8],
        limits: &Limits,
        cancellation: Cancellation,
    ) -> NativeResult<CanonicalDocument> {
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session = Session::new(&mut guard, limits, source.len())?;
        parse_and_map(source, None, true, true, true, &mut session)
    }

    fn mapped(source: &str) -> NativeResult<CanonicalDocument> {
        mapped_with(
            source.as_bytes(),
            &Limits::default(),
            Cancellation::with_duration(None),
        )
    }

    fn ontology(member: &str) -> String {
        format!("<Ontology xmlns=\"{OWL}\">{member}</Ontology>")
    }

    fn functional_axioms(source: &str) -> Vec<Vec<u8>> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session =
            Session::new(&mut guard, &limits, source.len()).expect("functional session");
        let encoded = crate::parse::parse(
            SourceRequest {
                source: source.as_bytes(),
                allow_swrl: false,
            },
            &mut session,
        )
        .expect("functional parity source");
        decode_axioms(&encoded)
    }

    fn decode_axioms(encoded: &[u8]) -> Vec<Vec<u8>> {
        let mut cursor = 20;
        for _ in 0..2 {
            let marker = encoded[cursor];
            cursor += 1;
            if marker == 1 {
                let _ = frame(encoded, &mut cursor);
            }
        }
        let imports = take_u64(encoded, &mut cursor);
        for _ in 0..imports {
            let _ = frame(encoded, &mut cursor);
        }
        let annotations = take_u64(encoded, &mut cursor);
        for _ in 0..annotations {
            cursor += 32;
            let _ = frame(encoded, &mut cursor);
        }
        let axioms = take_u64(encoded, &mut cursor);
        (0..axioms)
            .map(|_| {
                cursor += 32;
                frame(encoded, &mut cursor)
            })
            .collect()
    }

    fn take_u64(encoded: &[u8], cursor: &mut usize) -> usize {
        let value =
            u64::from_le_bytes(encoded[*cursor..*cursor + 8].try_into().expect("u64 frame"));
        *cursor += 8;
        usize::try_from(value).expect("usize count")
    }

    fn frame(encoded: &[u8], cursor: &mut usize) -> Vec<u8> {
        let mut length = 0_usize;
        let mut shift = 0;
        loop {
            let byte = encoded[*cursor];
            *cursor += 1;
            length |= usize::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                break;
            }
            shift += 7;
        }
        let value = encoded[*cursor..*cursor + length].to_vec();
        *cursor += length;
        value
    }

    fn assert_parity(xml_member: &str, functional_member: &str) {
        let xml = ontology(xml_member);
        let document = mapped(&xml).expect(xml_member);
        let functional = functional_axioms(&format!("Ontology({functional_member})"));
        assert_eq!(document.axioms, functional, "{xml_member}");
    }

    #[test]
    fn axiom_constructor_surface_matches_functional_canonical_rows() {
        let cases = [
            (
                r#"<Declaration><Class IRI="urn:C"/></Declaration>"#,
                "Declaration(Class(<urn:C>))",
            ),
            (
                r#"<Declaration><Datatype IRI="urn:D"/></Declaration>"#,
                "Declaration(Datatype(<urn:D>))",
            ),
            (
                r#"<Declaration><ObjectProperty IRI="urn:p"/></Declaration>"#,
                "Declaration(ObjectProperty(<urn:p>))",
            ),
            (
                r#"<Declaration><DataProperty IRI="urn:p"/></Declaration>"#,
                "Declaration(DataProperty(<urn:p>))",
            ),
            (
                r#"<Declaration><AnnotationProperty IRI="urn:p"/></Declaration>"#,
                "Declaration(AnnotationProperty(<urn:p>))",
            ),
            (
                r#"<Declaration><NamedIndividual IRI="urn:i"/></Declaration>"#,
                "Declaration(NamedIndividual(<urn:i>))",
            ),
            (
                r#"<SubClassOf><Class IRI="urn:C"/><Class IRI="urn:D"/></SubClassOf>"#,
                "SubClassOf(<urn:C> <urn:D>)",
            ),
            (
                r#"<EquivalentClasses><Class IRI="urn:C"/><Class IRI="urn:D"/></EquivalentClasses>"#,
                "EquivalentClasses(<urn:C> <urn:D>)",
            ),
            (
                r#"<DisjointClasses><Class IRI="urn:C"/><Class IRI="urn:D"/></DisjointClasses>"#,
                "DisjointClasses(<urn:C> <urn:D>)",
            ),
            (
                r#"<DisjointUnion><Class IRI="urn:C"/><Class IRI="urn:D"/><Class IRI="urn:E"/></DisjointUnion>"#,
                "DisjointUnion(<urn:C> <urn:D> <urn:E>)",
            ),
            (
                r#"<SubObjectPropertyOf><ObjectPropertyChain><ObjectProperty IRI="urn:p"/><ObjectInverseOf><ObjectProperty IRI="urn:q"/></ObjectInverseOf></ObjectPropertyChain><ObjectProperty IRI="urn:r"/></SubObjectPropertyOf>"#,
                "SubObjectPropertyOf(ObjectPropertyChain(<urn:p> ObjectInverseOf(<urn:q>)) <urn:r>)",
            ),
            (
                r#"<EquivalentObjectProperties><ObjectProperty IRI="urn:p"/><ObjectProperty IRI="urn:q"/></EquivalentObjectProperties>"#,
                "EquivalentObjectProperties(<urn:p> <urn:q>)",
            ),
            (
                r#"<DisjointObjectProperties><ObjectProperty IRI="urn:p"/><ObjectProperty IRI="urn:q"/></DisjointObjectProperties>"#,
                "DisjointObjectProperties(<urn:p> <urn:q>)",
            ),
            (
                r#"<InverseObjectProperties><ObjectProperty IRI="urn:p"/><ObjectProperty IRI="urn:q"/></InverseObjectProperties>"#,
                "InverseObjectProperties(<urn:p> <urn:q>)",
            ),
            (
                r#"<ObjectPropertyDomain><ObjectProperty IRI="urn:p"/><Class IRI="urn:C"/></ObjectPropertyDomain>"#,
                "ObjectPropertyDomain(<urn:p> <urn:C>)",
            ),
            (
                r#"<ObjectPropertyRange><ObjectProperty IRI="urn:p"/><Class IRI="urn:C"/></ObjectPropertyRange>"#,
                "ObjectPropertyRange(<urn:p> <urn:C>)",
            ),
            (
                r#"<FunctionalObjectProperty><ObjectProperty IRI="urn:p"/></FunctionalObjectProperty>"#,
                "FunctionalObjectProperty(<urn:p>)",
            ),
            (
                r#"<InverseFunctionalObjectProperty><ObjectProperty IRI="urn:p"/></InverseFunctionalObjectProperty>"#,
                "InverseFunctionalObjectProperty(<urn:p>)",
            ),
            (
                r#"<ReflexiveObjectProperty><ObjectProperty IRI="urn:p"/></ReflexiveObjectProperty>"#,
                "ReflexiveObjectProperty(<urn:p>)",
            ),
            (
                r#"<IrreflexiveObjectProperty><ObjectProperty IRI="urn:p"/></IrreflexiveObjectProperty>"#,
                "IrreflexiveObjectProperty(<urn:p>)",
            ),
            (
                r#"<SymmetricObjectProperty><ObjectProperty IRI="urn:p"/></SymmetricObjectProperty>"#,
                "SymmetricObjectProperty(<urn:p>)",
            ),
            (
                r#"<AsymmetricObjectProperty><ObjectProperty IRI="urn:p"/></AsymmetricObjectProperty>"#,
                "AsymmetricObjectProperty(<urn:p>)",
            ),
            (
                r#"<TransitiveObjectProperty><ObjectProperty IRI="urn:p"/></TransitiveObjectProperty>"#,
                "TransitiveObjectProperty(<urn:p>)",
            ),
            (
                r#"<SubDataPropertyOf><DataProperty IRI="urn:p"/><DataProperty IRI="urn:q"/></SubDataPropertyOf>"#,
                "SubDataPropertyOf(<urn:p> <urn:q>)",
            ),
            (
                r#"<EquivalentDataProperties><DataProperty IRI="urn:p"/><DataProperty IRI="urn:q"/></EquivalentDataProperties>"#,
                "EquivalentDataProperties(<urn:p> <urn:q>)",
            ),
            (
                r#"<DisjointDataProperties><DataProperty IRI="urn:p"/><DataProperty IRI="urn:q"/></DisjointDataProperties>"#,
                "DisjointDataProperties(<urn:p> <urn:q>)",
            ),
            (
                r#"<DataPropertyDomain><DataProperty IRI="urn:p"/><Class IRI="urn:C"/></DataPropertyDomain>"#,
                "DataPropertyDomain(<urn:p> <urn:C>)",
            ),
            (
                r#"<DataPropertyRange><DataProperty IRI="urn:p"/><Datatype IRI="urn:D"/></DataPropertyRange>"#,
                "DataPropertyRange(<urn:p> <urn:D>)",
            ),
            (
                r#"<FunctionalDataProperty><DataProperty IRI="urn:p"/></FunctionalDataProperty>"#,
                "FunctionalDataProperty(<urn:p>)",
            ),
            (
                r#"<DatatypeDefinition><Datatype IRI="urn:D"/><Datatype IRI="urn:E"/></DatatypeDefinition>"#,
                "DatatypeDefinition(<urn:D> <urn:E>)",
            ),
            (
                r#"<HasKey><Class IRI="urn:C"/><ObjectProperty IRI="urn:p"/><DataProperty IRI="urn:q"/></HasKey>"#,
                "HasKey(<urn:C> (<urn:p>) (<urn:q>))",
            ),
            (
                r#"<SameIndividual><NamedIndividual IRI="urn:i"/><AnonymousIndividual nodeID="b"/></SameIndividual>"#,
                "SameIndividual(<urn:i> _:b)",
            ),
            (
                r#"<DifferentIndividuals><NamedIndividual IRI="urn:i"/><NamedIndividual IRI="urn:j"/></DifferentIndividuals>"#,
                "DifferentIndividuals(<urn:i> <urn:j>)",
            ),
            (
                r#"<ClassAssertion><Class IRI="urn:C"/><NamedIndividual IRI="urn:i"/></ClassAssertion>"#,
                "ClassAssertion(<urn:C> <urn:i>)",
            ),
            (
                r#"<ObjectPropertyAssertion><ObjectProperty IRI="urn:p"/><NamedIndividual IRI="urn:i"/><NamedIndividual IRI="urn:j"/></ObjectPropertyAssertion>"#,
                "ObjectPropertyAssertion(<urn:p> <urn:i> <urn:j>)",
            ),
            (
                r#"<NegativeObjectPropertyAssertion><ObjectProperty IRI="urn:p"/><NamedIndividual IRI="urn:i"/><NamedIndividual IRI="urn:j"/></NegativeObjectPropertyAssertion>"#,
                "NegativeObjectPropertyAssertion(<urn:p> <urn:i> <urn:j>)",
            ),
            (
                r#"<DataPropertyAssertion><DataProperty IRI="urn:p"/><NamedIndividual IRI="urn:i"/><Literal>v</Literal></DataPropertyAssertion>"#,
                r#"DataPropertyAssertion(<urn:p> <urn:i> "v")"#,
            ),
            (
                r#"<NegativeDataPropertyAssertion><DataProperty IRI="urn:p"/><NamedIndividual IRI="urn:i"/><Literal>v</Literal></NegativeDataPropertyAssertion>"#,
                r#"NegativeDataPropertyAssertion(<urn:p> <urn:i> "v")"#,
            ),
            (
                r#"<AnnotationAssertion><AnnotationProperty IRI="urn:p"/><IRI>urn:s</IRI><Literal lang="EN">v</Literal></AnnotationAssertion>"#,
                r#"AnnotationAssertion(<urn:p> <urn:s> "v"@EN)"#,
            ),
            (
                r#"<SubAnnotationPropertyOf><AnnotationProperty IRI="urn:p"/><AnnotationProperty IRI="urn:q"/></SubAnnotationPropertyOf>"#,
                "SubAnnotationPropertyOf(<urn:p> <urn:q>)",
            ),
            (
                r#"<AnnotationPropertyDomain><AnnotationProperty IRI="urn:p"/><IRI>urn:C</IRI></AnnotationPropertyDomain>"#,
                "AnnotationPropertyDomain(<urn:p> <urn:C>)",
            ),
            (
                r#"<AnnotationPropertyRange><AnnotationProperty IRI="urn:p"/><IRI>urn:C</IRI></AnnotationPropertyRange>"#,
                "AnnotationPropertyRange(<urn:p> <urn:C>)",
            ),
        ];
        for (xml, functional) in cases {
            assert_parity(xml, functional);
        }
    }

    #[test]
    fn expression_constructor_surface_matches_functional_canonical_rows() {
        let cases = [
            (
                r#"<ObjectIntersectionOf><Class IRI="urn:C"/><Class IRI="urn:D"/></ObjectIntersectionOf>"#,
                "ObjectIntersectionOf(<urn:C> <urn:D>)",
            ),
            (
                r#"<ObjectUnionOf><Class IRI="urn:C"/><Class IRI="urn:D"/></ObjectUnionOf>"#,
                "ObjectUnionOf(<urn:C> <urn:D>)",
            ),
            (
                r#"<ObjectComplementOf><Class IRI="urn:C"/></ObjectComplementOf>"#,
                "ObjectComplementOf(<urn:C>)",
            ),
            (
                r#"<ObjectOneOf><NamedIndividual IRI="urn:i"/><AnonymousIndividual nodeID="b"/></ObjectOneOf>"#,
                "ObjectOneOf(<urn:i> _:b)",
            ),
            (
                r#"<ObjectSomeValuesFrom><ObjectProperty IRI="urn:p"/><Class IRI="urn:C"/></ObjectSomeValuesFrom>"#,
                "ObjectSomeValuesFrom(<urn:p> <urn:C>)",
            ),
            (
                r#"<ObjectAllValuesFrom><ObjectProperty IRI="urn:p"/><Class IRI="urn:C"/></ObjectAllValuesFrom>"#,
                "ObjectAllValuesFrom(<urn:p> <urn:C>)",
            ),
            (
                r#"<ObjectHasValue><ObjectProperty IRI="urn:p"/><NamedIndividual IRI="urn:i"/></ObjectHasValue>"#,
                "ObjectHasValue(<urn:p> <urn:i>)",
            ),
            (
                r#"<ObjectHasSelf><ObjectProperty IRI="urn:p"/></ObjectHasSelf>"#,
                "ObjectHasSelf(<urn:p>)",
            ),
            (
                r#"<ObjectMinCardinality cardinality="1"><ObjectProperty IRI="urn:p"/></ObjectMinCardinality>"#,
                "ObjectMinCardinality(1 <urn:p>)",
            ),
            (
                r#"<ObjectMaxCardinality cardinality="2"><ObjectProperty IRI="urn:p"/><Class IRI="urn:C"/></ObjectMaxCardinality>"#,
                "ObjectMaxCardinality(2 <urn:p> <urn:C>)",
            ),
            (
                r#"<ObjectExactCardinality cardinality="3"><ObjectProperty IRI="urn:p"/></ObjectExactCardinality>"#,
                "ObjectExactCardinality(3 <urn:p>)",
            ),
            (
                r#"<DataSomeValuesFrom><DataProperty IRI="urn:p"/><Datatype IRI="urn:D"/></DataSomeValuesFrom>"#,
                "DataSomeValuesFrom(<urn:p> <urn:D>)",
            ),
            (
                r#"<DataAllValuesFrom><DataProperty IRI="urn:p"/><DataProperty IRI="urn:q"/><Datatype IRI="urn:D"/></DataAllValuesFrom>"#,
                "DataAllValuesFrom(<urn:p> <urn:q> <urn:D>)",
            ),
            (
                r#"<DataHasValue><DataProperty IRI="urn:p"/><Literal datatypeIRI="urn:D">v</Literal></DataHasValue>"#,
                r#"DataHasValue(<urn:p> "v"^^<urn:D>)"#,
            ),
            (
                r#"<DataMinCardinality cardinality="1"><DataProperty IRI="urn:p"/></DataMinCardinality>"#,
                "DataMinCardinality(1 <urn:p>)",
            ),
            (
                r#"<DataMaxCardinality cardinality="2"><DataProperty IRI="urn:p"/><Datatype IRI="urn:D"/></DataMaxCardinality>"#,
                "DataMaxCardinality(2 <urn:p> <urn:D>)",
            ),
            (
                r#"<DataExactCardinality cardinality="3"><DataProperty IRI="urn:p"/></DataExactCardinality>"#,
                "DataExactCardinality(3 <urn:p>)",
            ),
        ];
        for (expression, functional_expression) in cases {
            let xml = format!(r#"<SubClassOf><Class IRI="urn:S"/>{expression}</SubClassOf>"#);
            let functional = format!("SubClassOf(<urn:S> {functional_expression})");
            assert_parity(&xml, &functional);
        }

        let ranges = [
            (
                r#"<DataIntersectionOf><Datatype IRI="urn:D"/><Datatype IRI="urn:E"/></DataIntersectionOf>"#,
                "DataIntersectionOf(<urn:D> <urn:E>)",
            ),
            (
                r#"<DataUnionOf><Datatype IRI="urn:D"/><Datatype IRI="urn:E"/></DataUnionOf>"#,
                "DataUnionOf(<urn:D> <urn:E>)",
            ),
            (
                r#"<DataComplementOf><Datatype IRI="urn:D"/></DataComplementOf>"#,
                "DataComplementOf(<urn:D>)",
            ),
            (
                r#"<DataOneOf><Literal>a</Literal><Literal>b</Literal></DataOneOf>"#,
                r#"DataOneOf("a" "b")"#,
            ),
            (
                r#"<DatatypeRestriction><Datatype IRI="urn:D"/><FacetRestriction facet="urn:f"><Literal datatypeIRI="urn:D">1</Literal></FacetRestriction></DatatypeRestriction>"#,
                r#"DatatypeRestriction(<urn:D> <urn:f> "1"^^<urn:D>)"#,
            ),
        ];
        for (range, functional_range) in ranges {
            let xml = format!(
                r#"<DataPropertyRange><DataProperty IRI="urn:p"/>{range}</DataPropertyRange>"#
            );
            let functional = format!("DataPropertyRange(<urn:p> {functional_range})");
            assert_parity(&xml, &functional);
        }
    }

    #[test]
    fn prefixes_annotations_metadata_and_occurrences_are_preserved() {
        let source = format!(
            r#"<?xml version="1.0" encoding="UTF-8"?>
            <o:Ontology xmlns:o="{OWL}" xmlns:e="urn:xml:" ontologyIRI="urn:ontology" versionIRI="urn:version">
              <o:Prefix name="ex:" IRI="urn:ex:"/>
              <o:Import>urn:import</o:Import>
              <o:Annotation><o:AnnotationProperty abbreviatedIRI="ex:note"/><o:Literal xml:lang="EN-gb">hello &amp; bye</o:Literal></o:Annotation>
              <o:SubClassOf>
                <o:Annotation><o:AnnotationProperty abbreviatedIRI="ex:note"/><o:IRI>urn:value</o:IRI></o:Annotation>
                <o:Class abbreviatedIRI="ex:C"/><o:Class abbreviatedIRI="ex:D"/>
              </o:SubClassOf>
            </o:Ontology>"#
        );
        let document = mapped(&source).expect("metadata document");
        assert_eq!(document.ontology_iri.as_deref(), Some("urn:ontology"));
        assert_eq!(document.version_iri.as_deref(), Some("urn:version"));
        assert_eq!(document.imports, ["urn:import"]);
        assert_eq!(document.ontology_annotations.len(), 1);
        assert_eq!(document.axioms.len(), 1);
        assert_eq!(document.occurrences.len(), 2);
        assert_eq!(document.occurrences[0].collection, 0);
        assert_eq!(document.occurrences[1].collection, 1);
        assert_eq!(document.language_spellings, ["EN-gb"]);
        assert_eq!(
            document.source_prefixes,
            [
                ("e".to_owned(), "urn:xml:".to_owned()),
                ("o".to_owned(), OWL.to_owned()),
            ]
        );
    }

    #[test]
    fn hostile_xml_and_structural_confusion_fail_closed() {
        let hostile = [
            (
                format!(r#"<!DOCTYPE Ontology [<!ENTITY x "boom">]><Ontology xmlns="{OWL}"/>"#),
                "NATIVE_XML_FORBIDDEN_CONSTRUCT",
            ),
            (
                format!(
                    r#"<Ontology xmlns="{OWL}" xmlns:xi="http://www.w3.org/2001/XInclude"><xi:include href="file:///etc/passwd"/></Ontology>"#
                ),
                "NATIVE_XML_FORBIDDEN_CONSTRUCT",
            ),
            (
                format!(r#"<Ontology xmlns="{OWL}"><Import>&external;</Import></Ontology>"#),
                "NATIVE_XML_FORBIDDEN_CONSTRUCT",
            ),
            (
                format!(
                    r#"<Ontology xmlns="{OWL}"><SubClassOf><Class IRI="urn:C"/><Datatype IRI="urn:D"/></SubClassOf></Ontology>"#
                ),
                "NATIVE_OWLXML_SYNTAX",
            ),
            (
                format!(
                    r#"<Ontology xmlns="{OWL}"><ObjectInverseOf><ObjectInverseOf><ObjectProperty IRI="urn:p"/></ObjectInverseOf></ObjectInverseOf></Ontology>"#
                ),
                "NATIVE_OWLXML_SYNTAX",
            ),
            (
                format!(
                    r#"<Ontology xmlns="{OWL}"><Declaration><Class IRI="relative"/></Declaration></Ontology>"#
                ),
                "NATIVE_OWLXML_SYNTAX",
            ),
        ];
        for (source, code) in hostile {
            assert_eq!(mapped(&source).expect_err(&source).code, code);
        }
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
            let start = 16 + index * 8;
            encoded[start..start + 8].copy_from_slice(&configured.to_le_bytes());
        }
        let start = 16 + key as usize * 8;
        encoded[start..start + 8].copy_from_slice(&value.to_le_bytes());
        Limits::decode(&encoded).expect("test limits")
    }

    #[test]
    fn depth_literal_prefix_axiom_and_deadline_limits_are_exact() {
        let nested = ontology(
            r#"<SubClassOf><Class IRI="urn:C"/><ObjectComplementOf><Class IRI="urn:D"/></ObjectComplementOf></SubClassOf>"#,
        );
        let depth = limits_with(LimitKey::MaxNestingDepth, 3);
        assert_eq!(
            mapped_with(nested.as_bytes(), &depth, Cancellation::with_duration(None))
                .expect_err("depth limit")
                .code,
            "NATIVE_WIRE_LIMIT"
        );

        let literal = ontology(
            r#"<DataPropertyAssertion><DataProperty IRI="urn:p"/><NamedIndividual IRI="urn:i"/><Literal>1234</Literal></DataPropertyAssertion>"#,
        );
        let literal_limit = limits_with(LimitKey::MaxLiteralBytes, 3);
        assert_eq!(
            mapped_with(
                literal.as_bytes(),
                &literal_limit,
                Cancellation::with_duration(None)
            )
            .expect_err("literal limit")
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        let axioms = ontology(
            r#"<Declaration><Class IRI="urn:C"/></Declaration><Declaration><Class IRI="urn:D"/></Declaration>"#,
        );
        let axiom_limit = limits_with(LimitKey::MaxAxioms, 1);
        assert_eq!(
            mapped_with(
                axioms.as_bytes(),
                &axiom_limit,
                Cancellation::with_duration(None)
            )
            .expect_err("axiom limit")
            .code,
            "NATIVE_WIRE_LIMIT"
        );

        let work_limit = limits_with(LimitKey::MaxCanonicalWork, 1);
        mapped_with(
            ontology("").as_bytes(),
            &work_limit,
            Cancellation::with_duration(None),
        )
        .expect("document-wide XML traversal is not component canonical work");

        let limits = Limits::default();
        assert_eq!(
            mapped_with(
                ontology("").as_bytes(),
                &limits,
                Cancellation::with_duration(Some(Duration::ZERO))
            )
            .expect_err("deadline")
            .code,
            "NATIVE_DEADLINE"
        );
    }

    #[test]
    fn utf16_and_utf8_bom_have_stable_decoded_lengths() {
        let source = ontology(
            r#"<Annotation><AnnotationProperty IRI="urn:p"/><Literal>é</Literal></Annotation>"#,
        );
        let utf8 = mapped(&format!("\u{feff}{source}")).expect("UTF-8 BOM");
        let mut utf16 = vec![0xff, 0xfe];
        for unit in source.encode_utf16() {
            utf16.extend_from_slice(&unit.to_le_bytes());
        }
        let utf16 = mapped_with(
            &utf16,
            &Limits::default(),
            Cancellation::with_duration(None),
        )
        .expect("UTF-16");
        assert_eq!(utf8.decoded_codepoints, utf16.decoded_codepoints);
        assert_eq!(utf8.ontology_annotations, utf16.ontology_annotations);
    }

    #[cfg(feature = "test-hooks")]
    #[test]
    fn allocation_failures_are_retryable_without_hidden_parser_state() {
        let source = ontology(
            r#"<SubClassOf><ObjectSomeValuesFrom><ObjectProperty IRI="urn:p"/><Class IRI="urn:C"/></ObjectSomeValuesFrom><Class IRI="urn:D"/></SubClassOf>"#,
        );
        let limits = Limits::default();
        let mut baseline_guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut baseline =
            Session::with_allocation_failure(&mut baseline_guard, &limits, source.len(), None)
                .expect("baseline session");
        let expected = parse_and_map(source.as_bytes(), None, true, true, true, &mut baseline)
            .expect("baseline parse");
        let allocations = baseline.allocation_count();
        assert!(allocations > 8);

        for fail_after in [0, 1, allocations / 2, allocations.saturating_sub(1)] {
            let mut failing_guard = Guard::new(
                Cancellation::with_duration(None),
                limits.deadline,
                limits.cancellation_stride,
            );
            let mut failing = Session::with_allocation_failure(
                &mut failing_guard,
                &limits,
                source.len(),
                Some(fail_after),
            )
            .expect("failing session");
            assert_eq!(
                parse_and_map(source.as_bytes(), None, true, true, true, &mut failing)
                    .expect_err("injected allocation failure")
                    .code,
                "NATIVE_WIRE_LIMIT"
            );

            let retry = mapped(&source).expect("fresh retry");
            assert_eq!(retry, expected);
        }
    }
}
