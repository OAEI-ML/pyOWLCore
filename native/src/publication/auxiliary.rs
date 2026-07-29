//! Pure retained-publication auxiliary rows shared by extension and comparator builds.

pub(crate) const AUXILIARY_CODEC_SCHEMA_SHA256_V2: [u8; 32] = [
    0x60, 0x72, 0x8e, 0xf2, 0x00, 0x6e, 0x0b, 0x9c, 0x46, 0x7e, 0x4e, 0x7d, 0xd1, 0xb4, 0x38, 0xb9,
    0x13, 0x34, 0x48, 0xfd, 0x3d, 0x2b, 0x6b, 0xe6, 0x7d, 0x7e, 0xd4, 0x01, 0x93, 0x7e, 0x8a, 0xab,
];

#[derive(Debug)]
pub(crate) struct TypedRdfReportRowsV2 {
    pub(crate) header: Vec<u8>,
    pub(crate) unconsumed_triples: Vec<Vec<u8>>,
    pub(crate) rule_ids: Vec<Vec<u8>>,
    pub(crate) diagnostics: Vec<Vec<u8>>,
}

#[derive(Debug)]
pub(crate) struct TypedSourceMapRowsV2 {
    pub(crate) entries: Vec<Vec<u8>>,
    pub(crate) prefixes: Vec<Vec<u8>>,
}
