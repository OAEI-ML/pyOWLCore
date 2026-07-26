//! Forward-only RDF 1.1 Turtle tokenization feeding the shared OWL RDF mapper.
//!
//! This parser owns one token of lookahead plus the RDF graph required by the
//! common mapper. It remains private until retained publication and the
//! installed forced-native capability matrix are complete.

use std::borrow::Cow;
use std::time::Instant;

use crate::error::{NativeError, NativeResult};
use crate::limits::LimitKey;
use crate::session::Session;

use super::rdfxml::{map_graph, resolve_iri, sort_graph_like_python, Resource, Term, Triple};
use super::CanonicalDocument;

const RDF_TYPE: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const RDF_FIRST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first";
const RDF_REST: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest";
const RDF_NIL: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil";
const XSD: &str = "http://www.w3.org/2001/XMLSchema#";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TokenKind {
    Iri,
    String,
    Number,
    Word,
    Language,
    Directive,
    Hat,
    Dot,
    Semicolon,
    Comma,
    LeftBracket,
    RightBracket,
    LeftParen,
    RightParen,
    Eof,
}

#[derive(Debug)]
struct Token<'text> {
    kind: TokenKind,
    value: Cow<'text, str>,
}

struct Lexer<'text> {
    text: &'text str,
    offset: usize,
}

impl<'text> Lexer<'text> {
    const fn new(text: &'text str) -> Self {
        Self { text, offset: 0 }
    }

