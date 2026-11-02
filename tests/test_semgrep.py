"""semgrep in the images: verifier/semgrep/install-semgrep.sh, the script every Dockerfile runs, the
two lock files it reads, check-installed.py, and how the six Dockerfiles use them. The real run needs
GitHub and PyPI, so it happens in the image build (and is checked there by verifier/test/contract.sh
and the scan); what is pinned here is the logic that decides what to fetch, what to trust and what
to leave behind, against stand-ins for `curl` and for the standalone Python."""

import hashlib
import io
import os
import re
import shutil
import stat
import tarfile
import unittest

from support import (
    CONTRACT,
    IMAGES,
    RUNTIMES,
    VERIFIER,
    TempDirCase,
    dockerfile,
    read,
    requires_tools,
    run,
    write_executable,
)

SEMGREP_DIR = VERIFIER / "semgrep"
INSTALL = SEMGREP_DIR / "install-semgrep.sh"
CHECK = SEMGREP_DIR / "check-installed.py"
SEMGREP_LOCK = SEMGREP_DIR / "semgrep.lock"
PYTHON_LOCK = SEMGREP_DIR / "python-standalone.lock"

INSTALL_RUN = re.compile(
    r"RUN --mount=type=bind,source=semgrep,target=/tmp/semgrep-install \\\n"
    r"    bash /tmp/semgrep-install/install-semgrep\.sh --python (\S+) --semgrep (\S+)\n"
)
REQUIREMENT = re.compile(r"\A[A-Za-z0-9._-]+==[0-9][A-Za-z0-9.!+]*( --hash=sha256:[0-9a-f]{64})+\Z")


def requirement_lines(path):
    text = "\n".join(l for l in path.read_text().split("\n") if not re.match(r"\A\s*#", l))
    text = re.sub(r"\\\n\s*", "", text)
    return [l for l in text.split("\n") if l]


