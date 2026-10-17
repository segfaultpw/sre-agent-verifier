"""verifier/images/python/fix-pip-vendored.sh, the script the python Dockerfile runs to raise
the libraries pip bundles. The real run needs GitHub, PyPI and a build, so it happens in the
image build; what is pinned here is every decision the script makes, run end to end against a
real git repository (a local fixture that stands in for pypa/pip through git's
`url.<base>.insteadOf`) and stand-ins for `python`, its venvs and the vendoring tool.

The stand-ins keep a state that behaves like the real one where the script looks at it: the
"installed" and "seeded" site-packages hold what the wheel they were given holds, the sync
rewrites the vendored directories and `bom.cdx.json` from `vendor.txt`, and the build packs
them. So the script's own checks (the pinned commit, the locks, the allowlist of changed
paths, the installed and seeded copies, the bom) are the ones that pass or refuse."""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import IMAGES, TempDirCase, requires_tools, run, write_executable

SCRIPT = IMAGES / "python" / "fix-pip-vendored.sh"

VENDOR_TXT = """# what pip bundles
CacheControl==0.14.4
msgpack==1.1.2
requests==2.34.2
    certifi==2026.6.17
    urllib3==2.7.0  # indented, with a comment
setuptools==70.3.0
tomli-w==1.2.0
"""

FIXED_VENDOR_TXT = """# what pip bundles
CacheControl==0.14.4
msgpack==1.2.1
requests==2.34.2
    certifi==2026.6.17
    urllib3==2.8.0  # indented, with a comment
setuptools==70.3.0
tomli-w==1.2.0
"""

HASH = "a" * 64

TOOLS_LOCK = """# pip-noxfile: vendoring~=1.4.0
vendoring==1.4.0 \\
    --hash=sha256:%s
""" % HASH

VENDORED_LOCK = "".join(
    "%s \\\n    --hash=sha256:%s\n" % (pin, HASH)
    for pin in [
        "CacheControl==0.14.4",
        "msgpack==1.2.1",
        "requests==2.34.2",
        "certifi==2026.6.17",
        "urllib3==2.8.0",
        "setuptools==70.3.0",
        "tomli-w==1.2.0",
    ]
)

