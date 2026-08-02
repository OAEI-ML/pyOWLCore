#[allow(dead_code)]
#[path = "src/hash.rs"]
mod native_hash;

use std::env;
use std::fmt::Write as _;
use std::fs;
use std::path::PathBuf;

#[derive(Clone, Copy)]
struct EncodedViewSchemaInput {
    ledger: &'static str,
    descriptor: &'static str,
    generated_source: &'static str,
    generated_descriptor: &'static str,
    expected_version: u32,
    expected_model_schema: u32,
}

const ENCODED_VIEW_SCHEMAS: [EncodedViewSchemaInput; 2] = [
    EncodedViewSchemaInput {
        ledger: "../schemas/encoded-view-v1.toml",
        descriptor: "../schemas/encoded-view-v1.json",
        generated_source: "encoded_view_v1.rs",
        generated_descriptor: "encoded-view-v1.json",
        expected_version: 1,
        expected_model_schema: 1,
    },
    EncodedViewSchemaInput {
        ledger: "../schemas/encoded-view-v2.toml",
        descriptor: "../schemas/encoded-view-v2.json",
        generated_source: "encoded_view_v2.rs",
        generated_descriptor: "encoded-view-v2.json",
        expected_version: 2,
        expected_model_schema: 2,
    },
];

fn main() {
    println!("cargo:rustc-check-cfg=cfg(fuzzing)");
    for input in ENCODED_VIEW_SCHEMAS {
        if let Err(error) = generate_encoded_view_schema(input) {
            panic!(
                "cannot generate native encoded-view schema {}: {error}",
                input.expected_version
            );
        }
    }
    #[cfg(feature = "extension-module")]
    {
        pyo3_build_config::add_extension_module_link_args();
    }
}

fn generate_encoded_view_schema(input: EncodedViewSchemaInput) -> Result<(), String> {
    println!("cargo:rerun-if-changed={}", input.ledger);
    println!("cargo:rerun-if-changed={}", input.descriptor);
    println!("cargo:rerun-if-changed=src/hash.rs");

    let manifest = PathBuf::from(
        env::var_os("CARGO_MANIFEST_DIR")
            .ok_or_else(|| "CARGO_MANIFEST_DIR is unavailable".to_owned())?,
    );
    let schema_path = manifest.join(input.ledger);
    let descriptor_path = manifest.join(input.descriptor);
    let schema = fs::read_to_string(&schema_path)
        .map_err(|error| format!("cannot read {}: {error}", schema_path.display()))?;
    let descriptor = fs::read(&descriptor_path)
        .map_err(|error| format!("cannot read {}: {error}", descriptor_path.display()))?;

    let version = parse_u32(top_level_value(&schema, "schema")?, "schema")?;
    let name = parse_string(top_level_value(&schema, "name")?, "name")?;
    let status = parse_string(top_level_value(&schema, "status")?, "status")?;
    let descriptor_format = parse_string(
        top_level_value(&schema, "descriptor_format")?,
        "descriptor_format",
    )?;
    let digest_hex = parse_string(
        top_level_value(&schema, "descriptor_sha256")?,
        "descriptor_sha256",
    )?;
    let model_schema = parse_u32(top_level_value(&schema, "model_schema")?, "model_schema")?;
    let byte_order = parse_string(top_level_value(&schema, "byte_order")?, "byte_order")?;
    let advertised = parse_bool(
        top_level_value(&schema, "capability_advertised")?,
        "capability_advertised",
    )?;

    if version != input.expected_version || model_schema != input.expected_model_schema {
        return Err(format!(
            "encoded-view schema/model version mismatch: expected {}/{}, observed {version}/{model_schema}",
            input.expected_version, input.expected_model_schema
        ));
    }
    if name.is_empty() || !name.is_ascii() {
        return Err("schema name must be nonempty ASCII".to_owned());
    }
    if status != "frozen-advertised" || !advertised {
        return Err("encoded-view ledger must remain frozen and advertised".to_owned());
    }
    if descriptor_format != "canonical-json-sorted-keys-compact-ascii" {
        return Err("native schema requires the supported canonical JSON format".to_owned());
    }
    if byte_order != "little" {
        return Err("native encoded views require little-endian columns".to_owned());
    }

    validate_compact_ascii_json(&descriptor)?;
    require_json_field(&descriptor, "schema_name", &json_string(&name))?;
    require_json_field(&descriptor, "schema_version", &version.to_string())?;
    require_json_field(&descriptor, "model_schema", &model_schema.to_string())?;
    require_json_field(&descriptor, "byte_order", &json_string(&byte_order))?;

    let expected_digest = parse_digest(&digest_hex)?;
    let observed_digest = native_hash::sha256(&descriptor);
    if observed_digest != expected_digest {
        return Err(format!(
            "descriptor SHA-256 mismatch: ledger={digest_hex}, descriptor={}",
            encode_digest(observed_digest)
        ));
    }

    let output =
        PathBuf::from(env::var_os("OUT_DIR").ok_or_else(|| "OUT_DIR is unavailable".to_owned())?);
    fs::write(output.join(input.generated_descriptor), &descriptor)
        .map_err(|error| format!("cannot write generated descriptor: {error}"))?;
    fs::write(
        output.join(input.generated_source),
        render_constants(
            &name,
            version,
            model_schema,
            &status,
            advertised,
            expected_digest,
            input.ledger,
            input.generated_descriptor,
        ),
    )
    .map_err(|error| format!("cannot write generated Rust constants: {error}"))?;
    Ok(())
}

