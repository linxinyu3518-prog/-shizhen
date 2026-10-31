# SPDX-License-Identifier: GPL-3.0-or-later
"""Relocated installations retain history without trusting old download paths."""
import copy
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from framekeep.config import Settings
from framekeep.jobs import JobManager
from framekeep.network import MediaError


class MigratedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(data_dir=self.root)

    def record(self, identifier="a" * 32, filename="fixture-H264-120fps.mp4", status="completed"):
        return {"id": identifier, "title": "Migration fixture", "status": status,
                "kind": "video", "container": "mp4", "height": 360,
                "video_quality": "high", "fps": 120, "frame_rate_mode": "interpolate",
                "created_at": "2026-09-07T00:00:00+00:00", "filename": filename,
                "download_url": f"/api/jobs/{identifier}/download", "progress": 100,
                "stage": "Completed fixture", "message": "Existing result",
                "result": {"fps": 120, "width": 640, "height": 360, "size": 13},
                "_path": "C:/old-framekeep/data/downloads/" + str(identifier) + "/fixture.mp4"}

    def seed(self, records):
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as connection:
            connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, record TEXT NOT NULL)")
            connection.executemany("INSERT INTO jobs VALUES (?, ?)",
                                   [(record["id"], json.dumps(record)) for record in records])
            connection.commit()

    def open_manager(self):
        manager = JobManager(self.settings, Mock())
        self.addCleanup(manager.close)
        return manager

    def save_local_file(self, record):
        path = self.root / "downloads" / record["id"] / record["filename"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture-media")
        return path.resolve()

    def assert_unavailable_and_unchanged(self, manager, record):
        self.assertEqual(manager.jobs[record["id"]], record)
        with self.assertRaises(MediaError):
            manager.download_path(record["id"])

    def test_existing_file_relocates_and_preserves_every_other_history_field(self):
        record = self.record()
        original = copy.deepcopy(record)
        destination = self.save_local_file(record)
        self.seed([record])
        manager = self.open_manager()

        expected = {**original, "_path": str(destination)}
        self.assertEqual(manager.jobs[record["id"]], expected)
        self.assertEqual(manager.download_path(record["id"]), destination)
        self.assertEqual(manager.download_path(record["id"]).read_bytes(), b"fixture-media")
        public = {key: value for key, value in original.items() if not key.startswith("_")}
        self.assertEqual(manager.list(), [public])
        with closing(sqlite3.connect(self.root / "jobs.sqlite3")) as connection:
            persisted = json.loads(connection.execute("SELECT record FROM jobs WHERE id = ?",
                                                      (record["id"],)).fetchone()[0])
        self.assertEqual(persisted, expected)

    def test_missing_destination_does_not_rewrite_record(self):
        record = self.record()
        self.seed([record])
        self.assert_unavailable_and_unchanged(self.open_manager(), record)

    def test_same_filename_in_another_job_directory_is_not_used(self):
        record = self.record()
        self.save_local_file(self.record(identifier="b" * 32))
        self.seed([record])
        self.assert_unavailable_and_unchanged(self.open_manager(), record)

    def test_invalid_ids_are_not_relocated(self):
        records = [self.record(identifier=value) for value in
                   ("short-id", "a" * 31, "a" * 33, "g" * 32, "../escape", "nested/job")]
        # A real file under an invalid, but filesystem-safe ID must still be refused.
        self.save_local_file(records[0])
        self.seed(records)
        manager = self.open_manager()
        for record in records:
            with self.subTest(identifier=record["id"]):
                self.assert_unavailable_and_unchanged(manager, record)

    def test_invalid_filenames_do_not_escape_or_rewrite_history(self):
        filenames = ("../outside.mp4", "sub/fixture.mp4", "sub\\fixture.mp4",
                     "/absolute.mp4", "C:\\secret.mp4", ".", "..", "", None, ["fixture.mp4"])
        records = [self.record(identifier=f"{index:032x}", filename=filename)
                   for index, filename in enumerate(filenames, 1)]
        # An existing traversal target makes this stronger than a missing-file check.
        outside = self.root / "downloads" / "outside.mp4"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"unrelated-file")
        self.seed(records)
        manager = self.open_manager()
        for record in records:
            with self.subTest(filename=record["filename"]):
                self.assert_unavailable_and_unchanged(manager, record)
        self.assertEqual(outside.read_bytes(), b"unrelated-file")

    def test_failed_and_cancelled_records_are_not_relocated(self):
        records = [self.record(identifier=f"{index:032x}", status=status)
                   for index, status in enumerate(("failed", "cancelled"), 1)]
        for record in records:
            self.save_local_file(record)
        self.seed(records)
        manager = self.open_manager()
        for record in records:
            with self.subTest(status=record["status"]):
                self.assert_unavailable_and_unchanged(manager, record)

    def test_current_location_is_unchanged(self):
        record = self.record()
        destination = self.save_local_file(record)
        record["_path"] = str(destination)
        self.seed([record])
        manager = self.open_manager()
        self.assertEqual(manager.jobs[record["id"]], record)
        self.assertEqual(manager.download_path(record["id"]), destination)


if __name__ == "__main__":
    unittest.main()
