//! Owned PYOCORE v1 framing, canonical-model scanning, and semantic ledgers.

mod reader;

use std::cmp::Ordering;

use crate::cancel::Guard;
use crate::error::{NativeError, NativeResult};
use crate::hash::{crc32c, Sha256};
use crate::limits::{LimitKey, Limits, MemoryBudget};
use crate::model::{scan_canonical, validate_iri, Category, ScanBudget};

use reader::{u16_at, u32_at, u64_at, Reader};

const MAGIC: &[u8; 8] = b"PYOCORE\0";
const HEADER_BYTES: usize = 96;
const DIRECTORY_BYTES: usize = 72;
const WIRE_MAJOR: u16 = 1;
const MODEL_SCHEMA: u32 = 1;
const CANONICAL_PROFILE: u32 = 1;
const FEATURE_SWRL: u32 = 1;
const SECTION_REQUIRED: u16 = 1;
const SECTION_OPTIONAL: u16 = 2;
const SWRL_KIND: u16 = 0x8001;
const VIEW_PROVENANCE_KIND: u16 = 0x8002;
const ENCODED_STRUCTURAL_KIND: u16 = 0x8003;
const ENCODED_STRUCTURAL_MAGIC: &[u8; 8] = b"PYOCEV1\0";
const ENCODED_STRUCTURAL_DESCRIPTOR_SHA256: [u8; 32] = [
    0x9a, 0xd2, 0x9d, 0xb6, 0xa7, 0xe6, 0x16, 0xf6, 0x5c, 0xea, 0x29, 0x57, 0xbc, 0x5b, 0xa8, 0xd1,
    0xf9, 0xb9, 0x9e, 0xf0, 0xeb, 0x1f, 0xe1, 0x43, 0x2c, 0x09, 0xbe, 0x25, 0x78, 0x62, 0x67, 0xb5,
];
const NONE_U64: u64 = u64::MAX;
const HASH_CHUNK: usize = 64 * 1024;
pub(crate) const RECEIPT_MAGIC: &[u8; 8] = b"PYNVAL1\0";
pub(crate) const RECEIPT_BYTES: usize = 76;

#[derive(Debug, Default)]
struct AllocationProbe {
    #[cfg(feature = "test-hooks")]
    fail_after: Option<u64>,
    #[cfg(feature = "test-hooks")]
    allocations: u64,
}

impl AllocationProbe {
    const fn disabled() -> Self {
        Self {
            #[cfg(feature = "test-hooks")]
            fail_after: None,
            #[cfg(feature = "test-hooks")]
            allocations: 0,
        }
    }

    #[cfg(feature = "test-hooks")]
    const fn configured(fail_after: Option<u64>) -> Self {
        Self {
            fail_after,
            allocations: 0,
        }
    }

    fn checkpoint(&mut self) -> NativeResult<()> {
        #[cfg(feature = "test-hooks")]
        {
            if self
                .fail_after
                .is_some_and(|maximum| self.allocations >= maximum)
            {
                return Err(NativeError::limit(
                    "injected native wire allocation failure",
                ));
            }
            self.allocations = self
                .allocations
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native wire allocation counter overflow"))?;
        }
        Ok(())
    }

