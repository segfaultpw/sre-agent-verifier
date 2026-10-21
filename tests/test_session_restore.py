"""What a session job can and cannot carry into the next job.

The per-job restore is the boundary between two jobs of one session: whatever the first job did
to the tree, to git's own state, to the unprivileged user's home and to the temporary
directories, the second starts from the base commit, the job's own files, the paths setup left
ignored, and the files setup left in the home and temporary directories. Each test below is a
bypass of that boundary (the review's probes and the variants of them) and fails without the
matching part of `restore_tree` in verifier/run.sh. Standard library only."""

import unittest

from session_support import SessionCase, git, job, runner_tools, step

SETUP = step(
    0,
    "deps",
    "setup",
    'mkdir -p cache "$HOME/.cache" && echo run >> cache/setup-runs'
    ' && echo dep >"$HOME/.cache/dep" && echo keep >"$HOME/.cache/keep"',
)
UNIT = step(1, "unit", "test", "sh test_old.sh")
LINT = step(2, "lint", "check", "echo lint")
BASE = [SETUP, UNIT, LINT]

FORGE = """\
import glob, os, subprocess, sys, zlib

base = sys.argv[1]
run = lambda *a, **k: subprocess.run(a, check=True, capture_output=True, **k).stdout
tree = run("git", "rev-parse", base + "^{tree}", text=True).strip()
evil = run("git", "hash-object", "-w", "--stdin", input="evil\\n", text=True).strip()
packs = glob.glob(".git/objects/pack/*.pack")
data = [open(p, "rb").read() for p in packs]
for path in glob.glob(".git/objects/pack/*"):
    os.remove(path)
for blob in data:
    subprocess.run(["git", "unpack-objects", "-q"], input=blob, check=True, capture_output=True)
raw = run("git", "cat-file", "tree", tree)
out, i = b"", 0
while i < len(raw):
    nul = raw.index(b"\\0", i)
    head = raw[i:nul]
    sha = raw[nul + 1 : nul + 21]
    if head.endswith(b" README"):
        sha = bytes.fromhex(evil)
    out += head + b"\\0" + sha
    i = nul + 21
path = ".git/objects/%s/%s" % (tree[:2], tree[2:])
os.chmod(os.path.dirname(path), 0o755)
os.remove(path)
with open(path, "wb") as handle:
    handle.write(zlib.compress(b"tree %d\\0" % len(out) + out))
"""


