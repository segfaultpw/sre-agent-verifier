"""verifier/check-vex.sh, which the publishing workflow runs before it passes an OpenVEX
document to Trivy: the workflow that publishes an image decides for itself what a scan may
suppress, so a broader exception is refused here."""

import copy
import json
import unittest

from support import IMAGES, RUNTIMES, VERIFIER, TempDirCase, requires_tools, run

CHECK_VEX = VERIFIER / "check-vex.sh"
VEX = IMAGES / "python" / "pip-pkg-resources.openvex.json"


def real_vex():
    return json.loads(VEX.read_text())


def widen(vex, fn):
    vex = copy.deepcopy(vex)
    fn(vex["statements"][0])
    return vex


@requires_tools("bash", "jq")
class CheckVexTest(TempDirCase):
    def check(self, runtime, images=None):
        args = ["bash", CHECK_VEX, runtime] + ([images] if images else [])
        return run(args)

    def images_with(self, runtime, doc, name="images"):
        images = self.dir / name
        (images / runtime).mkdir(parents=True)
        text = doc if isinstance(doc, str) else json.dumps(doc)
        (images / runtime / "x.openvex.json").write_text(text)
        return images

    def test_accepts_the_committed_document_and_every_runtime_that_has_none(self):
        self.assertEqual(self.check("python")[1], 0)
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertEqual(self.check(runtime)[1], 0)

    def test_accepts_a_copy_of_the_committed_document(self):
        self.assertEqual(self.check("python", self.images_with("python", real_vex()))[1], 0)

    def test_refuses_every_way_of_widening_the_exception(self):
        statement = real_vex()["statements"][0]

        def put(key, value):
            return lambda st: st.__setitem__(key, value)

        def second_statement():
            vex = real_vex()
            vex["statements"].append(statement)
            return vex

        def extra_document_field():
            vex = real_vex()
            vex["products"] = []
            return vex

        def no_statement():
            vex = real_vex()
            vex["statements"] = []
            return vex

        def subcomponents(st):
            st["products"][0]["subcomponents"] = [{"@id": "pkg:pypi/urllib3@2.7.0"}]

        def second_product(st):
            st["products"].append({"@id": "pkg:pypi/urllib3@2.7.0"})

        widened = {
            "a second statement": second_statement(),
            "another CVE": widen(real_vex(), lambda st: st["vulnerability"].__setitem__("name", "CVE-2026-1")),
            "an alias on the vulnerability": widen(
                real_vex(), lambda st: st["vulnerability"].__setitem__("aliases", ["CVE-2026-1"])
            ),
            "another version of the package": widen(
                real_vex(), put("products", [{"@id": "pkg:pypi/setuptools@71.0.0"}])
            ),
            "the package without a version": widen(
                real_vex(), put("products", [{"@id": "pkg:pypi/setuptools"}])
            ),
            "another package": widen(real_vex(), put("products", [{"@id": "pkg:pypi/urllib3@2.7.0"}])),
            "a second product": widen(real_vex(), second_product),
            "subcomponents": widen(real_vex(), subcomponents),
            "another status": widen(real_vex(), put("status", "fixed")),
            "another justification": widen(real_vex(), put("justification", "component_not_present")),
            "an extra field on the statement": widen(real_vex(), put("scope", "*")),
            "an extra field on the document": extra_document_field(),
            "no statement": no_statement(),
            "text that is not JSON": "{not json",
        }

        for label, doc in widened.items():
            with self.subTest(label=label):
                images = self.images_with("python", doc, name=label.replace(" ", "-"))
                output, status = self.check("python", images)
                self.assertEqual(status, 1, "%s was accepted" % label)
                self.assertIn("not the one exception the python image is allowed", output)
                self.assertIn("CVE-2025-47273 on pkg:pypi/setuptools@70.3.0", output)

    def test_refuses_any_document_in_the_directory_of_a_runtime_that_is_allowed_none(self):
        images = self.images_with("node", real_vex())
        output, status = self.check("node", images)
        self.assertEqual(status, 1)
        self.assertIn("no scanner exception is allowed for the node image", output)

        # the same document is fine in the python image's own directory, and the script looks
        # only at the directory of the runtime it is asked about
        (images / "python").mkdir()
        (images / "python" / "x.openvex.json").write_text(json.dumps(real_vex()))
        self.assertEqual(self.check("python", images)[1], 0)

    def test_needs_a_runtime(self):
        output, status = run(["bash", CHECK_VEX])
        self.assertEqual(status, 2)
        self.assertIn("usage:", output)


if __name__ == "__main__":
    unittest.main()
