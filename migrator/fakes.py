"""In-memory stand-ins for the Meta APIs, used by the tests and `demo` (no network, no real posting)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .api import MetaError


class FakeInstagram:
    def __init__(self, n: int = 12, start: datetime | None = None):
        start = start or datetime(2024, 1, 5, 18, 0, tzinfo=timezone.utc)
        self.media = []
        for k in range(n):
            ts = start + timedelta(days=9 * k)
            self.media.append({
                "id": f"1790000000000{k:04d}", "media_type": "VIDEO",
                "media_product_type": "REELS" if k % 4 else "FEED",
                "caption": f"ABM Media clip #{k + 1} 🎬 " + ("long caption " * 60 if k == 3 else "#abmmedia"),
                "permalink": f"https://www.instagram.com/reel/DEMO{k:03d}/",
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            })
        self.media.append({"id": "17900000009999", "media_type": "IMAGE", "timestamp": "2024-02-01T10:00:00+0000"})
        self.no_url: set[str] = set()

    def profile(self):
        return {"user_id": "1", "username": "abm.media (demo)"}

    def list_media(self):
        return sorted(self.media, key=lambda m: m["timestamp"], reverse=True)  # newest first, like the API

    def video_url(self, media_id):
        return None if media_id in self.no_url else f"https://cdn.example/{media_id}.mp4"


class FakeThreads:
    def __init__(self):
        self.posts: list[dict] = []
        self.containers: dict[str, dict] = {}
        self.fail_next_create: list[MetaError] = []
        self.fail_next_publish_after_success = False
        self.status_sequence: list[str] = []

    def profile(self):
        return {"id": "2", "username": "abm.media (demo)"}

    def create_video_container(self, video_url, text):
        if self.fail_next_create:
            raise self.fail_next_create.pop(0)
        cid = f"c{len(self.containers) + 1}"
        self.containers[cid] = {"url": video_url, "text": text, "status": "FINISHED"}
        return cid

    def container_status(self, cid):
        if self.status_sequence:
            self.containers[cid]["status"] = self.status_sequence.pop(0)
        return {"id": cid, "status": self.containers[cid]["status"]}

    def wait_until_ready(self, cid, max_wait=0, interval=0):
        return self.container_status(cid)

    def publish(self, cid):
        c = self.containers[cid]
        if c["status"] == "PUBLISHED":
            raise MetaError("already published", transient=False, code=100)
        pid = f"p{len(self.posts) + 1}"
        self.posts.append({"id": pid, "text": c["text"], "media_type": "VIDEO",
                           "timestamp": datetime.now(timezone.utc).isoformat(),
                           "permalink": f"https://www.threads.net/@demo/post/{pid}"})
        c["status"] = "PUBLISHED"
        if self.fail_next_publish_after_success:
            self.fail_next_publish_after_success = False
            raise MetaError("HTTP 500: timeout", transient=True)
        return pid

    def permalink(self, pid):
        return f"https://www.threads.net/@demo/post/{pid}"

    def recent_posts(self, limit=50):
        return list(reversed(self.posts))[:limit]
