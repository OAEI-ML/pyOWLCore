use std::alloc::{GlobalAlloc, Layout, System};
use std::ptr;
use std::sync::atomic::{AtomicU8, AtomicUsize, Ordering};

use _native::process_allocator_test::{
    wire_validation_receipt, ComponentBuildFixture, ComponentEncodingFixture, Failure,
    TypedFacadeIndexFixture, WireValidationFixture,
};

const DISARMED: u8 = 0;
const COUNTING: u8 = 1;
const FAILING: u8 = 2;

struct InjectingAllocator;

static MODE: AtomicU8 = AtomicU8::new(DISARMED);
static ALLOCATIONS: AtomicUsize = AtomicUsize::new(0);
static FAIL_AFTER: AtomicUsize = AtomicUsize::new(usize::MAX);

#[global_allocator]
static ALLOCATOR: InjectingAllocator = InjectingAllocator;

unsafe impl GlobalAlloc for InjectingAllocator {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if rejects_next_allocation() {
            return ptr::null_mut();
        }
        // SAFETY: The request is forwarded unchanged to the system allocator.
        unsafe { System.alloc(layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        if rejects_next_allocation() {
            return ptr::null_mut();
        }
        // SAFETY: The request is forwarded unchanged to the system allocator.
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        // SAFETY: Every non-null allocation returned by this wrapper came from
        // the system allocator with the same layout.
        unsafe { System.dealloc(pointer, layout) }
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if rejects_next_allocation() {
            return ptr::null_mut();
        }
        // SAFETY: The pointer and old layout came from the system allocator,
        // and the requested size is forwarded unchanged.
        unsafe { System.realloc(pointer, layout, new_size) }
    }
}

fn rejects_next_allocation() -> bool {
    let mode = MODE.load(Ordering::Relaxed);
    if mode == DISARMED {
        return false;
    }
    let index = ALLOCATIONS.fetch_add(1, Ordering::Relaxed);
    mode == FAILING && index == FAIL_AFTER.load(Ordering::Relaxed)
}

struct ArmedAllocator;

impl ArmedAllocator {
    fn counting() -> Self {
        ALLOCATIONS.store(0, Ordering::Relaxed);
        FAIL_AFTER.store(usize::MAX, Ordering::Relaxed);
        MODE.store(COUNTING, Ordering::SeqCst);
        Self
    }

    fn failing(fail_after: usize) -> Self {
        ALLOCATIONS.store(0, Ordering::Relaxed);
        FAIL_AFTER.store(fail_after, Ordering::Relaxed);
        MODE.store(FAILING, Ordering::SeqCst);
        Self
    }

