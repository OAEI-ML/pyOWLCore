//! Pure retained-publication auxiliary rows shared by extension and comparator builds.

pub(crate) const AUXILIARY_CODEC_SCHEMA_SHA256_V2: [u8; 32] = [
    0x9c, 0x9f, 0x12, 0x91, 0xd2, 0x1a, 0xe5, 0xac, 0x52, 0x0d, 0x10, 0x61, 0x20, 0xf5, 0x56, 0x33,
    0x5b, 0x23, 0x81, 0xc0, 0x18, 0xa4, 0x9e, 0xaa, 0x53, 0xfb, 0xaf, 0x1f, 0x3b, 0x5b, 0xa4, 0x60,
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
