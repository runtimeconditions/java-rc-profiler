#!/bin/sh
# Install the built packages in clean containers and run the profiler.
#
#   scripts/check-packages.sh PACKAGE_DIR [FIXTURES_DIR]
#
# The containers have no Java, so this proves that the packages carry their own runtime.
# FIXTURES_DIR defaults to src/test/resources, which holds generated binding packages and workloads.
set -eu

packages=$(cd "${1:?usage: check-packages.sh PACKAGE_DIR [FIXTURES_DIR]}" && pwd)
fixtures=$(cd "${2:-$(dirname "$0")/../src/test/resources}" && pwd)

# Runs inside the container, after the package is installed.
check='
set -eu
if command -v java >/dev/null 2>&1; then echo "java is on PATH, the test would prove nothing" >&2; exit 1; fi
version=$(rc-profiler-java --version --json)
echo "$version" | grep -q "\"coreSchemas\":\[\"0.4.0\"\]"
profile=$(rc-profiler-java profile generate --project /fixtures/workloads/owned --classpath /fixtures/packages/owned-kind-interface --name owned --workload-uri https://example.test/owned)
echo "$profile" | grep -q "^  - kind: service"
'

run() {
    image=$1
    install=$2
    docker pull -q "$image" >/dev/null
    if docker run --rm -v "$packages:/packages:ro" -v "$fixtures:/fixtures:ro" "$image" \
        sh -c "set -e; $install >/dev/null 2>&1; $check" >/dev/null 2>&1; then
        echo "$image: ok"
    else
        echo "$image: FAILED" >&2
        return 1
    fi
}

arch=$(uname -m)
case $arch in
    x86_64) deb=amd64; rpm=x86_64 ;;
    aarch64|arm64) deb=arm64; rpm=aarch64 ;;
    *) echo "unsupported architecture $arch" >&2; exit 1 ;;
esac

for image in ubuntu:24.04 debian:12; do
    run "$image" "apt-get install -y /packages/rc-profiler-java_*_$deb.deb"
done
for image in rockylinux:8 rockylinux:9; do
    run "$image" "dnf install -y /packages/rc-profiler-java-*.$rpm.rpm"
done
