"""verifier/images/java/fix-maven-bundled.sh, the script the java Dockerfile runs to raise the
libraries Maven bundles in its lib directory (the node one is in test_fix_npm.py). The real run
needs Maven Central, so it happens in the image build (and is checked there by the scan); what is
pinned here is the logic that decides what to touch, against stand-ins for `mvn` and `curl` that
serve fixture jars: which jars are raised, which are left alone, that a jar whose digest is not
the pinned one (or not the one Central publishes) replaces nothing, what is checked afterwards,
and when nothing happens at all."""

import hashlib
import os
import unittest

from support import IMAGES, TempDirCase, requires_tools, run, write_executable

SCRIPT = IMAGES / "java" / "fix-maven-bundled.sh"
CENTRAL = "https://repo1.maven.org/maven2/"
WRONG_SHA256 = "0" * 64

MVN = r"""
if [ "${1:-}" = "-v" ]; then
  echo "mvn -v" >>"$FAKE_LOG"
  echo "Apache Maven 3.10.0 (c43a36b8d67be7e0805a411bc0898af1a51f5472)"
  echo "Maven home: $FAKE_MAVEN_HOME"
  exit 0
fi
echo "mvn $*" >>"$FAKE_LOG"
# what the smoke build runs through: every jar the Maven home names must be there
ls "$FAKE_MAVEN_HOME/lib" >>"$FAKE_LOG"
exit "${FAKE_MVN_STATUS:-0}"
"""

CURL = r"""
out=
url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    --proto|--retry) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
echo "curl $url" >>"$FAKE_LOG"
case "$url" in
  https://repo1.maven.org/maven2/*) ;;
  *) echo "not Maven Central: $url" >&2; exit 1 ;;
esac
file="$FAKE_REPO/${url#https://repo1.maven.org/maven2/}"
[ -f "$file" ] || exit 22
if [ -n "$out" ]; then cp "$file" "$out"; else cat "$file"; fi
"""


