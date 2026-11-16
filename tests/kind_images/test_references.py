import unittest

from stackwrench.kind_images.inputs import InputError
from stackwrench.kind_images.references import parse_reference


class ReferenceTests(unittest.TestCase):
    def test_docker_hub_aliases(self):
        for raw in ("busybox:1.36", "library/busybox:1.36", "docker.io/busybox:1.36",
                    "docker.io/library/busybox:1.36", "index.docker.io/library/busybox:1.36"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_reference(raw).canonical, "docker.io/library/busybox:1.36")

    def test_omitted_tag_is_latest(self):
        self.assertEqual(parse_reference("busybox").canonical, "docker.io/library/busybox:latest")

    def test_namespace_is_not_library(self):
        self.assertEqual(parse_reference("myorg/app:v1").canonical, "docker.io/myorg/app:v1")

    def test_registry_port_is_not_tag(self):
        self.assertEqual(parse_reference("localhost:5000/team/app").canonical,
                         "localhost:5000/team/app:latest")
        self.assertEqual(parse_reference("REGISTRY.EXAMPLE:5000/app:v2").canonical,
                         "registry.example:5000/app:v2")

    def test_non_hub_registry_is_not_aliased(self):
        self.assertEqual(parse_reference("registry-1.docker.io/app:v1").canonical,
                         "registry-1.docker.io/app:v1")

    def test_tag_case_is_preserved(self):
        self.assertNotEqual(parse_reference("app:V1"), parse_reference("app:v1"))

    def test_digest_takes_precedence_over_tag(self):
        digest = "sha256:" + "a" * 64
        ref = parse_reference("app:latest@" + digest)
        self.assertEqual(ref.canonical, "docker.io/library/app@" + digest)
        self.assertEqual(ref.default_policy, "IfNotPresent")

    def test_default_policies(self):
        for image, expected in [("app", "Always"), ("app:latest", "Always"),
                                ("app:LATEST", "IfNotPresent"), ("app:v1", "IfNotPresent"),
                                ("reg:5000/app", "Always"),
                                ("app@sha256:" + "1" * 64, "IfNotPresent"),
                                ("app@sha512:" + "2" * 128, "IfNotPresent")]:
            with self.subTest(image=image):
                self.assertEqual(parse_reference(image).default_policy, expected)

    def test_invalid_and_out_of_subset_references(self):
        invalid = [None, 12, "", " app:v1", "app:v1 ", "app:v 1", "app:v1\n", "App:v1",
                   "-app:v1", "app:", "app:/v1", "https://registry/app:v1", "reg:0/app:v1",
                   "reg:65536/app:v1", "reg:abc/app:v1", "[::1]:5000/app:v1", "app//child:v1",
                   "docker.io/:v1", "app@sha256:123", "app@sha256:" + "A" * 64,
                   "app@md5:" + "a" * 32, "app@sha256:" + "a" * 64 + "@other",
                   "app:$(touch-injected)", "app:tag;id", "app/../name:v1", "app:" + "t" * 129,
                   "a" * 256 + ":v1", "app\x1b:v1"]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(InputError):
                parse_reference(raw)


if __name__ == "__main__":
    unittest.main()