class DockerfilesTest(unittest.TestCase):
    def pins(self):
        found = {}
        for runtime in RUNTIMES:
            matches = INSTALL_RUN.findall(dockerfile(runtime))
            self.assertEqual(len(matches), 1, "%s runs the semgrep install %d times" % (runtime, len(matches)))
            found[runtime] = matches[0]
        return found

    def test_every_image_installs_the_same_semgrep_and_python_once(self):
        pins = self.pins()
        self.assertEqual(len(set(pins.values())), 1, pins)
        python, semgrep = next(iter(pins.values()))
        self.assertRegex(python, r"\A3\.1[0-9]\.[0-9]+\+[0-9]{8}\Z")
        self.assertRegex(semgrep, r"\A[0-9]+\.[0-9]+\.[0-9]+\Z")

    def test_the_install_comes_after_the_last_package_step_and_before_the_hardening(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                text = dockerfile(runtime)
                at = text.index("install-semgrep.sh")
                self.assertLess(at, text.index("bash /tmp/harden-writable.sh sandbox"))
                for earlier in (
                    "useradd --create-home --uid 10001",
                    "fix-npm-bundled.sh",
                    "fix-pip-vendored.sh",
                    "fix-maven-bundled.sh",
                    "mix local.hex",
                ):
                    if earlier in text:
                        self.assertLess(text.index(earlier), at, earlier)
                for later in ("ENTRYPOINT", "ENV VERIFIER_RUNTIME"):
                    self.assertGreater(text.index(later), at, later)

    def test_every_image_has_what_the_install_needs_to_fetch_and_to_check(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                text = dockerfile(runtime)
                for tool in ("curl", "ca-certificates", " jq ", "coreutils"):
                    self.assertIn(tool, text)

    def test_the_semgrep_lock_pins_the_release_the_dockerfiles_name(self):
        _, semgrep = next(iter(self.pins().values()))
        pinned = [l for l in requirement_lines(SEMGREP_LOCK) if l.startswith("semgrep==")]
        self.assertEqual(len(pinned), 1)
        self.assertTrue(pinned[0].startswith("semgrep==%s " % semgrep), pinned[0][:40])

    def test_the_python_lock_has_a_digest_for_the_release_the_dockerfiles_name_on_both_architectures(self):
        python, _ = next(iter(self.pins().values()))
        entries = {}
        for line in PYTHON_LOCK.read_text().split("\n"):
            if line and not line.startswith("#"):
                match = re.fullmatch(r"([0-9a-f]{64})  (cpython-\S+-unknown-linux-gnu-install_only_stripped\.tar\.gz)", line)
                self.assertIsNotNone(match, line)
                entries[match.group(2)] = match.group(1)
        for arch in ("x86_64", "aarch64"):
            self.assertIn("cpython-%s-%s-unknown-linux-gnu-install_only_stripped.tar.gz" % (python, arch), entries)
        self.assertEqual(len(entries), 2, "the lock holds only the pinned release: %s" % sorted(entries))


class LockFilesTest(unittest.TestCase):
    def test_every_requirement_is_pinned_and_carries_only_sha256_hashes(self):
        lines = requirement_lines(SEMGREP_LOCK)
        self.assertGreater(len(lines), 50)
        for line in lines:
            self.assertRegex(line, REQUIREMENT, "%s is not pinned by hash" % line[:40])

    def test_no_requirement_is_listed_twice_and_the_locked_set_is_closed_under_the_header_claims(self):
        names = [l.split("==")[0].lower().replace("_", "-") for l in requirement_lines(SEMGREP_LOCK)]
        self.assertEqual(len(names), len(set(names)))
        for needed in ("semgrep", "click", "rich", "jsonschema", "requests", "urllib3", "certifi", "cryptography"):
            self.assertIn(needed, names)
        self.assertNotIn("pip", names, "the installer is not part of the install")
        self.assertNotIn("setuptools", names)

    def test_the_header_says_how_to_regenerate_it(self):
        self.assertIn("Bumping semgrep", SEMGREP_LOCK.read_text())
        self.assertIn("Bumping semgrep", PYTHON_LOCK.read_text())

    def test_the_contract_documents_the_bump_and_the_install(self):
        text = " ".join(read(CONTRACT).split())
        for needle in (
            "### Bumping semgrep, or the Python it runs on",
            "pip-compile --generate-hashes",
            "python-standalone.lock",
            "install_only_stripped",
            "SHA256SUMS",
            "--require-hashes --only-binary :all: --no-deps",
            "`/usr/local/bin/semgrep` is a root-owned wrapper",
            "python -I",
        ):
            self.assertIn(needle, text)


class ScriptShapeTest(unittest.TestCase):
    def test_the_script_is_executable_stops_on_error_and_only_ever_fetches_from_github_and_pypi_by_pin(self):
        text = INSTALL.read_text()
        self.assertTrue(os.access(INSTALL, os.X_OK))
        self.assertIn("set -euo pipefail", text)
        self.assertEqual(re.findall(r"https?://\S+", text.replace("$release_base", "")), ["https://github.com/astral-sh/python-build-standalone/releases/download"])
        self.assertIn("--proto '=https' --tlsv1.2", text)
        for flag in ("--require-hashes", "--only-binary :all:", "--no-deps", "--isolated"):
            self.assertIn(flag, text)
        self.assertIn("uninstall --yes pip", text)

    def test_the_wrapper_runs_semgrep_isolated_with_metrics_and_the_version_check_off(self):
        text = INSTALL.read_text()
        wrapper = text.split("<<WRAPPER\n")[1].split("\nWRAPPER")[0]
        for needle in (
            "SEMGREP_SEND_METRICS=off",
            "SEMGREP_ENABLE_VERSION_CHECK=0",
            "PYTHONNOUSERSITE=1",
            "PYTHONSAFEPATH=1",
            'exec $py -I $prefix/python/bin/semgrep "\\$@"',
        ):
            self.assertIn(needle, wrapper)

    def test_the_contract_script_proves_the_install_and_a_scan_in_every_image(self):
        text = (VERIFIER / "test" / "contract.sh").read_text()
        for needle in ("semgrep_cases", "scan_cases", "corpus_cases", "--network none", "planted-import", "mode: \"scan\""):
            self.assertIn(needle, text)
        body = text.split("semgrep_cases() {")[1].split("\nrun_scan() {")[0]
        self.assertIn(":/fixture:ro", body, "the tree is read-only")
        self.assertIn("--user 10001:10001", body, "as the unprivileged user")


class CheckInstalledTest(TempDirCase):
    def site(self, packages):
        site = self.dir / "site"
        site.mkdir(exist_ok=True)
        for name, version in packages.items():
            info = site / ("%s-%s.dist-info" % (name.replace("-", "_"), version))
            info.mkdir(exist_ok=True)
            (info / "METADATA").write_text("Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version))
        return site

    def lock(self, pins):
        path = self.dir / "x.lock"
        path.write_text("# header\n" + "".join("%s==%s \\\n    --hash=sha256:%s\n    # via x\n" % (n, v, "0" * 64) for n, v in pins.items()))
        return path

    def check(self, lock, site, *extra):
        return run(["python3", "-I", CHECK, lock, site, *extra])

    def test_agrees_when_the_installed_set_is_the_lock(self):
        output, status = self.check(self.lock({"a-b": "1.0", "c": "2"}), self.site({"a_b": "1.0", "c": "2"}))
        self.assertEqual((status, output), (0, ""))

    def test_names_are_compared_the_way_pip_does(self):
        output, status = self.check(self.lock({"Ruamel.Yaml": "1"}), self.site({"ruamel-yaml": "1"}))
        self.assertEqual(status, 0, output)

    def test_names_every_difference(self):
        site = self.site({"a": "1", "b": "9", "extra": "3"})
        output, status = self.check(self.lock({"a": "1", "b": "2", "gone": "4"}), site)
        self.assertEqual(status, 1)
        self.assertIn("missing: gone==4", output)
        self.assertIn("not in the lock: extra==3", output)
        self.assertIn("b is 9, the lock pins 2", output)

    def test_an_ignored_name_may_be_installed_and_is_not_required(self):
        site = self.site({"a": "1", "pip": "26"})
        self.assertEqual(self.check(self.lock({"a": "1"}), site, "--ignore", "pip")[1], 0)
        self.assertEqual(self.check(self.lock({"a": "1"}), site)[1], 1)
        self.assertEqual(self.check(self.lock({"a": "1", "pip": "26"}), site, "--ignore", "pip")[1], 1)

    def test_a_lock_that_pins_nothing_is_refused_and_so_is_bad_usage(self):
        output, status = self.check(self.dir / "empty.lock", self.site({"a": "1"}))
        self.assertNotEqual(status, 0)
        (self.dir / "empty.lock").write_text("# nothing\n")
        output, status = self.check(self.dir / "empty.lock", self.site({"a": "1"}))
        self.assertEqual(status, 1)
        self.assertIn("pins nothing", output)
        self.assertEqual(run(["python3", "-I", CHECK])[1], 2)
        self.assertEqual(run(["python3", "-I", CHECK, "a", "b", "--wrong"])[1], 2)

    def test_reads_the_real_lock(self):
        site = self.site({"semgrep": "0"})
        output, status = self.check(SEMGREP_LOCK, site)
        self.assertEqual(status, 1)
        self.assertIn("missing: click==", output)
        self.assertIn("semgrep is 0, the lock pins", output)


PYTHON_STUB = r"""
echo "python $*" >>"$FAKE_LOG"
if [ "${1:-}" = -I ]; then shift; fi
site="$FAKE_PREFIX/python/lib/python3.13/site-packages"
case "${1:-}" in
  -c)
    case "$2" in
      *python_version*) echo "${FAKE_PY_VERSION:-3.13.16}" ;;
      *purelib*) echo "$site" ;;
      *stdlib*) echo "$FAKE_PREFIX/python/lib/python3.13" ;;
      *) exit 1 ;;
    esac
    ;;
  -m)
    [ "$2" = pip ] || exit 1
    shift 2
    lock=
    sub=
    while [ "$#" -gt 0 ]; do
      case "$1" in
        install | check | uninstall) sub=$1 ;;
        -r) lock=$2; shift ;;
      esac
      shift
    done
    case "$sub" in
      install)
        [ -z "${FAKE_PIP_STATUS:-}" ] || exit "$FAKE_PIP_STATUS"
        mkdir -p "$site"
        sed -nE 's/^([A-Za-z0-9._-]+)==([^ ]+).*/\1 \2/p' "$lock" | while read -r name version; do
          mkdir -p "$site/${name//-/_}-$version.dist-info"
          printf 'Metadata-Version: 2.1\nName: %s\nVersion: %s\n' "$name" "$version" >"$site/${name//-/_}-$version.dist-info/METADATA"
        done
        if [ -n "${FAKE_EXTRA_DIST:-}" ]; then
          mkdir -p "$site/$FAKE_EXTRA_DIST-1.dist-info"
          printf 'Metadata-Version: 2.1\nName: %s\nVersion: 1\n' "$FAKE_EXTRA_DIST" >"$site/$FAKE_EXTRA_DIST-1.dist-info/METADATA"
        fi
        ;;
      uninstall)
        for p in "$site"/pip "$site"/pip-*.dist-info; do [ -e "$p" ] && rm -rf "$p"; done
        ;;
    esac
    ;;
  *check-installed.py) exec python3 -I "$@" ;;
  */bin/semgrep) exec bash "$@" ;;
  *) exit 1 ;;
esac
"""

SEMGREP_STUB = r"""
echo "semgrep $*" >>"$FAKE_LOG"
env | grep -E '^(SEMGREP_|PYTHON)' | sort >>"$FAKE_LOG"
case "${1:-}" in
  --version) echo "${FAKE_SEMGREP_VERSION:-1.183.0}" ;;
  scan)
    if [ "${FAKE_SMOKE:-find}" = find ]; then
      printf '{"results": [{"check_id": "sre-install-smoke"}], "errors": []}\n'
    else
      printf '{"results": [], "errors": []}\n'
    fi
    ;;
esac
"""

CURL = r"""
out=
url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    --proto | --tlsv1.2 | --retry) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
echo "curl $url" >>"$FAKE_LOG"
file="$FAKE_DOWNLOADS/${url##*/}"
[ -f "$file" ] || exit 22
cp "$file" "$out"
"""

VERSION = "3.13.16+20261013"
FILE = {
    "x86_64": "cpython-3.13.16+20261013-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
    "aarch64": "cpython-3.13.16+20261013-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz",
}


def standalone_tarball(extra_entries=()):
    """What python-build-standalone's install_only_stripped holds, in the parts the script touches."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        def add(name, body, mode=0o644):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            tar.addfile(info, io.BytesIO(body))

        add("python/bin/python3", ("#!/usr/bin/env bash\n" + PYTHON_STUB).encode(), 0o755)
        add("python/bin/semgrep", ("#!/usr/bin/env bash\n" + SEMGREP_STUB).encode(), 0o755)
        for name in ("pip", "pip3", "pip3.13", "idle3", "idle3.13", "pydoc3", "pydoc3.13", "python3.13"):
            add("python/bin/" + name, b"#!/bin/sh\n", 0o755)
        add("python/include/python3.13/Python.h", b"x")
        add("python/share/man/python3.1", b"x")
        add("python/lib/python3.13/os.py", b"x")
        add("python/lib/python3.13/test/test_x.py", b"x")
        add("python/lib/python3.13/idlelib/x.py", b"x")
        add("python/lib/python3.13/turtledemo/x.py", b"x")
        add("python/lib/python3.13/ensurepip/__init__.py", b"x")
        add("python/lib/python3.13/ensurepip/_bundled/pip-26.whl", b"x")
        add("python/lib/python3.13/site-packages/README.txt", b"x")
        add("python/lib/python3.13/site-packages/pip/__init__.py", b"x")
        add("python/lib/python3.13/site-packages/pip-26.0.dist-info/METADATA", b"Metadata-Version: 2.1\nName: pip\nVersion: 26.0\n")
        for name, body in extra_entries:
            add(name, body)
    return buffer.getvalue()


@requires_tools("bash", "awk", "tar", "sha256sum", "install", "mktemp", "jq", "grep", "python3")
class InstallSemgrepTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.scripts = self.dir / "scripts"
        self.scripts.mkdir()
        shutil.copy(INSTALL, self.scripts / "install-semgrep.sh")
        shutil.copy(CHECK, self.scripts / "check-installed.py")
        (self.scripts / "semgrep.lock").write_text(
            "# header\n"
            "semgrep==1.183.0 \\\n    --hash=sha256:%s\n"
            "click==8.4.2 \\\n    --hash=sha256:%s \\\n    --hash=sha256:%s\n    # via semgrep\n"
            "ruamel-yaml==0.19.1 \\\n    --hash=sha256:%s\n" % ("1" * 64, "2" * 64, "3" * 64, "4" * 64)
        )
        self.downloads = self.dir / "downloads"
        self.downloads.mkdir()
        self.prefix = self.dir / "opt" / "semgrep"
        self.bin = self.dir / "usr-local-bin"
        self.bin.mkdir()
        (self.dir / "tmp").mkdir()
        write_executable(self.dir / "stubs" / "curl", CURL)
        self.log = self.dir / "calls.log"
        self.tarball = standalone_tarball()
        self.serve(self.tarball)

    def serve(self, tarball, arch="x86_64"):
        (self.downloads / FILE[arch].replace("+", "%2B")).write_bytes(tarball)
        digest = hashlib.sha256(tarball).hexdigest()
        lines = []
        for other in ("aarch64", "x86_64"):
            lines.append("%s  %s\n" % (digest if other == arch else "e" * 64, FILE[other]))
        (self.scripts / "python-standalone.lock").write_text("# lock\n" + "".join(lines))

    def install(self, *args, env=None):
        merged = {
            "PATH": "%s:%s" % (self.dir / "stubs", os.environ["PATH"]),
            "SEMGREP_PREFIX": str(self.prefix),
            "SEMGREP_BIN_DIR": str(self.bin),
            "FAKE_LOG": str(self.log),
            "FAKE_DOWNLOADS": str(self.downloads),
            "FAKE_PREFIX": str(self.prefix),
            "TMPDIR": str(self.dir / "tmp"),
        }
        merged.update(env or {})
        arguments = list(args) if args else ["--python", VERSION, "--semgrep", "1.183.0"]
        return run(["bash", self.scripts / "install-semgrep.sh", *arguments], env=merged)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_installs_from_the_pinned_release_with_hashes_and_nothing_resolved(self):
        output, status = self.install()
        self.assertEqual(status, 0, output)
        calls = self.calls()
        self.assertIn(
            "curl https://github.com/astral-sh/python-build-standalone/releases/download/20261013/%s" % FILE["x86_64"].replace("+", "%2B"),
            calls,
        )
        pip = [l for l in calls.split("\n") if " -m pip " in l and " install " in l]
        self.assertEqual(len(pip), 1)
        for flag in ("--require-hashes", "--only-binary :all:", "--no-deps", "--isolated", "--no-cache-dir", "-r %s/semgrep.lock" % self.scripts):
            self.assertIn(flag, pip[0])
        self.assertEqual(calls.count("curl "), 1, "nothing else is fetched by the script itself")
        self.assertIn("installed semgrep 1.183.0 on Python %s" % VERSION, output)

    def test_the_package_manager_the_standard_library_tests_and_the_headers_are_left_out(self):
        output, status = self.install()
        self.assertEqual(status, 0, output)
        python = self.prefix / "python"
        site = python / "lib/python3.13/site-packages"
        self.assertFalse((site / "pip").exists())
        self.assertEqual([p.name for p in site.glob("pip*")], [])
        for gone in ("include", "share", "lib/python3.13/test", "lib/python3.13/idlelib", "lib/python3.13/turtledemo", "lib/python3.13/ensurepip/_bundled"):
            self.assertFalse((python / gone).exists(), gone)
        for name in ("pip", "pip3", "pip3.13", "idle3", "idle3.13", "pydoc3", "pydoc3.13"):
            self.assertFalse((python / "bin" / name).exists(), name)
        for kept in ("bin/python3", "bin/python3.13", "bin/semgrep", "lib/python3.13/os.py", "lib/python3.13/ensurepip/__init__.py"):
            self.assertTrue((python / kept).exists(), kept)
        installed = sorted(p.name for p in site.glob("*.dist-info"))
        self.assertEqual(installed, ["click-8.4.2.dist-info", "ruamel_yaml-0.19.1.dist-info", "semgrep-1.183.0.dist-info"])

    def test_the_wrapper_is_a_small_script_that_runs_the_pinned_interpreter_isolated(self):
        output, status = self.install()
        self.assertEqual(status, 0, output)
        wrapper = self.bin / "semgrep"
        self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o755)
        text = wrapper.read_text()
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertIn("exec %s/python/bin/python3 -I %s/python/bin/semgrep \"$@\"" % (self.prefix, self.prefix), text)
        for needle in ("SEMGREP_SEND_METRICS=off", "SEMGREP_ENABLE_VERSION_CHECK=0", "PYTHONNOUSERSITE=1", "PYTHONSAFEPATH=1"):
            self.assertIn(needle, text)
        # the smoke run went through the wrapper, with those variables
        self.assertIn("SEMGREP_SEND_METRICS=off", self.calls())
        self.assertIn("PYTHONSAFEPATH=1", self.calls())

    def test_nothing_of_the_install_is_writable_by_group_or_other(self):
        output, status = self.install()
        self.assertEqual(status, 0, output)
        for path in [self.prefix, *self.prefix.rglob("*")]:
            if path.is_symlink():
                continue
            self.assertEqual(path.stat().st_mode & 0o022, 0, path)

    def test_smoke_runs_the_version_check_and_a_scan_of_a_file_of_its_own(self):
        output, status = self.install()
        self.assertEqual(status, 0, output)
        calls = self.calls()
        self.assertIn("semgrep --version", calls)
        scan = [l for l in calls.split("\n") if l.startswith("semgrep scan")][0]
        for flag in ("--config rule.yml", "--json", "--metrics=off", "--disable-version-check", "--oss-only", "--no-rewrite-rule-ids"):
            self.assertIn(flag, scan)

    def test_a_second_run_with_the_same_pins_changes_and_fetches_nothing(self):
        self.assertEqual(self.install()[1], 0)
        self.log.write_text("")
        output, status = self.install()
        self.assertEqual(status, 0, output)
        self.assertIn("already installed", output)
        self.assertEqual(self.calls(), "")

    def test_another_pin_over_an_existing_install_is_refused_not_mixed(self):
        self.assertEqual(self.install()[1], 0)
        with open(self.scripts / "python-standalone.lock", "a") as handle:
            handle.write("%s  cpython-3.13.17+20261021-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz\n" % ("a" * 64))
        output, status = self.install("--python", "3.13.17+20261021", "--semgrep", "1.183.0")
        self.assertEqual(status, 1)
        self.assertIn("is not the install of", output)

    def test_the_architecture_picks_its_own_file_and_digest(self):
        arm = standalone_tarball()
        self.serve(arm, arch="aarch64")
        output, status = self.install(env={"SEMGREP_FORCE_ARCH": "aarch64"})
        self.assertEqual(status, 0, output)
        self.assertIn(FILE["aarch64"].replace("+", "%2B"), self.calls())
        self.assertNotIn(FILE["x86_64"].replace("+", "%2B"), self.calls())

    def test_an_architecture_nothing_is_pinned_for_is_refused_before_anything_is_fetched(self):
        output, status = self.install(env={"SEMGREP_FORCE_ARCH": "riscv64"})
        self.assertEqual(status, 1)
        self.assertIn("no standalone Python is pinned for the architecture 'riscv64'", output)
        self.assertEqual(self.calls(), "")

    def test_a_download_with_another_digest_than_the_pinned_one_is_refused_and_unpacks_nothing(self):
        (self.downloads / FILE["x86_64"].replace("+", "%2B")).write_bytes(standalone_tarball([("python/bin/extra", b"evil")]))
        output, status = self.install()
        self.assertEqual(status, 1)
        self.assertIn("does not have the pinned sha256", output)
        self.assertFalse(self.prefix.exists())
        self.assertFalse((self.bin / "semgrep").exists())

    def test_a_tarball_that_reaches_outside_python_is_refused_even_with_the_pinned_digest(self):
        for name in ("etc/passwd", "python/../escape", "/abs/path", "pythonx/file"):
            with self.subTest(name=name):
                tarball = standalone_tarball()
                buffer = io.BytesIO()
                with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as source, tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                    for member in source.getmembers():
                        tar.addfile(member, source.extractfile(member) if member.isfile() else None)
                    info = tarfile.TarInfo(name)
                    info.size = 1
                    tar.addfile(info, io.BytesIO(b"x"))
                self.serve(buffer.getvalue())
                output, status = self.install()
                self.assertEqual(status, 1, output)
                self.assertRegex(output, r"outside python/|climbs out of python/")
                self.assertFalse(self.prefix.exists())

    def test_a_lock_that_does_not_pin_the_requested_release_is_refused_before_anything_is_fetched(self):
        output, status = self.install("--python", VERSION, "--semgrep", "1.200.0")
        self.assertEqual(status, 1)
        self.assertIn("semgrep.lock does not pin semgrep 1.200.0", output)
        self.assertIn("Bumping semgrep", output)
        self.assertEqual(self.calls(), "")

    def test_a_python_release_with_no_digest_is_refused_before_anything_is_fetched(self):
        output, status = self.install("--python", "3.13.17+20261021", "--semgrep", "1.183.0")
        self.assertEqual(status, 1)
        self.assertIn("python-standalone.lock has no sha256 for", output)
        self.assertEqual(self.calls(), "")

    def test_a_package_the_lock_does_not_name_is_refused(self):
        output, status = self.install(env={"FAKE_EXTRA_DIST": "surprise"})
        self.assertEqual(status, 1)
        self.assertIn("not in the lock: surprise==1", output)
        self.assertIn("what was installed is not what semgrep.lock pins", output)
        self.assertFalse((self.bin / "semgrep").exists())

    def test_the_wrong_interpreter_is_refused(self):
        output, status = self.install(env={"FAKE_PY_VERSION": "3.13.15"})
        self.assertEqual(status, 1)
        self.assertIn("the unpacked Python is 3.13.15, not 3.13.16", output)

    def test_a_failing_package_install_fails_the_build(self):
        output, status = self.install(env={"FAKE_PIP_STATUS": "1"})
        self.assertNotEqual(status, 0, output)
        self.assertFalse((self.bin / "semgrep").exists())

    def test_a_semgrep_of_another_release_than_the_pinned_one_fails_the_build(self):
        output, status = self.install(env={"FAKE_SEMGREP_VERSION": "1.182.0"})
        self.assertEqual(status, 1)
        self.assertIn("semgrep reports another version than 1.183.0", output)

    def test_a_semgrep_that_finds_nothing_in_the_smoke_file_fails_the_build(self):
        output, status = self.install(env={"FAKE_SMOKE": "none"})
        self.assertEqual(status, 1)
        self.assertIn("did not find the rule's match", output)

    def test_usage_errors(self):
        for args in (
            [],
            ["--python", VERSION],
            ["--semgrep", "1.183.0"],
            ["--python", "3.13.16", "--semgrep", "1.183.0"],
            ["--python", VERSION, "--semgrep", "latest"],
            ["--python", VERSION, "--semgrep", "1.183.0", "--extra"],
        ):
            with self.subTest(args=args):
                output, status = self.install(*args) if args else run(["bash", self.scripts / "install-semgrep.sh"])
                self.assertEqual(status, 2, output)
                self.assertIn("usage:" if "--extra" in args or len(args) < 4 else "takes", output)


if __name__ == "__main__":
    unittest.main()
