"""Generate dependency-free persistent comparator runners for protocol tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tools.benchmark.comparators.manifest import ComparatorPin

_RUNNER_BODY = r"""
import hashlib
import json
import os
import select
import signal
import sys
import time

CONFIG = json.loads(__CONFIG__)
MODE = __MODE__
PROTOCOL = "pyowl-core/comparator-persistent-runner/v3"
HANDSHAKE = "pyowl-core/comparator-persistent-handshake/v3"
REQUEST = "pyowl-core/comparator-persistent-request/v3"
PREPARED = "pyowl-core/comparator-persistent-prepared/v1"
EXECUTE = "pyowl-core/comparator-persistent-execute/v1"
COMPLETED = "pyowl-core/comparator-persistent-completed/v1"
PUBLISH = "pyowl-core/comparator-persistent-publish/v1"
RESPONSE = "pyowl-core/comparator-persistent-response/v3"
SHUTDOWN = "pyowl-core/comparator-persistent-shutdown/v3"
SHUTDOWN_ACK = "pyowl-core/comparator-persistent-shutdown-ack/v3"
RAW_SCHEMA = "pyowl-core/comparator-raw-inventory/v1"
RAW_DOMAIN = b"pyowl-core:comparator-raw-inventory:v1\x00"


def framed_payload(payload):
    return str(len(payload)).encode("ascii") + b"\n" + payload + b"\n"


def write_payload(payload):
    sys.stdout.buffer.write(framed_payload(payload))
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
    "prepared_schema": PREPARED,
    "execute_schema": EXECUTE,
    "completed_schema": COMPLETED,
    "publish_schema": PUBLISH,
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
if MODE == "wrong-completed-handshake":
    handshake["completed_schema"] = "wrong-completed-schema"
