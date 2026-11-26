"""Specs for the rules pkgs.py decides by: promotion, yanks and image tags."""

import datetime as dt
import unittest
from pathlib import Path

import pkgs

T0 = dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
HOUR = dt.timedelta(hours=1)


def history(*entries):
    """entries: (version, hours in edge before T0, promoted?, yanked?)"""
    h = pkgs.History("skift", {})
    for version, age, promoted, yanked in entries:
        e = h.add(version, f"v{version}", {}, T0 - age * HOUR)
        if promoted:
            e["stable_at"] = pkgs.iso(T0 - HOUR)
        if yanked:
            e.update(yanked=True, yanked_at=pkgs.iso(T0), yank_reason="test")
    return h


class Promotion(unittest.TestCase):
    def test_nothing_has_soaked(self):
        self.assertIsNone(pkgs.choose_promotion(history(("1.0.0", 47, False, False)), T0))

    def test_a_build_at_exactly_48_hours_is_promoted(self):
        self.assertEqual(pkgs.choose_promotion(history(("1.0.0", 48, False, False)), T0), "1.0.0")

    def test_the_newest_soaked_build_wins_over_older_ones(self):
        h = history(("1.0.0", 100, False, False), ("1.1.0", 60, False, False), ("1.2.0", 10, False, False))
        self.assertEqual(pkgs.choose_promotion(h, T0), "1.1.0")

    def test_versions_compare_numerically(self):
        h = history(("1.9.0", 100, False, False), ("1.10.0", 60, False, False))
        self.assertEqual(pkgs.choose_promotion(h, T0), "1.10.0")

    def test_a_yanked_build_is_never_promoted(self):
        h = history(("1.0.0", 100, False, False), ("1.1.0", 60, False, True))
        self.assertEqual(pkgs.choose_promotion(h, T0), "1.0.0")

    def test_stable_never_moves_backwards(self):
        # 1.2.0 was promoted by hand before 1.1.0 finished soaking.
        h = history(("1.1.0", 60, False, False), ("1.2.0", 10, True, False))
        self.assertIsNone(pkgs.choose_promotion(h, T0))

    def test_a_yanked_stable_build_no_longer_holds_stable_back(self):
        h = history(("1.0.0", 200, True, False), ("1.1.0", 60, False, False), ("1.2.0", 100, True, True))
        self.assertEqual(pkgs.choose_promotion(h, T0), "1.1.0")


class ManualControls(unittest.TestCase):
    def test_promote_now_skips_the_soak(self):
        h = history(("1.0.0", 1, False, False))
        pkgs.promote(h, "1.0.0", T0)
        self.assertEqual(h.versions["1.0.0"]["stable_at"], pkgs.iso(T0))

    def test_promote_refuses_a_yanked_or_unknown_or_older_build(self):
        h = history(("1.0.0", 1, False, True), ("1.1.0", 100, False, False), ("1.2.0", 100, True, False))
        for version in ("1.0.0", "9.9.9", "1.1.0", "1.2.0"):
            with self.subTest(version), self.assertRaises(pkgs.Refused):
                pkgs.promote(h, version, T0)

    def test_yank_removes_a_build_from_both_channels(self):
        h = history(("1.0.0", 100, True, False))
        pkgs.yank(h, "1.0.0", "breaks upgrades", T0)
        self.assertEqual(h.in_channel("stable"), [])
        self.assertEqual(h.in_channel("edge"), [])
        self.assertEqual(h.versions["1.0.0"]["yank_reason"], "breaks upgrades")

    def test_yank_needs_a_reason_and_a_published_build(self):
        h = history(("1.0.0", 100, False, False))
        with self.assertRaises(pkgs.Refused):
            pkgs.yank(h, "1.0.0", "  ", T0)
        with self.assertRaises(pkgs.Refused):
            pkgs.yank(h, "2.0.0", "reason", T0)


class ImageTags(unittest.TestCase):
    def test_stable_latest_and_each_series(self):
        h = history(("1.0.0", 300, True, False), ("1.0.1", 200, True, False),
                    ("1.1.0", 100, True, False), ("1.2.0", 1, False, False))
        self.assertEqual(pkgs.image_tags(h),
                         {"1.0": "1.0.1", "1.1": "1.1.0", "stable": "1.1.0", "latest": "1.1.0"})

    def test_a_yanked_build_is_never_tagged(self):
        h = history(("1.0.0", 300, True, False), ("1.0.1", 200, True, True))
        self.assertEqual(pkgs.image_tags(h), {"1.0": "1.0.0", "stable": "1.0.0", "latest": "1.0.0"})

    def test_no_stable_build_means_no_tags(self):
        self.assertEqual(pkgs.image_tags(history(("1.0.0", 1, False, False))), {})


class HistoryShape(unittest.TestCase):
    """channels/<component>.json is read by other repositories."""

    def test_round_trip_newest_first_with_every_field(self):
        h = history(("1.0.0", 100, True, False), ("1.1.0", 1, False, False))
        data = h.dump(T0)
        self.assertEqual([e["version"] for e in data["versions"]], ["1.1.0", "1.0.0"])
        self.assertEqual(set(data["versions"][0]),
                         {"version", "tag", "edge_at", "stable_at", "yanked", "yanked_at", "yank_reason", "rpms"})
        self.assertEqual(pkgs.History.load("skift", data).versions, h.versions)

    def test_a_history_for_another_component_is_refused(self):
        with self.assertRaises(pkgs.Refused):
            pkgs.History.load("skift", {"component": "other", "versions": []})

    def test_only_plain_versions(self):
        for bad in ("1.0", "1.0.0-rc.1", "01.0.0", "v1.0.0"):
            with self.subTest(bad), self.assertRaises(ValueError):
                pkgs.version_key(bad)


class ContentTypes(unittest.TestCase):
    def test_the_key_is_served_as_a_key(self):
        self.assertEqual(pkgs.content_type(Path("RPM-GPG-KEY-steading")), "application/pgp-keys")

    def test_a_changed_content_type_changes_the_tag(self):
        # put_if_changed compares tags, so a file whose type changes is put again.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            key, plain = Path(d) / "RPM-GPG-KEY-steading", Path(d) / "key.bin"
            key.write_text("same"), plain.write_text("same")
            self.assertNotEqual(pkgs.Store.tag_of(key), pkgs.Store.tag_of(plain))


class RepoFiles(unittest.TestCase):
    def test_the_site_and_the_rpm_configure_the_same_repositories(self):
        root = Path(pkgs.ROOT)
        site = (root / "site" / "steading.repo").read_text().splitlines()
        rpm = (root / "release" / "steading.repo").read_text().splitlines()
        self.assertEqual([l for l in site if not l.startswith("gpgkey=")],
                         [l for l in rpm if not l.startswith("gpgkey=")])


if __name__ == "__main__":
    unittest.main()