    fn next(&mut self, session: &mut Session<'_>) -> NativeResult<Token<'text>> {
        self.skip_ignored(session)?;
        if self.offset == self.text.len() {
            return Ok(Token {
                kind: TokenKind::Eof,
                value: Cow::Borrowed(""),
            });
        }
        session.step(1)?;
        let start = self.offset;
        let remaining = &self.text[start..];
        if remaining.starts_with("^^") {
            self.offset += 2;
            return Ok(Token {
                kind: TokenKind::Hat,
                value: Cow::Borrowed("^^"),
            });
        }
        let character = remaining.chars().next().ok_or_else(turtle_syntax)?;
        if let Some(kind) = punctuation(character) {
            self.offset += character.len_utf8();
            return Ok(Token {
                kind,
                value: Cow::Borrowed(&self.text[start..self.offset]),
            });
        }
        if character == '<' {
            return self.iri_token(start, session);
        }
        if matches!(character, '"' | '\'') {
            return self.string_token(start, character, session);
        }
        if character == '@' {
            return self.at_token(start, session);
        }
        if let Some(end) = number_end(self.text, start, session)? {
            self.offset = end;
            return Ok(Token {
                kind: TokenKind::Number,
                value: Cow::Borrowed(&self.text[start..end]),
            });
        }
        self.word_token(start, session)
    }

    fn skip_ignored(&mut self, session: &mut Session<'_>) -> NativeResult<()> {
        loop {
            while self.offset < self.text.len() {
                let character = self.text[self.offset..]
                    .chars()
                    .next()
                    .ok_or_else(turtle_syntax)?;
                if !character.is_whitespace() {
                    break;
                }
                session.step(1)?;
                self.offset += character.len_utf8();
            }
            if self.text[self.offset..].starts_with('#') {
                while self.offset < self.text.len() {
                    let character = self.text[self.offset..]
                        .chars()
                        .next()
                        .ok_or_else(turtle_syntax)?;
                    if matches!(character, '\r' | '\n') {
                        break;
                    }
                    session.step(1)?;
                    self.offset += character.len_utf8();
                }
                continue;
            }
            return Ok(());
        }
    }

    fn iri_token(&mut self, start: usize, session: &mut Session<'_>) -> NativeResult<Token<'text>> {
        let content_start = start + 1;
        let mut end = content_start;
        while end < self.text.len() {
            session.step(1)?;
            let character = self.text[end..].chars().next().ok_or_else(turtle_syntax)?;
            if character == '>' {
                let value = &self.text[content_start..end];
                self.offset = end + 1;
                return Ok(Token {
                    kind: TokenKind::Iri,
                    value: Cow::Borrowed(value),
                });
            }
            if matches!(character, '\r' | '\n') {
                return Err(turtle_syntax());
            }
            end += character.len_utf8();
        }
        Err(turtle_syntax())
    }

    fn string_token(
        &mut self,
        start: usize,
        quote: char,
        session: &mut Session<'_>,
    ) -> NativeResult<Token<'text>> {
        let triple = match quote {
            '"' => self.text[start..].starts_with("\"\"\""),
            '\'' => self.text[start..].starts_with("'''"),
            _ => false,
        };
        let delimiter_width = if triple { 3 } else { 1 };
        let content_start = start + delimiter_width;
        let delimiter = &self.text[start..content_start];
        let mut cursor = content_start;
        let mut plain_start = content_start;
        let mut output: Option<String> = None;
        while cursor < self.text.len() {
            session.step(1)?;
            if self.text[cursor..].starts_with(delimiter) {
                let value = if let Some(mut output) = output {
                    push_tracked(&mut output, &self.text[plain_start..cursor], session)?;
                    Cow::Owned(output)
                } else {
                    Cow::Borrowed(&self.text[content_start..cursor])
                };
                enforce_length(
                    value.len(),
                    session.limits().value(LimitKey::MaxLiteralBytes),
                    "native Turtle literal exceeds max_literal_bytes",
                )?;
                self.offset = cursor + delimiter_width;
                return Ok(Token {
                    kind: TokenKind::String,
                    value,
                });
            }
            let character = self.text[cursor..]
                .chars()
                .next()
                .ok_or_else(turtle_syntax)?;
            if !triple && matches!(character, '\r' | '\n') {
                return Err(turtle_syntax());
            }
            if character == '\\' {
                let destination = output.get_or_insert_with(String::new);
                push_tracked(destination, &self.text[plain_start..cursor], session)?;
                let (decoded, next) = decode_string_escape(self.text, cursor)?;
                push_character_tracked(destination, decoded, session)?;
                cursor = next;
                plain_start = cursor;
            } else {
                cursor += character.len_utf8();
            }
        }
        Err(turtle_syntax())
    }

    fn at_token(&mut self, start: usize, session: &mut Session<'_>) -> NativeResult<Token<'text>> {
        for directive in ["@prefix", "@base"] {
            let end = start + directive.len();
            if self.text[start..].starts_with(directive) {
                self.offset = end;
                return Ok(Token {
                    kind: TokenKind::Directive,
                    value: Cow::Borrowed(&self.text[start..end]),
                });
            }
        }
        let mut end = start + 1;
        let language_start = end;
        while self
            .text
            .as_bytes()
            .get(end)
            .is_some_and(u8::is_ascii_alphabetic)
        {
            session.step(1)?;
            end += 1;
        }
        if end == language_start {
            return Err(turtle_syntax());
        }
        while self.text.as_bytes().get(end) == Some(&b'-') {
            end += 1;
            let part_start = end;
            while self
                .text
                .as_bytes()
                .get(end)
                .is_some_and(u8::is_ascii_alphanumeric)
            {
                session.step(1)?;
                end += 1;
            }
            if end == part_start {
                return Err(turtle_syntax());
            }
        }
        self.offset = end;
        Ok(Token {
            kind: TokenKind::Language,
            value: Cow::Borrowed(&self.text[language_start..end]),
        })
    }

    fn word_token(
        &mut self,
        start: usize,
        session: &mut Session<'_>,
    ) -> NativeResult<Token<'text>> {
        let mut end = start;
        while end < self.text.len() {
            session.step(1)?;
            let character = self.text[end..].chars().next().ok_or_else(turtle_syntax)?;
            if word_stop(character)
                || character == '.' && word_boundary(self.text, end + character.len_utf8())
            {
                break;
            }
            end += character.len_utf8();
        }
        if end == start {
            return Err(turtle_syntax());
        }
        self.offset = end;
        Ok(Token {
            kind: TokenKind::Word,
            value: Cow::Borrowed(&self.text[start..end]),
        })
    }
}

struct Parser<'text, 'session, 'guard> {
    lexer: Lexer<'text>,
    lookahead: Option<Token<'text>>,
    session: &'session mut Session<'guard>,
    base: Option<String>,
    prefixes: Vec<(String, String)>,
    triples: Vec<Triple>,
    blank_counter: u64,
    preserve_source_map: bool,
    language_spellings: Vec<String>,
    source_blank_labels: Vec<String>,
}

struct ParsedGraph {
    triples: Vec<Triple>,
    prefixes: Vec<(String, String)>,
    language_spellings: Vec<String>,
    source_blank_labels: Vec<String>,
}

impl<'text, 'session, 'guard> Parser<'text, 'session, 'guard> {
    fn new(
        text: &'text str,
        document_iri: Option<&str>,
        preserve_source_map: bool,
        session: &'session mut Session<'guard>,
    ) -> NativeResult<Self> {
        let base = document_iri
            .map(|value| owned_text(value, session))
            .transpose()?;
        Ok(Self {
            lexer: Lexer::new(text),
            lookahead: None,
            session,
            base,
            prefixes: Vec::new(),
            triples: Vec::new(),
            blank_counter: 0,
            preserve_source_map,
            language_spellings: Vec::new(),
            source_blank_labels: Vec::new(),
        })
    }

