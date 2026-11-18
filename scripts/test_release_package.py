"""Tests for the helpers in release-package.py. Run: python3 -m unittest scripts/test_release_package.py"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).with_name("release-package.py")
ROOT = SCRIPT.parent.parent
spec = importlib.util.spec_from_file_location("release_package", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class VersionTagTests(unittest.TestCase):
    def test_a_stable_tag_has_no_suffix(self):
        for tag in ("v0.1.0", "v1.20.3"):
            self.assertTrue(release.STABLE.fullmatch(tag), tag)
        for tag in ("v0.1.0-rc.1", "0.1.0", "v1.0", "v01.0.0", "main", ""):
            self.assertFalse(release.STABLE.fullmatch(tag), tag)

    def test_any_release_tag_may_have_a_prerelease_suffix(self):
        for tag in ("v0.1.0", "v0.1.0-rc.1", "v2.0.0-beta.2"):
            self.assertTrue(release.ANY.fullmatch(tag), tag)
        for tag in ("0.1.0", "v1.0", "v1.0.0.1", "v1.0.0-", "main"):
            self.assertFalse(release.ANY.fullmatch(tag), tag)


class FetchTests(unittest.TestCase):
    def test_a_download_must_match_the_pinned_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "jdk.tar.gz")
            source.write_bytes(b"pretend archive")
            good = hashlib.sha256(b"pretend archive").hexdigest()
            destination = Path(directory, "good")
            release.fetch(source.as_uri(), destination, good)
            self.assertEqual(destination.read_bytes(), b"pretend archive")
            bad = Path(directory, "bad")
            with self.assertRaisesRegex(ValueError, "differs from the pinned"):
                release.fetch(source.as_uri(), bad, "0" * 64)
            self.assertFalse(bad.exists(), "a download with the wrong checksum must not be kept")


class JdkLockTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads((ROOT / "distribution/jdk.lock.json").read_text())

    def test_the_lock_pins_a_checksum_for_each_architecture(self):
        self.assertEqual(sorted(self.lock["platforms"]), ["amd64", "arm64"])
        version = self.lock["version"].replace("+", "_")
        for goarch, platform in self.lock["platforms"].items():
            self.assertRegex(platform["sha256"], r"^[0-9a-f]{64}$", goarch)
            self.assertTrue(platform["url"].startswith("https://"), goarch)
            self.assertTrue(platform["url"].endswith(platform["file"]), goarch)
            self.assertIn(version, platform["file"], goarch)
        self.assertEqual(self.lock["top"], "jdk-" + self.lock["version"])

    def test_the_bundled_jdk_is_new_enough_for_the_jar(self):
        pom = (ROOT / "pom.xml").read_text()
        release_level = int(pom.split("<maven.compiler.release>")[1].split("<")[0])
        self.assertGreaterEqual(int(self.lock["version"].split(".")[0]), release_level)

    def test_the_two_architectures_come_from_the_same_jdk_release(self):
        files = [platform["file"] for platform in self.lock["platforms"].values()]
        versions = {file.split("hotspot_")[1].removesuffix(".tar.gz") for file in files}
        self.assertEqual(len(versions), 1, files)


class RuntimeTests(unittest.TestCase):
    def jdeps(self, directory, output):
        script = Path(directory, "jdeps")
        script.write_text(f"#!/bin/sh\necho '{output}'\n")
        script.chmod(0o755)
        return script

    def test_the_runtime_has_the_modules_the_jar_needs_and_the_compiler(self):
        with tempfile.TemporaryDirectory() as directory:
            jdeps = self.jdeps(directory, "java.base,java.desktop,java.sql")
            self.assertEqual(release.runtime_modules(jdeps, Path("x.jar")), "java.base,java.desktop,java.sql,jdk.compiler")
            jdeps = self.jdeps(directory, "java.base,jdk.compiler")
            self.assertEqual(release.runtime_modules(jdeps, Path("x.jar")), "java.base,jdk.compiler")

    def test_a_list_without_java_base_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "java.base"):
                release.runtime_modules(self.jdeps(directory, "java.sql"), Path("x.jar"))

    def test_jlink_is_asked_for_a_small_image_of_those_modules(self):
        with mock.patch.object(release, "run") as run:
            release.make_runtime(Path("/jdk/bin/jlink"), Path("/jmods"), "java.base,jdk.compiler", Path("/out"))
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["/jdk/bin/jlink", "--module-path", "/jmods", "--add-modules", "java.base,jdk.compiler"])
        self.assertIn("--strip-debug", command)
        self.assertEqual(command[-2:], ["--output", "/out"])


class LayoutTests(unittest.TestCase):
    def test_the_package_puts_the_launcher_where_it_finds_the_jar_and_the_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "launcher").write_text("#!/bin/sh\n")
            (base / "app.jar").write_bytes(b"jar")
            (base / "LICENSE").write_text("license")
            runtime = base / "runtime"
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin/java").write_text("java")
            (runtime / "legal").mkdir()
            (runtime / "legal/link").symlink_to("../bin/java")
            root = base / "root"
            release.stage_package_root(base / "launcher", base / "app.jar", runtime, base / "LICENSE", root, 1700000000)
            self.assertTrue(os.access(root / "usr/bin/rc-profiler-java", os.X_OK))
            self.assertEqual((root / "usr/lib/rc-profiler-java/runtimeconditions-java-profiler.jar").read_bytes(), b"jar")
            self.assertEqual((root / "usr/lib/rc-profiler-java/runtime/bin/java").read_text(), "java")
            self.assertTrue((root / "usr/lib/rc-profiler-java/runtime/legal/link").is_symlink(), "links in the runtime are kept")
            self.assertEqual((root / "usr/share/doc/rc-profiler-java/copyright").read_text(), "license")
            self.assertTrue(all(path.lstat().st_mtime == 1700000000 for path in [root, *root.rglob("*")]))

    def test_the_real_launcher_finds_the_jar_and_runtime_of_that_layout(self):
        # <prefix>/bin/rc-profiler-java looks for <prefix>/lib/rc-profiler-java, which is what the packages install.
        launcher = (ROOT / "distribution/rc-profiler-java").read_text()
        self.assertIn('"$here/../lib/rc-profiler-java"', launcher)
        self.assertIn('"$app/runtime/bin/java"', launcher)
        self.assertIn(release.JAR, launcher)


class ArchiveTests(unittest.TestCase):
    def test_equal_input_gives_equal_bytes_with_fixed_owner_and_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, "root")
            (root / "rc-profiler-java_1.2.3").mkdir(parents=True)
            (root / "rc-profiler-java_1.2.3/LICENSE").write_text("license")
            first, second = Path(directory, "a.tar.gz"), Path(directory, "b.tar.gz")
            release.make_archive(root, first, 1700000000)
            release.make_archive(root, second, 1700000000)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first) as archive:
                self.assertEqual([m.name for m in archive.getmembers()], ["rc-profiler-java_1.2.3", "rc-profiler-java_1.2.3/LICENSE"])
                self.assertTrue(all(m.uid == 0 and m.gid == 0 and m.mtime == 1700000000 for m in archive.getmembers()))


class FormulaTests(unittest.TestCase):
    def test_the_release_fields_are_filled_in(self):
        template = (ROOT / "distribution/homebrew/rc-profiler-java.rb").read_text()
        rendered = release.render_formula(template, "1.2.3", "a" * 64)
        self.assertIn('version "1.2.3"', rendered)
        self.assertIn('sha256 "' + "a" * 64 + '"', rendered)
        self.assertNotIn("{{", rendered)

    def test_the_formula_installs_the_two_files_the_archive_holds(self):
        template = (ROOT / "distribution/homebrew/rc-profiler-java.rb").read_text()
        self.assertIn("rc-profiler-java_#{version}.tar.gz", template)
        self.assertIn('libexec.install "rc-profiler-java", "runtimeconditions-java-profiler.jar"', template)
        self.assertIn('depends_on "openjdk@21"', template)

    def test_an_unresolved_field_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "unresolved"):
            release.render_formula("{{VERSION}} {{OTHER}}", "1.2.3", "a" * 64)


class ChecksumTests(unittest.TestCase):
    def test_every_file_but_the_list_itself_is_listed_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / "b.deb").write_bytes(b"b")
            (out / "a.jar").write_bytes(b"a")
            (out / "SHA256SUMS").write_text("stale")
            self.assertEqual(
                release.checksums(out),
                f"{hashlib.sha256(b'a').hexdigest()}  a.jar\n{hashlib.sha256(b'b').hexdigest()}  b.deb\n",
            )


class CommandLineTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(["python3", str(SCRIPT), *args], capture_output=True, text=True)

    def test_a_prerelease_builds_the_jar_the_archive_and_the_checksums_only(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory, "x.jar")
            jar.write_bytes(b"jar")
            out = Path(directory, "out")
            done = self.run_script("--version", "v0.1.0-rc.1", "--jar", str(jar), "--output", str(out))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(
                sorted(path.name for path in out.iterdir()),
                ["SHA256SUMS", "rc-profiler-java_0.1.0-rc.1.tar.gz", "runtimeconditions-java-profiler-0.1.0-rc.1.jar"],
            )
            with tarfile.open(out / "rc-profiler-java_0.1.0-rc.1.tar.gz") as archive:
                names = [m.name for m in archive.getmembers()]
                self.assertEqual(names, [
                    "rc-profiler-java_0.1.0-rc.1", "rc-profiler-java_0.1.0-rc.1/LICENSE", "rc-profiler-java_0.1.0-rc.1/README.md",
                    "rc-profiler-java_0.1.0-rc.1/rc-profiler-java", "rc-profiler-java_0.1.0-rc.1/runtimeconditions-java-profiler.jar"])
                launcher = archive.getmember("rc-profiler-java_0.1.0-rc.1/rc-profiler-java")
                self.assertTrue(launcher.mode & stat.S_IXUSR, "the launcher must stay executable")
            listed = {line.split("  ")[1] for line in (out / "SHA256SUMS").read_text().splitlines()}
            self.assertEqual(listed, {"rc-profiler-java_0.1.0-rc.1.tar.gz", "runtimeconditions-java-profiler-0.1.0-rc.1.jar"})

    def test_system_packages_need_a_stable_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory, "x.jar")
            jar.write_bytes(b"jar")
            done = self.run_script("--version", "v0.1.0-rc.1", "--jar", str(jar), "--output", str(Path(directory, "o")), "--system-packages")
            self.assertEqual(done.returncode, 1)
            self.assertIn("stable", done.stderr)

    def test_a_bad_tag_a_missing_jar_and_a_used_output_directory_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            jar = Path(directory, "x.jar")
            jar.write_bytes(b"jar")
            self.assertIn("vMAJOR.MINOR.PATCH", self.run_script("--version", "1.0", "--jar", str(jar), "--output", str(Path(directory, "a"))).stderr)
            self.assertIn("is not a file", self.run_script("--version", "v1.0.0", "--jar", str(Path(directory, "no.jar")), "--output", str(Path(directory, "b"))).stderr)
            used = Path(directory, "used")
            used.mkdir()
            (used / "x").write_text("x")
            self.assertIn("new or empty", self.run_script("--version", "v1.0.0", "--jar", str(jar), "--output", str(used)).stderr)


if __name__ == "__main__":
    unittest.main()
