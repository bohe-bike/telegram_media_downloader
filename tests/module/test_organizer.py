"""Tests for the post-download organizer."""
import os
import tempfile
import unittest
from unittest import mock

from module import organizer


class OrganizerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name
        self.downloads = os.path.join(self.root, "downloads")
        self.sync_root = os.path.join(self.root, "library")
        os.makedirs(self.downloads, exist_ok=True)
        os.makedirs(self.sync_root, exist_ok=True)

        self.config = organizer.OrganizeConfig()
        self.config.enable = True
        self.config.sync_root = self.sync_root
        self.config.backup_dir = ".backup"
        self.config.plan_file = os.path.join(self.root, "plan.json")
        self.config.scan_library_roots = False
        self.config.auto_learn_aliases = False
        organizer.configure(self.config)
        organizer.reset()

    def tearDown(self):
        organizer.reset()
        self.temp_dir.cleanup()

    def _touch(self, path, content=b"data"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as file:
            file.write(content)
        return path

    def test_clean_author_removes_source_markers(self):
        self.assertEqual(
            "Alice",
            organizer.clean_author(
                "TG@Jingluoasmr Alice ASMR圖書館",
                ["ASMR圖書館"],
            ),
        )

    def test_plan_uses_audio_tags(self):
        song = self._touch(os.path.join(self.downloads, "song.mp3"))
        organizer.enqueue_downloaded_file(song)

        with mock.patch.object(
            organizer,
            "_read_audio_tags",
            return_value={"artist": "TG@Jingluoasmr Alice", "album": "ASMR圖書館"},
        ):
            plan = organizer.build_plan()

        entry = plan["entries"][0]
        self.assertEqual("Alice", entry["author"])
        self.assertEqual(
            os.path.join(self.sync_root, "mp3", "Alice", "song.mp3"),
            entry["target"],
        )
        self.assertEqual("move", entry["action"])

    def test_unknown_author_goes_to_pending(self):
        song = self._touch(os.path.join(self.downloads, "mystery.mp3"))
        organizer.enqueue_downloaded_file(song)

        with mock.patch.object(organizer, "_read_audio_tags", return_value={}):
            plan = organizer.build_plan()

        entry = plan["entries"][0]
        self.assertEqual("", entry["author"])
        self.assertEqual(
            os.path.join(self.sync_root, "未整理", "mystery.mp3"),
            entry["target"],
        )

    def test_plan_scans_download_root_after_restart(self):
        self.config.scan_download_root = True
        self.config.download_root = self.downloads
        nested = self._touch(
            os.path.join(self.downloads, "频道A", "2026-11-10", "song.mp3")
        )

        with mock.patch.object(
            organizer, "_read_audio_tags", return_value={"artist": "Alice"}
        ):
            plan = organizer.build_plan()

        sources = [entry["source"] for entry in plan["entries"]]
        self.assertEqual([nested], sources)
        self.assertEqual("频道A", plan["entries"][0]["channel_folder"])

    def test_video_shared_prefix_is_scoped_per_channel_folder(self):
        self.config.scan_download_root = True
        self.config.download_root = self.downloads
        for name in ("Alice -01.mp4", "Alice -02.mp4"):
            self._touch(os.path.join(self.downloads, "频道A", name))
        for name in ("Bob -01.mp4", "Bob -02.mp4"):
            self._touch(os.path.join(self.downloads, "频道B", name))

        plan = organizer.build_plan()

        authors = {
            (entry["channel_folder"], entry["filename"]): entry["author"]
            for entry in plan["entries"]
        }
        self.assertEqual("Alice", authors[("频道A", "Alice -01.mp4")])
        self.assertEqual("Alice", authors[("频道A", "Alice -02.mp4")])
        self.assertEqual("Bob", authors[("频道B", "Bob -01.mp4")])
        self.assertEqual("Bob", authors[("频道B", "Bob -02.mp4")])

    def test_execute_plan_only_selected_entries(self):
        first = self._touch(os.path.join(self.downloads, "first.mp3"), b"first")
        second = self._touch(os.path.join(self.downloads, "second.mp3"), b"second")
        organizer.enqueue_downloaded_file(first)
        organizer.enqueue_downloaded_file(second)

        with mock.patch.object(organizer, "MutagenFile", object()), mock.patch.object(
            organizer, "_read_audio_tags", return_value={"artist": "Alice"}
        ), mock.patch.object(
            organizer, "_write_audio_tags", return_value=(True, "")
        ), mock.patch.object(
            organizer, "_verify_audio", return_value=(True, "")
        ):
            plan = organizer.build_plan()
            selected = plan["entries"][0]["id"]
            result = organizer.execute_plan(entry_ids=[selected])

        statuses = sorted(entry["status"] for entry in result["plan"]["entries"])
        self.assertEqual(["done", "planned"], statuses)
        self.assertTrue(
            os.path.isfile(os.path.join(self.sync_root, "mp3", "Alice", "first.mp3"))
        )

    def test_video_matches_existing_author_directory(self):
        os.makedirs(os.path.join(self.sync_root, "mp4", "Alice"))
        video = self._touch(os.path.join(self.downloads, "Alice - live.mp4"))
        organizer.enqueue_downloaded_file(video)

        plan = organizer.build_plan()

        entry = plan["entries"][0]
        self.assertEqual("Alice", entry["author"])
        self.assertEqual(
            os.path.join(self.sync_root, "mp4", "Alice", "Alice - live.mp4"),
            entry["target"],
        )

    def test_video_bracket_author_is_used(self):
        video = self._touch(os.path.join(self.downloads, "[Alice] live.mp4"))
        organizer.enqueue_downloaded_file(video)

        plan = organizer.build_plan()

        self.assertEqual("Alice", plan["entries"][0]["author"])

    def test_execute_moves_file_and_creates_backup(self):
        song = self._touch(os.path.join(self.downloads, "song.mp3"), b"audio-data")
        organizer.enqueue_downloaded_file(song)

        with mock.patch.object(organizer, "MutagenFile", object()), mock.patch.object(
            organizer, "_read_audio_tags", return_value={"artist": "Alice"}
        ), mock.patch.object(
            organizer, "_write_audio_tags", return_value=(True, "")
        ), mock.patch.object(
            organizer, "_verify_audio", return_value=(True, "")
        ):
            organizer.build_plan()
            result = organizer.execute_plan()

        self.assertTrue(result["ok"])
        target = os.path.join(self.sync_root, "mp3", "Alice", "song.mp3")
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(song))
        entry = result["plan"]["entries"][0]
        self.assertEqual("done", entry["status"])
        self.assertTrue(os.path.isfile(entry["backup"]))
        self.assertTrue(os.path.isfile(self.config.plan_file))

    def test_execute_does_not_overwrite_existing_target(self):
        song = self._touch(os.path.join(self.downloads, "song.mp3"), b"new")
        target = self._touch(
            os.path.join(self.sync_root, "mp3", "Alice", "song.mp3"), b"old"
        )
        organizer.enqueue_downloaded_file(song)

        with mock.patch.object(
            organizer, "_read_audio_tags", return_value={"artist": "Alice"}
        ):
            organizer.build_plan()
            result = organizer.execute_plan()

        entry = result["plan"]["entries"][0]
        self.assertEqual("skipped", entry["status"])
        self.assertTrue(os.path.isfile(song))
        with open(target, "rb") as existing:
            self.assertEqual(b"old", existing.read())

    def test_tag_failure_restores_source_file(self):
        song = self._touch(os.path.join(self.downloads, "song.mp3"), b"audio-data")
        organizer.enqueue_downloaded_file(song)

        with mock.patch.object(organizer, "MutagenFile", object()), mock.patch.object(
            organizer, "_read_audio_tags", return_value={"artist": "Alice"}
        ), mock.patch.object(
            organizer, "_write_audio_tags", return_value=(False, "boom")
        ):
            organizer.build_plan()
            result = organizer.execute_plan()

        entry = result["plan"]["entries"][0]
        self.assertEqual("failed", entry["status"])
        self.assertTrue(os.path.isfile(song))
        self.assertFalse(
            os.path.exists(os.path.join(self.sync_root, "mp3", "Alice", "song.mp3"))
        )

    def test_missing_mutagen_still_moves_with_warning(self):
        song = self._touch(os.path.join(self.downloads, "song.mp3"), b"audio-data")
        organizer.enqueue_downloaded_file(song)

        with mock.patch.object(organizer, "MutagenFile", None), mock.patch.object(
            organizer, "_read_audio_tags", return_value={"artist": "Alice"}
        ):
            plan = organizer.build_plan()
            self.assertTrue(plan["warning"])
            result = organizer.execute_plan()

        entry = result["plan"]["entries"][0]
        self.assertEqual("done", entry["status"])
        self.assertIn("mutagen", entry["message"])
        self.assertTrue(
            os.path.isfile(os.path.join(self.sync_root, "mp3", "Alice", "song.mp3"))
        )
