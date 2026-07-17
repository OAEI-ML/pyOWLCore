"""Deterministic document rendering and atomic output publication."""

from __future__ import annotations

import io
import os
import tempfile
import warnings
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO, TextIO, TypeAlias

from pyowl_core._immutable import FrozenMap, freeze_mapping
from pyowl_core.config import DocumentFormat
from pyowl_core.document import OntologyDocument
from pyowl_core.exceptions import LossyRenderWarning, UnsupportedSyntaxError
from pyowl_core.model import CanonicalSet

from .detection import coerce_format
from .functional import render_functional
from .owlxml import render_owlxml
from .rdfxml import render_rdfxml
from .turtle import render_turtle


class LossyPolicy(str, Enum):
    ERROR = "error"
    WARN = "warn"
    DROP = "drop"


@dataclass(frozen=True, slots=True)
class RenderOptions:
    canonical: bool = True
    prefixes: Mapping[str, str] = field(default_factory=FrozenMap)
    include_provenance: bool = False
    deterministic_blank_nodes: bool = True
    lossy: LossyPolicy = LossyPolicy.ERROR

    def __post_init__(self) -> None:
        if not isinstance(self.canonical, bool):
            raise TypeError("canonical must be bool")
        if not isinstance(self.include_provenance, bool):
            raise TypeError("include_provenance must be bool")
        if not isinstance(self.deterministic_blank_nodes, bool):
            raise TypeError("deterministic_blank_nodes must be bool")
        if not isinstance(self.lossy, LossyPolicy):
            raise TypeError("lossy must be LossyPolicy")
        prefixes: dict[str, str] = {}
        for key, value in self.prefixes.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("prefixes must map strings to strings")
            prefixes[key] = value
        object.__setattr__(self, "prefixes", freeze_mapping(prefixes))


DocumentTarget: TypeAlias = str | os.PathLike[str] | BinaryIO | TextIO


def render_document(
    document: OntologyDocument,
    *,
    format: DocumentFormat | str,
    options: RenderOptions | None = None,
) -> bytes:
    if not isinstance(document, OntologyDocument):
        raise TypeError("document must be OntologyDocument")
    selected = coerce_format(format)
    if selected is None:
        raise ValueError("format is required for rendering")
    render_options = RenderOptions() if options is None else options
    if not isinstance(render_options, RenderOptions):
        raise TypeError("options must be RenderOptions or None")
    render_value = document
    if document.extension_components and selected in {
        DocumentFormat.RDF_XML,
        DocumentFormat.TURTLE,
        DocumentFormat.OWL_XML,
    }:
        if render_options.lossy is LossyPolicy.ERROR:
            raise UnsupportedSyntaxError(
                f"{selected.value} writer cannot serialize extension components",
                code="RENDER_EXTENSION_UNSUPPORTED",
            )
        if render_options.lossy is LossyPolicy.WARN:
            warnings.warn(
                f"dropping extension components while rendering {selected.value}",
                LossyRenderWarning,
                stacklevel=2,
            )
        render_value = OntologyDocument(
            document.ontology_id,
            document.document_iri,
            document.direct_imports,
            document.ontology_annotations,
            document.axioms,
            CanonicalSet(),
            document.provenance,
            document.source_map,
            document.origin_index,
            document.rdf_mapping_report,
            document.diagnostics,
        )
    renderers = {
        DocumentFormat.FUNCTIONAL: render_functional,
        DocumentFormat.OWL_XML: render_owlxml,
        DocumentFormat.TURTLE: render_turtle,
        DocumentFormat.RDF_XML: render_rdfxml,
    }
    return renderers[selected](render_value)


def write_document(
    document: OntologyDocument,
    target: DocumentTarget,
    *,
    format: DocumentFormat | str,
    options: RenderOptions | None = None,
    atomic: bool = True,
) -> None:
    data = render_document(document, format=format, options=options)
    if isinstance(target, (str, os.PathLike)):
        path = Path(target)
        if atomic:
            _atomic_write(path, data)
        else:
            with path.open("wb") as stream:
                _write_binary(stream, data)
        return
    if isinstance(target, io.TextIOBase):
        _write_text(target, data)
        return
    write = getattr(target, "write", None)
    if not callable(write):
        raise TypeError("target must be a path, BinaryIO, or TextIO")
    try:
        result = write(data[:0])
    except TypeError:
        _write_text(target, data)  # type: ignore[arg-type]
    else:
        if result not in {None, 0}:
            raise OSError("zero-length stream probe unexpectedly wrote data")
        _write_binary(target, data)  # type: ignore[arg-type]


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=False, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            _write_binary(stream, data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise


def _write_binary(stream: BinaryIO, data: bytes) -> None:
    for offset in range(0, len(data), 64 * 1024):
        chunk = data[offset : offset + 64 * 1024]
        view = memoryview(chunk)
        while view:
            written = stream.write(view)
            if written is None:
                break
            if not isinstance(written, int) or written <= 0:
                raise OSError("binary stream made no write progress")
            view = view[written:]


def _write_text(stream: TextIO, data: bytes) -> None:
    text = data.decode("utf-8")
    for offset in range(0, len(text), 64 * 1024):
        chunk = text[offset : offset + 64 * 1024]
        written = stream.write(chunk)
        if written is not None and written != len(chunk):
            raise OSError("short write to text stream")


__all__ = [
    "DocumentTarget",
    "LossyPolicy",
    "RenderOptions",
    "render_document",
    "write_document",
]
