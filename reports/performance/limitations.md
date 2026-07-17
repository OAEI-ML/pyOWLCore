# Performance evidence limitations

The WP10 implementation is complete and its available shared-host gates pass,
but the following external/evidence limits remain explicit.

1. This machine is not an approved dedicated release reference. Darwin,
   architecture, logical CPU count, RAM, Python, Rust, native artifact, and Git
   state are recorded, but the CPU model lookup returned no value and power
   mode/storage were not measured. Absolute RSS is process high-water RSS; a
   release run should isolate one scenario per fresh process and record OS cache
   control. Relative release approval remains pending that machine.

2. The 20-run tiny baseline satisfies the small-run repetition rule. The
   9,999-axiom synthetic satisfies the five-run large rule. Uberon has five
   validated runs, not the 20 required when classified as a small corpus, so it
   is informative biomedical evidence rather than a release gate.

3. HPO `v2026-06-23/hp-base.owl` was qualified once (588,921 triples, 195,809
   axioms, 242.7487 seconds, 1,088,692,224-byte peak RSS). It is not a
   distribution and is not used for a regression decision. Its terms require
   user review; its 49,479,533 bytes are manifest-only and not republished.

4. The exact OAEI Bio-ML 2024 pairs cannot currently form a strict pyOWLCore
   composite. DOID and OMIM map completely and are pinned with their mapping
   files. NCIt fails because one blank node is used ambiguously as an
   individual and RDF list; ORDO fails because an `owl:Axiom` reification lacks
   its main triple. No partial mapping, dropped annotation, or cross-pair proxy
   is presented as equivalent OAEI evidence. The errors and member hashes are
   retained in `biomedical-observations.json`.

5. Exact-OM and py-horned-owl were not installed or otherwise runnable in this
   isolated repository environment. No comparative speedup against them is
   claimed, and WP10 did not add a dependency or fetch unreviewed code merely
   to manufacture a comparison. The harness can accept an independently
   prepared future comparison lane under equivalent semantics.

6. Native Functional parsing is 1.12x faster for public parse and 1.65x for
   load/freeze on the generated large source, not 2x. The native artifact is
   instead justified by the measured 2.01x axiom-index build, GIL release, and
   bounded safe-Rust validation. AUTO's conservative source/index thresholds
   remain in place; unsupported RDF/XML, Turtle, and OWL/XML parse lanes use the
   complete Python backend before native work begins.

7. cProfile changes absolute timings and does not expose time spent detached
   from the GIL as ordinary Python call time. Profiles locate Python boundary
   costs only; raw unprofiled medians determine gates. No unsafe or validation-
   disabling optimization was introduced from profiler output.

8. External corpus bytes, benchmark cache files, and temporary wire files are
   ignored and absent from source artifacts. Reproduction requires explicit
   preparation and continued publisher availability. SHA-256 locks detect
   mutation but cannot guarantee that an upstream URL remains online.

9. Performance evidence was captured on CPython 3.12.3. Python 3.10.11 runs the
   full pure tooling/acceptance path, but no separate Python 3.10 performance
   baseline is claimed.

10. The self-regression report validates comparator mechanics and thresholds;
    it is not evidence that a later candidate has no regression. Candidate and
    baseline must share the exact manifest, scenario set, machine/runtime key,
    status, and output fingerprints.
