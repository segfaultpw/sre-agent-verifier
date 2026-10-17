"""verifier/images/node/fix-npm-bundled.sh, the script the node Dockerfile runs to raise the
libraries npm bundles inside itself (the python one is in test_fix_pip.py). The real run needs
the npm registry, so it happens in the image build (and is checked there by the scan); what is
pinned here is the logic that decides what to touch, against a stand-in for `npm` that serves
fixture tarballs: which copies are raised, which are left alone, what survives a replacement,
that a tarball with the wrong integrity replaces nothing, and when nothing happens at all."""

import base64
import hashlib
import json
import os
import re
import tarfile
import unittest
from pathlib import Path

from support import IMAGES, TempDirCase, requires_tools, run, write_executable

SCRIPT = IMAGES / "node" / "fix-npm-bundled.sh"
WRONG_INTEGRITY = "sha512-" + "A" * 86 + "=="

NPM = r"""
echo "npm $*" >>"$FAKE_LOG"
# npm writes its cache and its debug logs under the cache directory, ~/.npm by default
cache=${npm_config_cache:-$HOME/.npm}
mkdir -p "$cache/_logs"
echo log >"$cache/_logs/debug-0.log"
echo "cache=$cache" >>"$FAKE_LOG"
# and Node's module compile cache goes to the temporary directory unless told otherwise
compile_cache=${NODE_COMPILE_CACHE:-${TMPDIR:-/tmp}/node-compile-cache}
mkdir -p "$compile_cache"
echo compiled >"$compile_cache/module"
case "$1" in
  root) echo "$FAKE_GLOBAL" ;;
  --version) echo 11.19.0 ;;
  pack)
    if [[ " $* " == *" --pack-destination "* ]]; then
      # a release from the registry: the tarball the test made for it
      dest=
      prev=
      for arg in "$@"; do
        [ "$prev" = --pack-destination ] && dest=$arg
        prev=$arg
        spec=$arg
      done
      file=$(printf '%s-%s.tgz' "${spec%@*}" "${spec##*@}" | tr '/@' '--')
      cp "$FAKE_TARBALLS/$file" "$dest/$file"
      printf '[{"filename":"%s"}]\n' "$file"
    else
      # the package the smoke made in the working directory
      : >"$(basename "$PWD")-1.0.0.tgz"
      echo "$(basename "$PWD")-1.0.0.tgz"
    fi
    ;;
  ls) exit "${FAKE_NPM_LS_STATUS:-0}" ;;
  init) printf '{"name":"%s","version":"1.0.0"}\n' "$(basename "$PWD")" >package.json ;;
  install)
    target=${!#}
    case "$target" in
      ./*|../*|/*) ;;
      *) echo "npm install from the registry: $target" >&2; exit 1 ;;
    esac
    name=$(basename "$target" -1.0.0.tgz)
    mkdir -p "node_modules/$name"
    echo 'module.exports = 1' >"node_modules/$name/index.js"
    ;;
esac
"""


def package(path, name, version):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(json.dumps({"name": name, "version": version}, separators=(",", ":")))


def version_of(path):
    return json.loads((Path(path) / "package.json").read_text())["version"]


def pack_calls(calls):
    return re.findall(r"(?m)^npm pack .*--pack-destination", calls)