    fn parse(mut self) -> NativeResult<ParsedGraph> {
        while !self.at(TokenKind::Eof)? {
            if self.directive()? {
                self.parse_directive()?;
                continue;
            }
            let subject = self.subject()?;
            self.predicate_object_list(&subject, &[TokenKind::Dot])?;
            self.expect(TokenKind::Dot)?;
        }
        sort_graph_like_python(&mut self.triples);
        self.prefixes
            .sort_unstable_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
        self.source_blank_labels
            .sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
        self.source_blank_labels.dedup();
        Ok(ParsedGraph {
            triples: self.triples,
            prefixes: self.prefixes,
            language_spellings: self.language_spellings,
            source_blank_labels: self.source_blank_labels,
        })
    }

    fn parse_directive(&mut self) -> NativeResult<()> {
        let token = self.take()?;
        let at_form = token.kind == TokenKind::Directive;
        if token.value.eq_ignore_ascii_case("@prefix") || token.value.eq_ignore_ascii_case("prefix")
        {
            let prefix_token = self.expect(TokenKind::Word)?;
            let prefix = prefix_token
                .value
                .strip_suffix(':')
                .ok_or_else(turtle_syntax)?;
            let iri = self.expect(TokenKind::Iri)?;
            let decoded = decode_uchar(iri.value.as_ref(), self.session)?;
            let resolved =
                resolve_turtle_iri(decoded.as_ref(), self.base.as_deref(), self.session)?;
            self.install_prefix(prefix, resolved)?;
        } else if token.value.eq_ignore_ascii_case("@base")
            || token.value.eq_ignore_ascii_case("base")
        {
            let iri = self.expect(TokenKind::Iri)?;
            let decoded = decode_uchar(iri.value.as_ref(), self.session)?;
            self.base = Some(resolve_turtle_iri(
                decoded.as_ref(),
                self.base.as_deref(),
                self.session,
            )?);
        } else {
            return Err(turtle_syntax());
        }
        if at_form {
            self.expect(TokenKind::Dot)?;
        }
        Ok(())
    }

    fn directive(&mut self) -> NativeResult<bool> {
        let token = self.peek()?;
        Ok(token.kind == TokenKind::Directive
            || token.kind == TokenKind::Word
                && (token.value.eq_ignore_ascii_case("prefix")
                    || token.value.eq_ignore_ascii_case("base")))
    }

    fn predicate_object_list(
        &mut self,
        subject: &Resource,
        terminators: &[TokenKind],
    ) -> NativeResult<()> {
        loop {
            let predicate = self.verb()?;
            loop {
                let object = self.object()?;
                self.add(subject, &predicate, &object)?;
                if !self.at(TokenKind::Comma)? {
                    break;
                }
                self.take()?;
            }
            if !self.at(TokenKind::Semicolon)? {
                return Ok(());
            }
            while self.at(TokenKind::Semicolon)? {
                self.take()?;
            }
            let next = self.peek()?.kind;
            if terminators.contains(&next) || next == TokenKind::RightBracket {
                return Ok(());
            }
        }
    }

    fn verb(&mut self) -> NativeResult<String> {
        let shorthand = {
            let token = self.peek()?;
            token.kind == TokenKind::Word && token.value == "a"
        };
        if shorthand {
            self.take()?;
            owned_text(RDF_TYPE, self.session)
        } else {
            self.iri()
        }
    }

    fn subject(&mut self) -> NativeResult<Resource> {
        match self.peek()?.kind {
            TokenKind::Iri | TokenKind::Word => {
                let blank = self.peek()?.value.starts_with("_:");
                if blank {
                    let token = self.take()?;
                    let label = token.value.strip_prefix("_:").ok_or_else(turtle_syntax)?;
                    self.record_explicit_blank(label)?;
                    Ok(Resource::Blank(owned_text(label, self.session)?))
                } else {
                    Ok(Resource::Iri(self.iri()?))
                }
            }
            TokenKind::LeftBracket => self.blank_property_list(),
            TokenKind::LeftParen => self.collection(),
            _ => Err(turtle_syntax()),
        }
    }