if MODE == "wrong-publish-handshake":
    handshake["publish_schema"] = "wrong-publish-schema"
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
    if MODE == "prepared-before-request-write-completes":
        select.select([sys.stdin.buffer], [], [], 5.0)
        write_frame(
            {
                "schema": PREPARED,
                "protocol": PROTOCOL,
                "sequence": instance_counter,
                "pid": os.getpid(),
            }
        )
    try:
        frame = read_frame()
    except EOFError:
        raise SystemExit(3)
    if frame.get("schema") == SHUTDOWN:
        if (
            set(frame) != {"schema", "protocol", "sequence"}
            or frame.get("protocol") != PROTOCOL
            or isinstance(frame.get("sequence"), bool)
            or not isinstance(frame.get("sequence"), int)
            or frame.get("sequence") != instance_counter
        ):
            raise SystemExit(7)
        if MODE == "shutdown-hang":
            time.sleep(10)
        if MODE == "shutdown-ignore-term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(10)
        if MODE == "shutdown-clean-exit-descendant":
            ready_read, ready_write = os.pipe()
            descendant_pid = os.fork()
            if descendant_pid == 0:
                os.close(ready_read)
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                os.write(ready_write, b"x")
                os.close(ready_write)
                time.sleep(10)
                os._exit(0)
            os.close(ready_write)
            os.read(ready_read, 1)
            os.close(ready_read)
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
    if (
        set(frame) != {"schema", "protocol", "sequence", "request"}
        or frame.get("schema") != REQUEST
        or frame.get("protocol") != PROTOCOL
        or isinstance(frame.get("sequence"), bool)
        or not isinstance(frame.get("sequence"), int)
        or frame.get("sequence") != instance_counter
        or not isinstance(frame.get("request"), dict)
    ):
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

    prepared = {
        "schema": PREPARED,
        "protocol": PROTOCOL,
        "sequence": frame["sequence"],
        "pid": os.getpid(),
    }
    if MODE == "prepared-wrong-schema":
        prepared["schema"] = "wrong-prepared-schema"
    if MODE == "prepared-wrong-protocol":
        prepared["protocol"] = "wrong-prepared-protocol"
    if MODE == "prepared-wrong-sequence":
        prepared["sequence"] += 1
    if MODE == "prepared-float-sequence":
        prepared["sequence"] = float(prepared["sequence"])
    if MODE == "prepared-wrong-pid":
        prepared["pid"] += 1
    if MODE == "prepared-float-pid":
        prepared["pid"] = float(prepared["pid"])
    if MODE == "prepared-extra-field":
        prepared["extra"] = True
    write_frame(prepared)

    if MODE == "completed-before-execute-write-completes":
        select.select([sys.stdin.buffer], [], [], 5.0)
        early_instance = hashlib.sha256(
            f"{os.getpid()}:{instance_counter}:{frame['sequence']}".encode("ascii")
        ).hexdigest()
        write_frame(
            {
                "schema": COMPLETED,
                "protocol": PROTOCOL,
                "sequence": frame["sequence"],
                "pid": os.getpid(),
                "ontology_instance_id": early_instance,
            }
        )
    execute = read_frame()
    if (
        set(execute) != {"schema", "protocol", "sequence", "pid"}
        or execute.get("schema") != EXECUTE
        or execute.get("protocol") != PROTOCOL
        or isinstance(execute.get("sequence"), bool)
        or not isinstance(execute.get("sequence"), int)
        or execute.get("sequence") != frame["sequence"]
        or isinstance(execute.get("pid"), bool)
        or not isinstance(execute.get("pid"), int)
        or execute.get("pid") != os.getpid()
    ):
        raise SystemExit(5)

    request = frame["request"]
    rss_burst = bytearray(8 * 1024 * 1024) if MODE == "rss-burst" else None
    if rss_burst is not None:
        for offset in range(0, len(rss_burst), 4096):
            rss_burst[offset] = 1
        time.sleep(0.02)
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
    serialization_burst_bytes = 64 * 1024 * 1024
    if MODE == "post-publish-serialization-burst":
        result["metrics"]["object_count"] = serialization_burst_bytes
    response_sequence = execute["sequence"] + (1 if MODE == "cross-request" else 0)
    if MODE == "boolean-sequence":
        response_sequence = False
    if MODE == "float-sequence":
        response_sequence = float(response_sequence)
    if MODE == "replay-sequence" and instance_counter > 0:
        response_sequence -= 1
    instance_seed = (
        "reused" if MODE == "reuse-instance" else f"{os.getpid()}:{instance_counter}"
    )
    ontology_instance_id = hashlib.sha256(instance_seed.encode("ascii")).hexdigest()
    completed = {
        "schema": COMPLETED,
        "protocol": PROTOCOL,
        "sequence": execute["sequence"],
        "pid": os.getpid(),
        "ontology_instance_id": ontology_instance_id,
    }
    if MODE == "completed-wrong-schema":
        completed["schema"] = "wrong-completed-schema"
    if MODE == "completed-wrong-protocol":
        completed["protocol"] = "wrong-completed-protocol"
    if MODE == "completed-wrong-sequence":
        completed["sequence"] += 1
    if MODE == "completed-bool-sequence":
        completed["sequence"] = True
    if MODE == "completed-float-sequence":
        completed["sequence"] = float(completed["sequence"])
    if MODE == "completed-negative-sequence":
        completed["sequence"] = -1
    if MODE == "completed-wrong-pid":
        completed["pid"] += 1
    if MODE == "completed-bool-pid":
        completed["pid"] = True
    if MODE == "completed-float-pid":
        completed["pid"] = float(completed["pid"])
    if MODE == "completed-negative-pid":
        completed["pid"] = -1
    if MODE == "completed-invalid-instance":
        completed["ontology_instance_id"] = "invalid"
    if MODE == "completed-extra-field":
        completed["extra"] = True
    if MODE == "completed-missing-pid":
        del completed["pid"]

    if MODE == "response-before-publish":
        completed_payload = json.dumps(
            completed, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        early_response_payload = json.dumps(
            {
                "schema": RESPONSE,
                "protocol": PROTOCOL,
                "sequence": response_sequence,
                "ontology_instance_id": ontology_instance_id,
                "result": result,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        sys.stdout.buffer.write(
            framed_payload(completed_payload) + framed_payload(early_response_payload)
        )
        sys.stdout.buffer.flush()
    else:
        write_frame(completed)
    if MODE == "response-before-publish-write-completes":
        select.select([sys.stdin.buffer], [], [], 5.0)
        write_frame(
            {
                "schema": RESPONSE,
                "protocol": PROTOCOL,
                "sequence": response_sequence,
                "ontology_instance_id": ontology_instance_id,
                "result": result,
            }
        )
    publish = read_frame()
    if (
        set(publish)
        != {"schema", "protocol", "sequence", "pid", "ontology_instance_id"}
        or publish.get("schema") != PUBLISH
        or publish.get("protocol") != PROTOCOL
        or isinstance(publish.get("sequence"), bool)
        or not isinstance(publish.get("sequence"), int)
        or publish.get("sequence") != execute["sequence"]
        or isinstance(publish.get("pid"), bool)
        or not isinstance(publish.get("pid"), int)
        or publish.get("pid") != os.getpid()
        or publish.get("ontology_instance_id") != ontology_instance_id
    ):
        raise SystemExit(6)

    response_instance_id = ontology_instance_id
    if MODE == "response-instance-mismatch":
        response_instance_id = hashlib.sha256(b"response-mismatch").hexdigest()
    response = {
        "schema": RESPONSE,
        "protocol": PROTOCOL,
        "sequence": response_sequence,
        "ontology_instance_id": response_instance_id,
        "result": result,
    }
    if MODE == "response-wrong-schema":
        response["schema"] = "wrong-response-schema"
    if MODE == "response-wrong-protocol":
        response["protocol"] = "wrong-response-protocol"
    if MODE == "response-invalid-instance":
        response["ontology_instance_id"] = "invalid"
    if MODE == "response-extra-field":
        response["extra"] = True
    if MODE == "response-missing-instance":
        del response["ontology_instance_id"]
    encoded_response = json.dumps(
        response, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    serialization_shadow = None
    if MODE == "post-publish-serialization-burst":
        serialization_shadow = json.dumps(
            {"shadow": "x" * serialization_burst_bytes},
            separators=(",", ":"),
        ).encode("utf-8")
        time.sleep(0.03)
    if MODE == "duplicate-json-field":
        marker = f'"sequence":{response_sequence}'.encode("ascii")
        encoded_response = encoded_response.replace(marker, marker + b"," + marker, 1)
    write_payload(encoded_response)
    instance_counter += 1
    if MODE == "extra-output":
        write_frame(response)
    if MODE == "late-output":
        time.sleep(0.05)
        write_frame(response)
    if MODE == "between-response-bytes":
        time.sleep(0.05)
        sys.stdout.buffer.write(b"x")
        sys.stdout.buffer.flush()
    del serialization_shadow
    del rss_burst
"""


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