@requires_tools("bash", "sort", "mktemp", "sha256sum", "install", "cut", "tr", "sed")
class FixMavenBundledTest(TempDirCase):
    def setUp(self):
        super().setUp()
        write_executable(self.dir / "bin" / "mvn", MVN)
        write_executable(self.dir / "bin" / "curl", CURL)

        self.home = self.dir / "maven"
        self.lib = self.home / "lib"
        self.lib.mkdir(parents=True)
        (self.dir / "tmp").mkdir()
        self.env = {
            "FAKE_LOG": str(self.dir / "calls.log"),
            "FAKE_MAVEN_HOME": str(self.home),
            "FAKE_REPO": str(self.dir / "central"),
            "TMPDIR": str(self.dir / "tmp"),
        }

    def central(self, group, artifact, version, published=None):
        """The jar Central would serve for the coordinates, with its published .sha256 beside it.
        Returns the --min argument the Dockerfile would carry for it."""
        directory = self.dir / "central" / group.replace(".", "/") / artifact / version
        directory.mkdir(parents=True, exist_ok=True)
        jar = directory / ("%s-%s.jar" % (artifact, version))
        jar.write_bytes(("fixed %s %s\n" % (artifact, version)).encode())
        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        (directory / (jar.name + ".sha256")).write_text((published or digest) + "\n")
        return "%s:%s@%s=%s" % (group, artifact, version, digest)

    def jar(self, name, body="old"):
        path = self.lib / name
        path.write_text(body)
        return path

    def run_script(self, args, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        env["PATH"] = "%s:%s" % (self.dir / "bin", os.environ["PATH"])
        return run(["bash", SCRIPT] + args, env=env)

    def log(self):
        path = self.dir / "calls.log"
        return path.read_text() if path.exists() else ""

    def test_replaces_the_jar_below_the_floor_and_leaves_the_others(self):
        old = self.jar("guava-33.7.1-jre.jar")
        other = self.jar("failureaccess-1.0.3.jar", "keep")
        self.jar("guava.license", "licence")

        output, status = self.run_script(["--min", self.central("com.google.guava", "guava", "33.7.2-jre")])
        self.assertEqual(status, 0, output)
        self.assertIn("raising lib/guava-33.7.1-jre.jar -> guava-33.7.2-jre.jar", output)

        self.assertFalse(old.exists())
        self.assertEqual((self.lib / "guava-33.7.2-jre.jar").read_text(), "fixed guava 33.7.2-jre\n")
        self.assertEqual(other.read_text(), "keep")
        self.assertEqual((self.lib / "guava.license").read_text(), "licence")
        self.assertEqual(sorted(p.name for p in self.lib.glob("guava-*.jar")), ["guava-33.7.2-jre.jar"])
        self.assertEqual((self.lib / "guava-33.7.2-jre.jar").stat().st_mode & 0o777, 0o644)

        calls = self.log()
        self.assertIn(
            "curl %scom/google/guava/guava/33.7.2-jre/guava-33.7.2-jre.jar\n" % CENTRAL, calls
        )
        self.assertIn("curl %scom/google/guava/guava/33.7.2-jre/guava-33.7.2-jre.jar.sha256\n" % CENTRAL, calls)

    def test_maven_is_started_and_runs_a_build_after_the_replacement_with_nothing_fetched_for_it(self):
        self.jar("guava-33.7.1-jre.jar")

        output, status = self.run_script(["--min", self.central("com.google.guava", "guava", "33.7.2-jre")])
        self.assertEqual(status, 0, output)

        calls = self.log()
        self.assertIn("mvn -v\n", calls)
        self.assertIn("mvn --offline --batch-mode --quiet", calls)
        self.assertIn("validate", calls)
        # the jar the smoke ran through is the new one
        self.assertIn("guava-33.7.2-jre.jar", calls.split("mvn --offline")[1])
        self.assertNotIn("guava-33.7.1-jre.jar", calls.split("mvn --offline")[1])
        # nothing was downloaded for the smoke: only the jar and its checksum
        self.assertEqual(calls.count("curl "), 2)

    def test_the_smoke_failing_fails_the_build(self):
        self.jar("guava-33.7.1-jre.jar")

        _, status = self.run_script(
            ["--min", self.central("com.google.guava", "guava", "33.7.2-jre")], {"FAKE_MVN_STATUS": "1"}
        )
        self.assertNotEqual(status, 0)

    def test_does_nothing_and_fetches_nothing_when_the_base_is_already_at_or_above_the_floor(self):
        self.jar("guava-33.7.2-jre.jar", "base")
        self.jar("failureaccess-1.0.3.jar")

        output, status = self.run_script(
            [
                "--min", self.central("com.google.guava", "guava", "33.7.2-jre"),
                "--min", self.central("com.google.guava", "failureaccess", "1.0.3"),
            ]
        )
        self.assertEqual(status, 0, output)
        self.assertIn("already carries every requested version", output)
        self.assertEqual((self.lib / "guava-33.7.2-jre.jar").read_text(), "base")

        calls = self.log()
        self.assertNotIn("curl", calls)
        self.assertNotIn("--offline", calls)

    def test_compares_versions_as_numbers_so_33_10_0_is_not_below_33_9_0(self):
        self.jar("guava-33.10.0-jre.jar", "newer")

        output, status = self.run_script(["--min", self.central("com.google.guava", "guava", "33.9.0-jre")])
        self.assertEqual(status, 0, output)
        self.assertIn("already carries every requested version", output)
        self.assertEqual((self.lib / "guava-33.10.0-jre.jar").read_text(), "newer")
        self.assertNotIn("curl", self.log())

    def test_a_jar_of_the_other_flavour_is_never_replaced_by_this_one(self):
        self.jar("guava-33.7.1-android.jar", "android")

        output, status = self.run_script(["--min", self.central("com.google.guava", "guava", "33.7.2-jre")])
        self.assertEqual(status, 0, output)
        self.assertIn("already carries every requested version", output)
        self.assertEqual((self.lib / "guava-33.7.1-android.jar").read_text(), "android")
        self.assertFalse((self.lib / "guava-33.7.2-jre.jar").exists())

    def test_a_jar_whose_digest_is_not_the_pinned_one_replaces_nothing_and_fails_the_build(self):
        old = self.jar("guava-33.7.1-jre.jar")
        self.central("com.google.guava", "guava", "33.7.2-jre")

        output, status = self.run_script(["--min", "com.google.guava:guava@33.7.2-jre=%s" % WRONG_SHA256])

        self.assertEqual(status, 1)
        self.assertIn("Maven Central publishes", output)
        self.assertIn("pinned %s" % WRONG_SHA256, output)
        self.assertEqual(old.read_text(), "old")
        self.assertFalse((self.lib / "guava-33.7.2-jre.jar").exists())
        self.assertNotIn("--offline", self.log())

    def test_a_published_checksum_that_is_not_the_bytes_served_replaces_nothing_and_fails_the_build(self):
        old = self.jar("guava-33.7.1-jre.jar")
        # the pin agrees with the published file, but the jar served is not what it describes
        directory = self.dir / "central" / "com/google/guava/guava/33.7.2-jre"
        directory.mkdir(parents=True)
        (directory / "guava-33.7.2-jre.jar").write_bytes(b"tampered")
        (directory / "guava-33.7.2-jre.jar.sha256").write_text("%s\n" % ("ab" * 32))

        output, status = self.run_script(["--min", "com.google.guava:guava@33.7.2-jre=%s" % ("ab" * 32)])

        self.assertEqual(status, 1)
        self.assertIn("does not have the pinned sha256", output)
        self.assertIn("got %s" % hashlib.sha256(b"tampered").hexdigest(), output)
        self.assertEqual(old.read_text(), "old")
        self.assertFalse((self.lib / "guava-33.7.2-jre.jar").exists())

    def test_a_download_that_fails_replaces_nothing(self):
        old = self.jar("guava-33.7.1-jre.jar")

        _, status = self.run_script(["--min", "com.google.guava:guava@33.7.2-jre=%s" % ("ab" * 32)])

        self.assertNotEqual(status, 0)
        self.assertEqual(old.read_text(), "old")

    def test_fails_when_maven_has_no_lib_directory(self):
        os_home = self.dir / "elsewhere"
        os_home.mkdir()

        output, status = self.run_script(
            ["--min", self.central("com.google.guava", "guava", "33.7.2-jre")],
            {"FAKE_MAVEN_HOME": str(os_home)},
        )
        self.assertEqual(status, 1)
        self.assertIn("no Maven lib directory", output)

    def test_refuses_a_missing_or_malformed_floor(self):
        output, status = self.run_script([])
        self.assertEqual(status, 2)
        self.assertIn("nothing to do", output)

        digest = "a" * 64
        for bad in [
            "guava",
            "guava@33.7.2-jre=%s" % digest,
            "com.google.guava:guava@33.7.2-jre",
            "com.google.guava:guava@33.7.2-jre=",
            "com.google.guava:guava@^33.7.2=%s" % digest,
            "com.google.guava:guava@33=%s" % digest,
            "com.google.guava:Guava@33.7.2-jre=%s" % digest,
            "com.google.guava:guava@33.7.2-jre=%s" % digest.upper(),
            "com.google.guava:guava@33.7.2-jre=%s0" % digest,
            "com.google.guava:guava@33.7.2-jre=sha512-%s" % digest,
            "",
        ]:
            with self.subTest(bad=bad):
                output, status = self.run_script(["--min", bad])
                self.assertEqual(status, 2)
                self.assertIn("--min takes GROUP:ARTIFACT@VERSION=<sha256 hex>", output)

        output, status = self.run_script(["--frobnicate"])
        self.assertEqual(status, 2)
        self.assertIn("usage:", output)


if __name__ == "__main__":
    unittest.main()