    fn object(&mut self) -> NativeResult<Term> {
        match self.peek()?.kind {
            TokenKind::String => {
                let token = self.take()?;
                let lexical = owned_text(token.value.as_ref(), self.session)?;
                if self.at(TokenKind::Language)? {
                    let language = self.take()?;
                    if self.preserve_source_map {
                        push_vec_owned(
                            &mut self.language_spellings,
                            language.value.as_ref(),
                            self.session,
                        )?;
                    }
                    Ok(Term::Literal {
                        lexical,
                        datatype: None,
                        language: Some(owned_text(language.value.as_ref(), self.session)?),
                    })
                } else if self.at(TokenKind::Hat)? {
                    self.take()?;
                    Ok(Term::Literal {
                        lexical,
                        datatype: Some(self.iri()?),
                        language: None,
                    })
                } else {
                    Ok(Term::Literal {
                        lexical,
                        datatype: Some(owned_prefixed(XSD, "string", self.session)?),
                        language: None,
                    })
                }
            }
            TokenKind::Number => {
                let token = self.take()?;
                let datatype = if token
                    .value
                    .bytes()
                    .any(|value| matches!(value, b'e' | b'E'))
                {
                    "double"
                } else if token.value.contains('.') {
                    "decimal"
                } else {
                    "integer"
                };
                Ok(Term::Literal {
                    lexical: owned_text(token.value.as_ref(), self.session)?,
                    datatype: Some(owned_prefixed(XSD, datatype, self.session)?),
                    language: None,
                })
            }
            TokenKind::Word => {
                let value = self.peek()?.value.as_ref();
                if matches!(value, "true" | "false") {
                    let token = self.take()?;
                    Ok(Term::Literal {
                        lexical: owned_text(token.value.as_ref(), self.session)?,
                        datatype: Some(owned_prefixed(XSD, "boolean", self.session)?),
                        language: None,
                    })
                } else if value.starts_with("_:") {
                    let token = self.take()?;
                    let label = token.value.strip_prefix("_:").ok_or_else(turtle_syntax)?;
                    self.record_explicit_blank(label)?;
                    Ok(Term::Blank(owned_text(label, self.session)?))
                } else {
                    Ok(Term::Iri(self.iri()?))
                }
            }
            TokenKind::Iri => Ok(Term::Iri(self.iri()?)),
            TokenKind::LeftBracket => self.blank_property_list().map(Term::from),
            TokenKind::LeftParen => self.collection().map(Term::from),
            _ => Err(turtle_syntax()),
        }
    }

    fn blank_property_list(&mut self) -> NativeResult<Resource> {
        self.expect(TokenKind::LeftBracket)?;
        let node = self.fresh("anon")?;
        if !self.at(TokenKind::RightBracket)? {
            self.predicate_object_list(&node, &[TokenKind::RightBracket])?;
        }
        self.expect(TokenKind::RightBracket)?;
        Ok(node)
    }

    fn collection(&mut self) -> NativeResult<Resource> {
        self.expect(TokenKind::LeftParen)?;
        let mut values = Vec::new();
        while !self.at(TokenKind::RightParen)? {
            reserve_vec_item(&mut values, self.session)?;
            values.push(self.object()?);
            enforce_length(
                values.len(),
                self.session.limits().value(LimitKey::MaxRdfListLength),
                "native Turtle collection exceeds max_rdf_list_length",
            )?;
        }
        self.expect(TokenKind::RightParen)?;
        if values.is_empty() {
            return Ok(Resource::Iri(owned_text(RDF_NIL, self.session)?));
        }
        let mut nodes = Vec::new();
        for _ in 0..values.len() {
            reserve_vec_item(&mut nodes, self.session)?;
            nodes.push(self.fresh("list")?);
        }
        for (index, value) in values.iter().enumerate() {
            self.add(&nodes[index], RDF_FIRST, value)?;
            let rest = if let Some(next) = nodes.get(index + 1) {
                Term::from(clone_resource(next, self.session)?)
            } else {
                Term::Iri(owned_text(RDF_NIL, self.session)?)
            };
            self.add(&nodes[index], RDF_REST, &rest)?;
        }
        clone_resource(&nodes[0], self.session)
    }

    fn iri(&mut self) -> NativeResult<String> {
        let token = self.take()?;
        if token.kind == TokenKind::Iri {
            let decoded = decode_uchar(token.value.as_ref(), self.session)?;
            return resolve_turtle_iri(decoded.as_ref(), self.base.as_deref(), self.session);
        }
        if token.kind != TokenKind::Word
            || !token.value.contains(':')
            || token.value.starts_with("_:")
        {
            return Err(turtle_syntax());
        }
        let (prefix, local) = token.value.split_once(':').ok_or_else(turtle_syntax)?;
        let base = self
            .prefixes
            .iter()
            .rev()
            .find_map(|(known, value)| (known == prefix).then_some(value.as_str()))
            .ok_or_else(turtle_syntax)?;
        let decoded = decode_pname(local, self.session)?;
        owned_prefixed(base, decoded.as_ref(), self.session)
    }

    fn install_prefix(&mut self, prefix: &str, iri: String) -> NativeResult<()> {
        if let Some((_, known)) = self.prefixes.iter_mut().find(|(known, _)| known == prefix) {
            *known = iri;
            return Ok(());
        }
        let following = self
            .prefixes
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native Turtle prefix count overflow"))?;
        enforce_length(
            following,
            self.session.limits().value(LimitKey::MaxPrefixes),
            "native Turtle prefixes exceed max_prefixes",
        )?;
        reserve_vec_item(&mut self.prefixes, self.session)?;
        self.prefixes.push((owned_text(prefix, self.session)?, iri));
        Ok(())
    }

