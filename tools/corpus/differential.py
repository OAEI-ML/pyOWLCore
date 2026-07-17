"""Run core, independent-wire, and explicit development-oracle comparisons."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pyowl_core
from pyowl_core import BackendPreference, ImportPolicy, LoadOptions
from tools.wire_reference.reference import read_wire, reencode

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "data" / "corpus" / "w3c"
FORMATS = {
    "functional": CORPUS / "functional" / "minimal.ofn",
    "owlxml": CORPUS / "owlxml" / "minimal.owx",
    "rdfxml": CORPUS / "rdfxml" / "minimal.rdf",
    "turtle": CORPUS / "turtle" / "minimal.ttl",
}
DOCUMENT_IRI = "https://example.org/conformance/ontology"
RDFLIB_VERSION = "7.6.0"


def core_comparison() -> dict[str, Any]:
    options = LoadOptions(backend=BackendPreference.PYTHON)
    documents = {
        name: pyowl_core.parse_document(
            path.read_bytes(), format=name, document_iri=DOCUMENT_IRI, options=options
        )
        for name, path in FORMATS.items()
    }
    baseline = documents["functional"]
    if any(document != baseline for document in documents.values()):
        raise AssertionError("cross-syntax structural differential")
    snapshot = pyowl_core.load_snapshot(
        baseline,
        options=LoadOptions(backend=BackendPreference.PYTHON, imports=ImportPolicy.IGNORE),
    )
    wire = pyowl_core.encode_snapshot(snapshot)
    image = read_wire(wire)
    if reencode(image) != wire:
        raise AssertionError("independent wire re-encoder differential")
    decoded = pyowl_core.decode_snapshot(wire)
    if decoded.structural_fingerprint != snapshot.structural_fingerprint:
        raise AssertionError("wire semantic differential")
    return {
        "axioms": len(baseline.axioms),
        "document_fingerprint": baseline.document_fingerprint.hex,
        "formats": sorted(documents),
        "independent_wire": True,
        "wire_bytes": len(wire),
    }


def rdflib_comparison() -> dict[str, Any]:
    installed = importlib.metadata.version("rdflib")
    if installed != RDFLIB_VERSION:
        raise RuntimeError(f"RDFLib must be exactly {RDFLIB_VERSION}, found {installed}")
    from rdflib import Graph  # type: ignore[import-not-found, unused-ignore]
    from rdflib.compare import isomorphic  # type: ignore[import-not-found, unused-ignore]

    turtle = Graph().parse(FORMATS["turtle"], format="turtle")
    rdfxml = Graph().parse(FORMATS["rdfxml"], format="xml")
    if not isomorphic(turtle, rdfxml):
        raise AssertionError("RDFLib reports non-isomorphic RDF exchange graphs")
    return {"isomorphic": True, "triples": len(turtle), "version": installed}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--external-rdflib",
        action="store_true",
        help="run the exact development-only RDFLib oracle",
    )
    args = parser.parse_args(argv)
    report: dict[str, Any] = {"core": core_comparison(), "schema": 1}
    if args.external_rdflib:
        report["rdflib"] = rdflib_comparison()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["core_comparison", "main", "rdflib_comparison"]