    fn allocations(&self) -> usize {
        ALLOCATIONS.load(Ordering::Relaxed)
    }
}

impl Drop for ArmedAllocator {
    fn drop(&mut self) {
        MODE.store(DISARMED, Ordering::SeqCst);
        FAIL_AFTER.store(usize::MAX, Ordering::Relaxed);
    }
}

fn count_allocations<T>(operation: impl FnOnce() -> T) -> (T, usize) {
    let armed = ArmedAllocator::counting();
    let output = operation();
    let allocations = armed.allocations();
    drop(armed);
    (output, allocations)
}

fn fail_allocation<T>(fail_after: usize, operation: impl FnOnce() -> T) -> T {
    let armed = ArmedAllocator::failing(fail_after);
    let output = operation();
    drop(armed);
    output
}

fn assert_typed_allocation_failure<T>(result: Result<T, Failure>, message: &'static str) {
    assert_eq!(typed_allocation_failure(result).message, message);
}

fn typed_allocation_failure<T>(result: Result<T, Failure>) -> Failure {
    let failure = match result {
        Ok(_) => panic!("allocation rejection unexpectedly succeeded"),
        Err(failure) => failure,
    };
    assert_eq!(failure.code, "NATIVE_WIRE_LIMIT");
    failure
}

#[test]
fn production_fallible_allocations_fail_closed_and_recover_at_the_boundary() {
    let canonical = [
        60, 1, 24, 2, 5, 5, b'c', b'l', b'a', b's', b's', 1, 14, 1, 2, 11, b'u', b'r', b'n', b':',
        b'p', b'r', b'o', b'c', b'e', b's', b's', 6, 0,
    ];
    let component_build =
        ComponentBuildFixture::new(&canonical).expect("component build fixture must initialize");
    let mut component =
        ComponentEncodingFixture::new(&canonical).expect("component fixture must freeze");
    let wire = WireValidationFixture::new()
        .expect("wire validation fixture must initialize before arming");

    let (baseline_build, build_allocations) = count_allocations(|| component_build.build());
    baseline_build.expect("component build baseline must succeed");
    assert!(build_allocations > 1);

    for fail_after in 0..build_allocations {
        let failure = fail_allocation(fail_after, || component_build.build())
            .expect_err("every observed component build allocation must be fallible");
        assert_eq!(failure.code, "NATIVE_WIRE_LIMIT");
    }
    fail_allocation(build_allocations, || component_build.build())
        .expect("first non-failing component build boundary must succeed");

    let (baseline_component, component_allocations) = count_allocations(|| component.encode());
    assert_eq!(
        baseline_component.expect("component baseline must encode"),
        canonical
    );
    assert_eq!(component_allocations, 1);

    assert_typed_allocation_failure(
        fail_allocation(0, || component.encode()),
        "native component encoding allocation failed",
    );
    let boundary_component = fail_allocation(component_allocations, || component.encode())
        .expect("first non-failing component boundary must encode");
    assert_eq!(boundary_component, canonical);

    let (baseline_index, index_allocations) = count_allocations(|| component.build_digest_index());
    let baseline_index = baseline_index.expect("component digest index baseline must build");
    assert_eq!(baseline_index.0, 1);
    assert!(baseline_index.1 > 0);
    assert!(index_allocations > 1);

    for fail_after in 0..index_allocations {
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            component.build_digest_index()
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let boundary_index = fail_allocation(index_allocations, || component.build_digest_index())
        .expect("first non-failing component digest index boundary must build");
    assert_eq!(boundary_index, baseline_index);

    let (baseline_signature, signature_allocations) =
        count_allocations(|| component.build_signature_index());
    let baseline_signature =
        baseline_signature.expect("retained signature index baseline must build");
    assert_eq!(baseline_signature, [1, 1, 1, 1, 1, 0]);
    assert!(signature_allocations > 1);

    for fail_after in 0..signature_allocations {
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            component.build_signature_index()
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let boundary_signature =
        fail_allocation(signature_allocations, || component.build_signature_index())
            .expect("first non-failing retained signature boundary must build");
    assert_eq!(boundary_signature, baseline_signature);

    let (baseline_axiom_type, axiom_type_allocations) =
        count_allocations(|| component.build_axiom_type_index());
    let baseline_axiom_type =
        baseline_axiom_type.expect("retained axiom-type index baseline must build");
    assert_eq!(baseline_axiom_type[..3], [1, 1, 1]);
    assert!(baseline_axiom_type[3] > 0);
    assert_eq!(baseline_axiom_type[4], 0);
    assert!(axiom_type_allocations > 1);

    for fail_after in 0..axiom_type_allocations {
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            component.build_axiom_type_index()
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let boundary_axiom_type = fail_allocation(axiom_type_allocations, || {
        component.build_axiom_type_index()
    })
    .expect("first non-failing retained axiom-type boundary must build");
    assert_eq!(boundary_axiom_type, baseline_axiom_type);

    let axiom_type_page = component
        .prepare_axiom_type_page()
        .expect("retained axiom-type page fixture must prepare");
    let (baseline_axiom_type_page, axiom_type_page_allocations) =
        count_allocations(|| axiom_type_page.page());
    let baseline_axiom_type_page =
        baseline_axiom_type_page.expect("retained axiom-type page baseline must encode");
    assert_eq!(baseline_axiom_type_page[0], 1);
    assert_eq!(baseline_axiom_type_page[1], u64::MAX);
    assert_eq!(baseline_axiom_type_page[2], 1);
    assert_eq!(baseline_axiom_type_page[3], canonical.len() as u64);
    assert!(baseline_axiom_type_page[4] > 0);
    assert_eq!(baseline_axiom_type_page[5], 1);
    assert!(axiom_type_page_allocations > 1);

    for fail_after in 0..axiom_type_page_allocations {
        let axiom_type_page = component
            .prepare_axiom_type_page()
            .expect("retained axiom-type page fixture must prepare for every rejection");
        let failure =
            typed_allocation_failure(fail_allocation(fail_after, || axiom_type_page.page()));
        assert!(failure.message.contains("allocation failed"));
    }
    let axiom_type_page = component
        .prepare_axiom_type_page()
        .expect("retained axiom-type page boundary fixture must prepare");
    let boundary_axiom_type_page =
        fail_allocation(axiom_type_page_allocations, || axiom_type_page.page())
            .expect("first non-failing retained axiom-type page boundary must encode");
    assert_eq!(boundary_axiom_type_page, baseline_axiom_type_page);

    let typed_freeze = component
        .prepare_typed_facade_freeze()
        .expect("typed facade freeze fixture must prepare");
    let (baseline_typed_freeze, typed_freeze_allocations) =
        count_allocations(|| typed_freeze.freeze());
    let baseline_typed_freeze =
        baseline_typed_freeze.expect("typed facade freeze baseline must succeed");
    let baseline_typed_freeze = baseline_typed_freeze
        .summary()
        .expect("typed facade freeze baseline must be observable");
    assert_eq!(baseline_typed_freeze[0..4], [1, 1, 1, 1]);
    assert!(baseline_typed_freeze[4] > 0);
    assert!(baseline_typed_freeze[5] > baseline_typed_freeze[4]);
    assert_eq!(baseline_typed_freeze[6..], [1, 0]);
    assert!(typed_freeze_allocations > 1);

    for fail_after in 0..typed_freeze_allocations {
        let typed_freeze = component
            .prepare_typed_facade_freeze()
            .expect("typed facade freeze fixture must prepare for every rejection");
        let failure =
            typed_allocation_failure(fail_allocation(fail_after, || typed_freeze.freeze()));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_freeze = component
        .prepare_typed_facade_freeze()
        .expect("typed facade freeze boundary fixture must prepare");
    let boundary_typed_freeze = fail_allocation(typed_freeze_allocations, || typed_freeze.freeze())
        .expect("first non-failing typed facade freeze boundary must succeed")
        .summary()
        .expect("typed facade freeze boundary must be observable");
    assert_eq!(boundary_typed_freeze, baseline_typed_freeze);

    let typed_raw_freeze = component
        .prepare_typed_facade_raw_freeze()
        .expect("typed raw facade freeze fixture must prepare");
    let (baseline_typed_raw_freeze, typed_raw_freeze_allocations) =
        count_allocations(|| typed_raw_freeze.freeze());
    let baseline_typed_raw_freeze = baseline_typed_raw_freeze
        .expect("typed raw facade freeze baseline must succeed")
        .summary()
        .expect("typed raw facade freeze baseline must be observable");
    assert_eq!(baseline_typed_raw_freeze[0..4], [2, 1, 1, 2]);
    assert!(baseline_typed_raw_freeze[4] > baseline_typed_freeze[4]);
    assert!(baseline_typed_raw_freeze[5] > baseline_typed_raw_freeze[4]);
    assert_eq!(baseline_typed_raw_freeze[6..], [1, 0]);
    assert!(typed_raw_freeze_allocations > typed_freeze_allocations);

    for fail_after in 0..typed_raw_freeze_allocations {
        let typed_raw_freeze = component
            .prepare_typed_facade_raw_freeze()
            .expect("typed raw facade freeze fixture must prepare for every rejection");
        typed_allocation_failure(fail_allocation(fail_after, || typed_raw_freeze.freeze()));
    }
    let typed_raw_freeze = component
        .prepare_typed_facade_raw_freeze()
        .expect("typed raw facade freeze boundary fixture must prepare");
    let boundary_typed_raw_freeze =
        fail_allocation(typed_raw_freeze_allocations, || typed_raw_freeze.freeze())
            .expect("first non-failing typed raw facade freeze boundary must succeed")
            .summary()
            .expect("typed raw facade freeze boundary must be observable");
    assert_eq!(boundary_typed_raw_freeze, baseline_typed_raw_freeze);

    let typed_builder_add = component
        .prepare_typed_builder_add(&canonical)
        .expect("typed builder add fixture must prepare");
    let (baseline_typed_builder_add, typed_builder_add_allocations) =
        count_allocations(|| typed_builder_add.add_document());
    let baseline_typed_builder_add =
        baseline_typed_builder_add.expect("typed builder add baseline must succeed");
    assert_eq!(baseline_typed_builder_add.ordinal(), 0);
    assert!(typed_builder_add_allocations > 1);

    for fail_after in 0..typed_builder_add_allocations {
        let typed_builder_add = component
            .prepare_typed_builder_add(&canonical)
            .expect("typed builder add fixture must prepare for every rejection");
        typed_allocation_failure(fail_allocation(fail_after, || {
            typed_builder_add.add_document()
        }));
    }
    let typed_builder_add = component
        .prepare_typed_builder_add(&canonical)
        .expect("typed builder add boundary fixture must prepare");
    let boundary_typed_builder_add = fail_allocation(typed_builder_add_allocations, || {
        typed_builder_add.add_document()
    })
    .expect("first non-failing typed builder add boundary must succeed");
    assert_eq!(
        boundary_typed_builder_add.ordinal(),
        baseline_typed_builder_add.ordinal()
    );

    let mut effective_canonical = canonical;
    assert_eq!(effective_canonical[26], b's');
    effective_canonical[26] = b'e';
    let typed_builder_add_scoped = component
        .prepare_typed_builder_add_scoped(&canonical, &effective_canonical)
        .expect("typed scoped builder add fixture must prepare");
    let (baseline_typed_builder_add_scoped, typed_builder_add_scoped_allocations) =
        count_allocations(|| typed_builder_add_scoped.add_document());
    let baseline_typed_builder_add_scoped =
        baseline_typed_builder_add_scoped.expect("typed scoped builder add baseline must succeed");
    assert_eq!(baseline_typed_builder_add_scoped.ordinal(), 0);
    assert!(typed_builder_add_scoped_allocations > typed_builder_add_allocations);

    for fail_after in 0..typed_builder_add_scoped_allocations {
        let typed_builder_add_scoped = component
            .prepare_typed_builder_add_scoped(&canonical, &effective_canonical)
            .expect("typed scoped builder add fixture must prepare for every rejection");
        typed_allocation_failure(fail_allocation(fail_after, || {
            typed_builder_add_scoped.add_document()
        }));
    }
    let typed_builder_add_scoped = component
        .prepare_typed_builder_add_scoped(&canonical, &effective_canonical)
        .expect("typed scoped builder add boundary fixture must prepare");
    let boundary_typed_builder_add_scoped =
        fail_allocation(typed_builder_add_scoped_allocations, || {
            typed_builder_add_scoped.add_document()
        })
        .expect("first non-failing typed scoped builder add boundary must succeed");
    assert_eq!(
        boundary_typed_builder_add_scoped.ordinal(),
        baseline_typed_builder_add_scoped.ordinal()
    );

    let typed_page = component
        .prepare_typed_facade_reads()
        .expect("typed facade page fixture must prepare");
    let (baseline_typed_page, typed_page_allocations) = count_allocations(|| typed_page.page());
    let baseline_typed_page = baseline_typed_page.expect("typed facade page baseline must encode");
    assert_eq!(baseline_typed_page[0], 1);
    assert_eq!(baseline_typed_page[1], u64::MAX);
    assert_eq!(baseline_typed_page[2], 1);
    assert_eq!(baseline_typed_page[3], canonical.len() as u64);
    assert!(baseline_typed_page[4] > 0);
    assert_eq!(
        baseline_typed_page[5..],
        [1, 1, 1, canonical.len() as u64, 1]
    );
    assert!(typed_page_allocations > 1);

    for fail_after in 0..typed_page_allocations {
        let typed_page = component
            .prepare_typed_facade_reads()
            .expect("typed facade page fixture must prepare for every rejection");
        let failure = typed_allocation_failure(fail_allocation(fail_after, || typed_page.page()));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_page = component
        .prepare_typed_facade_reads()
        .expect("typed facade page boundary fixture must prepare");
    let boundary_typed_page = fail_allocation(typed_page_allocations, || typed_page.page())
        .expect("first non-failing typed facade page boundary must encode");
    assert_eq!(boundary_typed_page, baseline_typed_page);

    let typed_contains = component
        .prepare_typed_facade_reads()
        .expect("typed facade contains fixture must prepare");
    let (baseline_typed_contains, typed_contains_allocations) =
        count_allocations(|| typed_contains.contains(&canonical));
    let baseline_typed_contains =
        baseline_typed_contains.expect("typed facade contains baseline must match");
    assert_eq!(baseline_typed_contains, [1, 1, 1, 1]);
    assert!(typed_contains_allocations > 0);

    for fail_after in 0..typed_contains_allocations {
        let typed_contains = component
            .prepare_typed_facade_reads()
            .expect("typed facade contains fixture must prepare for every rejection");
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            typed_contains.contains(&canonical)
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_contains = component
        .prepare_typed_facade_reads()
        .expect("typed facade contains boundary fixture must prepare");
    let boundary_typed_contains = fail_allocation(typed_contains_allocations, || {
        typed_contains.contains(&canonical)
    })
    .expect("first non-failing typed facade contains boundary must match");
    assert_eq!(boundary_typed_contains, baseline_typed_contains);

    let typed_raw_page =
        ComponentEncodingFixture::prepare_typed_facade_raw_reads(&canonical, &effective_canonical)
            .expect("typed raw facade page fixture must prepare");
    let (baseline_typed_raw_page, typed_raw_page_allocations) =
        count_allocations(|| typed_raw_page.page());
    let baseline_typed_raw_page =
        baseline_typed_raw_page.expect("typed raw facade page baseline must encode");
    assert_eq!(baseline_typed_raw_page[0], 1);
    assert_eq!(baseline_typed_raw_page[1], u64::MAX);
    assert_eq!(baseline_typed_raw_page[2], 1);
    assert_eq!(baseline_typed_raw_page[3], canonical.len() as u64);
    assert!(baseline_typed_raw_page[4] > 0);
    assert_eq!(
        baseline_typed_raw_page[5..],
        [1, 1, 1, canonical.len() as u64, 1]
    );
    assert_eq!(baseline_typed_raw_page, baseline_typed_page);
    assert!(typed_raw_page_allocations > 1);

    for fail_after in 0..typed_raw_page_allocations {
        let typed_raw_page = ComponentEncodingFixture::prepare_typed_facade_raw_reads(
            &canonical,
            &effective_canonical,
        )
        .expect("typed raw facade page fixture must prepare for every rejection");
        let failure =
            typed_allocation_failure(fail_allocation(fail_after, || typed_raw_page.page()));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_raw_page =
        ComponentEncodingFixture::prepare_typed_facade_raw_reads(&canonical, &effective_canonical)
            .expect("typed raw facade page boundary fixture must prepare");
    let boundary_typed_raw_page =
        fail_allocation(typed_raw_page_allocations, || typed_raw_page.page())
            .expect("first non-failing typed raw facade page boundary must encode");
    assert_eq!(boundary_typed_raw_page, baseline_typed_raw_page);

    let typed_raw_contains =
        ComponentEncodingFixture::prepare_typed_facade_raw_reads(&canonical, &effective_canonical)
            .expect("typed raw facade contains fixture must prepare");
    let (baseline_typed_raw_contains, typed_raw_contains_allocations) =
        count_allocations(|| typed_raw_contains.contains(&canonical));
    let baseline_typed_raw_contains =
        baseline_typed_raw_contains.expect("typed raw facade contains baseline must match");
    assert_eq!(baseline_typed_raw_contains, [1, 1, 1, 1]);
    assert!(typed_raw_contains_allocations > 0);

    for fail_after in 0..typed_raw_contains_allocations {
        let typed_raw_contains = ComponentEncodingFixture::prepare_typed_facade_raw_reads(
            &canonical,
            &effective_canonical,
        )
        .expect("typed raw facade contains fixture must prepare for every rejection");
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            typed_raw_contains.contains(&canonical)
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_raw_contains =
        ComponentEncodingFixture::prepare_typed_facade_raw_reads(&canonical, &effective_canonical)
            .expect("typed raw facade contains boundary fixture must prepare");
    let boundary_typed_raw_contains = fail_allocation(typed_raw_contains_allocations, || {
        typed_raw_contains.contains(&canonical)
    })
    .expect("first non-failing typed raw facade contains boundary must match");
    assert_eq!(boundary_typed_raw_contains, baseline_typed_raw_contains);

    let typed_axiom_type = component
        .prepare_typed_facade_indexes()
        .expect("typed facade axiom-type fixture must prepare");
    let (baseline_typed_axiom_type, typed_axiom_type_allocations) =
        count_allocations(|| typed_axiom_type.build_axiom_type_index());
    let baseline_typed_axiom_type =
        baseline_typed_axiom_type.expect("typed facade axiom-type baseline must build");
    assert_eq!(baseline_typed_axiom_type, baseline_axiom_type);
    assert!(typed_axiom_type_allocations > 1);

    for fail_after in 0..typed_axiom_type_allocations {
        let typed_axiom_type = component
            .prepare_typed_facade_indexes()
            .expect("typed facade axiom-type fixture must prepare for every rejection");
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            typed_axiom_type.build_axiom_type_index()
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_axiom_type = component
        .prepare_typed_facade_indexes()
        .expect("typed facade axiom-type boundary fixture must prepare");
    let boundary_typed_axiom_type = fail_allocation(typed_axiom_type_allocations, || {
        typed_axiom_type.build_axiom_type_index()
    })
    .expect("first non-failing typed facade axiom-type boundary must build");
    assert_eq!(boundary_typed_axiom_type, baseline_typed_axiom_type);

    let typed_signature = component
        .prepare_typed_facade_indexes()
        .expect("typed facade signature fixture must prepare");
    let (baseline_typed_signature, typed_signature_allocations) =
        count_allocations(|| typed_signature.build_signature_index());
    let baseline_typed_signature =
        baseline_typed_signature.expect("typed facade signature baseline must build");
    assert_eq!(baseline_typed_signature, baseline_signature);
    assert!(typed_signature_allocations > 1);

    for fail_after in 0..typed_signature_allocations {
        let typed_signature = component
            .prepare_typed_facade_indexes()
            .expect("typed facade signature fixture must prepare for every rejection");
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            typed_signature.build_signature_index()
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_signature = component
        .prepare_typed_facade_indexes()
        .expect("typed facade signature boundary fixture must prepare");
    let boundary_typed_signature = fail_allocation(typed_signature_allocations, || {
        typed_signature.build_signature_index()
    })
    .expect("first non-failing typed facade signature boundary must build");
    assert_eq!(boundary_typed_signature, baseline_typed_signature);

    for operation in [
        TypedFacadeIndexFixture::build_raw_axiom_type_index
            as fn(&TypedFacadeIndexFixture) -> Result<[u64; 5], Failure>,
        TypedFacadeIndexFixture::build_closure_axiom_type_index,
    ] {
        let typed_axiom_type = component
            .prepare_typed_facade_indexes()
            .expect("typed scoped axiom-type fixture must prepare");
        let (baseline_typed_axiom_type, typed_axiom_type_allocations) =
            count_allocations(|| operation(&typed_axiom_type));
        let baseline_typed_axiom_type =
            baseline_typed_axiom_type.expect("typed scoped axiom-type baseline must build");
        assert_eq!(baseline_typed_axiom_type, baseline_axiom_type);
        assert!(typed_axiom_type_allocations > 1);

        for fail_after in 0..typed_axiom_type_allocations {
            let typed_axiom_type = component
                .prepare_typed_facade_indexes()
                .expect("typed scoped axiom-type fixture must prepare for every rejection");
            let failure = typed_allocation_failure(fail_allocation(fail_after, || {
                operation(&typed_axiom_type)
            }));
            assert!(failure.message.contains("allocation failed"));
        }
        let typed_axiom_type = component
            .prepare_typed_facade_indexes()
            .expect("typed scoped axiom-type boundary fixture must prepare");
        let boundary_typed_axiom_type = fail_allocation(typed_axiom_type_allocations, || {
            operation(&typed_axiom_type)
        })
        .expect("first non-failing typed scoped axiom-type boundary must build");
        assert_eq!(boundary_typed_axiom_type, baseline_typed_axiom_type);
    }

    let typed_closure_signature = component
        .prepare_typed_facade_indexes()
        .expect("typed closure signature fixture must prepare");
    let (baseline_typed_closure_signature, typed_closure_signature_allocations) =
        count_allocations(|| typed_closure_signature.build_closure_signature_index());
    let baseline_typed_closure_signature =
        baseline_typed_closure_signature.expect("typed closure signature baseline must build");
    assert_eq!(baseline_typed_closure_signature, baseline_signature);
    assert!(typed_closure_signature_allocations > 1);

    for fail_after in 0..typed_closure_signature_allocations {
        let typed_closure_signature = component
            .prepare_typed_facade_indexes()
            .expect("typed closure signature fixture must prepare for every rejection");
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            typed_closure_signature.build_closure_signature_index()
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let typed_closure_signature = component
        .prepare_typed_facade_indexes()
        .expect("typed closure signature boundary fixture must prepare");
    let boundary_typed_closure_signature =
        fail_allocation(typed_closure_signature_allocations, || {
            typed_closure_signature.build_closure_signature_index()
        })
        .expect("first non-failing typed closure signature boundary must build");
    assert_eq!(
        boundary_typed_closure_signature,
        baseline_typed_closure_signature
    );

    let (baseline_prepared, preparation_allocations) =
        count_allocations(|| component.prepare_encoded_columns());
    let baseline_prepared = baseline_prepared.expect("encoded columns must prepare");
    assert!(preparation_allocations > 1);
    let baseline_prepared_columns = baseline_prepared
        .publish()
        .expect("prepared encoded columns must publish");

    for fail_after in 0..preparation_allocations {
        let failure = typed_allocation_failure(fail_allocation(fail_after, || {
            component.prepare_encoded_columns()
        }));
        assert!(failure.message.contains("allocation failed"));
    }
    let boundary_prepared = fail_allocation(preparation_allocations, || {
        component.prepare_encoded_columns()
    })
    .expect("first non-failing encoded-column preparation boundary must succeed");
    let boundary_prepared_columns = boundary_prepared
        .publish()
        .expect("boundary encoded columns must publish");
    assert_eq!(boundary_prepared_columns, baseline_prepared_columns);

    let prepared = component
        .prepare_encoded_columns()
        .expect("encoded-column fixture must prepare");
    let (baseline_columns, column_allocations) = count_allocations(|| prepared.publish());
    let baseline_columns = baseline_columns.expect("encoded columns must publish");
    assert!(column_allocations > 1);
    assert!(baseline_columns.into_iter().sum::<usize>() > canonical.len());

    for fail_after in 0..column_allocations {
        let prepared = component
            .prepare_encoded_columns()
            .expect("encoded-column fixture must prepare for every rejection");
        assert_typed_allocation_failure(
            fail_allocation(fail_after, || prepared.publish()),
            "native encoded-column buffer allocation failed",
        );
    }
    let prepared = component
        .prepare_encoded_columns()
        .expect("encoded-column boundary fixture must prepare");
    let boundary_columns = fail_allocation(column_allocations, || prepared.publish())
        .expect("first non-failing encoded-column boundary must publish");
    assert_eq!(boundary_columns, baseline_columns);

    let (baseline_wire, wire_allocations) = count_allocations(|| wire.validate());
    let baseline_wire =
        baseline_wire.expect_err("malformed wire baseline must reach its digest rejection");
    assert_eq!(baseline_wire.code, "NATIVE_WIRE_CORRUPTION");
    assert_eq!(baseline_wire.message, "PYOCORE section SHA-256 mismatch");
    assert!(wire_allocations > 1);

    for fail_after in 0..wire_allocations {
        let failure = typed_allocation_failure(fail_allocation(fail_after, || wire.validate()));
        assert!(failure.message.contains("allocation failed"));
    }
    let boundary_wire = fail_allocation(wire_allocations, || wire.validate())
        .expect_err("first non-failing wire boundary must recover its baseline corruption");
    assert_eq!(boundary_wire, baseline_wire);

    let (baseline_receipt, receipt_allocations) = count_allocations(wire_validation_receipt);
    let baseline_receipt = baseline_receipt.expect("wire receipt baseline must publish");
    assert_eq!(baseline_receipt.len(), 76);
    assert_eq!(receipt_allocations, 1);

    assert_typed_allocation_failure(
        fail_allocation(0, wire_validation_receipt),
        "native receipt allocation failed",
    );
    let boundary_receipt = fail_allocation(receipt_allocations, wire_validation_receipt)
        .expect("first non-failing receipt boundary must publish");
    assert_eq!(boundary_receipt, baseline_receipt);
}