    fn fresh(&mut self, stem: &str) -> NativeResult<Resource> {
        use std::fmt::Write;

        self.blank_counter = self
            .blank_counter
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native Turtle blank counter overflow"))?;
        let maximum = stem
            .len()
            .checked_add(1 + 20)
            .ok_or_else(|| NativeError::limit("native Turtle blank size overflow"))?;
        self.session.reserve_bytes(maximum)?;
        let mut value = String::new();
        value
            .try_reserve_exact(maximum)
            .map_err(|_| NativeError::limit("native Turtle blank allocation failed"))?;
        write!(&mut value, "{stem}-{}", self.blank_counter)
            .map_err(|_| NativeError::protocol("native Turtle blank formatting failed"))?;
        Ok(Resource::Blank(value))
    }

    fn record_explicit_blank(&mut self, label: &str) -> NativeResult<()> {
        if self.preserve_source_map && !self.source_blank_labels.iter().any(|known| known == label)
        {
            push_vec_owned(&mut self.source_blank_labels, label, self.session)?;
        }
        Ok(())
    }

    fn add(&mut self, subject: &Resource, predicate: &str, object: &Term) -> NativeResult<()> {
        self.session.step(
            u64::try_from(self.triples.len())
                .map_err(|_| NativeError::limit("native Turtle duplicate work exceeds u64"))?,
        )?;
        let candidate = Triple {
            subject: clone_resource(subject, self.session)?,
            predicate: owned_text(predicate, self.session)?,
            object: clone_term(object, self.session)?,
        };
        if self.triples.contains(&candidate) {
            return Ok(());
        }
        let following = self
            .triples
            .len()
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native Turtle triple count overflow"))?;
        enforce_length(
            following,
            self.session.limits().value(LimitKey::MaxTriples),
            "native Turtle graph exceeds max_triples",
        )?;
        reserve_vec_item(&mut self.triples, self.session)?;
        self.triples.push(candidate);
        Ok(())
    }

    fn at(&mut self, kind: TokenKind) -> NativeResult<bool> {
        Ok(self.peek()?.kind == kind)
    }

    fn peek(&mut self) -> NativeResult<&Token<'text>> {
        if self.lookahead.is_none() {
            self.lookahead = Some(self.lexer.next(self.session)?);
        }
        self.lookahead.as_ref().ok_or_else(turtle_syntax)
    }

    fn take(&mut self) -> NativeResult<Token<'text>> {
        if self.lookahead.is_none() {
            self.lookahead = Some(self.lexer.next(self.session)?);
        }
        self.lookahead.take().ok_or_else(turtle_syntax)
    }

    fn expect(&mut self, kind: TokenKind) -> NativeResult<Token<'text>> {
        if self.peek()?.kind != kind {
            return Err(turtle_syntax());
        }
        self.take()
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn parse_and_map_timed(
    source: &[u8],
    document_iri: Option<&str>,
    allow_swrl: bool,
    allow_partial_rdf_mapping: bool,
    capture_occurrences: bool,
    preserve_source_map: bool,
    session: &mut Session<'_>,
) -> NativeResult<(CanonicalDocument, u64)> {
    let text = std::str::from_utf8(source).map_err(|_| turtle_encoding())?;
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let decoded_codepoints = count_codepoints(text, session)?;
    let graph = Parser::new(text, document_iri, preserve_source_map, session)?.parse()?;
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
    document.source_prefixes = graph.prefixes;
    document.source_blank_labels = graph.source_blank_labels;
    let mapping_ns = u64::try_from(mapping_started.elapsed().as_nanos())
        .map_err(|_| NativeError::limit("native Turtle mapping phase time exceeds u64"))?;
    Ok((document, mapping_ns))
}

fn count_codepoints(value: &str, session: &mut Session<'_>) -> NativeResult<u64> {
    let mut count = 0_u64;
    for _ in value.chars() {
        session.step(1)?;
        count = count
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native Turtle codepoint count exceeds u64"))?;
    }
    Ok(count)
}

fn clone_resource(value: &Resource, session: &mut Session<'_>) -> NativeResult<Resource> {
    Ok(match value {
        Resource::Iri(value) => Resource::Iri(owned_text(value, session)?),
        Resource::Blank(value) => Resource::Blank(owned_text(value, session)?),
    })
}

fn clone_term(value: &Term, session: &mut Session<'_>) -> NativeResult<Term> {
    Ok(match value {
        Term::Iri(value) => Term::Iri(owned_text(value, session)?),
        Term::Blank(value) => Term::Blank(owned_text(value, session)?),
        Term::Literal {
            lexical,
            datatype,
            language,
        } => Term::Literal {
            lexical: owned_text(lexical, session)?,
            datatype: datatype
                .as_deref()
                .map(|value| owned_text(value, session))
                .transpose()?,
            language: language
                .as_deref()
                .map(|value| owned_text(value, session))
                .transpose()?,
        },
    })
}

