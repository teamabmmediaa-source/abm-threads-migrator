"""Offline tests: fake Meta APIs + a controllable clock. Run:  python -m unittest -v"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from migrator import dashboard, runner
from migrator.api import MetaError
from migrator.fakes import FakeInstagram, FakeThreads
from migrator.schedule import Slots, project

CASA = ZoneInfo("Africa/Casablanca")


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CASA).astimezone(timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = {**runner.load_config(), "state_path": str(Path(self.tmp) / "state.json")}
        self.clock = [at(2026, 11, 24, 9, 0)]
        self._orig = runner.now_utc
        runner.now_utc = lambda: self.clock[0]
        self.ig, self.th = FakeInstagram(6), FakeThreads()
        self.state = runner.empty_state()

    def tearDown(self):
        runner.now_utc = self._orig

    def go(self, when):
        self.clock[0] = when
        runner.run(self.state, self.cfg, self.ig, self.th, now=when)

    def status(self):
        return {i["ig_id"]: i["status"] for i in self.state["items"].values()}

    def ids_oldest_first(self):
        return [m["id"] for m in sorted(self.ig.media, key=lambda m: m["timestamp"]) if m["media_type"] == "VIDEO"]


class TestMigration(Base):
    def test_sync_videos_only_and_caption_limit(self):
        runner.sync(self.state, self.cfg, self.ig)
        self.assertEqual(len(self.state["items"]), 6)  # the IMAGE is ignored
        long_item = self.state["items"][self.ig.media[3]["id"]]
        self.assertTrue(long_item["caption_trimmed"])
        self.assertLessEqual(len(long_item["text"]), 500)

    def test_nothing_published_before_approval(self):
        self.go(at(2026, 11, 24, 10, 5))
        self.go(at(2026, 11, 24, 14, 5))
        self.assertEqual(self.th.posts, [])
        self.assertEqual(set(self.status().values()), {"queued"})

    def test_oldest_first_one_per_slot_no_duplicates(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)          # approved at 09:00
        self.go(at(2026, 11, 24, 9, 50))              # before 10:00: nothing
        self.assertEqual(len(self.th.posts), 0)
        for t in [(10, 5), (10, 20), (11, 35)]:       # three runs in the 10:00 window
            self.go(at(2026, 11, 24, *t))
        self.assertEqual(len(self.th.posts), 1)
        self.go(at(2026, 11, 24, 14, 5))
        self.go(at(2026, 11, 24, 20, 5))
        self.go(at(2026, 11, 24, 20, 50))
        self.assertEqual(len(self.th.posts), 3)
        published = sorted((i for i in self.state["items"].values() if i["status"] == "published"),
                           key=lambda i: i["published_at"])
        self.assertEqual([i["ig_id"] for i in published], self.ids_oldest_first()[:3])
        self.assertEqual(self.th.posts[0]["text"], published[0]["text"])  # caption carried over
        self.assertTrue(self.th.containers["c1"]["url"].endswith(".mp4"))  # real video file, not a link

    def test_slots_before_approval_are_not_caught_up(self):
        runner.sync(self.state, self.cfg, self.ig)
        self.clock[0] = at(2026, 11, 24, 10, 30)
        runner.approve(self.state, self.cfg)          # approved after 10:00 slot started
        self.go(at(2026, 11, 24, 10, 35))
        self.assertEqual(len(self.th.posts), 0)
        self.go(at(2026, 11, 24, 14, 5))
        self.assertEqual(len(self.th.posts), 1)

    def test_missed_window_is_skipped_not_burst(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)
        self.go(at(2026, 11, 24, 16, 30))  # 10:00 is outside the 3 h grace, 14:00 is inside
        self.go(at(2026, 11, 24, 16, 45))
        self.assertEqual(len(self.th.posts), 1)

    def test_transient_failure_retries_without_losing_slot(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)
        first, second = self.ids_oldest_first()[:2]
        self.th.fail_next_create = [MetaError("HTTP 500", transient=True)]
        self.go(at(2026, 11, 24, 10, 5))
        self.assertEqual(self.state["items"][first]["status"], "queued")
        self.assertEqual(self.state["items"][first]["attempts"], 1)
        self.go(at(2026, 11, 24, 10, 20))  # same slot, first is backing off → next video goes
        self.assertEqual(self.state["items"][second]["status"], "published")
        self.go(at(2026, 11, 24, 14, 5))   # back-off over → the failed one is retried
        self.assertEqual(self.state["items"][first]["status"], "published")
        self.assertEqual(len(self.th.posts), 2)

    def test_publish_error_after_success_is_not_duplicated(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)
        self.th.fail_next_publish_after_success = True
        self.go(at(2026, 11, 24, 10, 5))
        self.assertEqual(len(self.th.posts), 1)
        first = self.ids_oldest_first()[0]
        self.assertEqual(self.state["items"][first]["status"], "published")
        self.go(at(2026, 11, 24, 14, 5))
        self.assertEqual(len(self.th.posts), 2)
        self.assertEqual(len({p["text"] for p in self.th.posts}), 2)

    def test_gives_up_after_max_attempts_then_manual_retry(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)
        first = self.ids_oldest_first()[0]
        item = self.state["items"][first]
        for _ in range(self.cfg["max_attempts"]):
            runner._fail(self.state, self.cfg, item, "boom", permanent=False)
        self.assertEqual(item["status"], "failed")
        self.assertEqual(runner.retry_failed(self.state), 1)
        self.assertEqual(item["status"], "queued")

    def test_missing_video_file_fails_permanently(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)
        first = self.ids_oldest_first()[0]
        self.ig.no_url.add(first)
        self.go(at(2026, 11, 24, 10, 5))
        self.assertEqual(self.state["items"][first]["status"], "failed")
        self.go(at(2026, 11, 24, 10, 20))
        self.assertEqual(len(self.th.posts), 1)  # the next video still used the slot

    def test_slow_processing_resumes_next_run(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)
        self.th.status_sequence = ["IN_PROGRESS", "FINISHED"]
        self.go(at(2026, 11, 24, 10, 5))
        self.assertIn("processing", self.status().values())
        self.go(at(2026, 11, 24, 10, 20))
        self.assertEqual(len(self.th.posts), 1)
        self.assertNotIn("processing", self.status().values())

    def test_new_reel_after_approval_is_scheduled_last(self):
        runner.sync(self.state, self.cfg, self.ig)
        runner.approve(self.state, self.cfg)
        self.ig.media.append({"id": "18000000000001", "media_type": "VIDEO", "media_product_type": "REELS",
                              "caption": "brand new", "permalink": "https://www.instagram.com/reel/NEW/",
                              "timestamp": "2026-11-24T08:00:00+0000"})
        self.go(at(2026, 11, 24, 10, 5))
        plan = project(self.state, self.cfg, self.clock[0])
        self.assertEqual(plan[-1][0]["ig_id"], "18000000000001")

    def test_morocco_time_including_ramadan(self):
        s = Slots(self.cfg)
        # Whatever offset the IANA database gives Morocco on a date (UTC+1, UTC+0, Ramadan
        # changes), the first slot of the day is 10:00 on Moroccan clocks.
        for day in (datetime(2026, 1, 10, tzinfo=timezone.utc), datetime(2026, 3, 1, tzinfo=timezone.utc),
                    datetime(2026, 11, 25, tzinfo=timezone.utc), datetime(2027, 6, 1, tzinfo=timezone.utc)):
            first = next(s.iter_from(day + timedelta(hours=2)))
            self.assertEqual((first.astimezone(CASA).hour, first.astimezone(CASA).minute), (10, 0))

    def test_dashboard_renders(self):
        runner.sync(self.state, self.cfg, self.ig)
        html = dashboard.build(self.state, self.cfg, self.clock[0])
        self.assertIn("Waiting for your approval", html)
        self.assertNotIn("__DATA__", html)


if __name__ == "__main__":
    unittest.main()
