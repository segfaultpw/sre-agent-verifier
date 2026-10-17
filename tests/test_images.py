"""What ties the image sources together: one image per runtime, every base pinned by digest,
the runner copied in with its step environment, the pinned build inputs of the python and
node images, the scanner exception, and the documents that describe them."""

import json
import re
import unittest

from support import (
    CONTRACT,
    DEPENDABOT,
    IMAGES,
    RUNTIMES,
    VERIFIER,
    dockerfile,
    read,
)

VEX = IMAGES / "python" / "pip-pkg-resources.openvex.json"
PYTHON_DIR = IMAGES / "python"
FROM_LINE = re.compile(r"^FROM\s+(\S+?)@sha256:([0-9a-f]{64})")


def image_dirs():
    return sorted(path.name for path in IMAGES.iterdir())


class ImagePerRuntimeTest(unittest.TestCase):
    def test_there_is_one_image_per_runtime_and_no_other(self):
        self.assertEqual(image_dirs(), sorted(RUNTIMES))

    def test_every_image_directory_has_a_dockerfile_and_nothing_else_lives_in_the_glob(self):
        for runtime in image_dirs():
            self.assertTrue((IMAGES / runtime / "Dockerfile").is_file(), runtime)
        self.assertEqual(
            sorted(p.name for p in IMAGES.iterdir() if not p.is_dir()), [], "loose files in images/"
        )

    def test_the_contract_names_exactly_these_runtimes(self):
        line = re.search(r"^One per runtime: (.*?)\. Each is built", read(CONTRACT), re.M | re.S)
        self.assertIsNotNone(line)
        named = re.findall(r"`([a-z]+)`", line.group(1))
        self.assertEqual(sorted(named), sorted(RUNTIMES))