fn resolve_turtle_iri(
    value: &str,
    base: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<String> {
    resolve_iri(value, base, session).map_err(|error| match error.code {
        "NATIVE_RDFXML_RELATIVE_IRI_NO_BASE" => NativeError::new(
            "NATIVE_TURTLE_RELATIVE_IRI",
            "native Turtle relative IRI requires an absolute base",
        ),
        "NATIVE_RDFXML_INVALID_BASE_IRI"
        | "NATIVE_RDFXML_IRI_REFERENCE"
        | "NATIVE_RDFXML_SYNTAX" => turtle_syntax(),
        _ => error,
    })
}

fn owned_text(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native Turtle text allocation failed"))?;
    output.push_str(value);
    Ok(output)
}

fn owned_prefixed(prefix: &str, value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    let size = prefix
        .len()
        .checked_add(value.len())
        .ok_or_else(|| NativeError::limit("native Turtle text size overflow"))?;
    session.reserve_bytes(size)?;
    let mut output = String::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native Turtle text allocation failed"))?;
    output.push_str(prefix);
    output.push_str(value);
    Ok(output)
}

fn push_vec_owned(
    values: &mut Vec<String>,
    value: &str,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    reserve_vec_item(values, session)?;
    values.push(owned_text(value, session)?);
    Ok(())
}

fn reserve_vec_item<T>(values: &mut Vec<T>, session: &mut Session<'_>) -> NativeResult<()> {
    if values.len() == values.capacity() {
        session.reserve_bytes(std::mem::size_of::<T>())?;
        values
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native Turtle table allocation failed"))?;
    }
    Ok(())
}

fn push_tracked(output: &mut String, value: &str, session: &mut Session<'_>) -> NativeResult<()> {
    let required = output
        .len()
        .checked_add(value.len())
        .ok_or_else(|| NativeError::limit("native Turtle string size overflow"))?;
    if required > output.capacity() {
        let additional = required - output.capacity();
        session.reserve_bytes(additional)?;
        output
            .try_reserve_exact(additional)
            .map_err(|_| NativeError::limit("native Turtle string allocation failed"))?;
    }
    output.push_str(value);
    Ok(())
}

fn push_character_tracked(
    output: &mut String,
    value: char,
    session: &mut Session<'_>,
) -> NativeResult<()> {
    let mut encoded = [0_u8; 4];
    push_tracked(output, value.encode_utf8(&mut encoded), session)
}

fn decode_string_escape(source: &str, offset: usize) -> NativeResult<(char, usize)> {
    let next_offset = offset
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("native Turtle offset overflow"))?;
    let next = source[next_offset..]
        .chars()
        .next()
        .ok_or_else(turtle_syntax)?;
    let simple = match next {
        't' => Some('\t'),
        'b' => Some('\u{0008}'),
        'n' => Some('\n'),
        'r' => Some('\r'),
        'f' => Some('\u{000c}'),
        '"' => Some('"'),
        '\'' => Some('\''),
        '\\' => Some('\\'),
        _ => None,
    };
    if let Some(value) = simple {
        return Ok((value, next_offset + next.len_utf8()));
    }
    decode_unicode_escape(source, offset)
}

fn decode_uchar<'text>(
    value: &'text str,
    session: &mut Session<'_>,
) -> NativeResult<Cow<'text, str>> {
    if !value.contains('\\') {
        return Ok(Cow::Borrowed(value));
    }
    let mut output = String::new();
    let mut cursor = 0_usize;
    while let Some(relative) = value[cursor..].find('\\') {
        session.step(1)?;
        let offset = cursor + relative;
        push_tracked(&mut output, &value[cursor..offset], session)?;
        let (decoded, end) = decode_unicode_escape(value, offset)?;
        push_character_tracked(&mut output, decoded, session)?;
        cursor = end;
    }
    push_tracked(&mut output, &value[cursor..], session)?;
    if output.contains('\\') {
        return Err(turtle_syntax());
    }
    Ok(Cow::Owned(output))
}

fn decode_pname<'text>(
    value: &'text str,
    session: &mut Session<'_>,
) -> NativeResult<Cow<'text, str>> {
    let decoded = decode_uchar(value, session)?;
    if !decoded.contains('\\') {
        return Ok(decoded);
    }
    let mut output = String::new();
    let mut characters = decoded.chars();
    while let Some(character) = characters.next() {
        session.step(1)?;
        if character == '\\' {
            let escaped = characters.next().ok_or_else(turtle_syntax)?;
            push_character_tracked(&mut output, escaped, session)?;
        } else {
            push_character_tracked(&mut output, character, session)?;
        }
    }
    Ok(Cow::Owned(output))
}

