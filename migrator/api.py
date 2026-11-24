"""Thin clients for the official Instagram Graph API and Threads API.

Standard library only (urllib), so nothing has to be installed on the runner.
Access tokens are sent as query/form parameters but are never logged: every
error message passes through ``_scrub`` before it is raised.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

IG_BASE = os.environ.get("IG_API_BASE", "https://graph.instagram.com")
THREADS_BASE = os.environ.get("THREADS_API_BASE", "https://graph.threads.net/v1.0")

# Graph API error codes that are worth retrying later.
TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 368, 613, 80001, 80002}
AUTH_CODES = {102, 190, 463, 467}


class MetaError(Exception):
    def __init__(self, message: str, *, transient: bool, auth: bool = False, code=None):
        super().__init__(message)
        self.transient = transient
        self.auth = auth
        self.code = code


def _scrub(text: str) -> str:
    text = re.sub(r"(access_token|client_secret)=[^&\s\"']+", r"\1=***", text)
    return re.sub(r"\b(IG|TH|EA)[A-Za-z0-9_\-]{30,}", "***", text)


def _request(method: str, url: str, params: dict, timeout: int = 60) -> dict:
    data = None
    encoded = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    if method == "GET":
        url = f"{url}?{encoded}" if encoded else url
    else:
        data = encoded.encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", "threads-migrator/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode() or "{}"
            return json.loads(body)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            err = json.loads(raw).get("error", {})
        except ValueError:
            err = {"message": raw[:300]}
        code = err.get("code")
        msg = _scrub(f"HTTP {e.code}: {err.get('message', 'unknown error')} (code {code}, subcode {err.get('error_subcode')})")
        auth = code in AUTH_CODES or e.code == 401
        transient = (not auth) and (e.code >= 500 or e.code == 429 or code in TRANSIENT_CODES or bool(err.get("is_transient")))
        raise MetaError(msg, transient=transient, auth=auth, code=code) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise MetaError(_scrub(f"Network error: {e}"), transient=True) from None


FB_GRAPH = "https://graph.facebook.com/v21.0"


class InstagramClient:
    """Instagram API, with either kind of token Meta issues:

    * Instagram Login token (``IG…``) -> graph.instagram.com, account = "me".
    * Facebook/system-user token (``EA…``, e.g. from "Unified login") -> graph.facebook.com,
      account = the Instagram business account ID (IG_USER_ID). These never expire.
    """

    def __init__(self, token: str, user_id: str = "me", base: str = IG_BASE):
        self.fb = token.startswith("EA") or "graph.facebook.com" in base
        if self.fb and "graph.instagram.com" in base:
            base = FB_GRAPH
        if self.fb and user_id == "me":
            raise MetaError("This Instagram token is a Facebook/system-user token (EA...): also set the "
                            "IG_USER_ID variable to the Instagram account ID.", transient=False, auth=True)
        self.token, self.user_id, self.base = token, user_id, base.rstrip("/")

    def _get(self, path: str, **params) -> dict:
        return _request("GET", f"{self.base}/{path}", {**params, "access_token": self.token})

    def profile(self) -> dict:
        if self.fb:
            p = self._get(self.user_id, fields="id,username")
            return {"user_id": p.get("id"), "username": p.get("username")}
        return self._get("me", fields="user_id,username")

    def list_media(self) -> list[dict]:
        """All media on the account (the API returns newest first; caller sorts)."""
        fields = "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url"
        out, page = [], self._get(f"{self.user_id}/media", fields=fields, limit=50)
        while True:
            out.extend(page.get("data", []))
            nxt = page.get("paging", {}).get("next")
            if not nxt:
                return out
            # The "next" URL already carries every parameter, token included.
            page = _request("GET", nxt, {})

    def video_url(self, media_id: str) -> str | None:
        """Fresh signed CDN URL of the original video file (they expire, so fetch at publish time)."""
        return self._get(media_id, fields="media_url,media_type").get("media_url")

    def refresh_token(self) -> dict | None:
        if self.fb:
            return None  # system-user tokens do not expire and cannot be refreshed
        return _request("GET", f"{self.base}/refresh_access_token",
                        {"grant_type": "ig_refresh_token", "access_token": self.token})


class ThreadsClient:
    def __init__(self, token: str, user_id: str = "me", base: str = THREADS_BASE):
        self.token, self.user_id, self.base = token, user_id, base.rstrip("/")

    def _get(self, path: str, **params) -> dict:
        return _request("GET", f"{self.base}/{path}", {**params, "access_token": self.token})

    def _post(self, path: str, **params) -> dict:
        return _request("POST", f"{self.base}/{path}", {**params, "access_token": self.token}, timeout=120)

    def profile(self) -> dict:
        return self._get("me", fields="id,username")

    def create_video_container(self, video_url: str, text: str) -> str:
        return self._post(f"{self.user_id}/threads", media_type="VIDEO", video_url=video_url, text=text)["id"]

    def container_status(self, container_id: str) -> dict:
        """status is one of IN_PROGRESS, FINISHED, PUBLISHED, ERROR, EXPIRED."""
        return self._get(container_id, fields="id,status,error_message")

    def publish(self, container_id: str) -> str:
        return self._post(f"{self.user_id}/threads_publish", creation_id=container_id)["id"]

    def permalink(self, post_id: str) -> str | None:
        return self._get(post_id, fields="permalink").get("permalink")

    def recent_posts(self, limit: int = 50) -> list[dict]:
        return self._get(f"{self.user_id}/threads", fields="id,text,timestamp,permalink,media_type",
                         limit=limit).get("data", [])

    def publishing_limit(self) -> dict:
        data = self._get(f"{self.user_id}/threads_publishing_limit", fields="quota_usage,config").get("data", [])
        return data[0] if data else {}

    def refresh_token(self) -> dict:
        return _request("GET", f"{self.base.rsplit('/v', 1)[0]}/refresh_access_token",
                        {"grant_type": "th_refresh_token", "access_token": self.token})

    def wait_until_ready(self, container_id: str, max_wait: int = 300, interval: int = 15) -> dict:
        deadline = time.time() + max_wait
        time.sleep(min(30, max_wait))  # Meta recommends ~30 s before the first check.
        while True:
            st = self.container_status(container_id)
            if st.get("status") != "IN_PROGRESS" or time.time() >= deadline:
                return st
            time.sleep(interval)
