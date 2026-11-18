#!/usr/bin/env python3
"""Build the release files of the Java profiler.

For one release tag this writes, into a new or empty directory:

  runtimeconditions-java-profiler-<v>.jar            the jar, which needs a JDK 17 or newer
  rc-profiler-java_<v>.tar.gz                        the launcher and the jar, for Homebrew and for download
  SHA256SUMS                                         the checksum of every file above and below

and, for a stable tag, when --system-packages is given:

  rc-profiler-java_<v>-1_<amd64|arm64>.deb           the launcher and the jar with their own Java runtime, for apt
  rc-profiler-java-<v>-1.<x86_64|aarch64>.rpm        the same, for yum and dnf
  rc-profiler-java.rb                                the Homebrew formula for this release

The deb and rpm carry a Java runtime made with jlink from the Temurin JDK pinned in distribution/jdk.lock.json,
holding only the modules the jar needs, so installing them needs nothing but the system package manager. Homebrew
depends on its own openjdk formula. Run on Linux amd64: it runs the pinned amd64 jlink for both architectures,
with each architecture's own jmods.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

STABLE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
ANY = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$")
NAME = "rc-profiler-java"
JAR = "runtimeconditions-java-profiler.jar"
# jdeps finds the modules the jar's code names; the compiler is found through a service loader, so name it too.
ALWAYS_NEEDED = ("jdk.compiler",)


def run(args, *, cwd=None, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url, destination, expected_sha256):
    """Download url to destination and refuse it unless it matches the pinned checksum."""
    with urllib.request.urlopen(url, timeout=300) as response, open(destination, "wb") as out:
        shutil.copyfileobj(response, out)
    actual = sha256_file(destination)
    if actual != expected_sha256:
        Path(destination).unlink()
        raise ValueError(f"{url}: checksum {actual} differs from the pinned {expected_sha256}")


def jdk(lock, goarch, cache, whole):
    """Download the pinned JDK for one architecture and return its directory.

    The JDK that runs jlink is extracted whole; another architecture's only gives its jmods."""
    platform = lock["platforms"][goarch]
    archive = cache / platform["file"]
    if not archive.exists():
        fetch(platform["url"], archive, platform["sha256"])
    elif sha256_file(archive) != platform["sha256"]:
        raise ValueError(f"{archive}: cached JDK archive differs from the pinned checksum")
    out = cache / goarch
    top = lock["top"]
    out.mkdir(exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = [m for m in tar.getmembers() if whole or m.name.startswith(f"{top}/jmods/")]
        if not members:
            raise ValueError(f"{archive}: nothing to extract under {top}")
        tar.extractall(out, members=members, filter="data")
    return out / top


def runtime_modules(jdeps, jar):
    """The modules the jar needs, as a comma-separated list."""
    found = subprocess.run(
        [str(jdeps), "--multi-release", "21", "--ignore-missing-deps", "--print-module-deps", str(jar)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    modules = [item for item in found.split(",") if item]
    for needed in ALWAYS_NEEDED:
        if needed not in modules:
            modules.append(needed)
    if "java.base" not in modules:
        raise ValueError(f"jdeps did not list java.base for {jar}: {found!r}")
    return ",".join(sorted(set(modules)))


def make_runtime(jlink, jmods, modules, output):
    run([str(jlink), "--module-path", str(jmods), "--add-modules", modules, "--strip-debug", "--no-header-files",
         "--no-man-pages", "--compress=zip-6", "--output", str(output)])


def normalize(root, epoch):
    """Give every file and directory the same modification time, so equal input gives equal packages."""
    for path in [root, *root.rglob("*")]:
        os.utime(path, (epoch, epoch), follow_symlinks=False)


def stage_package_root(launcher, jar, runtime, project_license, root, epoch):
    usr = root / "usr"
    (usr / "bin").mkdir(parents=True)
    shutil.copy2(launcher, usr / "bin" / NAME)
    (usr / "bin" / NAME).chmod(0o755)
    library = usr / "lib" / NAME
    library.mkdir(parents=True)
    shutil.copy2(jar, library / JAR)
    # The launcher finds the jar in ../lib/rc-profiler-java, and a runtime in a runtime directory beside it.
    shutil.copytree(runtime, library / "runtime", symlinks=True)
    documentation = usr / "share/doc" / NAME
    documentation.mkdir(parents=True)
    shutil.copy2(project_license, documentation / "copyright")
    normalize(root, epoch)


def make_archive(root, output, epoch):
    """Write a reproducible tar.gz: sorted entries, fixed times, no owner names."""
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(root.rglob("*")):
                info = archive.gettarinfo(str(path), arcname=path.relative_to(root).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = epoch
                if info.isreg():
                    with open(path, "rb") as handle:
                        archive.addfile(info, handle)
                else:
                    archive.addfile(info)
    Path(output).write_bytes(buffer.getvalue())


def render_formula(template, version, archive_sha256):
    rendered = template.replace("{{VERSION}}", version).replace("{{SHA256}}", archive_sha256)
    if "{{" in rendered:
        raise ValueError("Homebrew formula template has unresolved release fields")
    return rendered


def checksums(output):
    files = sorted(path for path in output.iterdir() if path.name != "SHA256SUMS")
    return "".join(f"{sha256_file(path)}  {path.name}\n" for path in files)


def source_date_epoch(repository):
    value = os.environ.get("SOURCE_DATE_EPOCH")
    if value is not None:
        return int(value)
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=repository, check=True, capture_output=True, text=True)
        return int(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="release tag, such as v0.3.0; system packages need a stable one")
    parser.add_argument("--jar", required=True, type=Path, help="the jar built at this version")
    parser.add_argument("--output", required=True, type=Path, help="new or empty directory for the release files")
    parser.add_argument("--system-packages", action="store_true", help="also build the deb, rpm and Homebrew formula")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    if not ANY.fullmatch(args.version):
        raise ValueError("release requires a vMAJOR.MINOR.PATCH tag, optionally with a prerelease suffix")
    if args.system_packages and not STABLE.fullmatch(args.version):
        raise ValueError("system packages need a stable vMAJOR.MINOR.PATCH tag")
    version = args.version[1:]
    repository = args.repository.resolve()
    if not args.jar.is_file():
        raise ValueError(f"{args.jar} is not a file")
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("release output directory must be new or empty")
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    epoch = source_date_epoch(repository)
    launcher = repository / "distribution" / NAME
    project_license = repository / "LICENSE"

    shutil.copy2(args.jar, output / f"runtimeconditions-java-profiler-{version}.jar")
    with tempfile.TemporaryDirectory(prefix="rc-profiler-java-release-") as temporary:
        work = Path(temporary)
        archive_root = work / "archive"
        package = archive_root / f"{NAME}_{version}"
        package.mkdir(parents=True)
        shutil.copy2(launcher, package / NAME)
        (package / NAME).chmod(0o755)
        shutil.copy2(args.jar, package / JAR)
        shutil.copy2(repository / "README.md", package / "README.md")
        shutil.copy2(project_license, package / "LICENSE")
        normalize(archive_root, epoch)
        archive = output / f"{NAME}_{version}.tar.gz"
        make_archive(archive_root, archive, epoch)

        if args.system_packages:
            lock = json.loads((repository / "distribution/jdk.lock.json").read_text())
            nfpm_config = (repository / "distribution/nfpm.yaml").resolve()
            cache = work / "jdk"
            cache.mkdir()
            host = jdk(lock, "amd64", cache, whole=True)
            modules = runtime_modules(host / "bin/jdeps", args.jar)
            for goarch in sorted(lock["platforms"]):
                jmods = host / "jmods" if goarch == "amd64" else jdk(lock, goarch, cache, whole=False) / "jmods"
                runtime = work / goarch / "runtime"
                runtime.parent.mkdir()
                make_runtime(host / "bin/jlink", jmods, modules, runtime)
                package_root = work / goarch / "package-root"
                stage_package_root(launcher, args.jar, runtime, project_license, package_root, epoch)
                env = dict(os.environ, GOARCH=goarch, PACKAGE_VERSION=version, SOURCE_DATE_EPOCH=str(epoch))
                for packager in ("deb", "rpm"):
                    run(["nfpm", "package", "--config", str(nfpm_config), "--packager", packager, "--target", str(output)],
                        cwd=package_root, env=env)
                shutil.rmtree(work / goarch)
            template = (repository / "distribution/homebrew" / f"{NAME}.rb").read_text()
            (output / f"{NAME}.rb").write_text(render_formula(template, version, sha256_file(archive)))
    (output / "SHA256SUMS").write_text(checksums(output))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"release-package: {error}")
