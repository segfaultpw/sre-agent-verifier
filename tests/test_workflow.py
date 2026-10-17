"""The workflow that tests, scans and publishes the images. GitHub refuses a workflow with a
syntax error and runs a weaker one without saying so, so what it must contain is pinned
here."""

import re
import unittest

from support import RUNTIMES, WORKFLOW, job_blocks, read

WORKFLOW_TEXT = read(WORKFLOW)
JOBS = job_blocks(WORKFLOW_TEXT)


class ImageMatrixTest(unittest.TestCase):
    def test_builds_every_runtime_for_both_architectures_each_on_a_native_runner(self):
        matrix = re.search(r"runtime: \[([^\]]+)\]", WORKFLOW_TEXT).group(1)
        self.assertEqual(sorted(m.strip() for m in matrix.split(",")), sorted(RUNTIMES))
        for text in [
            "arch: [amd64, arm64]",
            "platform: linux/amd64",
            "platform: linux/arm64",
            "RUNNER_X64",
            "RUNNER_ARM64",
            "cron:",
            "contract.sh",
        ]:
            self.assertIn(text, WORKFLOW_TEXT)

    def test_tests_scans_and_pushes_the_same_image_for_every_architecture(self):
        self.assertIn('bash verifier/test/contract.sh "${{ matrix.runtime }}"', WORKFLOW_TEXT)
        self.assertIn("--exit-code 1", WORKFLOW_TEXT)
        self.assertIn('"verifier-contract-${{ matrix.runtime }}:local"', WORKFLOW_TEXT)
        self.assertIn("docker tag", WORKFLOW_TEXT)
        self.assertNotIn("build-push-action", WORKFLOW_TEXT, "a second, untested build is pushed")
        self.assertNotIn("matrix.arch ==", WORKFLOW_TEXT)
        self.assertNotIn("matrix.platform ==", WORKFLOW_TEXT)

    def test_lints_every_shell_script_under_verifier_found_by_name_so_none_can_be_left_out(self):
        self.assertIn("find verifier -name '*.sh'", WORKFLOW_TEXT)
        self.assertIn('bash -n "$script"', WORKFLOW_TEXT)
        self.assertIn('shellcheck -S warning "${scripts[@]}"', WORKFLOW_TEXT)

    def test_hands_every_image_directorys_openvex_statements_to_the_trivy_scan_and_only_those(self):
        self.assertIn("verifier/images/${{ matrix.runtime }}/*.openvex.json", WORKFLOW_TEXT)
        self.assertIn('trivy_vex+=(--vex "/vex/$(basename "$vex")")', WORKFLOW_TEXT)
        self.assertIn('"${trivy_vex[@]}"', WORKFLOW_TEXT)
        self.assertNotIn("--ignorefile", WORKFLOW_TEXT)
        self.assertNotIn("trivyignore", WORKFLOW_TEXT)

    def test_merges_the_two_architectures_into_the_multi_arch_image(self):
        for text in ["docker buildx imagetools create", "-amd64", "-arm64"]:
            self.assertIn(text, WORKFLOW_TEXT)

    def test_the_weekly_rebuild_re_resolves_the_base_digests_and_takes_nothing_from_a_cache(self):
        self.assertIn("github.event_name == 'schedule'", WORKFLOW_TEXT)
        self.assertIn("verifier/refresh-bases.sh", WORKFLOW_TEXT)
        self.assertIn("--no-cache", WORKFLOW_TEXT)

    def test_runs_check_vex_for_the_runtime_being_scanned_before_the_scan(self):
        call = 'bash verifier/check-vex.sh "${{ matrix.runtime }}"'
        self.assertIn(call, WORKFLOW_TEXT)
        self.assertLess(WORKFLOW_TEXT.index(call), WORKFLOW_TEXT.index("aquasec/trivy:0.58.1 image"))


