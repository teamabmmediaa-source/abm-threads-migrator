"""Sync Instagram videos into a queue and publish them to Threads, one per slot.

Item lifecycle::

    queued ──(slot due + approved)──> processing ──> published
       ▲                                  │
       └──── retry with back-off ◄────────┤ (transient error, attempts < max)
                                          └──> failed   (permanent error or attempts exhausted)
    excluded  (manually removed from the migration)

Duplicate protection:
  * one state entry per Instagram media id; published items are never queued again;
  * a slot is marked used as soon as a Threads container exists for it;
  * a container id is stored before publishing, and an unclear outcome is
    resolved by looking for the post on the Threads profile before re-trying;
  * the GitHub workflow runs with a concurrency lock, so two runs never overlap.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import InstagramClient, MetaError, ThreadsClient
from .schedule import Slots, approved_since, eligible, parse, project, queue, slot_key

ROOT = Path(__file__).resolve().parent.parent


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def log(msg: str) -> None:
    print(f"[{now_utc():%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


# ---------------------------------------------------------------- config/state
def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def empty_state() -> dict:
    return {"version": 1, "approval": {"approved": False, "approved_at": None, "approved_preview": []},
            "items": {}, "used_slots": [], "alerts": [], "last_sync": None, "last_run": None,
            "accounts": {}, "tokens": {}}


def load_state(cfg: dict) -> dict:
    p = ROOT / cfg["state_path"]
    if not p.exists():
        return empty_state()
    state = json.loads(p.read_text(encoding="utf-8"))
    for k, v in empty_state().items():
        state.setdefault(k, v)
    return state


def save_state(cfg: dict, state: dict) -> None:
    p = ROOT / cfg["state_path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, p)  # atomic: a crash never leaves a half-written state file


def alert(state: dict, msg: str) -> None:
    log("ALERT " + msg)
    state["alerts"] = ([{"at": iso(now_utc()), "message": msg}] + state["alerts"])[:20]


def history(item: dict, event: str) -> None:
    item.setdefault("history", []).append({"at": iso(now_utc()), "event": event})
    item["history"] = item["history"][-15:]


# ---------------------------------------------------------------- captions
def threads_text(caption: str | None, limit: int) -> tuple[str, bool]:
    text = (caption or "").strip()
    if len(text) <= limit:
        return text, False
    cut = text[: limit - 1]
    if " " in cut[-60:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip() + "…", True


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


# ---------------------------------------------------------------- sync
def is_video(m: dict, cfg: dict) -> bool:
    if m.get("media_type") != "VIDEO":
        return False
    return m.get("media_product_type") == "REELS" or cfg["include_feed_videos"]


def sync(state: dict, cfg: dict, ig: InstagramClient) -> int:
    media = [m for m in ig.list_media() if is_video(m, cfg)]
    added = 0
    for m in media:
        item = state["items"].get(m["id"])
        text, trimmed = threads_text(m.get("caption"), cfg["caption_limit"])
        if item is None:
            hold = state["approval"]["approved"] and not cfg["auto_schedule_new"]
            item = state["items"][m["id"]] = {
                "ig_id": m["id"], "status": "excluded" if hold else "queued", "attempts": 0,
                "discovered_at": iso(now_utc()), "new_after_approval": state["approval"]["approved"],
            }
            history(item, "discovered on Instagram" + (" (held: auto_schedule_new is off)" if hold else ""))
            added += 1
        if item["status"] in ("queued", "failed", "excluded"):  # keep caption edits until it is published
            item.update(caption=m.get("caption") or "", text=text, caption_trimmed=trimmed)
        item.update(permalink=m.get("permalink"), timestamp=m["timestamp"],
                    product=m.get("media_product_type"), thumbnail=m.get("thumbnail_url"))
    state["last_sync"] = iso(now_utc())
    log(f"sync: {len(media)} videos on Instagram, {added} new")
    return added


# ---------------------------------------------------------------- publishing
def _fail(state: dict, cfg: dict, item: dict, err: str, permanent: bool) -> None:
    item["attempts"] = item.get("attempts", 0) + 1
    item["last_error"] = err
    item.pop("container_id", None)
    if permanent or item["attempts"] >= cfg["max_attempts"]:
        item["status"] = "failed"
        item["next_retry_at"] = None
        history(item, f"FAILED after {item['attempts']} attempt(s): {err}")
    else:
        wait = cfg["retry_base_minutes"] * 2 ** (item["attempts"] - 1)
        item["status"] = "queued"
        item["next_retry_at"] = iso(now_utc() + timedelta(minutes=wait))
        history(item, f"attempt {item['attempts']} failed, retry after {wait} min: {err}")
    log(f"{item['ig_id']}: {item['history'][-1]['event']}")


def _mark_published(item: dict, post_id: str, permalink: str | None) -> None:
    item.update(status="published", threads_id=post_id, threads_permalink=permalink,
                published_at=iso(now_utc()), next_retry_at=None, last_error=None)
    history(item, f"published to Threads ({post_id})")
    log(f"{item['ig_id']}: published as {post_id}")


def _find_existing_post(state: dict, th: ThreadsClient, item: dict) -> dict | None:
    """Look for a Threads post that already carries this video (guards against double posting)."""
    since = parse(item.get("container_created_at")) or parse(item.get("discovered_at"))
    want = _norm(item.get("text"))
    taken = {i.get("threads_id") for i in state["items"].values() if i.get("threads_id")}
    for p in th.recent_posts():
        if p.get("id") in taken or p.get("media_type") not in (None, "VIDEO"):
            continue
        ts = parse(p.get("timestamp"))
        if since and ts and ts < since - timedelta(minutes=5):
            continue
        if want and _norm(p.get("text"))[:120] == want[:120]:
            return p
        if not want and not p.get("text"):
            return p
    return None


def _finish_container(state: dict, cfg: dict, th: ThreadsClient, item: dict, status: dict) -> None:
    st = status.get("status")
    if st == "FINISHED":
        try:
            post_id = th.publish(item["container_id"])
        except MetaError as e:
            if e.auth:
                raise
            # The publish call may have succeeded even though we got an error back.
            existing = _find_existing_post(state, th, item)
            if existing:
                _mark_published(item, existing["id"], existing.get("permalink"))
                return
            return _fail(state, cfg, item, f"publish: {e}", permanent=False)
        try:
            link = th.permalink(post_id)
        except MetaError:
            link = None
        _mark_published(item, post_id, link)
    elif st == "PUBLISHED":
        existing = _find_existing_post(state, th, item)
        if existing:
            _mark_published(item, existing["id"], existing.get("permalink"))
        else:
            item["status"] = "failed"
            history(item, "container reports PUBLISHED but the post was not found; check Threads manually")
    elif st == "IN_PROGRESS":
        age = now_utc() - (parse(item.get("container_created_at")) or now_utc())
        if age > timedelta(hours=cfg["container_timeout_hours"]):
            _fail(state, cfg, item, "Threads video processing timed out", permanent=False)
        else:
            log(f"{item['ig_id']}: Threads is still processing the video, will check next run")
    else:  # ERROR / EXPIRED / unknown
        _fail(state, cfg, item, f"Threads container {st}: {status.get('error_message') or 'no details'}",
              permanent=False)


def publish_one(state: dict, cfg: dict, ig: InstagramClient, th: ThreadsClient, item: dict, slot) -> None:
    log(f"{item['ig_id']}: publishing for slot {slot_key(slot)} (attempt {item.get('attempts', 0) + 1})")
    try:
        url = ig.video_url(item["ig_id"])
    except MetaError as e:
        if e.auth:
            raise
        return _fail(state, cfg, item, f"Instagram video URL: {e}", permanent=not e.transient)
    if not url:
        return _fail(state, cfg, item, "Instagram did not return a video file (often a reel with licensed "
                                       "music). Publish it manually or exclude it.", permanent=True)
    if item.get("attempts", 0) > 0:  # an earlier attempt may have gone through after all
        existing = _find_existing_post(state, th, item)
        if existing:
            return _mark_published(item, existing["id"], existing.get("permalink"))
    try:
        cid = th.create_video_container(url, item.get("text", ""))
    except MetaError as e:
        if e.auth:
            raise
        return _fail(state, cfg, item, f"create container: {e}", permanent=not e.transient)
    item.update(status="processing", container_id=cid, container_created_at=iso(now_utc()), slot=slot_key(slot))
    state["used_slots"] = sorted(set(state["used_slots"]) | {slot_key(slot)})[-500:]
    history(item, f"Threads container {cid} created for slot {slot_key(slot)}")
    save_state(cfg, state)  # persist the container id before waiting
    _finish_container(state, cfg, th, item, th.wait_until_ready(cid, max_wait=cfg["processing_wait_seconds"]))


def run(state: dict, cfg: dict, ig: InstagramClient, th: ThreadsClient, now: datetime | None = None) -> None:
    now = now or now_utc()
    state["last_run"] = iso(now)
    try:
        sync(state, cfg, ig)
        save_state(cfg, state)

        # 1) finish anything a previous run left half-way
        for item in [i for i in state["items"].values() if i["status"] == "processing"]:
            _finish_container(state, cfg, th, item, th.container_status(item["container_id"]))
            save_state(cfg, state)

        # 2) publish at most one video in the current slot
        if not state["approval"]["approved"]:
            log("schedule not approved yet: nothing will be published")
            return
        if any(i["status"] == "processing" for i in state["items"].values()):
            log("a video is still processing on Threads; waiting")
            return
        slot = Slots(cfg).due_slot(now, set(state["used_slots"]), approved_since(state))
        if slot is None:
            log("no publishing slot due right now")
            return
        item = next((i for i in queue(state) if eligible(i, now)), None)
        if item is None:
            log("slot due, but nothing is ready to publish")
            return
        publish_one(state, cfg, ig, th, item, slot)
    except MetaError as e:
        if e.auth:
            alert(state, f"Meta rejected an access token ({e}). Reconnect the account and update the secret.")
        else:
            alert(state, f"Run stopped by an API error, will try again next run: {e}")
    finally:
        save_state(cfg, state)


# ---------------------------------------------------------------- admin actions
def approve(state: dict, cfg: dict) -> list:
    if not queue(state):
        raise SystemExit("Nothing is queued. Run `sync` (or `verify`) first.")
    # Publishing starts at the first slot AFTER approval; earlier slots are never caught up.
    state["approval"] = {"approved": True, "approved_at": iso(now_utc()), "approved_preview": []}
    preview = [{"ig_id": i["ig_id"], "slot": slot_key(s)} for i, s in project(state, cfg, now_utc(), limit=30)]
    state["approval"]["approved_preview"] = preview
    for item in queue(state):
        history(item, "schedule approved")
    return preview


def unapprove(state: dict) -> None:
    state["approval"]["approved"] = False


def retry_failed(state: dict) -> int:
    n = 0
    for item in state["items"].values():
        if item["status"] == "failed":
            item.update(status="queued", attempts=0, next_retry_at=None)
            history(item, "manually re-queued")
            n += 1
    return n


def set_excluded(state: dict, ig_id: str, excluded: bool) -> None:
    item = state["items"].get(ig_id)
    if not item:
        raise SystemExit(f"Unknown Instagram media id {ig_id}")
    if item["status"] in ("published", "processing"):
        raise SystemExit(f"{ig_id} is {item['status']}; it can no longer be excluded")
    item["status"] = "excluded" if excluded else "queued"
    history(item, "excluded manually" if excluded else "re-included manually")
