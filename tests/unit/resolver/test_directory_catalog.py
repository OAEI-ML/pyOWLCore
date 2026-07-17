from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pyowl_core import (
    IRI,
    AccessDeniedError,
    CatalogResolver,
    DirectoryNamingStrategy,
    DirectoryResolver,
    ImportCycleError,
    ImportRequest,
    ParseLimits,
)


def request(value: str) -> ImportRequest:
    iri = IRI(value)
    return ImportRequest(iri, None, (iri,), ParseLimits())


def test_directory_strategies_and_bounded_read(tmp_path: Path) -> None:
    data = b"Ontology(<urn:child>)"
    (tmp_path / "child.owl").write_bytes(data)
    basename = DirectoryResolver(tmp_path)
    resolved = basename.resolve(request("https://example.test/child.owl"))
    assert resolved is not None
    assert resolved.source == data
    assert resolved.provenance["locator"] == "child.owl"

    relative_dir = tmp_path / "nested"
    relative_dir.mkdir()
    (relative_dir / "value.owl").write_bytes(data)
    relative = DirectoryResolver(
        tmp_path,
        strategy=DirectoryNamingStrategy.RELATIVE,
        iri_prefix="https://example.test/ontologies/",
    )
    assert relative.resolve(request("https://example.test/ontologies/nested/value.owl"))

    hashed_name = hashlib.sha256(b"urn:hashed").hexdigest() + ".owl"
    (tmp_path / hashed_name).write_bytes(data)
    hashed = DirectoryResolver(tmp_path, strategy="sha256")
    assert hashed.resolve(request("urn:hashed"))


def test_directory_rejects_traversal_and_symlinks(tmp_path: Path) -> None:
    resolver = DirectoryResolver(
        tmp_path,
        strategy="relative",
        iri_prefix="https://example.test/",
    )
    with pytest.raises(AccessDeniedError):
        resolver.resolve(request("https://example.test/%2e%2e/secret.owl"))

    target = tmp_path / "target.owl"
    target.write_bytes(b"Ontology()")
    link = tmp_path / "link.owl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(AccessDeniedError) as caught:
        DirectoryResolver(tmp_path).resolve(request("https://example.test/link.owl"))
    assert caught.value.code == "IMPORT_PATH_SYMLINK"


def test_json_and_xml_catalog_exact_rewrite_alias(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.owl").write_bytes(b"Ontology(<urn:a>)")
    (docs / "b.owl").write_bytes(b"Ontology(<urn:b>)")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "mappings": {
                    "urn:a": "docs/a.owl",
                    "urn:alias": {"alias": "urn:a"},
                },
                "rewrites": [
                    {"prefix": "urn:rewrite:", "replacement": "docs/"},
                ],
            }
        ),
        encoding="utf-8",
    )
    resolver = CatalogResolver(catalog)
    assert resolver.resolve(request("urn:a")) is not None
    assert resolver.resolve(request("urn:alias")) is not None
    assert resolver.resolve(request("urn:rewrite:b.owl")) is not None

    xml = tmp_path / "catalog.xml"
    xml.write_text(
        '<catalog xmlns="urn:oasis:names:tc:entity:xmlns:xml:catalog">'
        '<uri name="urn:a" uri="docs/a.owl"/>'
        "</catalog>",
        encoding="utf-8",
    )
    assert CatalogResolver(xml).resolve(request("urn:a")) is not None


def test_catalog_security_and_include_cycle(tmp_path: Path) -> None:
    hostile = b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><catalog/>'
    with pytest.raises(AccessDeniedError) as xml_error:
        CatalogResolver(hostile, base_dir=tmp_path)
    assert xml_error.value.code == "CATALOG_XML_FORBIDDEN"

    escaped = tmp_path / "escaped.json"
    escaped.write_text(json.dumps({"mappings": {"urn:x": "../secret.owl"}}))
    with pytest.raises(AccessDeniedError) as path_error:
        CatalogResolver(escaped)
    assert path_error.value.code == "CATALOG_PATH_ESCAPE"

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"next_catalogs": ["second.json"]}))
    second.write_text(json.dumps({"next_catalogs": ["first.json"]}))
    with pytest.raises(ImportCycleError) as cycle:
        CatalogResolver(first)
    assert cycle.value.code == "CATALOG_INCLUDE_CYCLE"
