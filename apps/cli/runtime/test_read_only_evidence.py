import unittest

from apps.cli.runtime.read_only_evidence import paths_align_for_listing


class TestReadOnlyEvidence(unittest.TestCase):
    def test_root_listing_does_not_align_with_arbitrary_subpath(self) -> None:
        self.assertFalse(paths_align_for_listing("apps/cli", "."))
        self.assertFalse(paths_align_for_listing("apps/cli", ""))

    def test_same_or_nested_listing_still_aligns(self) -> None:
        self.assertTrue(paths_align_for_listing("apps/cli", "apps/cli"))
        self.assertTrue(paths_align_for_listing("apps/cli", "./apps/cli"))
        self.assertTrue(paths_align_for_listing("server/api/adapters", "api/adapters"))


if __name__ == "__main__":
    unittest.main()
