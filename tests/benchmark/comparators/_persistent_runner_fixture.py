"""Generate dependency-free persistent comparator runners for protocol tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tools.benchmark.comparators.manifest import ComparatorPin

_RUNNER_BODY = r'''
import hashlib
import json
import os
import signal
import sys
import time

CONFIG = json.loads(__CONFIG__)
MODE = __MODE__
PROTOCOL = "pyowl-core/comparator-persistent-runner/v1"
HANDSHAKE = "pyowl-core/comparator-persistent-handshake/v1"
REQUEST = "pyowl-core/comparator-persistent-request/v1"
RESPONSE = "pyowl-core/comparator-persistent-response/v1"
SHUTDOWN = "pyowl-core/comparator-persistent-shutdown/v1"
SHUTDOWN_ACK = "pyowl-core/comparator-persistent-shutdown-ack/v1"
RAW_SCHEMA = "pyowl-core/comparator-raw-inventory/v1"
RAW_DOMAIN = b"pyowl-core:comparator-raw-inventory:v1\x00"


def write_payload(payload):
    sys.stdout.buffer.write(str(len(payload)).encode("ascii") + b"\n" + payload + b"\n")
    sys.stdout.buffer.flush()


def write_frame(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    write_payload(payload)


def read_frame():
    header = sys.stdin.buffer.readline()
    if not header:
        raise EOFError
    size = int(header.rstrip(b"\n"))
    payload = sys.stdin.buffer.read(size)
    if len(payload) != size or sys.stdin.buffer.read(1) != b"\n":
        raise RuntimeError("truncated frame")
    return json.loads(payload)


self_sha256 = hashlib.sha256(open(__file__, "rb").read()).hexdigest()
artifact = dict(CONFIG["artifact"])
artifact["runner_sha256"] = self_sha256
handshake = {
    "schema": HANDSHAKE,
    "protocol": PROTOCOL,
    "lane": CONFIG["lane"],
    "implementation": CONFIG["implementation"],
    "boundary": CONFIG["boundary"],
    "pid": os.getpid(),
    "request_schema": "pyowl-core/comparator-adapter-request/v2",
    "result_schema": "pyowl-core/comparator-adapter-result/v1",
    "fresh_ontology_per_request": True,
    "artifact": artifact,
}
if MODE == "handshake-hang":
    time.sleep(10)
if MODE == "handshake-partial-header":
    sys.stdout.buffer.write(b"12")
    sys.stdout.buffer.flush()
    time.sleep(10)
if MODE == "handshake-partial-body":
    sys.stdout.buffer.write(b"100\n{")
    sys.stdout.buffer.flush()
    time.sleep(10)
if MODE == "handshake-stderr-oversize":
    sys.stderr.buffer.write(b"h" * 100000)
    sys.stderr.buffer.flush()
    time.sleep(10)
if MODE == "wrong-handshake":
    handshake["lane"] = "wrong-lane"
if MODE == "forged-pid":
    handshake["pid"] += 1
if MODE == "float-pid":
    handshake["pid"] = float(handshake["pid"])
if MODE == "forged-artifact-sha":
    handshake["artifact"]["artifact_sha256"] = "f" * 64
if MODE == "forged-runner-revision":
    handshake["artifact"]["runner_revision"] = "forged-runner"
if MODE == "forged-runner-sha":
    handshake["artifact"]["runner_sha256"] = "f" * 64
if MODE == "not-fresh":
    handshake["fresh_ontology_per_request"] = False
if MODE == "numeric-fresh":
    handshake["fresh_ontology_per_request"] = 1
if MODE == "float-thread-ceiling":
    handshake["artifact"]["thread_ceiling"] = float(
        handshake["artifact"]["thread_ceiling"]
    )
write_frame(handshake)

instance_counter = 0
while True:
    try:
        frame = read_frame()
    except EOFError:
        raise SystemExit(3)
    if frame.get("schema") == SHUTDOWN:
        if MODE == "shutdown-hang":
            time.sleep(10)
        if MODE == "shutdown-ignore-term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(10)
        shutdown_sequence = frame["sequence"]
        shutdown_pid = os.getpid()
        if MODE == "shutdown-float-sequence":
            shutdown_sequence = float(shutdown_sequence)
        if MODE == "shutdown-float-pid":
            shutdown_pid = float(shutdown_pid)
        write_frame(
            {
                "schema": SHUTDOWN_ACK,
                "protocol": PROTOCOL,
                "sequence": shutdown_sequence,
                "pid": shutdown_pid,
            }
        )
        raise SystemExit(0)
    if frame.get("schema") != REQUEST or frame.get("protocol") != PROTOCOL:
        raise SystemExit(4)
    if MODE == "crash":
        os._exit(17)
    if MODE == "early-clean-exit":
        raise SystemExit(0)
    if MODE == "hang":
        time.sleep(10)
    if MODE == "partial-header":
        sys.stdout.buffer.write(b"12")
        sys.stdout.buffer.flush()
        time.sleep(10)
    if MODE == "partial-body":
        sys.stdout.buffer.write(b"100\n{")
        sys.stdout.buffer.flush()
        time.sleep(10)
    if MODE == "oversize":
        sys.stdout.buffer.write(b"999999999\n")
        sys.stdout.buffer.flush()
        time.sleep(10)
    if MODE == "stderr-oversize":
        sys.stderr.buffer.write(b"x" * 100000)
        sys.stderr.buffer.flush()
        time.sleep(10)
    if MODE == "malformed":
        write_frame("not-an-object")
        continue
    if MODE == "invalid-json":
        write_payload(b"{")
        continue

    request = frame["request"]
    counts = {
        "axiom_count": 4,
        "annotation_count": 0,
        "import_count": 0,
        "entity_count": 3,
        "diagnostic_count": 0,
    }
    raw_payload = {"schema": RAW_SCHEMA, "model_kind": "horned-model-ready", **counts}
    raw_digest = hashlib.sha256(
        RAW_DOMAIN
        + json.dumps(raw_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result = {
        "schema": "pyowl-core/comparator-adapter-result/v1",
        "lane": CONFIG["lane"],
        "implementation": CONFIG["implementation"],
        "boundary": CONFIG["boundary"],
        "status": "ok",
        "reason": None,
        "corpus_id": request["corpus_id"],
        "source_sha256": request["source_sha256"],
        "options_sha256": request["options_sha256"],
        "input_mode": request["input_mode"],
        "process_mode": request["process_mode"],
        "contract": None,
        "raw_inventory": {**raw_payload, "inventory_sha256": raw_digest},
        "metrics": {
            "wall_ns": 100,
            "cpu_ns": 90,
            "load_ns": 80,
            "rss_peak_before_bytes": 1000,
            "rss_peak_after_bytes": 1100,
            "rss_peak_increment_bytes": 100,
            "temporary_bytes": 0,
            "object_count": 10,
        },
        "timed_validation": None,
        "artifact": artifact,
    }
    if MODE == "result-float-thread-ceiling":
        result["artifact"] = dict(artifact)
        result["artifact"]["thread_ceiling"] = float(artifact["thread_ceiling"])
    response_sequence = frame["sequence"] + (1 if MODE == "cross-request" else 0)
    if MODE == "boolean-sequence":
        response_sequence = False
    if MODE == "float-sequence":
        response_sequence = float(response_sequence)
    if MODE == "replay-sequence" and instance_counter > 0:
        response_sequence -= 1
    instance_seed = (
        "reused" if MODE == "reuse-instance" else f"{os.getpid()}:{instance_counter}"
    )
    response = {
        "schema": RESPONSE,
        "protocol": PROTOCOL,
        "sequence": response_sequence,
        "ontology_instance_id": hashlib.sha256(instance_seed.encode("ascii")).hexdigest(),
        "result": result,
    }
    instance_counter += 1
    if MODE == "duplicate-json-field":
        encoded_response = json.dumps(
            response, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        marker = f'"sequence":{response_sequence}'.encode("ascii")
        write_payload(encoded_response.replace(marker, marker + b"," + marker, 1))
        continue
    write_frame(response)
    if MODE == "extra-output":
        write_frame(response)
    if MODE == "late-output":
        time.sleep(0.05)
        write_frame(response)
    if MODE == "between-response-bytes":
        time.sleep(0.05)
        sys.stdout.buffer.write(b"x")
        sys.stdout.buffer.flush()
'''


def write_persistent_runner(
    directory: Path,
    pin: ComparatorPin,
    *,
    mode: str = "normal",
) -> Path:
    """Write one exact executable whose embedded lane attestation matches ``pin``."""

    artifact = {
        "pin_state": pin.pin_state,
        "version": pin.version,
        "revision": pin.revision,
        "artifact": pin.artifact,
        "artifact_sha256": pin.artifact_sha256,
        "features": list(pin.features),
        "allocator": pin.allocator,
        "thread_ceiling": pin.thread_ceiling,
        "runner_revision": pin.runner_revision,
        "runner_sha256": None,
    }
    configuration = {
        "lane": pin.id,
        "implementation": pin.implementation,
        "boundary": pin.boundary,
        "artifact": artifact,
    }
    body = _RUNNER_BODY.replace("__CONFIG__", repr(json.dumps(configuration))).replace(
        "__MODE__", repr(mode)
    )
    path = directory / f"persistent-{mode}"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o700)
    return path


__all__ = ["write_persistent_runner"]