fn decode_unicode_escape(source: &str, offset: usize) -> NativeResult<(char, usize)> {
    let marker = source
        .as_bytes()
        .get(offset + 1)
        .copied()
        .ok_or_else(turtle_syntax)?;
    let digits = match marker {
        b'u' => 4,
        b'U' => 8,
        _ => return Err(turtle_syntax()),
    };
    let start = offset + 2;
    let end = start
        .checked_add(digits)
        .ok_or_else(|| NativeError::limit("native Turtle escape offset overflow"))?;
    let encoded = source.get(start..end).ok_or_else(turtle_syntax)?;
    if !encoded.bytes().all(|value| value.is_ascii_hexdigit()) {
        return Err(turtle_syntax());
    }
    let codepoint = u32::from_str_radix(encoded, 16).map_err(|_| turtle_syntax())?;
    let value = char::from_u32(codepoint).ok_or_else(turtle_syntax)?;
    Ok((value, end))
}

fn number_end(
    source: &str,
    start: usize,
    session: &mut Session<'_>,
) -> NativeResult<Option<usize>> {
    let bytes = source.as_bytes();
    let mut cursor = start;
    if bytes
        .get(cursor)
        .is_some_and(|value| matches!(*value, b'+' | b'-'))
    {
        session.step(1)?;
        cursor += 1;
    }
    let integer_start = cursor;
    while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
        session.step(1)?;
        cursor += 1;
    }
    let integer_digits = cursor - integer_start;
    if bytes.get(cursor) == Some(&b'.') {
        session.step(1)?;
        cursor += 1;
        let fraction_start = cursor;
        while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
            session.step(1)?;
            cursor += 1;
        }
        let decimal_digits = cursor - fraction_start;
        if integer_digits == 0 && decimal_digits == 0 {
            return Ok(None);
        }
    } else if integer_digits == 0 {
        return Ok(None);
    }
    if bytes
        .get(cursor)
        .is_some_and(|value| matches!(*value, b'e' | b'E'))
    {
        let exponent = cursor;
        session.step(1)?;
        cursor += 1;
        if bytes
            .get(cursor)
            .is_some_and(|value| matches!(*value, b'+' | b'-'))
        {
            session.step(1)?;
            cursor += 1;
        }
        let digits = cursor;
        while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
            session.step(1)?;
            cursor += 1;
        }
        if cursor == digits {
            cursor = exponent;
        }
    }
    Ok(number_boundary(source, cursor).then_some(cursor))
}

fn punctuation(value: char) -> Option<TokenKind> {
    Some(match value {
        '.' => TokenKind::Dot,
        ';' => TokenKind::Semicolon,
        ',' => TokenKind::Comma,
        '[' => TokenKind::LeftBracket,
        ']' => TokenKind::RightBracket,
        '(' => TokenKind::LeftParen,
        ')' => TokenKind::RightParen,
        _ => return None,
    })
}

fn word_stop(value: char) -> bool {
    value.is_whitespace()
        || matches!(
            value,
            ';' | ',' | '[' | ']' | '(' | ')' | '<' | '>' | '"' | '\'' | '^'
        )
}

fn word_boundary(source: &str, end: usize) -> bool {
    end == source.len()
        || source[end..]
            .chars()
            .next()
            .is_some_and(|value| word_stop(value) || value == '.')
}

fn number_boundary(source: &str, end: usize) -> bool {
    end == source.len()
        || source[end..].chars().next().is_some_and(|value| {
            value.is_whitespace() || matches!(value, ';' | ',' | '.' | '[' | ']' | '(' | ')')
        })
}

fn enforce_length(value: usize, maximum: u64, message: &'static str) -> NativeResult<()> {
    if u64::try_from(value).map_or(true, |value| value > maximum) {
        Err(NativeError::limit(message))
    } else {
        Ok(())
    }
}

pub(super) fn syntax_error() -> NativeError {
    NativeError::new("NATIVE_TURTLE_SYNTAX", "native Turtle source is malformed")
}

fn turtle_syntax() -> NativeError {
    syntax_error()
}

