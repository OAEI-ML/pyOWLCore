from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
RUNNER = Path(__file__).with_name("_retained_format_view_matrix_runner.py")


def test_forced_native_formats_cross_every_encoded_owner_without_scalar_work() -> None:
    environment = dict(os.environ)
    inherited = environment.get("PYTHONPATH", "")
    paths = [str(ROOT)]
    if environment.get("PYOWL_CORE_TEST_NATIVE_LIBRARY") != "1":
        paths.insert(0, str(ROOT / "src"))
    if inherited:
        paths.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(paths)

    completed = subprocess.run(
        [sys.executable, str(RUNNER)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    observed = json.loads(completed.stdout)
    assert set(observed) == {"functional", "owlxml", "rdfxml", "turtle"}
    syntax_codes = {
        "functional": "FORMAT_SYNTAX",
        "owlxml": "OWLXML_SYNTAX",
        "rdfxml": "RDFXML_SYNTAX",
        "turtle": "TURTLE_SYNTAX",
    }

    for format_name, result in observed.items():
        assert result == {
            "cancellation_error_code": "OPERATION_CANCELLED",
            "composite_model_row_deltas": [0, 0],
            "composite_owner_identity": True,
            "composite_page_request_deltas": [0, 0],
            "composite_root_parity": True,
            "composite_rows_emitted_deltas": [0, 0],
            "composite_zero_copy": True,
            "decoded_owner_identity": True,
            "decoded_root_parity": True,
            "direct_owner_identity": True,
            "direct_root_parity": True,
            "eager_structural_objects": 0,
            "fingerprint_parity": True,
            "hostile_descriptor_code": "ENCODED_VIEW_DESCRIPTOR",
            "limit_error_code": "NATIVE_WIRE_LIMIT",
            "mapped_one_exporter": True,
            "mapped_owner_identity": True,
            "mapped_readonly": True,
            "mapped_root_parity": True,
            "no_composite_scalar_work": True,
            "no_segmented_scalar_work": True,
            "overlay_owner_identity": True,
            "overlay_root_parity": True,
            "parser_bytes": result["source_bytes"],
            "publication_structural_bytes_copied": 0,
            "publication_structural_rows_copied": 0,
            "source_bytes": result["source_bytes"],
            "source_map_parity": True,
            "syntax_error_code": syntax_codes[format_name],
            "right_direct_owner_identity": True,
            "right_direct_root_parity": True,
            "right_wire_parity": True,
            "wire_parity": True,
        }, format_name