class SyntaxTest(unittest.TestCase):
    def test_has_exactly_the_expected_jobs(self):
        self.assertEqual(sorted(JOBS), ["image", "merge", "unit-tests"])

    def test_has_no_tabs_and_no_plain_scalar_holding_a_colon_and_a_space(self):
        self.assertNotIn("\t", WORKFLOW_TEXT)
        # a step or job name such as "Trivy scan (blocking: fixable)" is a YAML syntax error
        for line in WORKFLOW_TEXT.split("\n"):
            match = re.match(r"^\s*(?:- )?name:\s+(.*)$", line)
            if match and not match.group(1).startswith(("'", '"')):
                self.assertNotIn(": ", match.group(1), line)

    def test_every_step_has_a_name_a_uses_or_a_run(self):
        for job, block in JOBS.items():
            steps = block.split("\n    steps:\n")[1]
            for step in re.split(r"(?m)^      - ", steps)[1:]:
                self.assertRegex(step, r"^(name:|uses:|run:)", "%s: %r" % (job, step[:60]))


class PublishingIsGatedAndIndependentTest(unittest.TestCase):
    def test_the_unit_tests_run_before_any_image_is_built_and_so_before_anything_is_published(self):
        self.assertRegex(JOBS["image"], r"(?m)^    needs: unit-tests$")
        tests = JOBS["unit-tests"]
        self.assertIn("python3 -I -m unittest discover -s tests -v", tests)
        self.assertIn("VERIFIER_TESTS_REQUIRE_TOOLS", tests)
        self.assertIn("RUNNER_X64", tests)

    def test_the_unit_tests_run_on_pull_requests_and_on_every_push_that_touches_them(self):
        self.assertRegex(WORKFLOW_TEXT, r'(?m)^      - "tests/\*\*"$')
        self.assertRegex(WORKFLOW_TEXT, r"(?m)^  pull_request:\n    paths:")
        self.assertNotRegex(JOBS["unit-tests"], r"if: .*pull_request")

    def test_one_runtime_failing_never_stops_another(self):
        self.assertRegex(JOBS["image"], r"(?m)^      fail-fast: false$")
        self.assertRegex(JOBS["merge"], r"(?m)^      fail-fast: false$")

        merge_matrix = re.search(r"runtime: \[([^\]]+)\]", JOBS["merge"]).group(1)
        self.assertEqual(sorted(m.strip() for m in merge_matrix.split(",")), sorted(RUNTIMES))

        # it waits for the legs, but a failed leg does not skip it: the implied success() of
        # a bare `needs` is what stopped all six images for one failure
        self.assertRegex(JOBS["merge"], r"(?m)^    needs: image$")
        condition = re.search(r"(?m)^    if: (.*)$", JOBS["merge"]).group(1)
        self.assertIn("!cancelled()", condition)
        self.assertIn("github.event_name != 'pull_request'", condition)
        self.assertNotIn("success()", condition)

    def test_publishes_only_a_runtime_whose_own_two_images_were_pushed_and_checks_before_merging(self):
        check = 'imagetools inspect "$IMAGE:$TAG-$arch"'
        merge = JOBS["merge"]
        for text in ["for arch in amd64 arm64", check, "exit 1", "imagetools create"]:
            self.assertIn(text, merge)
        self.assertLess(merge.index(check), merge.index("imagetools create"))

    def test_only_a_push_that_is_not_a_pull_request_logs_in_and_pushes(self):
        image = JOBS["image"]
        for step in ["Log in to GHCR", "Push the image that was tested and scanned"]:
            body = image.split("name: " + step)[1].split("\n      - ")[0]
            self.assertIn("if: github.event_name != 'pull_request'", body, step)

    def test_pushing_needs_packages_write_and_nothing_else_is_widened(self):
        permissions = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", WORKFLOW_TEXT).group(1)
        self.assertEqual(sorted(permissions.split("\n")[:-1]), ["  contents: read", "  packages: write"])


if __name__ == "__main__":
    unittest.main()