@requires_tools("bash", "jq", "tar", "find", "sort", "mktemp", "openssl")
class FixNpmBundledTest(TempDirCase):
    def setUp(self):
        super().setUp()
        write_executable(self.dir / "bin" / "npm", NPM)
        write_executable(self.dir / "bin" / "node", 'echo "node $*" >>"$FAKE_LOG"\n')

        self.glob = self.dir / "global"
        self.nm = self.glob / "npm" / "node_modules"
        self.nm.mkdir(parents=True)
        (self.dir / "tmp").mkdir()
        (self.dir / "home").mkdir()
        self.env = {
            "FAKE_LOG": str(self.dir / "calls.log"),
            "FAKE_GLOBAL": str(self.glob),
            "FAKE_TARBALLS": str(self.dir / "tarballs"),
            "TMPDIR": str(self.dir / "tmp"),
            "HOME": str(self.dir / "home"),
        }

    def floor(self, name, version):
        """The tarball the registry would serve for name@version, written where the stand-in
        npm looks for it. Returns the --min argument the Dockerfile would carry for it."""
        stage = self.dir / "stage" / ("%s-%s" % (name.replace("/", "-"), version))
        (stage / "package").mkdir(parents=True)
        (stage / "package" / "package.json").write_text(
            json.dumps({"name": name, "version": version}, separators=(",", ":"))
        )
        (stage / "package" / "fresh.js").write_text("%s\n" % version)

        filename = ("%s-%s.tgz" % (name, version)).replace("/", "-").replace("@", "-")
        (self.dir / "tarballs").mkdir(exist_ok=True)
        path = self.dir / "tarballs" / filename
        with tarfile.open(path, "w:gz") as archive:
            archive.add(stage / "package", arcname="package")
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
        return "%s@%s=%s" % (name, version, integrity)

    def run_script(self, args, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        env["PATH"] = "%s:%s" % (self.dir / "bin", os.environ["PATH"])
        return run(["bash", SCRIPT] + args, env=env)

    def log(self):
        return (self.dir / "calls.log").read_text()

    def test_replaces_a_copy_below_the_floor_on_its_major_line_and_keeps_what_lives_inside_it(self):
        brace = self.nm / "brace-expansion"
        package(brace, "brace-expansion", "5.0.7")
        (brace / "stale.js").write_text("old")
        # the build output folders carry a package.json with no name
        (brace / "dist" / "commonjs").mkdir(parents=True)
        (brace / "dist" / "commonjs" / "package.json").write_text('{"type":"commonjs"}')
        package(brace / "node_modules" / "balanced-match", "balanced-match", "4.0.4")

        package(self.nm / "minimatch" / "node_modules" / "brace-expansion", "brace-expansion", "2.0.2")
        package(self.nm / "ip-address", "ip-address", "10.5.0")

        output, status = self.run_script(
            ["--min", self.floor("brace-expansion", "5.0.11"), "--min", self.floor("ip-address", "10.3.1")]
        )
        self.assertEqual(status, 0, output)
        self.assertIn("raising npm/node_modules/brace-expansion 5.0.7 -> 5.0.11", output)

        self.assertEqual(version_of(brace), "5.0.11")
        self.assertFalse((brace / "stale.js").exists())
        self.assertFalse((brace / "dist").exists())
        self.assertEqual((brace / "fresh.js").read_text(), "5.0.11\n")
        self.assertEqual(version_of(brace / "node_modules" / "balanced-match"), "4.0.4")

        # another major line and a copy that is already new enough are not touched
        self.assertEqual(version_of(self.nm / "minimatch" / "node_modules" / "brace-expansion"), "2.0.2")
        self.assertEqual(version_of(self.nm / "ip-address"), "10.5.0")
        self.assertNotIn("ip-address", output)

        calls = self.log()
        self.assertEqual(len(pack_calls(calls)), 1)
        self.assertIn("brace-expansion@5.0.11", calls)
        # the tree is checked after the replacement
        self.assertIn("npm ls --all --omit=dev", calls)

    def test_raises_every_copy_of_a_library_nested_ones_included_from_one_download(self):
        package(self.nm / "tar", "tar", "7.5.19")
        package(self.nm / "pacote" / "node_modules" / "tar", "tar", "7.5.20")
        package(self.nm / "sigstore" / "node_modules" / "tar", "tar", "7.5.21")

        output, status = self.run_script(["--min", self.floor("tar", "7.5.21")])
        self.assertEqual(status, 0, output)

        self.assertEqual(version_of(self.nm / "tar"), "7.5.21")
        self.assertEqual(version_of(self.nm / "pacote" / "node_modules" / "tar"), "7.5.21")
        self.assertEqual(version_of(self.nm / "sigstore" / "node_modules" / "tar"), "7.5.21")
        self.assertIn("raising npm/node_modules/tar 7.5.19 -> 7.5.21", output)
        self.assertIn("raising npm/node_modules/pacote/node_modules/tar 7.5.20 -> 7.5.21", output)
        self.assertNotIn("sigstore", output)
        self.assertEqual(len(pack_calls(self.log())), 1)

    def test_handles_a_scoped_package_name(self):
        package(self.nm / "@isaacs" / "fs-minipass", "@isaacs/fs-minipass", "4.0.0")

        output, status = self.run_script(["--min", self.floor("@isaacs/fs-minipass", "4.0.1")])
        self.assertEqual(status, 0, output)

        self.assertEqual(version_of(self.nm / "@isaacs" / "fs-minipass"), "4.0.1")
        self.assertIn("@isaacs/fs-minipass@4.0.1", self.log())

    def test_compares_versions_as_numbers_so_5_10_0_is_not_below_5_9_0_and_nothing_happens(self):
        package(self.nm / "brace-expansion", "brace-expansion", "5.10.0")
        package(self.nm / "undici", "undici", "6.28.1")

        output, status = self.run_script(
            ["--min", self.floor("brace-expansion", "5.9.0"), "--min", self.floor("undici", "6.28.1")]
        )
        self.assertEqual(status, 0, output)

        self.assertIn("already carries every requested version", output)
        self.assertEqual(version_of(self.nm / "brace-expansion"), "5.10.0")

        calls = self.log()
        self.assertNotIn("npm pack", calls)
        self.assertNotIn("npm ls", calls)
        self.assertNotIn("npm install", calls)

    def test_a_pre_release_of_the_floor_is_below_it_and_a_release_with_build_metadata_is_not(self):
        package(self.nm / "brace-expansion", "brace-expansion", "5.0.11-beta.1")
        package(self.nm / "ip-address", "ip-address", "10.3.1+build.5")
        package(self.nm / "undici", "undici", "6.28.1-rc.0")
        package(self.nm / "tar", "tar", "7.5.21-1")

        output, status = self.run_script(
            [
                "--min", self.floor("brace-expansion", "5.0.11"),
                "--min", self.floor("ip-address", "10.3.1"),
                "--min", self.floor("undici", "6.28.1"),
                "--min", self.floor("tar", "7.5.21"),
            ]
        )
        self.assertEqual(status, 0, output)

        self.assertIn("raising npm/node_modules/brace-expansion 5.0.11-beta.1 -> 5.0.11", output)
        self.assertIn("raising npm/node_modules/undici 6.28.1-rc.0 -> 6.28.1", output)
        self.assertIn("raising npm/node_modules/tar 7.5.21-1 -> 7.5.21", output)
        self.assertNotIn("ip-address", output)
        self.assertEqual(version_of(self.nm / "ip-address"), "10.3.1+build.5")

    def test_a_tarball_without_the_pinned_integrity_replaces_nothing_and_fails_the_build(self):
        brace = self.nm / "brace-expansion"
        package(brace, "brace-expansion", "5.0.7")
        (brace / "stale.js").write_text("old")
        package(self.nm / "tar", "tar", "7.5.19")

        # the registry serves a real tarball for the first, but the pin is not its integrity
        self.floor("brace-expansion", "5.0.11")

        output, status = self.run_script(
            ["--min", "brace-expansion@5.0.11=%s" % WRONG_INTEGRITY, "--min", self.floor("tar", "7.5.21")]
        )

        self.assertEqual(status, 1)
        self.assertIn("brace-expansion@5.0.11 does not have the pinned integrity", output)
        self.assertIn("expected %s" % WRONG_INTEGRITY, output)
        self.assertIn("got sha512-", output)

        # nothing was replaced (the second floor's copy included), and the check that follows
        # a replacement never ran
        self.assertEqual(version_of(brace), "5.0.7")
        self.assertEqual(version_of(self.nm / "tar"), "7.5.19")
        self.assertEqual((brace / "stale.js").read_text(), "old")
        self.assertFalse((brace / "fresh.js").exists())
        self.assertNotIn("npm ls", self.log())

    def test_a_mismatch_stops_the_build_before_that_release_replaces_anything_not_before_the_earlier_ones(self):
        package(self.nm / "tar", "tar", "7.5.19")
        package(self.nm / "undici", "undici", "6.27.0")
        self.floor("undici", "6.28.1")

        output, status = self.run_script(
            ["--min", self.floor("tar", "7.5.21"), "--min", "undici@6.28.1=%s" % WRONG_INTEGRITY]
        )

        # floors go in order: the first is done, the second is refused before it touches its copy
        self.assertEqual(status, 1)
        self.assertIn("undici@6.28.1 does not have the pinned integrity", output)
        self.assertEqual(version_of(self.nm / "tar"), "7.5.21")
        self.assertEqual(version_of(self.nm / "undici"), "6.27.0")
        self.assertNotIn("npm ls", self.log())

    def test_the_tree_check_and_the_smoke_use_no_registry_package_and_run_no_third_party_code(self):
        package(self.nm / "undici", "undici", "6.27.0")

        output, status = self.run_script(["--min", self.floor("undici", "6.28.1")])
        self.assertEqual(status, 0, output)

        calls = self.log()
        self.assertIn("npm pack --silent\n", calls)
        self.assertIn("npm install --silent ../pkg/pkg-1.0.0.tgz", calls)
        self.assertIn("npm ls --silent", calls)
        # the only package the smoke installs is the one it just made, and nothing is executed
        self.assertEqual(len(re.findall(r"(?m)^npm install ", calls)), 1)
        self.assertNotIn("is-odd", calls)
        self.assertIsNone(re.search(r"(?m)^node ", calls))

    def test_npms_cache_and_logs_live_in_the_steps_own_directory_and_go_with_it(self):
        package(self.nm / "undici", "undici", "6.27.0")

        output, status = self.run_script(["--min", self.floor("undici", "6.28.1")])
        self.assertEqual(status, 0, output)

        caches = sorted(set(re.findall(r"(?m)^cache=(.*)$", self.log())))
        self.assertEqual(len(caches), 1)
        self.assertTrue(caches[0].startswith(str(self.dir / "tmp") + "/"))

        # no cache, no debug log, no compile cache, and no temporary directory is left in the image layer
        self.assertFalse(Path(caches[0]).exists())
        self.assertFalse((self.dir / "home" / ".npm").exists())
        self.assertEqual(os.listdir(self.dir / "tmp"), [])

    def test_fails_when_the_replaced_tree_no_longer_satisfies_a_declared_dependency(self):
        package(self.nm / "undici", "undici", "6.27.0")

        _, status = self.run_script(["--min", self.floor("undici", "6.28.1")], {"FAKE_NPM_LS_STATUS": "1"})
        self.assertNotEqual(status, 0)

    def test_refuses_a_missing_or_malformed_floor(self):
        output, status = self.run_script([])
        self.assertEqual(status, 2)
        self.assertIn("nothing to do", output)

        integrity = "sha512-" + "A" * 86 + "=="
        for bad in [
            "undici",
            "undici@6",
            "undici@^6.28.1",
            "Undici@6.28.1=%s" % integrity,
            "undici@6.28.1",
            "undici@6.28.1=",
            "undici@6.28.1=sha256-%s=" % ("A" * 43),
            "undici@6.28.1=sha512-short==",
            "undici@6.28.1=%s0" % integrity,
            "",
        ]:
            with self.subTest(bad=bad):
                output, status = self.run_script(["--min", bad])
                self.assertEqual(status, 2)
                self.assertIn("--min takes NAME@VERSION=sha512-<base64 integrity>", output)

        output, status = self.run_script(["--frobnicate"])
        self.assertEqual(status, 2)
        self.assertIn("usage:", output)


if __name__ == "__main__":
    unittest.main()