# one file for the interpreter and its venvs, so `$0` says which one was called
PYTHON = r"""self=${BASH_SOURCE[0]}
version=${FAKE_PIP_VERSION:-26.2.1}
case "$1" in
  -m)
    module=$2
    shift 2
    case "$module" in
      pip)
        sub=$1
        shift
        case "$sub" in
          --version)
            echo "pip $version from /fake (python 3.14)"
            ;;
          debug)
            echo "vendored library versions:"
            sed -nE 's/^[[:space:]]*([A-Za-z0-9._-]+==[^[:space:]#]+).*/  \1/p' "$FAKE_PURELIB/pip/_vendor/vendor.txt"
            [ -z "${FAKE_CONFLICT:-}" ] || echo "  urllib3==2.8.0 (CONFLICT: vendor.txt suggests version should be 2.7.0)"
            echo
            ;;
          install)
            echo "install[$0] $*" >>"$FAKE_LOG"
            if [[ " $* " == *" --force-reinstall "* ]]; then
              wheel=${!#}
              if [[ " $* " == *" --find-links dist "* ]]; then wheel=dist/pip-$version-py3-none-any.whl; fi
              tar -xzf "$wheel" -C "$FAKE_PURELIB"
            fi
            ;;
          download)
            echo "download[$0] $*" >>"$FAKE_LOG"
            dest=
            requirements=
            no_binary=
            while [ "$#" -gt 0 ]; do
              case "$1" in
                --dest) dest=$2; shift 2 ;;
                -r) requirements=$2; shift 2 ;;
                --no-binary) no_binary="$no_binary $2"; shift 2 ;;
                *) shift ;;
              esac
            done
            mkdir -p "$dest"
            while IFS= read -r line; do
              if [[ "$line" =~ ^([A-Za-z0-9._-]+)==([^[:space:]\\]+) ]]; then
                name=${BASH_REMATCH[1]}
                pinned=${BASH_REMATCH[2]}
                if [[ " $no_binary " == *" $name "* ]]; then
                  : >"$dest/$name-$pinned.tar.gz"
                else
                  : >"$dest/${name//-/_}-$pinned-py3-none-any.whl"
                fi
              fi
            done <"$requirements"
            ;;
          wheel)
            echo "wheel[$0] $*" >>"$FAKE_LOG"
            sdist=${!#}
            dest=
            while [ "$#" -gt 0 ]; do
              case "$1" in
                --wheel-dir) dest=$2; shift 2 ;;
                *) shift ;;
              esac
            done
            base=${sdist##*/}
            base=${base%.tar.gz}
            if [ -n "${FAKE_BUILT_BINARY:-}" ]; then
              : >"$dest/${base%-*}-${base##*-}-cp314-cp314-linux_x86_64.whl"
            else
              : >"$dest/${base%-*}-${base##*-}-py3-none-any.whl"
            fi
            ;;
        esac
        ;;
      venv)
        dir=$1
        mkdir -p "$dir/bin"
        cp "$self" "$dir/bin/python"
        cp "$FAKE_VENDORING" "$dir/bin/vendoring"
        printf '#!/usr/bin/env bash\nexec "%s/bin/python" -m pip "$@"\n' "$dir" >"$dir/bin/pip"
        chmod +x "$dir/bin/python" "$dir/bin/vendoring" "$dir/bin/pip"
        case "$dir" in
          */seeded) tar -xzf "${FAKE_SEED_WHEEL:-$FAKE_BUNDLED/pip-$version-py3-none-any.whl}" -C "$FAKE_SEEDED_PURELIB" ;;
        esac
        ;;
    esac
    ;;
  -c)
    case "$2" in
      *sysconfig*)
        case "$0" in
          */seeded/*) echo "$FAKE_SEEDED_PURELIB" ;;
          *) echo "$FAKE_PURELIB" ;;
        esac
        ;;
      *ensurepip*) echo "$FAKE_BUNDLED" ;;
    esac
    ;;
  build-project/build-project.py)
    echo "build[$PWD]" >>"$FAKE_LOG"
    stage=$(mktemp -d)
    mkdir -p "$stage/pip/_vendor" dist
    if [ -n "${FAKE_BUILD_STALE:-}" ]; then
      git show HEAD:src/pip/_vendor/vendor.txt >"$stage/pip/_vendor/vendor.txt"
    else
      cp src/pip/_vendor/vendor.txt "$stage/pip/_vendor/vendor.txt"
    fi
    if [ -n "${FAKE_BUILD_NO_PURL:-}" ]; then
      echo '{}' >"$stage/pip/_vendor/bom.cdx.json"
    else
      cp src/pip/_vendor/bom.cdx.json "$stage/pip/_vendor/bom.cdx.json"
    fi
    tar -czf "dist/pip-$version-py3-none-any.whl" -C "$stage" pip
    rm -rf "$stage"
    ;;
esac
"""

# what `vendoring sync` owns: every vendored directory and the bom, from vendor.txt
VENDORING = r"""echo "vendoring $* NO_INDEX=${PIP_NO_INDEX:-} PROXY=${HTTPS_PROXY:-} HEAD=$(git rev-parse HEAD) WHEELHOUSE=$(ls "$PIP_FIND_LINKS" | sort | tr '\n' ' ')" >>"$FAKE_LOG"
vendor=src/pip/_vendor
# the real tool leaves its downloaded wheels in the temporary directory
mktemp "${TMPDIR:-/tmp}/vendoring-downloads.XXXXXX" >/dev/null
: >"$vendor/bom.cdx.json"
while IFS= read -r line; do
  if [[ "$line" =~ ^[[:space:]]*([A-Za-z0-9._-]+)==([^[:space:]#]+) ]]; then
    name=${BASH_REMATCH[1]}
    pinned=${BASH_REMATCH[2]}
    dir=${name,,}
    dir=${dir//-/_}
    mkdir -p "$vendor/$dir"
    printf '%s %s\n' "$dir" "$pinned" >"$vendor/$dir/__init__.py"
    printf '{"purl": "pkg:pypi/%s@%s"}\n' "$name" "$pinned" >>"$vendor/bom.cdx.json"
  fi
done <"$vendor/vendor.txt"
[ -z "${FAKE_SYNC_TOUCH:-}" ] || echo changed >>"$FAKE_SYNC_TOUCH"
[ -z "${FAKE_SYNC_NEW:-}" ] || echo stray >"$FAKE_SYNC_NEW"
if [ -n "${FAKE_SYNC_IGNORED:-}" ]; then mkdir -p "$(dirname "$FAKE_SYNC_IGNORED")"; echo stray >"$FAKE_SYNC_IGNORED"; fi
"""

