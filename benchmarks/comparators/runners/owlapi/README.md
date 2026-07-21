# Isolated OWLAPI comparator runner

This directory builds the development-only OWLAPI 5.5.1
`common-contract-ready` runner. Nothing under this directory enters the
pyowl-core build, sdist, wheel, runtime dependency graph, or ordinary tests.

The authenticated runtime is Eclipse Temurin 21.0.7+6 for Darwin x86-64. The
official `OpenJDK21U-jdk_x64_mac_hotspot_21.0.7_6.tar.gz` archive has SHA-256
`8e6d876f60bc8b7866e91222ba9f27a78e5102d7a4ce4a6e915f95fe539b66ed`.
The build uses Apache Maven 3.9.9; its binary archive has SHA-512
`a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b`.

Starting with an empty ignored `runtime/` directory, reproduce the runtime:

```console
export JAVA_HOME=/absolute/path/to/jdk-21.0.7+6/Contents/Home
mvn -q clean package
mkdir -p runtime/lib
ln -s "$JAVA_HOME" runtime/jdk
cp target/dependency/*.jar runtime/lib/
cp target/pyowl-core-owlapi-comparator.jar runtime/lib/runner.jar
shasum -a 256 -c runtime.sha256
find -L runtime/jdk runtime/lib -type f -print | LC_ALL=C sort | cmp runtime.files -
export PYOWL_CORE_OWLAPI_RUNNER="$PWD/launcher.sh"
```

The deterministic runner JAR SHA-256 is
`45d8708817ad425e37d993c029ec39c214e83e5516faa87a046a24a04bf18219`.
`runtime.sha256` authenticates all 521 JDK/JAR files and `runtime.files`
rejects additions or omissions before Java starts. The launcher itself is
SHA-256 pinned by `comparators.toml` and passes its observed digest into the
runner handshake.

The launcher fixes `-Xms8g -Xmx8g`, G1GC, `AlwaysPreTouch`, UTF-8, headless
mode, and one active processor. Each fresh request creates a new ontology; a
persistent process creates a new ontology for every framed request and proves
distinct instance identities and deterministic shutdown. Functional Syntax,
OWL/XML, RDF/XML, and Turtle use explicit OWLAPI reader formats. Semantics or
RDF provenance order that OWLAPI cannot preserve independently fail closed as
`ineligible`; they are never reduced to a smaller comparison contract.