class EachImageTest(unittest.TestCase):
    def test_every_from_is_pinned_by_the_digest_of_its_index(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                froms = [l for l in dockerfile(runtime).split("\n") if l.startswith("FROM ")]
                self.assertTrue(froms)
                for line in froms:
                    self.assertRegex(line, r"@sha256:[0-9a-f]{64}", "%s is not pinned by digest" % line)

    def test_installs_the_runner_and_its_step_environment_and_runs_as_the_runner(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                text = dockerfile(runtime)
                self.assertIn("COPY run.sh /usr/local/bin/verifier-run", text)
                self.assertIn("COPY images/%s/step.env /etc/verifier/step.env" % runtime, text)
                self.assertIn('ENTRYPOINT ["/usr/local/bin/verifier-run"]', text)
                self.assertIn("ENV VERIFIER_RUNTIME=%s" % runtime, text)
                self.assertIn("useradd --create-home --uid 10001", text)
                self.assertIn("util-linux", text)
                self.assertTrue((IMAGES / runtime / "step.env").is_file())

    def test_everything_a_run_mounts_is_in_the_build_context_and_is_what_that_run_runs(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                text = dockerfile(runtime)
                for source, target in re.findall(r"--mount=type=bind,source=(\S+),target=(\S+)", text):
                    self.assertTrue((VERIFIER / source).exists(), "%s is not under verifier/" % source)
                    self.assertIn("bash %s" % target, text, "%s is mounted but never run" % target)

    def test_does_not_end_as_the_unprivileged_user_because_the_runner_must_start_as_root(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                users = [l for l in dockerfile(runtime).split("\n") if l.startswith("USER ")]
                self.assertIn(users[-1] if users else None, [None, "USER root"])

    def test_the_step_environment_is_plain_key_value_lines_and_carries_no_secret(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                lines = [
                    l
                    for l in (IMAGES / runtime / "step.env").read_text().split("\n")
                    if l and not l.startswith("#")
                ]
                self.assertTrue(lines)
                for line in lines:
                    self.assertRegex(line, r"\A[A-Za-z_][A-Za-z0-9_]*=[^\s]*\Z", "%s is not KEY=value" % line)
                    self.assertNotRegex(line.lower(), r"token|secret|password|key=")


class PinnedToolchainTest(unittest.TestCase):
    def test_the_go_image_follows_the_floating_tag_of_a_go_line_from_1_26_on(self):
        # A patch tag such as 1.26.9-trixie stops being re-pointed once the next patch ships, so
        # the weekly refresh could never move it; the 1.26.9 floor is checked on the built image
        # by verifier/test/contract.sh instead.
        froms = [FROM_LINE.match(l) for l in dockerfile("go").split("\n") if l.startswith("FROM ")]
        (ref,) = [m.group(1) for m in froms]
        match = re.fullmatch(r"golang:1\.(\d+)-trixie", ref)
        self.assertIsNotNone(match, "%s is not the floating tag of a Go line" % ref)
        self.assertGreaterEqual(int(match.group(1)), 26, "%s is below the Go 1.26 line" % ref)

    def test_the_node_image_stays_on_an_even_lts_major_from_24_on(self):
        froms = [FROM_LINE.match(l) for l in dockerfile("node").split("\n") if l.startswith("FROM ")]
        (ref,) = [m.group(1) for m in froms]
        match = re.fullmatch(r"node:(\d+)-trixie-slim", ref)
        self.assertIsNotNone(match, "%s is not a node major on trixie-slim" % ref)
        major = int(match.group(1))
        self.assertGreaterEqual(major, 24, "%s is below Node 24" % ref)
        self.assertEqual(major % 2, 0, "%s is not a long-term support (even) major" % ref)

    def test_the_java_image_stays_on_an_lts_jdk(self):
        froms = [FROM_LINE.match(l) for l in dockerfile("java").split("\n") if l.startswith("FROM ")]
        (ref,) = [m.group(1) for m in froms]
        jdk = int(re.search(r"temurin-(\d+)$", ref).group(1))
        self.assertIn(jdk, [17, 21, 25, 29], "%s is not a long-term support JDK" % ref)


class PythonImageInputsTest(unittest.TestCase):
    @staticmethod
    def requirement_lines(name):
        text = "\n".join(
            l for l in (PYTHON_DIR / name).read_text().split("\n") if not re.match(r"\A\s*#", l)
        )
        text = re.sub(r"\\\n\s*", "", text)
        return [l for l in text.split("\n") if l]

    @staticmethod
    def normalized(name):
        return name.lower().replace("_", "-")

    def test_every_requirement_in_both_lock_files_is_pinned_and_carries_only_sha256_hashes(self):
        for name in ["pip-fix-tools.lock", "pip-vendored.lock"]:
            for line in self.requirement_lines(name):
                self.assertRegex(
                    line,
                    r"\A[A-Za-z0-9._-]+==[0-9][A-Za-z0-9.!+]*( --hash=sha256:[0-9a-f]{64})+\Z",
                    "%s: %s is not pinned by hash" % (name, line),
                )

    def test_the_tools_lock_says_which_vendoring_requirement_it_was_made_for_and_has_the_tool(self):
        lock = (PYTHON_DIR / "pip-fix-tools.lock").read_text()
        self.assertRegex(lock, r"(?m)^# pip-noxfile: vendoring~=\d+\.\d+\.\d+$")
        names = [l.split("==")[0] for l in self.requirement_lines("pip-fix-tools.lock")]
        self.assertIn("vendoring", names)
        self.assertIn("setuptools", names)

    def test_the_dockerfile_pins_pip_by_a_full_commit_and_its_floors_are_what_the_vendored_lock_pins(self):
        text = dockerfile("python")
        self.assertEqual(
            len(re.findall(r"--pip-source [0-9][A-Za-z0-9.]*=[0-9a-f]{40}(?![0-9a-f])", text)), 1
        )

        locked = {}
        for line in self.requirement_lines("pip-vendored.lock"):
            match = re.match(r"\A([A-Za-z0-9._-]+)==(\S+)", line)
            if match:
                locked[self.normalized(match.group(1))] = match.group(2)

        floors = re.findall(r"--min (\S+)==(\S+)", text)
        self.assertTrue(floors)
        for name, version in floors:
            self.assertEqual(
                locked.get(self.normalized(name)),
                version,
                "%s==%s is a floor in the Dockerfile but pip-vendored.lock pins %r"
                % (name, version, locked.get(self.normalized(name))),
            )

        for name in re.findall(r"--build-from-sdist (\S+)", text):
            self.assertIn(self.normalized(name), locked)


class NodeImageFloorsTest(unittest.TestCase):
    def test_every_floor_carries_the_sha512_its_tarball_must_have(self):
        floors = re.findall(r"--min (\S+)", dockerfile("node"))
        self.assertTrue(floors)
        for floor in floors:
            self.assertRegex(
                floor,
                r"\A@?[a-z0-9][a-z0-9._/-]*@\d+\.\d+\.\d+=sha512-[A-Za-z0-9+/]{86}==\Z",
                "%s has no sha512 integrity" % floor,
            )


class ScannerExceptionTest(unittest.TestCase):
    def test_is_the_only_openvex_document_under_verifier_and_it_is_the_python_images(self):
        documents = sorted(VERIFIER.glob("**/*.openvex.json"))
        self.assertEqual(documents, [VEX])

    def test_holds_one_statement_for_one_cve_on_one_package_and_nothing_broader(self):
        vex = json.loads(VEX.read_text())
        self.assertEqual(vex["@context"], "https://openvex.dev/ns/v0.2.0")
        (statement,) = vex["statements"]
        self.assertEqual(statement["vulnerability"], {"name": "CVE-2025-47273"})
        self.assertEqual(statement["products"], [{"@id": "pkg:pypi/setuptools@70.3.0"}])
        self.assertEqual(statement["status"], "not_affected")
        self.assertEqual(statement["justification"], "vulnerable_code_not_present")
        self.assertEqual(
            sorted(statement),
            ["impact_statement", "justification", "products", "status", "vulnerability"],
        )
        self.assertIn("pkg_resources/__init__.py", statement["impact_statement"])
        self.assertIn("setuptools/package_index.py", statement["impact_statement"])

    def test_the_contract_documents_the_exception_its_evidence_and_when_it_goes(self):
        contract = read(CONTRACT)
        for text in [
            "CVE-2025-47273",
            "pkg:pypi/setuptools@70.3.0",
            "pip-pkg-resources.openvex.json",
            "vulnerable_code_not_present",
            "verifier/check-vex.sh",
            "removed when pip vendors a fixed setuptools",
        ]:
            self.assertIn(text, contract)


class DocumentedProceduresTest(unittest.TestCase):
    def test_the_contract_states_what_a_repository_can_observe_of_the_fix_steps(self):
        contract = read(CONTRACT)
        for text in [
            "does nothing and fetches nothing",
            "sha512",
            "pip-fix-tools.lock",
            "ensurepip",
            "virtualenv, tox and uv",
            "pip freeze",
        ]:
            self.assertIn(text, contract)

    def test_one_documented_procedure_names_the_pin_both_locks_and_the_commands_that_regenerate_them(self):
        parts = read(CONTRACT).split("### Bumping pip or Python in the python image")
        self.assertEqual(len(parts), 2)
        section = parts[1].split("\n### ")[0]

        self.assertIn("git ls-remote", section)
        self.assertIn("--pip-source", section)
        self.assertIn("pip-vendored.lock", section)
        self.assertIn('curl -sf "https://pypi.org/pypi/', section)
        self.assertIn("pip-fix-tools.lock", section)
        self.assertIn("pip-compile --generate-hashes", section)
        self.assertIn("# pip-noxfile:", section)
        self.assertIn("tests/test_images.py", section)

        text = dockerfile("python")
        for floor in re.findall(r"--min (\S+)", text):
            self.assertIn(floor, section, "the procedure does not apply the floor %s" % floor)
        for name in re.findall(r"--build-from-sdist (\S+)", text):
            self.assertIn('SDISTS="%s"' % name, section)

    def test_the_fix_pip_scripts_refusals_point_at_that_procedure(self):
        script = (PYTHON_DIR / "fix-pip-vendored.sh").read_text()
        for refusal in [
            "has no pinned source",
            "pip-vendored.lock does not pin what the edited vendor.txt names",
            "pip-fix-tools.lock was made for another vendoring requirement",
        ]:
            self.assertRegex(
                script,
                r'fail "[^"]*%s[^"]*Bumping pip or Python in the python image[^"]*"' % re.escape(refusal),
                "%s does not say where to look" % refusal,
            )


class BaseImagesDoNotGoStaleTest(unittest.TestCase):
    def test_dependabot_watches_every_verifier_image_directory_with_the_docker_ecosystem(self):
        dependabot = read(DEPENDABOT)
        self.assertIn('"/verifier/images/*"', dependabot)
        entries = [e for e in re.split(r"(?m)^  - package-ecosystem: ", dependabot) if '"/verifier/images/*"' in e]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].startswith("docker"))

    def test_dependabot_watches_the_github_actions_the_workflow_uses(self):
        dependabot = read(DEPENDABOT)
        self.assertRegex(dependabot, r"(?m)^  - package-ecosystem: github-actions$")

    def test_dependabot_ignores_the_elixir_image_minor_and_major_bumps_under_the_name_the_image_has(self):
        entry = self.docker_entry()
        self.assertIn("FROM elixir:", dockerfile("elixir"))
        self.assertNotIn("hexpm/elixir", entry)
        self.assertRegex(
            entry,
            r"dependency-name: elixir\n\s+update-types:\n\s+- \"version-update:semver-major\"\n\s+- \"version-update:semver-minor\"",
        )

    def test_dependabot_ignores_the_non_lts_majors_of_node_and_the_jdk(self):
        entry = self.docker_entry()
        node = re.search(r"dependency-name: node\n\s+versions:\n((?:\s+- .*\n)+)", entry)
        self.assertIsNotNone(node, "no ignore rule for node")
        for odd in ["25", "27", "29"]:
            self.assertRegex(node.group(1), r">= %s, < %d" % (int(odd), int(odd) + 1))

        self.assertIn("FROM maven:3-eclipse-temurin-", dockerfile("java"))
        maven = re.search(r"dependency-name: maven\n\s+versions:\n((?:\s+- .*\n)+)", entry)
        self.assertIsNotNone(maven, "no ignore rule for the JDK")
        # Dependabot reads maven:3-eclipse-temurin-JDK as the version 3.JDK, so a range in
        # plain JDK numbers (">= 22, < 25") matches no tag at all
        ranges = re.findall(r'">= 3\.(\d+), < 3\.(\d+)"', maven.group(1))
        self.assertEqual(len(ranges), len(re.findall(r"- \"", maven.group(1))), "a range is not in the 3.N form")
        ignored = {v for low, high in ranges for v in range(int(low), int(high))}
        for lts in [21, 25, 29, 33]:
            self.assertNotIn(lts, ignored, "JDK %d is an LTS and must stay reachable" % lts)
        for non_lts in [22, 23, 24, 26, 27, 28, 30, 31, 32]:
            self.assertIn(non_lts, ignored, "JDK %d is not an LTS and must be ignored" % non_lts)

    def test_dependabot_does_not_take_a_maven_major_jump(self):
        entry = self.docker_entry()
        self.assertRegex(
            entry,
            r"dependency-name: maven\n\s+update-types:\n\s+- \"version-update:semver-major\"",
        )

    def docker_entry(self):
        dependabot = read(DEPENDABOT)
        (entry,) = [
            e
            for e in re.split(r"(?m)^  - package-ecosystem: ", dependabot)
            if '"/verifier/images/*"' in e
        ]
        return entry

    def test_no_base_is_a_dated_tag_because_a_tag_that_never_moves_cannot_be_refreshed(self):
        for runtime in RUNTIMES:
            for line in dockerfile(runtime).split("\n"):
                if not line.startswith("FROM "):
                    continue
                ref = FROM_LINE.match(line).group(1)
                self.assertNotRegex(ref, r"(^|[-.:])20\d{6}(\D|$)", "%s names a dated build" % ref)


if __name__ == "__main__":
    unittest.main()