PIN = re.compile(r"^\s*([A-Za-z0-9._-]+)==([^\s#]+)")


def git(cwd, *args):
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def pins(vendor_txt):
    return [m.groups() for m in map(PIN.match, vendor_txt.split("\n")) if m]


def vendored_dir(name):
    return name.lower().replace("-", "_")


def write_source(repo, version, vendor_txt):
    """A pypa/pip stand-in at one commit: the files the script reads, with the vendored
    directories and bom that `vendoring sync` would leave for that vendor.txt."""
    (repo / "src/pip/_vendor").mkdir(parents=True, exist_ok=True)
    (repo / "src/pip/_internal").mkdir(parents=True, exist_ok=True)
    (repo / "build-project").mkdir(parents=True, exist_ok=True)
    (repo / "src/pip/__init__.py").write_text('__version__ = "%s"\n' % version)
    (repo / "src/pip/_internal/x.py").write_text("internal\n")
    (repo / "src/pip/_vendor/vendor.txt").write_text(vendor_txt)
    (repo / "noxfile.py").write_text('    session.install("vendoring~=1.4.0")\n')
    (repo / "build-project/build-project.py").write_text("# stand-in\n")
    # pip's own .gitignore (at the pinned commit) ignores these, and the wheel packs them anyway
    (repo / ".gitignore").write_text("*.egg-info/\n__pycache__/\n.*cache\n*~\n")

    bom = []
    for name, v in pins(vendor_txt):
        directory = repo / "src/pip/_vendor" / vendored_dir(name)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("%s %s\n" % (vendored_dir(name), v))
        bom.append('{"purl": "pkg:pypi/%s@%s"}\n' % (name, v))
    (repo / "src/pip/_vendor/bom.cdx.json").write_text("".join(bom))


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def wheel_file(wheel, member):
    return subprocess.run(
        ["tar", "-xzOf", str(wheel), member], stdout=subprocess.PIPE, check=True, text=True
    ).stdout


