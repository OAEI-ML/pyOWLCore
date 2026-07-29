"""Generate dependency-free fresh comparator runners for protocol tests."""

from __future__ import annotations

import sys
from pathlib import Path

_RUNNER_BODY = r"""
import hashlib
import json
import os
import signal
import sys
import time

MODE = __MODE__
PROTOCOL = "pyowl-core/comparator-fresh-runner/v1"
REQUEST = "pyowl-core/comparator-fresh-request/v1"
COMPLETED = "pyowl-core/comparator-fresh-completed/v1"
PUBLISH = "pyowl-core/comparator-fresh-publish/v1"
RESPONSE = "pyowl-core/comparator-fresh-response/v1"


def payload(value):
    return json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def framed(raw):
    return str(len(raw)).encode("ascii") + b"\n" + raw + b"\n"


def write_payload(raw):
    sys.stdout.buffer.write(framed(raw))
    sys.stdout.buffer.flush()


def write_frame(value):
    write_payload(payload(value))


def read_exact(size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sys.stdin.buffer.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise RuntimeError("duplicate JSON field")
        value[key] = item
    return value


def read_frame():
    header = sys.stdin.buffer.readline(34)
    if not header or not header.endswith(b"\n"):
        raise RuntimeError("invalid frame header")
    raw_size = header[:-1]
    if (
        not raw_size
        or any(value < ord("0") or value > ord("9") for value in raw_size)
        or (len(raw_size) > 1 and raw_size.startswith(b"0"))
    ):
        raise RuntimeError("invalid frame size")
    size = int(raw_size)
    body = read_exact(size)
    if len(body) != size or read_exact(1) != b"\n":
        raise RuntimeError("truncated frame")
    return json.loads(body, object_pairs_hook=unique_object)


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
if MODE == "oversize-control":
    sys.stdout.buffer.write(b"99999999\n")
    sys.stdout.buffer.flush()
    time.sleep(10)
if MODE == "stderr-oversize":
    sys.stderr.buffer.write(b"x" * 100000)
    sys.stderr.buffer.flush()
    time.sleep(10)

outer = read_frame()
if (
    set(outer) != {"schema", "protocol", "sequence", "request"}
    or outer.get("schema") != REQUEST
    or outer.get("protocol") != PROTOCOL
    or isinstance(outer.get("sequence"), bool)
    or not isinstance(outer.get("sequence"), int)
    or outer.get("sequence") != 0
    or not isinstance(outer.get("request"), dict)
):
    raise SystemExit(3)

if MODE == "nondecimal-header":
    sys.stdout.buffer.write(b"x\n")
    sys.stdout.buffer.flush()
    time.sleep(10)
if MODE == "noncanonical-header":
    sys.stdout.buffer.write(b"02\n{}\n")
    sys.stdout.buffer.flush()
    time.sleep(10)
if MODE == "zero-payload":
    sys.stdout.buffer.write(b"0\n\n")
    sys.stdout.buffer.flush()
    time.sleep(10)
if MODE == "missing-terminal-newline":
    sys.stdout.buffer.write(b"2\n{}x")
    sys.stdout.buffer.flush()
    time.sleep(10)

pid = os.getpid()
instance = hashlib.sha256(f"{pid}:0:0".encode("ascii")).hexdigest()
completed = {
    "schema": COMPLETED,
    "protocol": PROTOCOL,
    "sequence": 0,
    "pid": pid,
    "ontology_instance_id": instance,
}
if MODE == "completed-wrong-schema":
    completed["schema"] = "wrong-schema"
if MODE == "completed-wrong-protocol":
    completed["protocol"] = "wrong-protocol"
if MODE == "completed-bool-sequence":
    completed["sequence"] = True
if MODE == "completed-float-sequence":
    completed["sequence"] = 0.0
if MODE == "completed-negative-sequence":
    completed["sequence"] = -1
if MODE == "completed-wrong-sequence":
    completed["sequence"] = 1
if MODE == "completed-bool-pid":
    completed["pid"] = True
if MODE == "completed-float-pid":
    completed["pid"] = float(pid)
if MODE == "completed-negative-pid":
    completed["pid"] = -1
if MODE == "completed-wrong-pid":
    completed["pid"] = pid + 1
if MODE == "completed-token-type":
    completed["ontology_instance_id"] = 7
if MODE == "completed-invalid-token":
    completed["ontology_instance_id"] = "A" * 64
if MODE == "completed-wrong-token":
    completed["ontology_instance_id"] = hashlib.sha256(b"wrong").hexdigest()
if MODE == "completed-extra":
    completed["extra"] = True
if MODE == "completed-missing":
    del completed["ontology_instance_id"]
if MODE == "completed-non-object":
    write_frame([])
    time.sleep(10)
if MODE == "completed-invalid-json":
    write_payload(b"{")
    time.sleep(10)
if MODE == "completed-duplicate-json":
    raw = payload(completed)
    marker = b'"sequence":0'
    write_payload(raw.replace(marker, marker + b"," + marker, 1))
    time.sleep(10)

result = {"accepted": outer["request"]}
response = {
    "schema": RESPONSE,
    "protocol": PROTOCOL,
    "sequence": 0,
    "ontology_instance_id": instance,
    "result": result,
}
if MODE == "response-before-publish":
    sys.stdout.buffer.write(framed(payload(completed)) + framed(payload(response)))
    sys.stdout.buffer.flush()
    time.sleep(10)
if MODE == "partial-response-before-publish":
    sys.stdout.buffer.write(framed(payload(completed)) + b"12")
    sys.stdout.buffer.flush()
    time.sleep(10)

write_frame(completed)
publish = read_frame()
if (
    set(publish)
    != {"schema", "protocol", "sequence", "pid", "ontology_instance_id"}
    or publish.get("schema") != PUBLISH
    or publish.get("protocol") != PROTOCOL
    or isinstance(publish.get("sequence"), bool)
    or not isinstance(publish.get("sequence"), int)
    or publish.get("sequence") != 0
    or isinstance(publish.get("pid"), bool)
    or not isinstance(publish.get("pid"), int)
    or publish.get("pid") != pid
    or publish.get("ontology_instance_id") != instance
):
    raise SystemExit(4)
if sys.stdin.buffer.read(1) != b"":
    raise SystemExit(5)

if MODE == "post-publish-serialization-burst":
    shadow = json.dumps({"value": "x" * (64 * 1024 * 1024)}).encode("utf-8")
    result["serialization_bytes"] = len(shadow)
if MODE == "response-wrong-schema":
    response["schema"] = "wrong-schema"
if MODE == "response-wrong-protocol":
    response["protocol"] = "wrong-protocol"
if MODE == "response-bool-sequence":
    response["sequence"] = True
if MODE == "response-float-sequence":
    response["sequence"] = 0.0
if MODE == "response-negative-sequence":
    response["sequence"] = -1
if MODE == "response-wrong-sequence":
    response["sequence"] = 1
if MODE == "response-token-type":
    response["ontology_instance_id"] = 7
if MODE == "response-invalid-token":
    response["ontology_instance_id"] = "A" * 64
if MODE == "response-wrong-token":
    response["ontology_instance_id"] = hashlib.sha256(b"response-wrong").hexdigest()
if MODE == "response-extra":
    response["extra"] = True
if MODE == "response-missing":
    del response["ontology_instance_id"]
if MODE == "response-result-non-object":
    response["result"] = []
if MODE == "response-duplicate-json":
    raw = payload(response)
    marker = b'"sequence":0'
    write_payload(raw.replace(marker, marker + b"," + marker, 1))
else:
    write_frame(response)
if MODE == "extra-output":
    sys.stdout.buffer.write(b"x")
    sys.stdout.buffer.flush()
if MODE == "late-output":
    time.sleep(0.05)
    sys.stdout.buffer.write(b"x")
    sys.stdout.buffer.flush()
if MODE == "nonzero-after-response":
    raise SystemExit(9)
if MODE == "hang-after-response":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(10)
if MODE == "clean-exit-with-descendant":
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
"""


def write_fresh_runner(
    directory: Path,
    *,
    mode: str = "normal",
) -> Path:
    """Write one exact dependency-free executable fresh runner."""

    body = _RUNNER_BODY.replace("__MODE__", repr(mode))
    path = directory / f"fresh-{mode}"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o700)
    return path


__all__ = ["write_fresh_runner"]
