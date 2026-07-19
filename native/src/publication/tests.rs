use super::*;

#[test]
fn generated_fixture_matches_the_python_v1_attestation_vector() {
    let publication = fixture::publication().expect("fixture publication");
    let attestation = publication.storage().attestation();
    assert_eq!(attestation.version, 1);
    assert_eq!(attestation.ledger_sha256, PUBLICATION_LEDGER_SHA256_V1);
    assert_eq!(attestation.document_count, 1);
    assert_eq!(attestation.import_edge_count, 0);
    assert_eq!(attestation.diagnostic_count, 2);
    assert_eq!(attestation.stored_axiom_count, 1);
    assert_eq!(attestation.effective_axiom_count, 1);
    assert_eq!(attestation.origin_entry_count, 1);
    assert_eq!(attestation.capability_bits, 23);
    assert_eq!(
        codec::attestation_digest(attestation).expect("attestation digest"),
        hex_digest("97e02d37406dfcc065723c621969ea7377c1def9e6919c2a8dc6e1b957c40616")
    );
}

#[test]
fn snapshot_close_is_idempotent_and_forks_have_independent_lifecycle() {
    let publication = fixture::publication().expect("fixture publication");
    let handle = publication.handle();
    let fork = handle.fork();
    assert!(!handle.closed());
    assert!(!fork.closed());
    handle.close();
    handle.close();
    assert!(handle.closed());
    assert!(!fork.closed());
    assert_eq!(
        handle.storage().expect("V1 storage").attestation(),
        fork.storage().expect("forked V1 storage").attestation()
    );
}

#[test]
fn document_owner_shares_arena_and_closes_independently() {
    let publication = fixture::publication().expect("fixture publication");
    let snapshot = publication.handle();
    let document = snapshot.document(0).expect("document handle");
    assert_eq!(document.document_ordinal(), 0);
    assert!(document.shares_storage_with(snapshot));
    assert!(snapshot.document(1).is_none());
    document.close();
    assert!(document.closed());
    assert!(!snapshot.closed());
}

#[test]
fn publication_rejects_unbacked_capabilities_and_member_counts() {
    let mut capabilities = fixture::draft().expect("fixture draft");
    capabilities.capability_bits &= !CAPABILITY_ORIGIN_INDEX;
    assert_eq!(capabilities.freeze().unwrap_err().code, "NATIVE_PROTOCOL");

    let mut members = fixture::draft().expect("fixture draft");
    members.document_members[0].axioms = Box::new([]);
    assert_eq!(members.freeze().unwrap_err().code, "NATIVE_PROTOCOL");
}

#[test]
fn publication_counters_prove_membership_without_arena_row_copying() {
    let publication = fixture::publication().expect("fixture publication");
    let counters = publication.storage().counters();
    assert_eq!(publication.version, PUBLICATION_VERSION_V1);
    assert_eq!(publication.ledger_sha256, PUBLICATION_LEDGER_SHA256_V1);
    assert!(Arc::ptr_eq(
        &publication.documents,
        &publication.storage().documents
    ));
    assert!(Arc::ptr_eq(
        &publication.import_manifest,
        &publication.storage().import_manifest
    ));
    assert_eq!(counters.arena_rows_copied, 0);
    assert_eq!(counters.membership_rows, 1);
    assert!(counters.metadata_records >= 4);
    assert!(counters.retained_metadata_bytes > 0);
    assert_eq!(publication.storage().arena().canonical_rows().len(), 1);
}

#[test]
fn arbitrary_positive_option_integers_are_not_narrowed() {
    let huge = PositiveIntegerV1::from_decimal(
        "179769313486231590772930519078902473361797697894230657273430081157732675805500\
         963132708477322407536021120113879871393357658789768814416622492847430639474124\
         377767893424865485276302219601246094119453082952085005768838150682342462881473\
         913110540827237163350510684586298239947245938479716304835356329624224137216",
    )
    .expect("large canonical integer");
    assert!(huge.allows(u64::MAX));
    assert!(huge.decimal().len() > 300);
    assert!(PositiveIntegerV1::from_decimal("01").is_err());
    assert!(PositiveIntegerV1::from_decimal("0").is_err());
}

fn hex_digest(value: &str) -> [u8; 32] {
    let mut result = [0_u8; 32];
    for (slot, pair) in result.iter_mut().zip(value.as_bytes().chunks_exact(2)) {
        *slot = (nibble(pair[0]) << 4) | nibble(pair[1]);
    }
    result
}

fn nibble(value: u8) -> u8 {
    match value {
        b'0'..=b'9' => value - b'0',
        b'a'..=b'f' => value - b'a' + 10,
        _ => panic!("invalid fixture hex"),
    }
}