@requires_tools("bash", "git", "tar", "sort", "sed", "awk", "diff", "cmp", "mktemp")
class FixPipVendoredTest(TempDirCase):
    def fixture(self, installed=VENDOR_TXT, lock=VENDORED_LOCK, tools_lock=TOOLS_LOCK):
        """Everything one run needs, under the test's directory. `installed` is the vendor.txt
        of the installed pip, `lock` and `tools_lock` the locks next to the script."""
        d = self.dir
        repo = d / "source"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
        write_source(repo, "26.2.0", VENDOR_TXT)
        older = commit(repo, "26.2.0")
        write_source(repo, "26.2.1", VENDOR_TXT)
        sha = commit(repo, "26.2.1")

        purelib = d / "site"
        seeded_purelib = d / "seeded-site"
        bundled = d / "bundled"
        (purelib / "pip/_vendor").mkdir(parents=True)
        seeded_purelib.mkdir()
        bundled.mkdir()
        (purelib / "pip/_vendor/vendor.txt").write_text(installed)
        shutil.copy(repo / "src/pip/_vendor/bom.cdx.json", purelib / "pip/_vendor/bom.cdx.json")

        old_wheel = bundled / "pip-26.2.1-py3-none-any.whl"
        subprocess.run(["tar", "-czf", str(old_wheel), "-C", str(purelib), "pip"], check=True)
        kept_old_wheel = d / "old.whl"
        shutil.copy(old_wheel, kept_old_wheel)

        # the script is run from a copy next to the locks it reads
        pipfix = d / "pipfix"
        pipfix.mkdir()
        shutil.copy(SCRIPT, pipfix / "fix-pip-vendored.sh")
        (pipfix / "pip-fix-tools.lock").write_text(tools_lock)
        (pipfix / "pip-vendored.lock").write_text(lock)

        write_executable(d / "bin/python", PYTHON)
        write_executable(d / "vendoring", VENDORING)

        tmp = d / "tmp"
        tmp.mkdir()

        return SimpleNamespace(
            dir=d,
            sha=sha,
            older=older,
            purelib=purelib,
            seeded_purelib=seeded_purelib,
            bundled=bundled / "pip-26.2.1-py3-none-any.whl",
            old_wheel=kept_old_wheel,
            tmp=tmp,
            script=pipfix / "fix-pip-vendored.sh",
            pipfix=pipfix,
            log=d / "calls.log",
            args=[
                "--pip-source",
                "26.2.1=%s" % sha,
                "--min",
                "urllib3==2.8.0",
                "--min",
                "msgpack==1.2.1",
                "--build-from-sdist",
                "msgpack",
            ],
            env={
                "PATH": "%s:%s" % (d / "bin", os.environ["PATH"]),
                "TMPDIR": str(tmp),
                "FAKE_LOG": str(d / "calls.log"),
                "FAKE_PURELIB": str(purelib),
                "FAKE_SEEDED_PURELIB": str(seeded_purelib),
                "FAKE_BUNDLED": str(bundled),
                "FAKE_VENDORING": str(d / "vendoring"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.file://%s.insteadOf" % repo,
                "GIT_CONFIG_VALUE_0": "https://github.com/pypa/pip",
            },
        )

    def run_script(self, fx, args=None, env=None):
        return run(
            ["bash", fx.script] + (fx.args if args is None else args),
            env={**fx.env, **(env or {})},
            cwd=fx.dir,
        )

    def log(self, fx):
        return fx.log.read_text() if fx.log.exists() else ""

    def stub_git_that_fails(self):
        write_executable(self.dir / "bin/git", 'echo "git $*" >>"$FAKE_LOG"\nexit 99\n')

    # a run that has something to raise

    def test_fetches_the_pinned_commit_uses_only_locked_inputs_and_replaces_the_installed_and_the_bundled_pip(self):
        fx = self.fixture()
        output, status = self.run_script(fx)
        self.assertEqual(status, 0, output)

        self.assertIn("raising msgpack 1.1.2 -> 1.2.1", output)
        self.assertIn("raising urllib3 2.7.0 -> 2.8.0", output)

        calls = self.log(fx)
        pipfix = re.escape(str(fx.pipfix))

        # the build tools come from the locked, hashed file and nothing else
        self.assertRegex(
            calls,
            r"install\[\S+/venv/bin/python\] --quiet --require-hashes --only-binary :all: --no-deps -r %s/pip-fix-tools.lock"
            % pipfix,
        )

        # the libraries are downloaded by hash, msgpack as the sdist it is built from
        self.assertRegex(
            calls,
            r"download\[\S+/venv/bin/python\] --quiet --require-hashes --no-deps --only-binary :all: --no-binary msgpack --no-build-isolation --dest \S+/wheelhouse -r %s/pip-vendored.lock"
            % pipfix,
        )
        self.assertRegex(calls, r"wheel\[\S+/venv/bin/python\] .*--no-index .*msgpack-1.2.1.tar.gz")

        # the sync is run on the pinned commit, offline, against the wheelhouse alone
        sync = re.search(r"(?m)^vendoring sync .*$", calls).group(0)
        self.assertIn("NO_INDEX=1", sync)
        self.assertIn("PROXY=http://127.0.0.1:9", sync)
        self.assertIn("HEAD=%s" % fx.sha, sync)
        self.assertIn("msgpack-1.2.1-py3-none-any.whl", sync)
        self.assertNotIn("tar.gz", sync)
        self.assertIn("urllib3-2.8.0-py3-none-any.whl", sync)

        # the smoke download is checked by hash too
        self.assertRegex(
            calls,
            r"download\[\S+/bin/python\] --quiet --no-deps --require-hashes --only-binary :all: .*smoke\.txt",
        )

        # the wheel is installed by name from the directory it was built in, so pip records no
        # direct URL (a path install writes direct_url.json and `pip freeze` prints `pip @ file:///...`)
        self.assertRegex(
            calls,
            r"install\[\S+/bin/python\] --quiet --no-index --no-deps --force-reinstall --find-links dist pip==26\.2\.1\n",
        )

        # the installed pip and the wheel a venv is seeded from carry the floors
        self.assertEqual((fx.purelib / "pip/_vendor/vendor.txt").read_text(), FIXED_VENDOR_TXT)
        self.assertEqual(wheel_file(fx.bundled, "pip/_vendor/vendor.txt"), FIXED_VENDOR_TXT)
        self.assertIn("pkg:pypi/urllib3@2.8.0", (fx.purelib / "pip/_vendor/bom.cdx.json").read_text())

        # nothing a tool wrote to a temporary file outlives the step
        self.assertEqual(os.listdir(fx.tmp), [])

    def test_raises_names_however_vendor_txt_spells_them_and_numbers_by_value(self):
        installed = "CacheControl==0.14.4\nmsgpack==1.1.2\ntomli-w==1.2.0\n    urllib3==2.10.0\n"
        fx = self.fixture(installed=installed)

        output, _ = self.run_script(
            fx,
            fx.args
            + ["--min", "cachecontrol==0.14.5", "--min", "tomli_w==1.3.0", "--min", "urllib3==2.9.0"],
        )

        self.assertIn("raising CacheControl 0.14.4 -> 0.14.5", output)
        self.assertIn("raising tomli-w 1.2.0 -> 1.3.0", output)
        self.assertIn("raising msgpack 1.1.2 -> 1.2.1", output)
        # 2.10.0 is above 2.9.0 (and above the 2.8.0 given first): left as it is
        self.assertNotIn("raising urllib3", output)

    # which carried versions are below a floor

    def test_a_pre_release_or_dev_release_of_the_floor_is_below_it_a_post_release_or_build_is_not(self):
        installed = (
            "CacheControl==0.14.5rc1\n"
            "msgpack==1.2.1.post1\n"
            "tomli-w==1.3.0.dev0\n"
            "urllib3==2.8.0-1\n"
            "requests==2.35.0a2\n"
            "packaging==26.3+local\n"
        )
        fx = self.fixture(installed=installed)

        output, _ = self.run_script(
            fx,
            fx.args
            + [
                "--min", "cachecontrol==0.14.5",
                "--min", "tomli-w==1.3.0",
                "--min", "requests==2.35.0",
                "--min", "packaging==26.3",
            ],
        )

        self.assertIn("raising CacheControl 0.14.5rc1 -> 0.14.5", output)
        self.assertIn("raising tomli-w 1.3.0.dev0 -> 1.3.0", output)
        self.assertIn("raising requests 2.35.0a2 -> 2.35.0", output)
        self.assertNotIn("raising msgpack", output)
        self.assertNotIn("raising urllib3", output)
        self.assertNotIn("raising packaging", output)

    # a run with nothing to raise

    def test_does_nothing_fetches_nothing_and_needs_no_pinned_source(self):
        installed = "CacheControl==0.14.4\nmsgpack==1.10.0\n    urllib3==2.8.0\n"
        fx = self.fixture(installed=installed)
        # git that fails when called: a no-op must not reach for the network
        self.stub_git_that_fails()

        output, status = self.run_script(fx, ["--min", "msgpack==1.9.0", "--min", "urllib3==2.8.0"])
        self.assertEqual(status, 0, output)

        self.assertIn("pip 26.2.1 already carries every requested version", output)
        self.assertNotIn("git", self.log(fx))
        self.assertNotIn("python -m venv", self.log(fx))
        self.assertEqual(os.listdir(fx.tmp), [])

    # the pinned source

    def test_refuses_a_pip_that_has_no_pinned_commit_before_fetching_anything(self):
        fx = self.fixture()
        self.stub_git_that_fails()

        output, status = self.run_script(fx, ["--min", "urllib3==2.8.0"])
        self.assertEqual(status, 1)
        self.assertIn("pip 26.2.1 has no pinned source", output)

        output, status = self.run_script(fx, ["--pip-source", "26.1.0=%s" % fx.sha, "--min", "urllib3==2.8.0"])
        self.assertEqual(status, 1)
        self.assertIn("pip 26.2.1 has no pinned source", output)

        self.assertNotIn("git", self.log(fx))

    def test_refuses_a_commit_that_is_not_the_release_of_the_installed_pip(self):
        fx = self.fixture()

        output, status = self.run_script(fx, ["--pip-source", "26.2.1=%s" % fx.older, "--min", "urllib3==2.8.0"])

        self.assertEqual(status, 1)
        self.assertIn("is not pip 26.2.1", output)
        self.assertNotIn("vendoring", self.log(fx))

    def test_refuses_a_checkout_that_is_not_the_pinned_commit_whatever_the_fetch_said(self):
        fx = self.fixture()
        git_path = shutil.which("git")
        write_executable(
            self.dir / "bin/git",
            'if [ "$1 $2" = "rev-parse HEAD" ]; then echo %s; exit 0; fi\nexec %s "$@"\n' % ("0" * 40, git_path),
        )

        output, status = self.run_script(fx)

        self.assertEqual(status, 1)
        self.assertIn("the fetched commit is not %s" % fx.sha, output)
        self.assertNotIn("vendoring", self.log(fx))

    def test_refuses_a_commit_the_repository_does_not_have(self):
        fx = self.fixture()

        _, status = self.run_script(fx, ["--pip-source", "26.2.1=%s" % ("1" * 40), "--min", "urllib3==2.8.0"])

        self.assertNotEqual(status, 0)
        self.assertNotIn("vendoring", self.log(fx))

    def test_refuses_a_source_whose_vendor_txt_is_not_the_installed_pips(self):
        fx = self.fixture(installed=VENDOR_TXT + "extra==1.0\n")

        output, status = self.run_script(fx)

        self.assertEqual(status, 1)
        self.assertIn("does not carry the vendor.txt of the installed pip 26.2.1", output)
        self.assertNotIn("download[", self.log(fx))

    # the locks

    def test_refuses_a_vendored_lock_that_does_not_pin_what_the_edited_vendor_txt_names(self):
        fx = self.fixture(lock=VENDORED_LOCK.replace("urllib3==2.8.0", "urllib3==2.7.0"))

        output, status = self.run_script(fx)

        self.assertEqual(status, 1)
        self.assertIn("pip-vendored.lock does not pin what the edited vendor.txt names", output)
        self.assertIn("urllib3==2.7.0", output)
        self.assertNotIn("download[", self.log(fx))

    def test_refuses_a_vendored_lock_with_a_library_the_vendor_txt_does_not_have(self):
        fx = self.fixture(lock=VENDORED_LOCK + "extra==1.0 \\\n    --hash=sha256:%s\n" % HASH)

        _, status = self.run_script(fx)

        self.assertEqual(status, 1)
        self.assertNotIn("download[", self.log(fx))

    def test_refuses_a_tools_lock_made_for_another_vendoring_requirement(self):
        fx = self.fixture(tools_lock=TOOLS_LOCK.replace("~=1.4.0", "~=1.5.0"))

        output, status = self.run_script(fx)

        self.assertEqual(status, 1)
        self.assertIn(
            "pip-fix-tools.lock was made for another vendoring requirement than vendoring~=1.4.0", output
        )
        self.assertNotIn("install[", self.log(fx))

    # what the build refuses

    def test_refuses_a_sync_that_changed_a_file_it_was_not_asked_about(self):
        fx = self.fixture()

        output, status = self.run_script(fx, env={"FAKE_SYNC_TOUCH": "src/pip/_internal/x.py"})

        self.assertEqual(status, 1)
        self.assertIn("the vendoring sync changed more than was asked for:", output)
        self.assertIn("src/pip/_internal/x.py", output)
        self.assertNotIn("src/pip/_vendor/urllib3", output)
        self.assertNotIn("build[", self.log(fx))

    def test_refuses_a_sync_that_left_a_new_file_in_the_vendored_tree(self):
        fx = self.fixture()

        output, status = self.run_script(fx, env={"FAKE_SYNC_NEW": "src/pip/_vendor/stray.txt"})

        self.assertEqual(status, 1)
        self.assertIn("src/pip/_vendor/stray.txt", output)
        self.assertNotIn("build[", self.log(fx))

    def test_a_floor_for_a_library_pip_does_not_bundle_stops_the_build_instead_of_passing_silently(self):
        fx = self.fixture()

        output, status = self.run_script(fx, fx.args + ["--min", "ghost==1.0"])

        self.assertEqual(status, 1)
        self.assertIn("ghost has no directory src/pip/_vendor/ghost", output)
        self.assertNotIn("build[", self.log(fx))

    def test_a_file_the_sync_left_behind_that_pips_gitignore_hides_is_refused_too(self):
        fx = self.fixture()

        # *.egg-info/ is ignored by git, but flit-core packs everything under src/pip into the
        # wheel except __pycache__ and *.pyc, so only the allowlist's --ignored stops it
        output, status = self.run_script(
            fx, env={"FAKE_SYNC_IGNORED": "src/pip/_vendor/requests.egg-info/PKG-INFO"}
        )

        self.assertEqual(status, 1)
        self.assertIn("the vendoring sync changed more than was asked for:", output)
        self.assertIn("src/pip/_vendor/requests.egg-info/", output)
        self.assertNotIn("build[", self.log(fx))

    def test_a_library_built_from_its_sdist_that_is_not_pure_python(self):
        fx = self.fixture()

        output, status = self.run_script(fx, env={"FAKE_BUILT_BINARY": "1"})

        self.assertEqual(status, 1)
        self.assertIn("msgpack did not build to a pure-Python wheel", output)
        self.assertNotIn("vendoring sync", self.log(fx))

    # what the result is checked for

    def test_a_built_wheel_that_still_carries_a_version_below_the_floor(self):
        fx = self.fixture()

        output, status = self.run_script(fx, env={"FAKE_BUILD_STALE": "1"})

        self.assertEqual(status, 1)
        self.assertIn("%s/pip carries msgpack==1.1.2, below 1.2.1" % fx.purelib, output)

    def test_a_bom_that_does_not_list_a_raised_library(self):
        fx = self.fixture()

        output, status = self.run_script(fx, env={"FAKE_BUILD_NO_PURL": "1"})

        self.assertEqual(status, 1)
        self.assertIn("bom.cdx.json does not list msgpack==1.2.1", output)

    def test_a_virtual_environment_seeded_from_a_wheel_that_was_not_replaced(self):
        fx = self.fixture()

        # the installed copy is right, the wheel a venv is seeded from is the old one
        output, status = self.run_script(fx, env={"FAKE_SEED_WHEEL": str(fx.old_wheel)})

        self.assertEqual(status, 1)
        self.assertIn("seeded-site/pip carries msgpack==1.1.2, below 1.2.1", output)

    def test_vendored_libraries_that_do_not_match_vendor_txt_as_pip_itself_reports_it(self):
        fx = self.fixture()

        output, status = self.run_script(fx, env={"FAKE_CONFLICT": "1"})

        self.assertEqual(status, 1)
        self.assertIn("pip's vendored libraries do not match its vendor.txt", output)

    # arguments

    def test_a_missing_or_malformed_request_is_refused(self):
        fx = self.fixture()

        output, status = self.run_script(fx, [])
        self.assertEqual(status, 2)
        self.assertIn("nothing to do", output)

        output, status = self.run_script(fx, ["--pip-source", "26.2.1=%s" % fx.sha])
        self.assertEqual(status, 2)
        self.assertIn("nothing to do", output)

        for bad in ["urllib3", "urllib3=2.8.0", "urllib3==", "urllib3==^2.8"]:
            with self.subTest(min=bad):
                output, status = self.run_script(fx, ["--min", bad])
                self.assertEqual(status, 2)
                self.assertIn("--min takes NAME==VERSION", output)

        for bad in [
            "26.2.1",
            "26.2.1=abc",
            "26.2.1=%s" % fx.sha.upper(),
            "=%s" % fx.sha,
            "26.2.1=%s0" % fx.sha,
        ]:
            with self.subTest(pip_source=bad):
                output, status = self.run_script(fx, ["--pip-source", bad, "--min", "urllib3==2.8.0"])
                self.assertEqual(status, 2)
                self.assertIn("--pip-source takes VERSION=COMMIT", output)

        for bad in ["", "a b", "msg/pack"]:
            with self.subTest(sdist=bad):
                output, status = self.run_script(fx, ["--build-from-sdist", bad, "--min", "urllib3==2.8.0"])
                self.assertEqual(status, 2)
                self.assertIn("--build-from-sdist takes NAME", output)

        output, status = self.run_script(fx, ["--drop", "setuptools=pkg_resources"])
        self.assertEqual(status, 2)
        self.assertIn("usage:", output)


if __name__ == "__main__":
    unittest.main()
