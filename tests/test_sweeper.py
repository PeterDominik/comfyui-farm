"""The retention sweeper's dry run (manager/sweeper.py, decision 0041): which finished jobs
have expired, and the exact files it would delete, only those their own provenance file
lists, in their own folder. It deletes nothing. The endpoints are in test_e2e.

    python -m unittest tests.test_sweeper -v
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.manager import sweeper as SW
from comfyfarm.manager.db import JobStore
from comfyfarm.protocol import now

DAY = 86400


class Sweeps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = self.tmp.replace("\\", "/")
        self.cfg = Config.model_validate({
            "database": f"{self.root}/data/farm.db", "workers": [],
            "paths": {"mappings": [{"posix": "/rdo/shows", "windows": f"{self.root}/shows"}]},
            "retention": {"keep_days": 3}})
        self.store = JobStore(Path(self.cfg.database))
        self.t = now()

    def tearDown(self):
        self.store.close()                       # Windows can't delete an open database
        shutil.rmtree(self.tmp, ignore_errors=True)

    def folder(self, name="abc_0010") -> str:
        d = f"{self.root}/shows/xyz/renders/{name}"
        os.makedirs(d, exist_ok=True)
        return d

    def job(self, jid, days_ago=5.0, files=("plate.wan_v001.1001.png", "plate.wan_v001.1002.png"), folder=None,
            status="done", provenance=True, doc=None, **kw) -> dict:
        """A finished job whose outputs (and provenance file listing them) are on disk."""
        folder = folder or self.folder()
        for name in files:
            Path(folder, name).write_bytes(b"x" * 10)
        outputs = {"output_dir": folder}
        if provenance:
            path = f"{folder}/{jid}.provenance.json"
            Path(path).write_text(json.dumps(doc if doc is not None else
                                             {"comfyfarm_provenance": 1, "job": jid, "files": list(files)}))
            outputs["provenance"] = path
        row = {"id": jid, "workflow": "wan22_i2i", "user": "ann", "status": status,
               "created_at": self.t - days_ago * DAY, "finished_at": self.t - days_ago * DAY, "output_dir": folder,
               "outputs": outputs,
               "info": {"show": "xyz"}}
        row.update(kw)
        return self.store.insert(row)

    def sweep(self) -> dict:
        return SW.sweep(self.store, self.cfg, self.t)

    def entry(self, report, jid) -> dict:
        return next(e for e in report["jobs"] if e["job"] == jid)

    def test_an_expired_job_lists_the_files_its_provenance_names_and_deletes_nothing(self):
        d = self.folder()
        self.job("j1")
        Path(d, "someone_elses.exr").write_bytes(b"y")
        before = sorted(os.listdir(d))
        r = self.sweep()
        self.assertEqual((r["dry_run"], r["expired"], r["kept"], r["files"], r["bytes"], r["records"]),
                         (True, 1, 0, 3, 20, 1))
        e = self.entry(r, "j1")
        self.assertEqual(e["delete"], ["plate.wan_v001.1001.png", "plate.wan_v001.1002.png"])
        self.assertEqual((e["provenance"], e["left"], e["empties_folder"], e["problems"]),
                         (["j1.provenance.json"], 1, False, []))
        self.assertEqual(e["folder"], "/rdo/shows/xyz/renders/abc_0010")               # studio paths
        self.assertEqual(sorted(os.listdir(d)), before)                                # a dry run
        self.assertIsNotNone(self.store.get("j1"))

    def test_a_folder_holding_only_the_jobs_files_would_be_empty(self):
        self.job("j1", folder=self.folder("rockA.chord_v001"), files=("rock_BaseColor.1001.exr",))
        e = self.entry(self.sweep(), "j1")
        self.assertEqual((e["left"], e["empties_folder"]), (0, True))

    def test_kept_until_keep_days_after_it_finished(self):
        self.job("young", days_ago=2.9)
        self.job("old", days_ago=3.1)
        r = self.sweep()
        self.assertEqual(([e["job"] for e in r["jobs"]], r["kept"]), (["old"], 1))

    def test_a_publish_sent_to_tractor_restarts_the_clock(self):
        self.job("j1", days_ago=10)
        self.store.insert_publish({"id": "p1", "job_id": "j1", "created_at": self.t - 1.5 * DAY, "state": "dispatched",
                                   "finished_at": self.t - 1 * DAY})
        self.store.insert_publish({"id": "p0", "job_id": "j1", "created_at": self.t - 9 * DAY, "state": "failed"})
        self.assertEqual((self.sweep()["expired"], self.sweep()["kept"]), (0, 1))
        self.store.update_publish("p1", finished_at=self.t - 4 * DAY)
        e = self.entry(self.sweep(), "j1")
        self.assertEqual(datetime.fromisoformat(e["published"]).timestamp(), int(self.t - 4 * DAY))

    def test_kept_while_a_publish_is_on_its_way(self):
        self.job("queued", days_ago=10)
        self.store.insert_publish({"id": "p1", "job_id": "queued", "created_at": self.t, "state": "queued"})
        self.job("asked", days_ago=10, info={"show": "xyz", "publish_request": {"step": "comp"}})
        r = self.sweep()
        self.assertEqual((r["expired"], r["publishing"]), (0, 2))

    def test_without_a_provenance_file_only_the_record_would_go(self):
        self.job("j1", provenance=False)
        self.job("j2", status="failed", provenance=False, files=())
        r = self.sweep()
        for jid in ("j1", "j2"):
            e = self.entry(r, jid)
            self.assertEqual((e["delete"], e["records"]), ([], 1))
            self.assertIn("No provenance file", e["problems"][0])
        self.assertEqual(r["files"], 0)

    def test_only_its_own_provenance_file_in_its_own_folder_is_believed(self):
        self.job("other", doc={"comfyfarm_provenance": 1, "job": "someone", "files": ["plate.wan_v001.1001.png"]})
        self.job("version", doc={"comfyfarm_provenance": 9, "job": "version", "files": ["plate.wan_v001.1001.png"]})
        bad = self.job("broken")
        Path(bad["outputs"]["provenance"]).write_text("{not json")
        gone = self.job("gone")
        os.remove(gone["outputs"]["provenance"])
        away = self.job("away")
        moved = f"{self.folder('elsewhere')}/away.provenance.json"
        shutil.copy(away["outputs"]["provenance"], moved)
        self.store.update("away", outputs={"output_dir": away["output_dir"], "provenance": moved})
        r = self.sweep()
        for jid, says in (("other", "belongs to job someone"), ("version", "isn't a provenance file"),
                          ("broken", "can't be read"), ("gone", "is gone"),
                          ("away", "isn't in the job's output folder")):
            e = self.entry(r, jid)
            self.assertEqual(e["delete"], [], jid)
            self.assertIn(says, e["problems"][0], jid)
        self.assertEqual(r["files"], 0)

    def test_names_that_leave_the_folder_or_arent_files_are_left_alone(self):
        d = self.folder()
        os.makedirs(f"{d}/sub")
        Path(f"{self.root}/shows/xyz/renders/secret.txt").write_text("keep")
        names = ["../secret.txt", f"{self.root}/shows/xyz/renders/secret.txt", "C:/Windows/win.ini", "sub", "",
                 "plate.wan_v001.1001.png", "never_written.png"]
        self.job("j1", files=("plate.wan_v001.1001.png",),
                 doc={"comfyfarm_provenance": 1, "job": "j1", "files": names})
        if hasattr(os, "symlink"):
            try:
                os.symlink(f"{self.root}/shows/xyz/renders/secret.txt", f"{d}/link.txt")
                names.append("link.txt")
                Path(d, "j1.provenance.json").write_text(json.dumps({"comfyfarm_provenance": 1, "job": "j1",
                                                                     "files": names}))
            except OSError:
                pass                                   # Windows without the right to make links
        e = self.entry(self.sweep(), "j1")
        self.assertEqual((e["delete"], e["missing"]), (["plate.wan_v001.1001.png"], ["never_written.png"]))
        self.assertEqual(len([p for p in e["problems"] if "not in the job's folder" in p]), 4)
        self.assertTrue(any(p.startswith("sub: not a plain file") for p in e["problems"]))
        if "link.txt" in names:
            self.assertTrue(any(p.startswith("link.txt: not a plain file") for p in e["problems"]))
        self.assertFalse(e["empties_folder"])

    def test_a_texture_set_goes_as_a_whole_with_its_batches_files(self):
        d = self.folder("rockA.chord_v001")
        self.job("s1", folder=d, provenance=False, files=(), kind="set")
        for i, tiles in ((1, ("a.1001.exr", "a.1002.exr")), (2, ("a.1003.exr",))):
            self.job(f"b{i}", folder=d, files=tiles, kind="batch", parent_id="s1")
            os.replace(f"{d}/b{i}.provenance.json", f"{d}/provenance.00{i}.json")
            self.store.update(f"b{i}", outputs={"output_dir": d, "provenance": f"{d}/provenance.00{i}.json"})
        r = self.sweep()
        self.assertEqual([e["job"] for e in r["jobs"]], ["s1"])                        # batches aren't listed alone
        e = r["jobs"][0]
        self.assertEqual((e["kind"], e["records"], e["delete"], e["provenance"], e["empties_folder"]),
                         ("set", 3, ["a.1001.exr", "a.1002.exr", "a.1003.exr"],
                          ["provenance.001.json", "provenance.002.json"], True))

    def test_jobs_that_havent_finished_are_never_listed(self):
        self.job("running", status="running", finished_at=None)
        self.assertEqual(self.sweep()["jobs"], [])

    def test_the_report_as_people_read_it(self):
        self.job("j1")
        self.job("j2", provenance=False)
        out = SW.text(self.sweep())
        self.assertIn("a dry run, nothing was deleted", out)
        self.assertIn("2 expired job(s): 3 file(s), 20 bytes, and 2 record(s) would go", out)
        self.assertIn("    plate.wan_v001.1002.png", out)
        self.assertIn("  ! No provenance file", out)


class Daily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = Config.model_validate({"database": f"{self.tmp}/data/farm.db", "workers": [],
                                          "retention": {"enabled": True, "at": "03:00", "reports": 2}})
        self.store = JobStore(Path(self.cfg.database))
        self.sweeper = SW.Sweeper(self.cfg, self.store)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reports_saved_listed_and_only_the_last_few_kept(self):
        day = datetime(2026, 11, 23, 3, 0).timestamp()
        self.sweeper.run(day)
        self.sweeper.run(day + 3600)                                      # asked for again that day: its own file
        self.sweeper.run(day + DAY)
        listed = self.sweeper.reports()
        self.assertEqual([r["name"] for r in listed], ["2026-11-24", "2026-11-23T040000"])     # reports: 2
        self.assertEqual((listed[0]["dry_run"], listed[0]["expired"]), (True, 0))
        self.assertEqual(self.sweeper.report("latest")["at"], listed[0]["at"])
        self.assertEqual(self.sweeper.report("2026-11-23T040000")["comfyfarm_sweep"], 1)
        self.assertIsNone(self.sweeper.report("2026-11-23"))
        self.assertEqual(sorted(os.listdir(Path(self.tmp) / "data" / "sweeps")),
                         ["2026-11-23T040000.json", "2026-11-24.json"])

    def test_once_a_day_at_its_time_and_now_if_todays_was_missed(self):
        at3 = datetime(2026, 11, 23, 3, 0)
        self.assertEqual(self.sweeper.next_run((at3 - timedelta(hours=2)).timestamp()), at3.timestamp())
        late = (at3 + timedelta(hours=7)).timestamp()
        self.assertEqual(self.sweeper.next_run(late), late)              # the manager was down at 03:00
        self.sweeper.run(late)
        self.assertEqual(self.sweeper.next_run(late + 60), (at3 + timedelta(days=1)).timestamp())

    def test_a_time_of_day_it_can_read(self):
        for bad in ("3am", "25:00", "03:7", "0300"):
            with self.assertRaisesRegex(ValueError, "retention.at is a time of day"):
                Config.model_validate({"workers": [], "retention": {"at": bad}})


if __name__ == "__main__":
    unittest.main()
