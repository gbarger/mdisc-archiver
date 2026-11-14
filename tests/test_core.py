import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import burner  # noqa: E402
import planner  # noqa: E402
import scanner  # noqa: E402
from catalog import Catalog  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lib = os.path.join(self.tmp, "Photos")
        write(os.path.join(self.lib, "2024", "a.jpg"), b"a" * 5000)
        write(os.path.join(self.lib, "2024", "b.jpg"), b"b" * 7000)
        write(os.path.join(self.lib, "2025", "c.mp4"), b"c" * 20000)
        write(os.path.join(self.lib, "2025", "Thumbs.db"), b"junk")
        self.cat = Catalog(os.path.join(self.tmp, "cat", "catalog.sqlite"))
        self.cat.add_source(self.lib)
        self.log = []

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.tmp)

    def scan(self):
        return scanner.scan(self.cat, log=self.log.append)

    def plan_and_burn(self, usable=10**9):
        chosen, used, _ = planner.plan(self.cat.pending_files(), usable)
        d = self.cat.create_disc(chosen, used)
        self.cat.set_status(d["id"], "burned")
        return d

    def test_scan_finds_new_files_and_skips_excludes(self):
        new, errors = self.scan()
        self.assertEqual((new, errors), (3, 0))
        paths = [p["rel_path"] for p in self.cat.pending_files()]
        self.assertEqual(paths, ["2024/a.jpg", "2024/b.jpg", "2025/c.mp4"])

    def test_rescan_unchanged_finds_nothing_new(self):
        self.scan()
        self.assertEqual(self.scan()[0], 0)

    def test_burned_files_are_not_pending(self):
        self.scan()
        d = self.plan_and_burn()
        self.assertEqual(d["label"], "MD-0001")
        self.assertEqual(self.cat.pending_files(), [])
        write(os.path.join(self.lib, "2026", "d.jpg"), b"d" * 100)
        self.scan()
        self.assertEqual([p["rel_path"] for p in self.cat.pending_files()], ["2026/d.jpg"])
        self.assertEqual(self.plan_and_burn()["label"], "MD-0002")

    def test_moved_file_is_not_burned_again(self):
        self.scan()
        self.plan_and_burn()
        shutil.move(os.path.join(self.lib, "2024", "a.jpg"), os.path.join(self.lib, "a_moved.jpg"))
        self.scan()
        self.assertEqual(self.cat.pending_files(), [])
        hits = self.cat.search("a_moved")
        self.assertEqual(hits[0]["disc_label"], "MD-0001")
        self.assertEqual(hits[0]["disc_path"], "Photos/2024/a.jpg")

    def test_changed_file_becomes_new_version(self):
        self.scan()
        self.plan_and_burn()
        p = os.path.join(self.lib, "2024", "a.jpg")
        write(p, b"edited" * 2000)
        os.utime(p, (2_000_000_000, 2_000_000_000))
        self.scan()
        pending = self.cat.pending_files()
        self.assertEqual([x["rel_path"] for x in pending], ["2024/a.jpg"])
        old = [r for r in self.cat.search("a.jpg") if not r["present"]]
        self.assertEqual(old[0]["disc_label"], "MD-0001")

    def test_duplicate_content_burned_once(self):
        write(os.path.join(self.lib, "copy", "a_copy.jpg"), b"a" * 5000)
        self.scan()
        self.assertEqual(len(self.cat.pending_files()), 3)

    def test_failed_disc_returns_files_to_pending(self):
        self.scan()
        d = self.plan_and_burn()
        self.cat.set_status(d["id"], "failed")
        self.assertEqual(len(self.cat.pending_files()), 3)

    def test_cancel_plan_frees_label(self):
        self.scan()
        chosen, used, _ = planner.plan(self.cat.pending_files(), 10**9)
        d = self.cat.create_disc(chosen, used)
        self.assertEqual(len(self.cat.pending_files()), 0)   # planned files are reserved
        self.cat.cancel_plan(d["id"])
        self.assertEqual(len(self.cat.pending_files()), 3)
        self.assertEqual(self.cat.create_disc(chosen, used)["label"], "MD-0001")

    def test_planner_respects_capacity_and_flags_oversized(self):
        files = [{"size": 5000}, {"size": 7000}, {"size": 20000}, {"size": 3000}]
        usable = planner.space_on_disc(5000) + planner.space_on_disc(3000)
        chosen, used, oversized = planner.plan(files, usable)
        self.assertEqual([f["size"] for f in chosen], [5000, 3000])
        self.assertLessEqual(used, usable)
        self.assertEqual([f["size"] for f in oversized], [20000])

    def test_removed_source_files_not_pending(self):
        self.scan()
        self.cat.remove_source(self.cat.active_sources()[0]["id"])
        self.assertEqual(self.cat.pending_files(), [])

    def test_prepare_writes_manifest_and_index(self):
        self.scan()
        chosen, used, _ = planner.plan(self.cat.pending_files(), 10**9)
        d = self.cat.create_disc(chosen, used)
        manifest, index_dir = burner.prepare(self.cat, d, self.log.append)
        lines = open(manifest, encoding="utf-8").read().splitlines()
        self.assertEqual(lines[0].split("\t")[0], "Photos\\2024\\a.jpg")
        self.assertTrue(os.path.isfile(lines[0].split("\t")[1]))
        for name in ("catalog.sqlite", "catalog.csv", "DISC_LABEL.txt", "README.txt"):
            self.assertTrue(os.path.isfile(os.path.join(index_dir, name)), name)
        snap = sqlite3.connect(os.path.join(index_dir, "catalog.sqlite"))
        self.assertEqual(snap.execute("SELECT status FROM discs").fetchone()[0], "burned")
        snap.close()
        self.assertEqual(self.cat.disc(d["id"])["status"], "planned")   # working DB unchanged
        self.assertIn("MD-0001,Photos/2024/a.jpg", open(os.path.join(index_dir, "catalog.csv")).read())

    def test_prepare_rejects_changed_files(self):
        self.scan()
        chosen, used, _ = planner.plan(self.cat.pending_files(), 10**9)
        d = self.cat.create_disc(chosen, used)
        os.remove(os.path.join(self.lib, "2024", "b.jpg"))
        with self.assertRaises(RuntimeError):
            burner.prepare(self.cat, d, self.log.append)

    def test_verify_against_exported_folder(self):
        self.scan()
        chosen, used, _ = planner.plan(self.cat.pending_files(), 10**9)
        d = self.cat.create_disc(chosen, used)
        _, index_dir = burner.prepare(self.cat, d, self.log.append)
        out = burner.export_folder(self.cat, d, index_dir, os.path.join(self.tmp, "export"), self.log.append)
        self.cat.set_status(d["id"], "burned")
        d = self.cat.disc(d["id"])
        self.assertEqual(burner.verify(self.cat, d, out, self.log.append), [])
        write(os.path.join(out, "Photos", "2024", "a.jpg"), b"corrupt")
        self.assertEqual(len(burner.verify(self.cat, d, out, self.log.append)), 1)


if __name__ == "__main__":
    unittest.main()
