from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from pyowl_core import load_snapshot
from tools.benchmark.comparators.adapters import default_options, options_inventory
from tools.benchmark.comparators.common_contract import build_core_common_contract
from tools.benchmark.comparators.manifest import COMMON_BOUNDARY
from tools.benchmark.comparators.runner import _equality_assertions
from tools.benchmark.manifest import generated_bytes, load_manifest


def test_mismatched_common_contract_cannot_pass_equality_fence() -> None:
    manifest = load_manifest()
    reference_corpus = manifest.by_id("generated-tiny-functional")
    candidate_corpus = manifest.by_id("generated-medium-owlxml")
    reference = _contract(reference_corpus)
    candidate = _contract(candidate_corpus)
    rows: list[dict[str, Any]] = [
        _row("pyowl-python-common", reference),
        _row("horned-owl-common", candidate),
    ]

    assertions = _equality_assertions(rows)
    by_id = {cast(str, value["id"]): value for value in assertions}
    candidate_assertion = by_id[
        "shared-case/resident-bytes/steady-process/horned-owl-common/common-contract-equality"
    ]

    assert candidate_assertion["passed"] is False
    assert candidate_assertion["reason"] == "published output inventory/digests differ"


def _contract(corpus: Any) -> dict[str, Any]:
    options = default_options(corpus.format)
    options_sha256 = hashlib.sha256(
        json.dumps(
            options_inventory(options),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return build_core_common_contract(
        load_snapshot(generated_bytes(corpus), options=options),
        corpus_id="shared-case",
        source_sha256=corpus.sha256,
        options_sha256=options_sha256,
    )


def _row(lane: str, contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "lane": lane,
        "boundary": COMMON_BOUNDARY,
        "status": "ok",
        "corpus_id": "shared-case",
        "input_mode": "resident-bytes",
        "process_mode": "steady-process",
        "contract": contract,
    }