fn turtle_encoding() -> NativeError {
    NativeError::new(
        "NATIVE_TURTLE_ENCODING",
        "native Turtle source must be valid UTF-8",
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::{Cancellation, Guard};
    use crate::limits::Limits;
    use std::time::Duration;

    fn with_session<T>(
        source: &[u8],
        limits: &Limits,
        operation: impl FnOnce(&mut Session<'_>) -> NativeResult<T>,
    ) -> NativeResult<T> {
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session = Session::new(&mut guard, limits, source.len())?;
        let result = operation(&mut session)?;
        session.finish()?;
        Ok(result)
    }

    fn parse(source: &[u8]) -> NativeResult<CanonicalDocument> {
        let limits = Limits::default();
        with_session(source, &limits, |session| {
            Ok(parse_and_map_timed(
                source,
                Some("urn:test:document"),
                true,
                false,
                true,
                true,
                session,
            )?
            .0)
        })
    }

    #[test]
    fn turtle_maps_the_shared_rdf_owl_surface() {
        let source = br#"
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            @prefix ex: <urn:turtle:> .
            ex:ontology a owl:Ontology .
            ex:A a owl:Class ; rdfs:subClassOf ex:B .
            ex:B a owl:Class .
        "#;
        let document = parse(source).expect("Turtle document");
        assert_eq!(
            document.ontology_iri.as_deref(),
            Some("urn:turtle:ontology")
        );
        assert_eq!(document.axioms.len(), 3);
        assert_eq!(document.mapping.total_triples, 4);
        assert_eq!(
            document.mapping.total_triples,
            document.mapping.consumed_triples
        );
        assert_eq!(document.source_prefixes.len(), 4);
    }

    #[test]
    fn turtle_collections_and_blank_property_lists_match_rdfxml_mapping() {
        let turtle = br#"
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix ex: <urn:shared:> .
            ex:ontology a owl:Ontology .
            ex:A a owl:Class ;
                owl:equivalentClass [
                    a owl:Class ;
                    owl:intersectionOf ( ex:B ex:C )
                ] .
            ex:B a owl:Class .
            ex:C a owl:Class .
        "#;
        let rdfxml = br#"
            <rdf:RDF
                xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
                xmlns:owl="http://www.w3.org/2002/07/owl#">
              <owl:Ontology rdf:about="urn:shared:ontology"/>
              <owl:Class rdf:about="urn:shared:A">
                <owl:equivalentClass>
                  <owl:Class>
                    <owl:intersectionOf rdf:parseType="Collection">
                      <owl:Class rdf:about="urn:shared:B"/>
                      <owl:Class rdf:about="urn:shared:C"/>
                    </owl:intersectionOf>
                  </owl:Class>
                </owl:equivalentClass>
              </owl:Class>
              <owl:Class rdf:about="urn:shared:B"/>
              <owl:Class rdf:about="urn:shared:C"/>
            </rdf:RDF>
        "#;
        let turtle_document = parse(turtle).expect("Turtle document");
        let limits = Limits::default();
        let rdfxml_document = with_session(rdfxml, &limits, |session| {
            super::super::rdfxml::parse_and_map(rdfxml, Some("urn:test:document"), session)
        })
        .expect("RDF/XML document");
        assert_eq!(turtle_document.ontology_iri, rdfxml_document.ontology_iri);
        assert_eq!(turtle_document.axioms, rdfxml_document.axioms);
        assert_eq!(turtle_document.extensions, rdfxml_document.extensions);
    }

    #[test]
    fn turtle_rejects_syntax_encoding_and_memory_limits_before_retry() {
        let limits = Limits::default();
        let malformed_source = b"@prefix ex: <urn:ex:> . ex:A ex:p";
        let malformed = with_session(malformed_source, &limits, |session| {
            parse_and_map_timed(malformed_source, None, true, false, false, false, session)
        })
        .expect_err("truncated triple");
        assert_eq!(malformed.code, "NATIVE_TURTLE_SYNTAX");

        let invalid = with_session(&[0xff], &limits, |session| {
            parse_and_map_timed(&[0xff], None, true, false, false, false, session)
        })
        .expect_err("invalid UTF-8");
        assert_eq!(invalid.code, "NATIVE_TURTLE_ENCODING");

        let source = b"<urn:s> <urn:p> <urn:o> .";
        let mut limited = Limits::default();
        limited.max_memory_bytes = Some(u64::try_from(source.len()).expect("source length"));
        let error = with_session(source, &limited, |session| {
            parse_and_map_timed(source, None, true, true, false, false, session)
        })
        .expect_err("memory limit");
        assert_eq!(error.code, "NATIVE_WIRE_LIMIT");
        let retry = with_session(source, &Limits::default(), |session| {
            parse_and_map_timed(source, None, true, true, false, false, session)
        })
        .expect("retry");
        assert_eq!(retry.0.mapping.total_triples, 1);

        let mut deadline_limits = Limits::default();
        deadline_limits.cancellation_stride = 1;
        let cancellation = Cancellation::with_duration(Some(Duration::ZERO));
        let mut guard = Guard::new(
            cancellation,
            deadline_limits.deadline,
            deadline_limits.cancellation_stride,
        );
        let mut session =
            Session::new(&mut guard, &deadline_limits, source.len()).expect("session");
        let deadline = parse_and_map_timed(source, None, true, false, false, false, &mut session)
            .expect_err("expired parse");
        assert_eq!(deadline.code, "NATIVE_DEADLINE");
    }
}
