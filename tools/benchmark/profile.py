"""Capture separate deterministic cProfile evidence for measured hot paths."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import pstats
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pyowl_core import (
    BackendPreference,
    ImportPolicy,
    LoadOptions,
    OntologyDocument,
    OntologySnapshot,
    decode_snapshot,
    encode_snapshot,
    load_snapshot,
    parse_document,
)
from pyowl_core.index import AxiomTypeIndex
from pyowl_core.index.cache import clear_index_cache

from .manifest import (
    DEFAULT_MANIFEST,
    ROOT,
    Corpus,
    generated_bytes,
    load_manifest,
    manifest_fingerprint,
    verify_prepared,
)
from .report import collect_environment


class ProfileError(RuntimeError):
    """A requested profile cannot be captured or validated."""


def capture_profile(
    corpus: Corpus,
    source: bytes,
    backend: BackendPreference,
    phase: str,
    *,
    iterations: int,
    top: int,
) -> str:
    """Profile only selected phase work and return human-readable evidence."""

    if iterations < 1 or top < 1:
        raise ProfileError("iterations and top must be positive")
    options = LoadOptions(
        format=corpus.format,
        imports=ImportPolicy.IGNORE,
        backend=backend,
    )
    operation, validate = _operation(phase, source, options)
    expected = validate(operation())
    profiler = cProfile.Profile()
    for _ in range(iterations):
        profiler.enable()
        value = operation()
        profiler.disable()
        observed = validate(value)
        if observed != expected:
            raise ProfileError(f"{phase} output fingerprint changed during profiling")
        del value
    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(top)
    environment = collect_environment(ROOT)
    return "\n".join(
        (
            "pyowl-core measured profile v1",
            f"phase: {phase}",
            f"corpus: {corpus.id}",
            f"corpus_sha256: {corpus.sha256}",
            f"manifest_sha256: {manifest_fingerprint()}",
            f"backend: {backend.value}",
            f"iterations: {iterations}",
            f"output_fingerprint: {expected}",
            f"git_commit: {environment['git_commit']}",
            f"python: {environment['python']}",
            "profiler: cProfile; validation excluded from profile",
            "",
            stream.getvalue().rstrip(),
            "",
        )
    )


def _operation(
    phase: str,
    source: bytes,
    options: LoadOptions,
) -> tuple[Callable[[], object], Callable[[object], str]]:
    def validate_document(value: object) -> str:
        if not isinstance(value, OntologyDocument):
            raise ProfileError("parse phase returned a non-document")
        return value.document_fingerprint.hex

    def validate_snapshot(value: object) -> str:
        if not isinstance(value, OntologySnapshot):
            raise ProfileError("snapshot phase returned a non-snapshot")
        return value.structural_fingerprint.hex

    if phase == "parse":
        return (
            lambda: parse_document(source, format=options.format, options=options),
            validate_document,
        )
    if phase == "load":
        return lambda: load_snapshot(source, options=options), validate_snapshot
    snapshot = load_snapshot(source, options=options)
    if phase == "index":

        def build_index() -> object:
            clear_index_cache(snapshot)
            return snapshot.view(AxiomTypeIndex)

        def validate_index(value: object) -> str:
            if not isinstance(value, AxiomTypeIndex):
                raise ProfileError("index phase returned a non-index")
            digest = hashlib.sha256(b"pyowl-core:profile-index:v1\0")
            count = 0
            for axiom in value.iter_all():
                encoded = axiom.canonical_bytes()
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                count += 1
            if count != snapshot.report.effective_axiom_count:
                raise ProfileError("profiled index row count differs from snapshot")
            return digest.hexdigest()

        return build_index, validate_index
    encoded = encode_snapshot(snapshot)
    if phase == "wire-encode":

        def validate_wire(value: object) -> str:
            if not isinstance(value, bytes):
                raise ProfileError("wire encode returned non-bytes")
            decoded = decode_snapshot(value)
            if decoded.structural_fingerprint != snapshot.structural_fingerprint:
                raise ProfileError("wire encode changed structural fingerprint")
            return hashlib.sha256(value).hexdigest()

        return lambda: encode_snapshot(snapshot), validate_wire
    if phase == "wire-decode":
        return lambda: decode_snapshot(encoded), validate_snapshot
    raise ProfileError(f"unsupported profile phase: {phase}")


def _payload(corpus: Corpus, cache_dir: Path) -> bytes:
    if corpus.source == "generated":
        return generated_bytes(corpus)
    path = cache_dir / corpus.filename
    if not path.is_file():
        raise ProfileError(f"prepared corpus is absent: {corpus.id}")
    verify_prepared(corpus, path)
    return path.read_bytes()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "benchmarks" / "results" / "corpora",
    )
    parser.add_argument("--corpus", required=True)
    parser.add_argument(
        "--backend",
        choices=tuple(value.value for value in BackendPreference),
        default="python",
    )
    parser.add_argument(
        "--phase",
        choices=("parse", "load", "index", "wire-encode", "wire-decode"),
        required=True,
    )
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--top", type=int, default=40)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        corpus = load_manifest(args.manifest).by_id(args.corpus)
        source = _payload(corpus, args.cache_dir)
        evidence = capture_profile(
            corpus,
            source,
            BackendPreference(args.backend),
            args.phase,
            iterations=args.iterations,
            top=args.top,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(evidence, encoding="utf-8")
        print(f"profile: {args.output}")
        return 0
    except (OSError, ProfileError) as error:
        print(f"profile error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ProfileError", "capture_profile"]