fn top_level_value<'a>(schema: &'a str, key: &str) -> Result<&'a str, String> {
    let mut selected = None;
    for line in schema.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            break;
        }
        let Some((candidate, value)) = line.split_once('=') else {
            continue;
        };
        if candidate.trim() == key && selected.replace(value.trim()).is_some() {
            return Err(format!("duplicate top-level schema field {key}"));
        }
    }
    selected.ok_or_else(|| format!("missing top-level schema field {key}"))
}

fn parse_string(value: &str, field: &str) -> Result<String, String> {
    let Some(inner) = value
        .strip_prefix('"')
        .and_then(|item| item.strip_suffix('"'))
    else {
        return Err(format!("schema field {field} must be a basic string"));
    };
    if inner.contains(['"', '\\']) {
        return Err(format!("schema field {field} contains unsupported escapes"));
    }
    Ok(inner.to_owned())
}

fn parse_u32(value: &str, field: &str) -> Result<u32, String> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(format!("schema field {field} must be an unsigned integer"));
    }
    value
        .parse()
        .map_err(|_| format!("schema field {field} does not fit u32"))
}

fn parse_bool(value: &str, field: &str) -> Result<bool, String> {
    match value {
        "true" => Ok(true),
        "false" => Ok(false),
        _ => Err(format!("schema field {field} must be a boolean")),
    }
}

fn parse_digest(value: &str) -> Result<[u8; 32], String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("descriptor_sha256 must be 64 lowercase hexadecimal digits".to_owned());
    }
    let mut digest = [0_u8; 32];
    for (slot, pair) in digest.iter_mut().zip(value.as_bytes().chunks_exact(2)) {
        *slot = (hex_nibble(pair[0])? << 4) | hex_nibble(pair[1])?;
    }
    Ok(digest)
}

fn hex_nibble(value: u8) -> Result<u8, String> {
    match value {
        b'0'..=b'9' => Ok(value - b'0'),
        b'a'..=b'f' => Ok(value - b'a' + 10),
        _ => Err("invalid hexadecimal descriptor digest".to_owned()),
    }
}

fn encode_digest(value: [u8; 32]) -> String {
    value
        .iter()
        .fold(String::with_capacity(64), |mut output, byte| {
            write!(output, "{byte:02x}").expect("writing to String cannot fail");
            output
        })
}

fn validate_compact_ascii_json(descriptor: &[u8]) -> Result<(), String> {
    if descriptor.first() != Some(&b'{') || descriptor.last() != Some(&b'}') {
        return Err("encoded-view descriptor must be one JSON object".to_owned());
    }
    if !descriptor.is_ascii() {
        return Err("encoded-view descriptor must be ASCII".to_owned());
    }
    let mut quoted = false;
    let mut escaped = false;
    for byte in descriptor {
        if escaped {
            escaped = false;
            continue;
        }
        match *byte {
            b'\\' if quoted => escaped = true,
            b'"' => quoted = !quoted,
            byte if !quoted && byte.is_ascii_whitespace() => {
                return Err("encoded-view descriptor JSON must be compact".to_owned());
            }
            _ => {}
        }
    }
    if quoted || escaped {
        return Err("encoded-view descriptor contains an unterminated JSON string".to_owned());
    }
    Ok(())
}

fn require_json_field(descriptor: &[u8], name: &str, value: &str) -> Result<(), String> {
    let descriptor = std::str::from_utf8(descriptor)
        .map_err(|_| "encoded-view descriptor must be UTF-8".to_owned())?;
    let needle = format!("\"{name}\":{value}");
    if descriptor.match_indices(&needle).count() != 1 {
        return Err(format!(
            "encoded-view descriptor must contain exact {name} metadata"
        ));
    }
    Ok(())
}

fn json_string(value: &str) -> String {
    format!("{value:?}")
}

fn render_constants(
    name: &str,
    version: u32,
    model_schema: u32,
    status: &str,
    advertised: bool,
    digest: [u8; 32],
    ledger: &str,
    generated_descriptor: &str,
) -> String {
    let digest = digest
        .iter()
        .map(|byte| format!("0x{byte:02x}"))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "// @generated by native/build.rs from {ledger}; do not edit.\n\
         pub(super) const NAME: &str = {name:?};\n\
         pub(super) const VERSION: u32 = {version};\n\
         pub(super) const MODEL_SCHEMA: u32 = {model_schema};\n\
         pub(super) const STATUS: &str = {status:?};\n\
         pub(super) const CAPABILITY_ADVERTISED: bool = {advertised};\n\
         pub(super) const DESCRIPTOR_SHA256: [u8; 32] = [{digest}];\n\
         pub(super) const DESCRIPTOR: &[u8] = include_bytes!(\"{generated_descriptor}\");\n"
    )
}