@runner_tools
class RestoreTest(SessionCase):
    def two_jobs(self, first, second, steps=None, files=(), **extra):
        scenario = self.session_scenario(
            (steps or BASE) + [first, second], [job(1, [first], files), job(2, [second])], **extra
        )
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertAllPass(self.ready())
        self.assertAllPass(self.result(1))
        return platform

    def second_job_passes(self, second):
        result = self.result(2)
        self.assertEqual(self.status_of(result, second["name"]), ["pass"], result["steps"])

    def test_a_job_made_ignore_rule_protects_nothing(self):
        plant = step(
            3,
            "plant",
            "check",
            "mkdir -p planted deep/er && printf '*\\n' > planted/.gitignore && echo x > planted/calc.sh"
            " && echo '*' >> .gitignore && echo y > outside.txt"
            " && printf '*\\n!.gitignore\\n' > deep/er/.gitignore && echo z > deep/er/f",
        )
        verify = step(
            4,
            "verify",
            "check",
            "! test -e planted && ! test -e outside.txt && ! test -e deep"
            ' && test -z "$(git status --porcelain --untracked-files=all)"'
            " && ! grep -q '^[*]$' .gitignore && test -e cache/setup-runs",
        )
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_replace_refs_do_not_change_the_restored_tree(self):
        plant = step(
            3,
            "plant",
            "check",
            "echo evil > README && git add README && git -c user.name=t -c user.email=t@t commit -q -m evil"
            " && git replace %s HEAD" % self.sha,
        )
        verify = step(4, "verify", "check", 'test "$(cat README)" = original && ! test -e .git/refs/replace')
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_a_forged_object_store_does_not_change_the_restored_tree(self):
        forge = step(3, "forge", "check", "python3 forge.py %s" % self.sha)
        verify = step(
            4,
            "verify",
            "check",
            'test "$(cat README)" = original && test "$(git rev-parse HEAD)" = %s' % self.sha,
        )
        self.two_jobs(forge, verify, files=[("forge.py", FORGE, False)])
        self.second_job_passes(verify)

    def test_alternates_grafts_and_a_redirected_git_directory_are_gone(self):
        plant = step(
            3,
            "plant",
            "check",
            'mkdir -p "$HOME/alt" && echo "$HOME/alt" > .git/objects/info/alternates'
            " && echo %s > .git/info/grafts && echo ../elsewhere > .git/commondir" % self.sha,
        )
        verify = step(
            4,
            "verify",
            "check",
            "! test -e .git/objects/info/alternates && ! test -e .git/info/grafts && ! test -e .git/commondir"
            ' && test "$(cat README)" = original',
        )
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_a_job_that_replaces_git_with_a_file_or_locks_it_is_restored(self):
        plant = step(
            3,
            "plant",
            "check",
            "chmod 000 .git/objects .git/refs && echo tampered > README",
        )
        verify = step(4, "verify", "check", 'test "$(cat README)" = original && git rev-parse HEAD >/dev/null')
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_a_job_made_gitattributes_changes_no_restored_file(self):
        (self.origin / ".gitattributes").write_text("# nothing here\n")
        git(self.origin, "add", ".gitattributes")
        git(self.origin, "commit", "-q", "-m", "attributes")
        self.sha = git(self.origin, "rev-parse", "HEAD")
        plant = step(
            3,
            "plant",
            "check",
            "printf '* text eol=crlf\\n' > .gitattributes && printf 'README text eol=crlf\\n' > .git/info/attributes"
            " && echo tampered > README && mkdir sub && printf '* text eol=crlf\\n' > sub/.gitattributes",
        )
        verify = step(
            4,
            "verify",
            "check",
            'test "$(cat README)" = original && ! grep -q "$(printf \'\\r\')" README'
            ' && test "$(cat .gitattributes)" = "# nothing here"'
            " && ! test -e sub/.gitattributes && ! test -e .git/info/attributes",
        )
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_home_and_temporary_files_a_job_adds_or_changes_do_not_reach_the_next_job(self):
        plant = step(
            3,
            "plant",
            "check",
            'echo evil > "$HOME/.npmrc" && printf "[user]\\nname = x\\n" > "$HOME/.gitconfig"'
            ' && mkdir -p "$TMPDIR/leak" "$HOME/newdir/sub" && echo x > "$TMPDIR/leak/f" && echo y > "$HOME/newdir/sub/g"'
            ' && echo more >> "$HOME/.cache/dep" && test -n "$TMPDIR"',
        )
        verify = step(
            4,
            "verify",
            "check",
            'test -n "$TMPDIR" && ! test -e "$HOME/.npmrc" && ! test -e "$HOME/.gitconfig" && ! test -e "$TMPDIR/leak"'
            ' && ! test -e "$HOME/newdir" && ! test -e "$HOME/.cache/dep" && test "$(cat "$HOME/.cache/keep")" = keep',
        )
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_what_setup_left_in_the_home_survives_every_job(self):
        read = step(3, "read", "check", 'test "$(cat "$HOME/.cache/dep")" = dep && test "$(cat "$HOME/.cache/keep")" = keep')
        scenario = self.session_scenario(BASE + [read], [job(1, [read]), job(2, [read]), job(3, [read])])
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        for n in (1, 2, 3):
            self.assertAllPass(self.result(n))

    def test_setup_caches_persist_with_what_a_job_writes_inside_them_and_nothing_else_does(self):
        setup = step(
            0,
            "deps",
            "setup",
            "mkdir -p cache 'cache/we*ird' && echo run >> cache/setup-runs"
            " && printf '*.log\\nweird*\\n\\\\#*\\n\\\\!*\\n' >> .git/info/exclude"
            " && echo l > build.log && mkdir -p 'weird*dir' && echo w > 'weird*dir/x'"
            " && echo h > '#cache' && echo b > '!bang' && echo untracked_not_ignored > stray.txt",
        )
        plant = step(
            3,
            "plant",
            "check",
            "echo c > cache/job1 && echo l2 > other.log && mkdir -p weirdQdir && echo q > weirdQdir/f"
            " && echo x > weirdA && echo y > '#other' && echo z > '!other' && echo '*' >> .git/info/exclude",
        )
        verify = step(
            4,
            "verify",
            "check",
            "test -e cache/job1 && test -e cache/setup-runs && test -e build.log && test -e 'weird*dir/x'"
            " && test -e '#cache' && test -e '!bang'"
            " && ! test -e other.log && ! test -e weirdQdir && ! test -e weirdA && ! test -e '#other' && ! test -e '!other'"
            " && ! test -e stray.txt",
        )
        scenario = self.session_scenario(
            [setup, UNIT, LINT, plant, verify], [job(1, [plant]), job(2, [verify])]
        )
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))
        self.second_job_passes(verify)

    def test_a_symlink_where_a_cache_directory_was_is_not_kept(self):
        plant = step(3, "plant", "check", 'rm -rf cache && ln -s "$HOME" cache')
        verify = step(4, "verify", "check", '! test -L cache && ! test -e cache/.cache && ! test -e cache/setup-runs')
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_a_job_that_locks_a_directory_in_the_tree_or_home_is_still_restored(self):
        plant = step(
            3,
            "plant",
            "check",
            'mkdir -p locked/in "$HOME/locked/in" && echo x > locked/in/f && echo x > "$HOME/locked/in/f"'
            ' && chmod 000 locked/in locked "$HOME/locked/in" "$HOME/locked"',
        )
        verify = step(4, "verify", "check", '! test -e locked && ! test -e "$HOME/locked"')
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_a_tracked_directory_a_job_locked_is_restored(self):
        (self.origin / "src").mkdir()
        (self.origin / "src" / "a.txt").write_text("tracked\n")
        git(self.origin, "add", "src")
        git(self.origin, "commit", "-q", "-m", "src")
        self.sha = git(self.origin, "rev-parse", "HEAD")
        plant = step(3, "plant", "check", "echo changed > src/a.txt && chmod 000 src/a.txt src")
        verify = step(4, "verify", "check", 'test "$(cat src/a.txt)" = tracked')
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)

    def test_a_tracked_file_made_unwritable_or_replaced_by_a_directory_is_restored(self):
        plant = step(3, "plant", "check", "rm -f calc.sh && mkdir calc.sh && echo x > calc.sh/f && chmod 000 README")
        verify = step(4, "verify", "check", 'test -f calc.sh && test "$(cat README)" = original && ! test -e calc.sh/f')
        self.two_jobs(plant, verify)
        self.second_job_passes(verify)


if __name__ == "__main__":
    unittest.main()
