#!/bin/sh
set -eu

EXPECTED_HASHES_SHA256="10249f52d8d60e9522214958a47f683171e1ac14951108d8c281f14c78786d0f"
EXPECTED_FILES_SHA256="e26c71ac232f11e565f884bb57d72b0631333167911dec623cd74c0d0cf25cc1"

SCRIPT_DIR=$(
    CDPATH= cd -- "$(dirname -- "$0")"
    pwd
)
SCRIPT_PATH="$SCRIPT_DIR/launcher.sh"
cd "$SCRIPT_DIR"

hash_file() {
    /usr/bin/shasum -a 256 "$1" | /usr/bin/awk '{print $1}'
}

if [ "$(hash_file runtime.sha256)" != "$EXPECTED_HASHES_SHA256" ] \
    || [ "$(hash_file runtime.files)" != "$EXPECTED_FILES_SHA256" ]; then
    echo "OWLAPI runtime manifests differ from the runner pin" >&2
    exit 1
fi

inventory=$(/usr/bin/mktemp "${TMPDIR:-/tmp}/pyowl-core-owlapi-files.XXXXXX")
trap '/bin/rm -f "$inventory"' EXIT HUP INT TERM
/usr/bin/find -L runtime/jdk runtime/lib -type f -print | LC_ALL=C /usr/bin/sort >"$inventory"
if ! /usr/bin/cmp -s runtime.files "$inventory"; then
    echo "OWLAPI runtime file inventory differs from the runner pin" >&2
    exit 1
fi
if ! /usr/bin/shasum -a 256 -c runtime.sha256 >/dev/null 2>&1; then
    echo "OWLAPI runtime content differs from the runner pin" >&2
    exit 1
fi

runner_sha256=$(hash_file "$SCRIPT_PATH")
trap - EXIT HUP INT TERM
/bin/rm -f "$inventory"

exec "$SCRIPT_DIR/runtime/jdk/bin/java" \
    -Xms8g \
    -Xmx8g \
    -XX:+UseG1GC \
    -XX:+AlwaysPreTouch \
    -XX:ActiveProcessorCount=1 \
    -Dfile.encoding=UTF-8 \
    -Djava.awt.headless=true \
    -Dpyowl.runner.sha256="$runner_sha256" \
    -cp "$SCRIPT_DIR/runtime/lib/*" \
    org.pyowlcore.comparator.OwlApiRunner