    #[cfg(feature = "test-hooks")]
    const fn count(&self) -> u64 {
        self.allocations
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Entry {
    kind: u16,
    flags: u16,
    schema: u32,
    offset: u64,
    stored_length: u64,
    decoded_length: u64,
    row_count: u64,
    digest: [u8; 32],
}

impl Entry {
    fn end(self) -> NativeResult<u64> {
        self.offset
            .checked_add(self.stored_length)
            .ok_or_else(|| NativeError::corrupt("wire section range overflow"))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Table {
    kind: u16,
    count: u64,
    section_start: usize,
    section_end: usize,
    payload_start: usize,
    digest: [u8; 32],
}

impl Table {
    fn row(self, data: &[u8], index: u64) -> NativeResult<&[u8]> {
        if index >= self.count {
            return Err(NativeError::corrupt(
                "wire table row reference exceeds bounds",
            ));
        }
        let index = usize::try_from(index)
            .map_err(|_| NativeError::corrupt("wire row index exceeds address space"))?;
        let section = data
            .get(self.section_start..self.section_end)
            .ok_or_else(|| NativeError::corrupt("wire table section exceeds bounds"))?;
        let start = usize_from_u64(u64_at(section, 8 + index * 8)?)?;
        let end = usize_from_u64(u64_at(section, 16 + index * 8)?)?;
        let absolute_start = self
            .payload_start
            .checked_add(start)
            .ok_or_else(|| NativeError::corrupt("wire row start overflow"))?;
        let absolute_end = self
            .payload_start
            .checked_add(end)
            .ok_or_else(|| NativeError::corrupt("wire row end overflow"))?;
        data.get(absolute_start..absolute_end)
            .ok_or_else(|| NativeError::corrupt("wire table row exceeds bounds"))
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct Validation {
    pub(crate) minor: u16,
    pub(crate) feature_flags: u32,
    pub(crate) total_length: u64,
    pub(crate) file_digest: [u8; 32],
    pub(crate) section_count: u32,
    pub(crate) total_rows: u64,
}

impl Validation {
    pub(crate) fn receipt(self) -> NativeResult<Vec<u8>> {
        let mut allocations = AllocationProbe::disabled();
        self.receipt_with_allocations(&mut allocations)
    }

    fn receipt_with_allocations(self, allocations: &mut AllocationProbe) -> NativeResult<Vec<u8>> {
        let mut result = Vec::new();
        allocations.checkpoint()?;
        result
            .try_reserve_exact(RECEIPT_BYTES)
            .map_err(|_| NativeError::limit("native receipt allocation failed"))?;
        result.extend_from_slice(RECEIPT_MAGIC);
        result.extend_from_slice(&crate::ABI_VERSION.to_le_bytes());
        result.extend_from_slice(&MODEL_SCHEMA.to_le_bytes());
        result.extend_from_slice(&WIRE_MAJOR.to_le_bytes());
        result.extend_from_slice(&self.minor.to_le_bytes());
        result.extend_from_slice(&self.feature_flags.to_le_bytes());
        result.extend_from_slice(&self.total_length.to_le_bytes());
        result.extend_from_slice(&self.file_digest);
        result.extend_from_slice(&self.section_count.to_le_bytes());
        result.extend_from_slice(&self.total_rows.to_le_bytes());
        debug_assert_eq!(result.len(), RECEIPT_BYTES);
        Ok(result)
    }
}

#[derive(Clone, Debug)]
pub(crate) struct WireArena {
    bytes: Vec<u8>,
    pub(crate) validation: Validation,
}

impl WireArena {
    pub(crate) fn decode(bytes: Vec<u8>, limits: &Limits, guard: &mut Guard) -> NativeResult<Self> {
        let validation = validate(&bytes, limits, guard)?;
        Ok(Self { bytes, validation })
    }

    pub(crate) fn encode(self) -> Vec<u8> {
        self.bytes
    }
}

fn validate(data: &[u8], limits: &Limits, guard: &mut Guard) -> NativeResult<Validation> {
    let mut allocations = AllocationProbe::disabled();
    validate_with_allocations(data, limits, guard, &mut allocations)
}

#[cfg(feature = "process-allocator-test")]
pub(crate) fn validate_process_allocator_fixture(
    data: &[u8],
    limits: &Limits,
    guard: &mut Guard,
) -> NativeResult<()> {
    validate(data, limits, guard).map(drop)
}

fn validate_with_allocations(
    data: &[u8],
    limits: &Limits,
    guard: &mut Guard,
    allocations: &mut AllocationProbe,
) -> NativeResult<Validation> {
    guard.check(0, true)?;
    limits.check_source_size(data.len())?;
    let mut memory = MemoryBudget::new(limits, data.len())?;
    if data.len() < HEADER_BYTES {
        return Err(NativeError::corrupt(
            "wire file is shorter than the fixed header",
        ));
    }
    if data.get(..8) != Some(MAGIC) {
        return Err(NativeError::corrupt("invalid PYOCORE magic"));
    }
    let major = u16_at(data, 8)?;
    let minor = u16_at(data, 10)?;
    let header_length = u32_at(data, 12)?;
    let feature_flags = u32_at(data, 16)?;
    let section_count = u32_at(data, 20)?;
    let model_schema = u32_at(data, 24)?;
    let profile = u32_at(data, 28)?;
    let total_length = u64_at(data, 32)?;
    let directory_offset = u64_at(data, 40)?;
    let directory_length = u64_at(data, 48)?;
    let file_digest = array_32(data, 56)?;
    let header_crc = u32_at(data, 88)?;
    let reserved = u32_at(data, 92)?;
    if major != WIRE_MAJOR {
        return Err(NativeError::version("unsupported PYOCORE major version"));
    }
    if header_length != HEADER_BYTES as u32 {
        return Err(NativeError::version("unsupported PYOCORE header layout"));
    }
    if model_schema != MODEL_SCHEMA {
        return Err(NativeError::version("unsupported PYOCORE model schema"));
    }
    if profile != CANONICAL_PROFILE {
        return Err(NativeError::version(
            "unsupported PYOCORE canonical profile",
        ));
    }
    if feature_flags & !FEATURE_SWRL != 0 {
        return Err(NativeError::version("unknown required PYOCORE feature"));
    }
    if reserved != 0 {
        return Err(NativeError::version(
            "nonzero reserved PYOCORE header field",
        ));
    }
    if usize_from_u64(total_length)? != data.len() {
        return Err(NativeError::corrupt(
            "wire total length does not match input",
        ));
    }
    if u64::from(section_count) > limits.max_wire_rows {
        return Err(NativeError::limit("wire section count exceeds limits"));
    }
    let expected_directory = u64::from(section_count)
        .checked_mul(DIRECTORY_BYTES as u64)
        .ok_or_else(|| NativeError::corrupt("wire directory length overflow"))?;
    if directory_offset != HEADER_BYTES as u64 || directory_length != expected_directory {
        return Err(NativeError::corrupt("wire directory metadata is invalid"));
    }
    let directory_end = directory_offset
        .checked_add(directory_length)
        .ok_or_else(|| NativeError::corrupt("wire directory range overflow"))?;
    if directory_end > total_length {
        return Err(NativeError::corrupt("wire directory exceeds file bounds"));
    }
    let mut zeroed_header = [0_u8; HEADER_BYTES];
    zeroed_header.copy_from_slice(&data[..HEADER_BYTES]);
    zeroed_header[56..92].fill(0);
    if crc32c(&zeroed_header) != header_crc {
        return Err(NativeError::corrupt("wire header CRC32C mismatch"));
    }

    let entry_capacity = usize::try_from(section_count)
        .map_err(|_| NativeError::limit("wire section count exceeds address space"))?;
    let mut entries = Vec::new();
    memory.reserve::<Entry>(entry_capacity)?;
    allocations.checkpoint()?;
    entries
        .try_reserve_exact(entry_capacity)
        .map_err(|_| NativeError::limit("wire directory allocation failed"))?;
    let mut previous_kind = 0_u16;
    let mut required_mask = 0_u16;
    let mut total_rows = 0_u64;
    let mut work = 0_u64;
    for index in 0..section_count {
        work = bump(work, 1)?;
        guard.check(work, false)?;
        let offset = HEADER_BYTES
            .checked_add(
                usize::try_from(index)
                    .map_err(|_| NativeError::corrupt("wire directory index overflow"))?
                    .checked_mul(DIRECTORY_BYTES)
                    .ok_or_else(|| NativeError::corrupt("wire directory index overflow"))?,
            )
            .ok_or_else(|| NativeError::corrupt("wire directory offset overflow"))?;
        let kind = u16_at(data, offset)?;
        let flags = u16_at(data, offset + 2)?;
        let schema = u32_at(data, offset + 4)?;
        let section_offset = u64_at(data, offset + 8)?;
        let stored_length = u64_at(data, offset + 16)?;
        let decoded_length = u64_at(data, offset + 24)?;
        let row_count = u64_at(data, offset + 32)?;
        let digest = array_32(data, offset + 40)?;
        if index != 0 && kind <= previous_kind {
            return Err(NativeError::corrupt(
                "wire directory kinds are not strictly ordered",
            ));
        }
        previous_kind = kind;
        if flags != SECTION_REQUIRED && flags != SECTION_OPTIONAL {
            return Err(NativeError::version("unknown PYOCORE section flags"));
        }
        let required_kind = (1..=14).contains(&kind);
        if flags == SECTION_REQUIRED {
            if !required_kind {
                return Err(NativeError::version("unknown required PYOCORE section"));
            }
            required_mask |= 1_u16 << (kind - 1);
        } else if required_kind {
            return Err(NativeError::version(
                "required PYOCORE section is marked optional",
            ));
        }
        if (required_kind
            || matches!(
                kind,
                SWRL_KIND | VIEW_PROVENANCE_KIND | ENCODED_STRUCTURAL_KIND
            ))
            && schema != 1
        {
            return Err(NativeError::version("unsupported PYOCORE section schema"));
        }
        if matches!(kind, VIEW_PROVENANCE_KIND | ENCODED_STRUCTURAL_KIND) && minor < 1 {
            return Err(NativeError::version(
                "optional PYOCORE section requires minor 1",
            ));
        }
        if decoded_length != stored_length {
            return Err(NativeError::version("unsupported PYOCORE section encoding"));
        }
        if section_offset % 8 != 0 || section_offset < directory_end {
            return Err(NativeError::corrupt("invalid PYOCORE section alignment"));
        }
        let entry = Entry {
            kind,
            flags,
            schema,
            offset: section_offset,
            stored_length,
            decoded_length,
            row_count,
            digest,
        };
        if entry.end()? > total_length {
            return Err(NativeError::corrupt("PYOCORE section exceeds file bounds"));
        }
        if row_count > limits.max_wire_rows || row_count > u64::from(u32::MAX) {
            return Err(NativeError::limit("PYOCORE row count exceeds limits"));
        }
        total_rows = total_rows
            .checked_add(row_count)
            .ok_or_else(|| NativeError::limit("PYOCORE total row count overflow"))?;
        entries.push(entry);
    }
    if required_mask != 0x3fff {
        return Err(NativeError::version("missing required PYOCORE section"));
    }

    let mut by_offset = Vec::new();
    memory.reserve::<Entry>(entries.len())?;
    allocations.checkpoint()?;
    by_offset
        .try_reserve_exact(entries.len())
        .map_err(|_| NativeError::limit("wire range validation allocation failed"))?;
    by_offset.extend(entries.iter().copied());
    by_offset.sort_unstable_by_key(|entry| entry.offset);
    let mut cursor = directory_end;
    for entry in &by_offset {
        if entry.offset < cursor {
            return Err(NativeError::corrupt("PYOCORE sections overlap"));
        }
        let padding = data
            .get(usize_from_u64(cursor)?..usize_from_u64(entry.offset)?)
            .ok_or_else(|| NativeError::corrupt("PYOCORE padding exceeds bounds"))?;
        if padding.iter().any(|byte| *byte != 0) {
            return Err(NativeError::corrupt("PYOCORE alignment padding is nonzero"));
        }
        cursor = entry.end()?;
    }
    let trailing = data
        .get(usize_from_u64(cursor)?..)
        .ok_or_else(|| NativeError::corrupt("PYOCORE trailing range exceeds bounds"))?;
    if trailing.iter().any(|byte| *byte != 0) {
        return Err(NativeError::corrupt("PYOCORE trailing padding is nonzero"));
    }

    let mut tables = Vec::new();
    memory.reserve::<Table>(16)?;
    allocations.checkpoint()?;
    tables
        .try_reserve_exact(16)
        .map_err(|_| NativeError::limit("wire table ledger allocation failed"))?;
    for entry in &entries {
        let section = data
            .get(usize_from_u64(entry.offset)?..usize_from_u64(entry.end()?)?)
            .ok_or_else(|| NativeError::corrupt("PYOCORE section exceeds bounds"))?;
        let digest = hash_checked(section, guard, &mut work)?;
        if digest != entry.digest {
            return Err(NativeError::corrupt("PYOCORE section SHA-256 mismatch"));
        }
        if (1..=14).contains(&entry.kind)
            || matches!(
                entry.kind,
                SWRL_KIND | VIEW_PROVENANCE_KIND | ENCODED_STRUCTURAL_KIND
            )
        {
            tables.push(validate_table(data, *entry, guard, &mut work)?);
        }
    }
    if limits.verify_file_digest {
        let mut hasher = Sha256::new();
        hash_update_checked(&mut hasher, &data[..56], guard, &mut work)?;
        hasher.update(&[0_u8; 36]);
        hash_update_checked(&mut hasher, &data[92..], guard, &mut work)?;
        if hasher.finish() != file_digest {
            return Err(NativeError::corrupt("PYOCORE file SHA-256 mismatch"));
        }
    }
    validate_semantics(
        data,
        &tables,
        feature_flags,
        limits,
        guard,
        &mut work,
        &mut memory,
        allocations,
    )?;
    guard.check(work, true)?;
    Ok(Validation {
        minor,
        feature_flags,
        total_length,
        file_digest,
        section_count,
        total_rows,
    })
}

fn validate_table(
    data: &[u8],
    entry: Entry,
    guard: &mut Guard,
    work: &mut u64,
) -> NativeResult<Table> {
    let section_start = usize_from_u64(entry.offset)?;
    let section_end = usize_from_u64(entry.end()?)?;
    let section = data
        .get(section_start..section_end)
        .ok_or_else(|| NativeError::corrupt("wire table exceeds section bounds"))?;
    if section.len() < 16 || u64_at(section, 0)? != entry.row_count {
        return Err(NativeError::corrupt("wire table count/header mismatch"));
    }
    let header_bytes_u64 = entry
        .row_count
        .checked_add(2)
        .and_then(|value| value.checked_mul(8))
        .ok_or_else(|| NativeError::corrupt("wire table offset header overflow"))?;
    let header_bytes = usize_from_u64(header_bytes_u64)?;
    if header_bytes > section.len() {
        return Err(NativeError::corrupt(
            "wire table offset header exceeds section",
        ));
    }
    let payload_size = section.len() - header_bytes;
    let mut previous_offset = 0_u64;
    let mut previous_row: Option<&[u8]> = None;
    for index in 0..=entry.row_count {
        *work = bump(*work, 1)?;
        guard.check(*work, false)?;
        let index = usize_from_u64(index)?;
        let offset = u64_at(section, 8 + index * 8)?;
        if (index == 0 && offset != 0) || offset < previous_offset || offset > payload_size as u64 {
            return Err(NativeError::corrupt("wire table row offsets are invalid"));
        }
        if index < usize_from_u64(entry.row_count)? {
            let next = u64_at(section, 16 + index * 8)?;
            if next < offset {
                return Err(NativeError::corrupt("wire table has a reversed row slice"));
            }
            let row = section
                .get(header_bytes + usize_from_u64(offset)?..header_bytes + usize_from_u64(next)?)
                .ok_or_else(|| NativeError::corrupt("wire table row exceeds payload"))?;
            if previous_row.is_some_and(|previous| row <= previous) {
                return Err(NativeError::corrupt(
                    "wire table rows are not strictly canonical",
                ));
            }
            previous_row = Some(row);
        }
        previous_offset = offset;
    }
    if previous_offset != payload_size as u64 {
        return Err(NativeError::corrupt(
            "wire table offsets do not cover payload exactly",
        ));
    }
    Ok(Table {
        kind: entry.kind,
        count: entry.row_count,
        section_start,
        section_end,
        payload_start: section_start + header_bytes,
        digest: entry.digest,
    })
}

#[allow(clippy::too_many_arguments)]
fn validate_semantics(
    data: &[u8],
    tables: &[Table],
    feature_flags: u32,
    limits: &Limits,
    guard: &mut Guard,
    work: &mut u64,
    memory: &mut MemoryBudget,
    allocations: &mut AllocationProbe,
) -> NativeResult<()> {
    let swrl = find_table(tables, SWRL_KIND);
    if swrl.is_some() != (feature_flags & FEATURE_SWRL != 0) {
        return Err(NativeError::version(
            "SWRL feature/section capability mismatch",
        ));
    }
    let strings = required_table(tables, 1)?;
    let annotations = required_table(tables, 7)?;
    let axioms = required_table(tables, 9)?;
    if strings.count > limits.max_strings {
        return Err(NativeError::limit("STRINGS count exceeds max_strings"));
    }
    if annotations.count > limits.max_annotations {
        return Err(NativeError::limit(
            "ANNOTATIONS count exceeds max_annotations",
        ));
    }
    if axioms.count > limits.max_axioms {
        return Err(NativeError::limit("AXIOMS count exceeds max_axioms"));
    }
    for index in 0..strings.count {
        checkpoint(work, guard)?;
        if std::str::from_utf8(strings.row(data, index)?).is_err() {
            return Err(NativeError::corrupt("STRINGS row is not valid UTF-8"));
        }
    }
    validate_model_table(
        data,
        required_table(tables, 2)?,
        Category::Iri,
        limits,
        guard,
        work,
    )?;
    validate_model_table(
        data,
        required_table(tables, 3)?,
        Category::Entity,
        limits,
        guard,
        work,
    )?;
    validate_model_table(
        data,
        required_table(tables, 4)?,
        Category::Literal,
        limits,
        guard,
        work,
    )?;
    validate_model_table(
        data,
        required_table(tables, 5)?,
        Category::Anonymous,
        limits,
        guard,
        work,
    )?;
    validate_sequences(data, required_table(tables, 6)?, limits, guard, work)?;
    validate_model_table(
        data,
        required_table(tables, 7)?,
        Category::Annotation,
        limits,
        guard,
        work,
    )?;
    validate_model_table(
        data,
        required_table(tables, 8)?,
        Category::Term,
        limits,
        guard,
        work,
    )?;
    validate_model_table(
        data,
        required_table(tables, 9)?,
        Category::Axiom,
        limits,
        guard,
        work,
    )?;
    if let Some(table) = swrl {
        validate_model_table(data, table, Category::Swrl, limits, guard, work)?;
    }
    let docs = validate_documents(data, tables, limits, guard, work, memory, allocations)?;
    validate_imports(data, tables, &docs, limits, guard, work)?;
    let view = validate_view(data, tables, &docs, limits)?;
    validate_encoded_structural(data, tables, limits)?;
    validate_view_provenance(data, tables, limits, guard, work)?;
    validate_origins(data, tables, limits, guard, work)?;
    validate_footer(data, tables, &view)?;
    Ok(())
}

fn validate_model_table(
    data: &[u8],
    table: Table,
    expected: Category,
    limits: &Limits,
    guard: &mut Guard,
    work: &mut u64,
) -> NativeResult<()> {
    for index in 0..table.count {
        checkpoint(work, guard)?;
        let mut budget = ScanBudget::from_limits(limits);
        if scan_canonical(table.row(data, index)?, &mut budget)? != expected {
            return Err(NativeError::corrupt(
                "canonical model row is in the wrong PYOCORE section",
            ));
        }
    }
    Ok(())
}

fn validate_sequences(
    data: &[u8],
    table: Table,
    limits: &Limits,
    guard: &mut Guard,
    work: &mut u64,
) -> NativeResult<()> {
    for index in 0..table.count {
        checkpoint(work, guard)?;
        let mut reader = Reader::new(table.row(data, index)?);
        if !matches!(reader.u8()?, 1 | 2) {
            return Err(NativeError::corrupt("unknown SEQUENCES collection kind"));
        }
        let count = reader.u64()?;
        if count > limits.max_sequence_arity {
            return Err(NativeError::limit("SEQUENCES arity exceeds limits"));
        }
        let bytes = count
            .checked_mul(32)
            .ok_or_else(|| NativeError::corrupt("SEQUENCES byte count overflow"))?;
        if usize_from_u64(bytes)? != reader.remaining() {
            return Err(NativeError::corrupt("invalid SEQUENCES digest vector"));
        }
        reader.take(usize_from_u64(bytes)?)?;
        reader.finish()?;
    }
    Ok(())
}

#[derive(Clone, Debug)]
struct Documents {
    key_ids: Vec<u32>,
    root_key_id: u32,
    total_source_bytes: u64,
}

fn validate_documents(
    data: &[u8],
    tables: &[Table],
    limits: &Limits,
    guard: &mut Guard,
    work: &mut u64,
    memory: &mut MemoryBudget,
    allocations: &mut AllocationProbe,
) -> NativeResult<Documents> {
    let table = required_table(tables, 10)?;
    if table.count > limits.max_documents {
        return Err(NativeError::limit("DOCUMENTS count exceeds limits"));
    }
    let strings = required_table(tables, 1)?;
    let iris = required_table(tables, 2)?;
    let annotations = required_table(tables, 7)?;
    let axioms = required_table(tables, 9)?;
    let extensions = find_table(tables, SWRL_KIND).map_or(0, |value| value.count);
    let mut key_ids = Vec::new();
    memory.reserve::<u32>(usize_from_u64(table.count)?)?;
    allocations.checkpoint()?;
    key_ids
        .try_reserve_exact(usize_from_u64(table.count)?)
        .map_err(|_| NativeError::limit("DOCUMENTS key ledger allocation failed"))?;
    let mut root_key_id = 0_u32;
    let mut root_count = 0_u64;
    let mut total_source_bytes = 0_u64;
    for index in 0..table.count {
        checkpoint(work, guard)?;
        let mut reader = Reader::new(table.row(data, index)?);
        let key = required_ref(reader.u32()?, strings.count)?;
        let key_bytes = strings.row(data, u64::from(key - 1))?;
        if key_bytes.is_empty() || !key_bytes.is_ascii() {
            return Err(NativeError::corrupt("DOCUMENTS key must be nonempty ASCII"));
        }
        let document_ontology = optional_ref(reader.u32()?, iris.count)?;
        let document_version = optional_ref(reader.u32()?, iris.count)?;
        optional_ref(reader.u32()?, iris.count)?;
        let record_ontology = optional_ref(reader.u32()?, iris.count)?;
        let record_version = optional_ref(reader.u32()?, iris.count)?;
        optional_ref(reader.u32()?, iris.count)?;
        if (document_version != 0 && document_ontology == 0)
            || (record_version != 0 && record_ontology == 0)
        {
            return Err(NativeError::corrupt(
                "DOCUMENTS version IRI has no ontology IRI",
            ));
        }
        reader.take(32)?;
        read_fingerprint(&mut reader)?;
        enum_u8(reader.u8()?, 1, 4)?;
        let status = enum_u8(reader.u8()?, 1, 2)?;
        reader.take(32)?;
        enum_u8(reader.u8()?, 1, 2)?;
        let byte_length = reader.u64()?;
        if byte_length > limits.max_source_bytes {
            return Err(NativeError::limit(
                "DOCUMENTS source exceeds max_source_bytes",
            ));
        }
        let _codepoints = reader.u64()?;
        optional_ref(reader.u32()?, iris.count)?;
        enum_u8(reader.u8()?, 1, 4)?;
        enum_u8(reader.u8()?, 1, 4)?;
        if reader.boolean()? {
            reader.take(32)?;
        }
        let parser = required_ref(reader.u32()?, strings.count)?;
        let backend = required_ref(reader.u32()?, strings.count)?;
        if strings.row(data, u64::from(parser - 1))?.is_empty()
            || strings.row(data, u64::from(backend - 1))?.is_empty()
        {
            return Err(NativeError::corrupt(
                "DOCUMENTS parser/backend metadata is empty",
            ));
        }
        reader.u16()?;
        reader.u16()?;
        if reader.u32()? != MODEL_SCHEMA {
            return Err(NativeError::version(
                "DOCUMENTS model schema is unsupported",
            ));
        }
        read_refs(&mut reader, limits.max_wire_rows, iris.count)?;
        read_refs(&mut reader, limits.max_annotations, annotations.count)?;
        read_refs(&mut reader, limits.max_axioms, axioms.count)?;
        read_refs(&mut reader, limits.max_wire_rows, extensions)?;
        read_refs(&mut reader, limits.max_annotations, annotations.count)?;
        read_refs(&mut reader, limits.max_axioms, axioms.count)?;
        read_refs(&mut reader, limits.max_wire_rows, extensions)?;
        reader.finish()?;
        total_source_bytes = total_source_bytes
            .checked_add(byte_length)
            .ok_or_else(|| NativeError::limit("DOCUMENTS source byte count overflow"))?;
        if total_source_bytes > limits.max_total_source_bytes {
            return Err(NativeError::limit(
                "DOCUMENTS sources exceed max_total_source_bytes",
            ));
        }
        key_ids.push(key);
        if status == 1 {
            root_count += 1;
            root_key_id = key;
        }
    }
    key_ids.sort_unstable();
    if key_ids.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(NativeError::corrupt("duplicate DOCUMENTS key"));
    }
    if root_count != 1 {
        return Err(NativeError::corrupt(
            "DOCUMENTS must contain exactly one root",
        ));
    }
    Ok(Documents {
        key_ids,
        root_key_id,
        total_source_bytes,
    })
}

#[cfg(feature = "test-hooks")]
pub(crate) fn allocation_probe(
    data: &[u8],
    limits: &Limits,
    guard: &mut Guard,
    fail_after: Option<u64>,
) -> NativeResult<(Vec<u8>, u64)> {
    let mut allocations = AllocationProbe::configured(fail_after);
    let validation = validate_with_allocations(data, limits, guard, &mut allocations)?;
    let receipt = validation.receipt_with_allocations(&mut allocations)?;
    Ok((receipt, allocations.count()))
}

fn validate_imports(
    data: &[u8],
    tables: &[Table],
    documents: &Documents,
    limits: &Limits,
    guard: &mut Guard,
    work: &mut u64,
) -> NativeResult<()> {
    let table = required_table(tables, 11)?;
    if table.count != 1 {
        return Err(NativeError::corrupt("IMPORTS must contain exactly one row"));
    }
    let strings = required_table(tables, 1)?;
    let iris = required_table(tables, 2)?;
    let mut reader = Reader::new(table.row(data, 0)?);
    enum_u8(reader.u8()?, 1, 4)?;
    reader.boolean()?;
    reader.take(32)?;
    let count = reader.u64()?;
    if count > limits.max_wire_rows || count > (reader.remaining() / 21) as u64 {
        return Err(NativeError::limit(
            "IMPORTS edge count exceeds limits/bounds",
        ));
    }
    let mut previous: Option<(u32, u32, u8, u32)> = None;
    for _ in 0..count {
        checkpoint(work, guard)?;
        let importer = required_ref(reader.u32()?, strings.count)?;
        if documents.key_ids.binary_search(&importer).is_err() {
            return Err(NativeError::corrupt(
                "IMPORTS importer is absent from DOCUMENTS",
            ));
        }
        let iri = required_ref(reader.u32()?, iris.count)?;
        let status = enum_u8(reader.u8()?, 1, 5)?;
        let target = optional_ref(reader.u32()?, strings.count)?;
        optional_ref(reader.u32()?, strings.count)?;
        let diagnostic = optional_ref(reader.u32()?, strings.count)?;
        if (status == 1 && (target == 0 || documents.key_ids.binary_search(&target).is_err()))
            || (status != 1 && target != 0)
        {
            return Err(NativeError::corrupt(
                "IMPORTS target/status relationship is invalid",
            ));
        }
        if diagnostic != 0 && !diagnostic_code(strings.row(data, u64::from(diagnostic - 1))?) {
            return Err(NativeError::corrupt("IMPORTS diagnostic code is invalid"));
        }
        let status_rank = match status {
            4 => 0,
            5 => 1,
            3 => 2,
            1 => 3,
            2 => 4,
            _ => unreachable!(),
        };
        let key = (importer, iri, status_rank, target);
        if previous.is_some_and(|value| key < value) {
            return Err(NativeError::corrupt("IMPORTS edges are not canonical"));
        }
        previous = Some(key);
    }
    reader.finish()
}

#[derive(Clone, Debug)]
struct View {
    fingerprints: [[u8; 36]; 3],
}

fn validate_view(
    data: &[u8],
    tables: &[Table],
    documents: &Documents,
    limits: &Limits,
) -> NativeResult<View> {
    let table = required_table(tables, 12)?;
    if table.count != 1 {
        return Err(NativeError::corrupt("VIEW must contain exactly one row"));
    }
    let strings = required_table(tables, 1)?;
    let annotations = required_table(tables, 7)?;
    let axioms = required_table(tables, 9)?;
    let extensions = find_table(tables, SWRL_KIND).map_or(0, |value| value.count);
    let mut reader = Reader::new(table.row(data, 0)?);
    let root = required_ref(reader.u32()?, strings.count)?;
    if documents.key_ids.binary_search(&root).is_err() || root != documents.root_key_id {
        return Err(NativeError::corrupt("VIEW root key is invalid"));
    }
    reader.boolean()?;
    let context_tag = reader.u8()?;
    let context_count = u64::from(reader.u32()?);
    if context_count > limits.max_composite_members {
        return Err(NativeError::limit("VIEW context count exceeds limits"));
    }
    if !matches!((context_tag, context_count), (0, 0) | (1, 1) | (2, 2..)) {
        return Err(NativeError::corrupt("VIEW structural context is invalid"));
    }
    for _ in 0..context_count {
        read_fingerprint(&mut reader)?;
    }
    let fingerprints = [
        read_fingerprint(&mut reader)?,
        read_fingerprint(&mut reader)?,
        read_fingerprint(&mut reader)?,
    ];
    let document_count = reader.u64()?;
    if document_count != documents.key_ids.len() as u64 || document_count > limits.max_documents {
        return Err(NativeError::corrupt(
            "VIEW document count disagrees with DOCUMENTS",
        ));
    }
    let effective_axiom_count = reader.u64()?;
    if effective_axiom_count > limits.max_axioms {
        return Err(NativeError::limit("VIEW axiom count exceeds limits"));
    }
    read_refs(&mut reader, limits.max_annotations, annotations.count)?;
    let axiom_postings = read_refs(&mut reader, limits.max_axioms, axioms.count)?;
    read_refs(&mut reader, limits.max_wire_rows, extensions)?;
    reader.finish()?;
    if axiom_postings != effective_axiom_count {
        return Err(NativeError::corrupt(
            "VIEW axiom count disagrees with postings",
        ));
    }
    let _ = documents.total_source_bytes;
    Ok(View { fingerprints })
}

fn validate_view_provenance(
    data: &[u8],
    tables: &[Table],
    limits: &Limits,
    guard: &mut Guard,
    work: &mut u64,
) -> NativeResult<()> {
    let Some(table) = find_table(tables, VIEW_PROVENANCE_KIND) else {
        return Ok(());
    };
    if table.count != 1 {
        return Err(NativeError::corrupt(
            "VIEW_PROVENANCE must contain exactly one row",
        ));
    }
    let mut reader = Reader::new(table.row(data, 0)?);
    reader.take(32)?;
    reader.take(32)?;
    let count = reader.u64()?;
    if count > limits.value(LimitKey::MaxIndexRows) {
        return Err(NativeError::limit(
            "VIEW_PROVENANCE document count exceeds max_index_rows",
        ));
    }
    if count == 0 || count > reader.remaining() as u64 / 11 {
        return Err(NativeError::corrupt(
            "VIEW_PROVENANCE document count exceeds row bounds",
        ));
    }
    let mut previous_key: Option<&[u8]> = None;
    for _ in 0..count {
        checkpoint(work, guard)?;
        let key = read_identity_text(&mut reader)?;
        if key.is_empty()
            || std::str::from_utf8(key).is_err()
            || previous_key.is_some_and(|previous| key <= previous)
        {
            return Err(NativeError::corrupt(
                "VIEW_PROVENANCE document keys are not canonical",
            ));
        }
        previous_key = Some(key);
        let has_ontology = read_identity_iri(&mut reader, limits)?;
        let has_version = read_identity_iri(&mut reader, limits)?;
        if has_version && !has_ontology {
            return Err(NativeError::corrupt(
                "VIEW_PROVENANCE version IRI has no ontology IRI",
            ));
        }
    }
    reader.finish()
}

fn validate_encoded_structural(data: &[u8], tables: &[Table], limits: &Limits) -> NativeResult<()> {
    let Some(table) = find_table(tables, ENCODED_STRUCTURAL_KIND) else {
        return Ok(());
    };
    if table.count != 1 {
        return Err(NativeError::corrupt(
            "ENCODED_STRUCTURAL_V1 must contain exactly one row",
        ));
    }
    let row = table.row(data, 0)?;
    const BUFFER_COUNT: usize = 11;
    const HEADER_BYTES: usize = 80;
    const DIRECTORY_BYTES: usize = 16;
    const PREFIX_BYTES: usize = HEADER_BYTES + BUFFER_COUNT * DIRECTORY_BYTES;
    const WIDTHS: [usize; BUFFER_COUNT] = [1, 4, 2, 8, 1, 8, 8, 1, 8, 8, 1];
    if row.len() < PREFIX_BYTES
        || row.get(..8) != Some(ENCODED_STRUCTURAL_MAGIC)
        || u16_at(row, 8)? != 1
        || u16_at(row, 10)? != 1
        || u32_at(row, 12)? != BUFFER_COUNT as u32
        || row.get(16..48) != Some(&ENCODED_STRUCTURAL_DESCRIPTOR_SHA256)
    {
        return Err(NativeError::corrupt(
            "ENCODED_STRUCTURAL_V1 descriptor metadata is invalid",
        ));
    }
    let mut buffers: [&[u8]; BUFFER_COUNT] = [&[]; BUFFER_COUNT];
    let mut cursor = PREFIX_BYTES;
    let mut total_bytes = 0_u64;
    for (index, width) in WIDTHS.into_iter().enumerate() {
        let offset = usize_from_u64(u64_at(row, HEADER_BYTES + index * DIRECTORY_BYTES)?)?;
        let length = usize_from_u64(u64_at(row, HEADER_BYTES + index * DIRECTORY_BYTES + 8)?)?;
        let expected = cursor
            .checked_add(7)
            .map(|value| value & !7)
            .ok_or_else(|| NativeError::corrupt("encoded column alignment overflow"))?;
        let end = offset
            .checked_add(length)
            .ok_or_else(|| NativeError::corrupt("encoded column range overflow"))?;
        if offset != expected
            || end > row.len()
            || (index != BUFFER_COUNT - 1 && length % width != 0)
            || row
                .get(cursor..offset)
                .is_none_or(|padding| padding.iter().any(|byte| *byte != 0))
        {
            return Err(NativeError::corrupt(
                "ENCODED_STRUCTURAL_V1 buffer directory is invalid",
            ));
        }
        buffers[index] = row
            .get(offset..end)
            .ok_or_else(|| NativeError::corrupt("encoded column exceeds row bounds"))?;
        total_bytes = total_bytes
            .checked_add(length as u64)
            .ok_or_else(|| NativeError::limit("encoded column byte count overflow"))?;
        cursor = end;
    }
    if cursor != row.len() {
        return Err(NativeError::corrupt(
            "ENCODED_STRUCTURAL_V1 row has trailing bytes",
        ));
    }
    if total_bytes > limits.value(LimitKey::MaxIndexBytes) {
        return Err(NativeError::limit("encoded columns exceed max_index_bytes"));
    }
    validate_encoded_column_shapes(buffers, limits)
}

fn validate_encoded_column_shapes(buffers: [&[u8]; 11], limits: &Limits) -> NativeResult<()> {
    let root_count = buffers[0].len();
    let node_count = buffers[2].len() / 2;
    let field_count = buffers[4].len();
    let item_count = buffers[7].len();
    if buffers[1].len() != root_count.saturating_mul(4)
        || buffers[3].len() != node_count.saturating_add(1).saturating_mul(8)
        || buffers[5].len() != field_count.saturating_mul(8)
        || buffers[6].len() != field_count.saturating_mul(8)
        || buffers[8].len() != item_count.saturating_mul(8)
        || buffers[9].len() != item_count.saturating_mul(8)
    {
        return Err(NativeError::corrupt(
            "encoded structural column lengths disagree",
        ));
    }
    let maximum_rows = root_count.max(node_count).max(field_count).max(item_count) as u64;
    if node_count as u64 > limits.max_terms || maximum_rows > limits.value(LimitKey::MaxIndexRows) {
        return Err(NativeError::limit("encoded structural rows exceed limits"));
    }
    if u64_at(buffers[3], 0)? != 0 || u64_at(buffers[3], node_count * 8)? != field_count as u64 {
        return Err(NativeError::corrupt(
            "encoded structural field offsets are invalid",
        ));
    }
    let mut previous = 0_u64;
    for index in 1..=node_count {
        let current = u64_at(buffers[3], index * 8)?;
        if current < previous || current > field_count as u64 {
            return Err(NativeError::corrupt(
                "encoded structural field offsets are invalid",
            ));
        }
        previous = current;
    }
    for index in 0..root_count {
        if !matches!(buffers[0][index], 1..=3) {
            return Err(NativeError::corrupt(
                "encoded structural root kind is invalid",
            ));
        }
        let node_id = u32_at(buffers[1], index * 4)?;
        if node_id == 0 || node_id as usize > node_count {
            return Err(NativeError::corrupt(
                "encoded structural root ID is invalid",
            ));
        }
    }
    let mut item_cursor = 0_usize;
    let mut scalar_cursor = 0_usize;
    for index in 0..field_count {
        let kind = buffers[4][index];
        let value = u64_at(buffers[5], index * 8)?;
        let length = u64_at(buffers[6], index * 8)?;
        if matches!(kind, 6 | 7) {
            let start = usize_from_u64(value)?;
            let count = usize_from_u64(length)?;
            let end = start
                .checked_add(count)
                .ok_or_else(|| NativeError::corrupt("encoded item range overflow"))?;
            if start != item_cursor || end > item_count {
                return Err(NativeError::corrupt(
                    "encoded structural item range is invalid",
                ));
            }
            for item_index in start..end {
                scalar_cursor = validate_encoded_leaf(
                    buffers[7][item_index],
                    u64_at(buffers[8], item_index * 8)?,
                    u64_at(buffers[9], item_index * 8)?,
                    node_count,
                    buffers[10],
                    scalar_cursor,
                )?;
            }
            item_cursor = end;
        } else {
            scalar_cursor =
                validate_encoded_leaf(kind, value, length, node_count, buffers[10], scalar_cursor)?;
        }
    }
    if item_cursor != item_count || scalar_cursor != buffers[10].len() {
        return Err(NativeError::corrupt(
            "encoded structural arenas are not exactly covered",
        ));
    }
    Ok(())
}

fn validate_encoded_leaf(
    kind: u8,
    value: u64,
    length: u64,
    node_count: usize,
    scalar_bytes: &[u8],
    scalar_cursor: usize,
) -> NativeResult<usize> {
    match kind {
        0 if value == 0 && length == 0 => Ok(scalar_cursor),
        1 if length == 0 && value != 0 && value <= node_count as u64 => Ok(scalar_cursor),
        2..=5 => {
            let start = usize_from_u64(value)?;
            let length = usize_from_u64(length)?;
            let end = start
                .checked_add(length)
                .ok_or_else(|| NativeError::corrupt("encoded scalar range overflow"))?;
            let payload = scalar_bytes
                .get(start..end)
                .ok_or_else(|| NativeError::corrupt("encoded scalar exceeds arena bounds"))?;
            if start != scalar_cursor
                || (kind == 2 && std::str::from_utf8(payload).is_err())
                || (kind == 5 && !payload.is_ascii())
                || (kind == 4
                    && (payload.is_empty() || (payload.len() > 1 && payload.last() == Some(&0))))
            {
                return Err(NativeError::corrupt("encoded structural scalar is invalid"));
            }
            Ok(end)
        }
        _ => Err(NativeError::corrupt(
            "encoded structural component kind is invalid",
        )),
    }
}

fn read_identity_text<'a>(reader: &mut Reader<'a>) -> NativeResult<&'a [u8]> {
    let length = usize_from_u64(reader.u64()?)?;
    reader.take(length)
}

fn read_identity_iri(reader: &mut Reader<'_>, limits: &Limits) -> NativeResult<bool> {
    if !reader.boolean()? {
        return Ok(false);
    }
    let value = read_identity_text(reader)?;
    if value.len() as u64 > limits.max_iri_bytes {
        return Err(NativeError::limit(
            "VIEW_PROVENANCE IRI exceeds max_iri_bytes",
        ));
    }
    let text = std::str::from_utf8(value)
        .map_err(|_| NativeError::corrupt("VIEW_PROVENANCE IRI is not UTF-8"))?;
    validate_iri(text)?;
    Ok(true)
}

fn validate_origins(
    data: &[u8],
    tables: &[Table],
    limits: &Limits,
    guard: &mut Guard,
    work: &mut u64,
) -> NativeResult<()> {
    let table = required_table(tables, 13)?;
    let strings = required_table(tables, 1)?;
    let mut total = 0_u64;
    let mut previous_digest: Option<[u8; 32]> = None;
    for index in 0..table.count {
        checkpoint(work, guard)?;
        let mut reader = Reader::new(table.row(data, index)?);
        let digest = array_from_slice(reader.take(32)?)?;
        if previous_digest.is_some_and(|value| digest <= value) {
            return Err(NativeError::corrupt("ORIGINS digests are not canonical"));
        }
        previous_digest = Some(digest);
        let count = reader.u64()?;
        total = total
            .checked_add(count)
            .ok_or_else(|| NativeError::limit("ORIGINS count overflow"))?;
        if total > limits.max_origin_entries
            || total > limits.max_source_map_entries
            || count > (reader.remaining() / 60) as u64
        {
            return Err(NativeError::limit("ORIGINS entries exceed limits/bounds"));
        }
        let mut previous: Option<(u32, u64, [u64; 6])> = None;
        for _ in 0..count {
            let document = required_ref(reader.u32()?, strings.count)?;
            if strings.row(data, u64::from(document - 1))?.is_empty() {
                return Err(NativeError::corrupt("ORIGINS document key is invalid"));
            }
            let occurrence = reader.u64()?;
            let span = [
                reader.u64()?,
                reader.u64()?,
                reader.u64()?,
                reader.u64()?,
                reader.u64()?,
                reader.u64()?,
            ];
            validate_span(span)?;
            let key = (document, occurrence, span);
            if previous.is_some_and(|value| compare_origin(&key, &value) != Ordering::Greater) {
                return Err(NativeError::corrupt(
                    "ORIGINS occurrences are not canonical",
                ));
            }
            previous = Some(key);
        }
        reader.finish()?;
    }
    Ok(())
}

fn validate_footer(data: &[u8], tables: &[Table], view: &View) -> NativeResult<()> {
    let table = required_table(tables, 14)?;
    if table.count != 1 {
        return Err(NativeError::corrupt("FOOTER must contain exactly one row"));
    }
    let mut reader = Reader::new(table.row(data, 0)?);
    for expected in &view.fingerprints {
        if &read_fingerprint(&mut reader)? != expected {
            return Err(NativeError::corrupt(
                "FOOTER fingerprints disagree with VIEW",
            ));
        }
    }
    if reader.u16()? != 13 {
        return Err(NativeError::corrupt("FOOTER section count is invalid"));
    }
    for kind in 1_u16..=13 {
        if reader.u16()? != kind {
            return Err(NativeError::corrupt(
                "FOOTER section kinds are not canonical",
            ));
        }
        let rows = reader.u64()?;
        let digest = array_from_slice(reader.take(32)?)?;
        let expected = required_table(tables, kind)?;
        if rows != expected.count || digest != expected.digest {
            return Err(NativeError::corrupt(
                "FOOTER ledger disagrees with directory",
            ));
        }
    }
    reader.finish()
}

fn read_refs(reader: &mut Reader<'_>, maximum: u64, target_rows: u64) -> NativeResult<u64> {
    let count = reader.u64()?;
    if count > maximum || count > (reader.remaining() / 4) as u64 {
        return Err(NativeError::limit(
            "wire reference count exceeds limits/bounds",
        ));
    }
    let mut previous = 0_u32;
    for _ in 0..count {
        let value = reader.u32()?;
        if value <= previous || u64::from(value) > target_rows {
            return Err(NativeError::corrupt("wire reference list is not canonical"));
        }
        previous = value;
    }
    Ok(count)
}

fn read_fingerprint(reader: &mut Reader<'_>) -> NativeResult<[u8; 36]> {
    let schema = reader.u32()?;
    if schema == 0 {
        return Err(NativeError::corrupt("wire fingerprint schema is invalid"));
    }
    let digest = reader.take(32)?;
    let mut result = [0_u8; 36];
    result[..4].copy_from_slice(&schema.to_le_bytes());
    result[4..].copy_from_slice(digest);
    Ok(result)
}

fn validate_span(span: [u64; 6]) -> NativeResult<()> {
    let value = |index: usize| (span[index] != NONE_U64).then_some(span[index]);
    if value(2).is_some_and(|number| number == 0)
        || value(3).is_some_and(|number| number == 0)
        || value(4).is_some_and(|number| number == 0)
        || value(5).is_some_and(|number| number == 0)
        || matches!((value(0), value(1)), (Some(start), Some(end)) if end < start)
        || matches!((value(2), value(4), value(3), value(5)),
            (Some(start_line), Some(end_line), start_column, end_column)
            if (end_line, end_column.unwrap_or(1)) < (start_line, start_column.unwrap_or(1)))
    {
        return Err(NativeError::corrupt("ORIGINS source span is invalid"));
    }
    Ok(())
}

fn compare_origin(left: &(u32, u64, [u64; 6]), right: &(u32, u64, [u64; 6])) -> Ordering {
    left.0
        .cmp(&right.0)
        .then_with(|| left.1.cmp(&right.1))
        .then_with(|| left.2.cmp(&right.2))
}

fn diagnostic_code(value: &[u8]) -> bool {
    value.first().is_some_and(|byte| byte.is_ascii_uppercase())
        && value
            .iter()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || *byte == b'_')
}

fn enum_u8(value: u8, minimum: u8, maximum: u8) -> NativeResult<u8> {
    if (minimum..=maximum).contains(&value) {
        Ok(value)
    } else {
        Err(NativeError::corrupt("unknown wire enum tag"))
    }
}

fn required_ref(value: u32, target_rows: u64) -> NativeResult<u32> {
    if value != 0 && u64::from(value) <= target_rows {
        Ok(value)
    } else {
        Err(NativeError::corrupt("invalid required wire reference"))
    }
}

fn optional_ref(value: u32, target_rows: u64) -> NativeResult<u32> {
    if u64::from(value) <= target_rows {
        Ok(value)
    } else {
        Err(NativeError::corrupt("invalid optional wire reference"))
    }
}

fn required_table(tables: &[Table], kind: u16) -> NativeResult<Table> {
    find_table(tables, kind)
        .ok_or_else(|| NativeError::version("required PYOCORE table is unavailable"))
}

fn find_table(tables: &[Table], kind: u16) -> Option<Table> {
    tables.iter().find(|table| table.kind == kind).copied()
}

fn array_32(data: &[u8], offset: usize) -> NativeResult<[u8; 32]> {
    let value = data
        .get(offset..offset + 32)
        .ok_or_else(|| NativeError::corrupt("truncated wire digest"))?;
    array_from_slice(value)
}

fn array_from_slice(value: &[u8]) -> NativeResult<[u8; 32]> {
    value
        .try_into()
        .map_err(|_| NativeError::corrupt("wire digest has invalid width"))
}

fn usize_from_u64(value: u64) -> NativeResult<usize> {
    usize::try_from(value).map_err(|_| NativeError::corrupt("wire scalar exceeds address space"))
}

fn bump(work: u64, amount: u64) -> NativeResult<u64> {
    work.checked_add(amount)
        .ok_or_else(|| NativeError::limit("native work counter overflow"))
}

fn checkpoint(work: &mut u64, guard: &mut Guard) -> NativeResult<()> {
    *work = bump(*work, 1)?;
    guard.check(*work, false)
}

fn hash_checked(data: &[u8], guard: &mut Guard, work: &mut u64) -> NativeResult<[u8; 32]> {
    let mut hasher = Sha256::new();
    hash_update_checked(&mut hasher, data, guard, work)?;
    Ok(hasher.finish())
}

fn hash_update_checked(
    hasher: &mut Sha256,
    data: &[u8],
    guard: &mut Guard,
    work: &mut u64,
) -> NativeResult<()> {
    for chunk in data.chunks(HASH_CHUNK) {
        hasher.update(chunk);
        *work = bump(
            *work,
            u64::try_from(chunk.len())
                .map_err(|_| NativeError::limit("native hash work counter overflow"))?,
        )?;
        guard.check(*work, false)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::Cancellation;

    #[test]
    fn receipt_layout_is_frozen() {
        let validation = Validation {
            minor: 0,
            feature_flags: 0,
            total_length: 123,
            file_digest: [7; 32],
            section_count: 14,
            total_rows: 99,
        };
        let receipt = validation.receipt().unwrap();
        assert_eq!(receipt.len(), RECEIPT_BYTES);
        assert_eq!(&receipt[..8], RECEIPT_MAGIC);
        assert_eq!(
            u32::from_le_bytes(receipt[8..12].try_into().unwrap()),
            crate::ABI_VERSION
        );
    }

    #[test]
    fn truncated_wire_fails_without_panicking() {
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, None, 1);
        assert_eq!(
            validate(&[], &limits, &mut guard).unwrap_err().code,
            "NATIVE_WIRE_CORRUPTION"
        );
    }
}
