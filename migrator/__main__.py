"""Command line:  python -m migrator <command>

  verify            check both tokens (read-only) and show what would be migrated
  sync              fetch Instagram videos into the queue (never publishes)
  plan              print the upcoming schedule
  approve           approve the schedule — publishing starts at the next slot
  unapprove         pause publishing
  run               sync + publish the video due in the current slot (only if approved)
  retry-failed      put every failed video back in the queue
  exclude ID / include ID
  dashboard         rebuild the HTML dashboard
  refresh-tokens    extend both 60-day tokens (updates GitHub secrets when GH_PAT is set)
  connect-instagram / connect-threads   one-time OAuth helpers to get long-lived tokens
  demo              build a dashboard from fake data (no network)
"""
from __future__ import annotations

import getpass
import os
import secrets
import shutil
import subprocess
import sys
import urllib.parse
from datetime import timedelta

from . import dashboard, runner
from .api import InstagramClient, MetaError, ThreadsClient, _request
from .runner import iso, load_config, load_state, now_utc, save_state
from .schedule import project, slot_key


def _env(name: str, prompt: str | None = None, secret: bool = False) -> str:
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if prompt and sys.stdin.isatty() and not os.environ.get("CI") and not os.environ.get("NO_PROMPT"):
        return (getpass.getpass if secret else input)(prompt).strip()
    raise SystemExit(f"Missing {name}. Set it as an environment variable / GitHub secret (see SETUP.md).")


def clients():
    ig = InstagramClient(_env("IG_ACCESS_TOKEN", "Instagram access token: ", True), os.environ.get("IG_USER_ID") or "me")
    th = ThreadsClient(_env("THREADS_ACCESS_TOKEN", "Threads access token: ", True), os.environ.get("THREADS_USER_ID") or "me")
    return ig, th


def write_dashboard(cfg, state):
    path = runner.ROOT / cfg["dashboard_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dashboard.build(state, cfg, now_utc()), encoding="utf-8")
    (path.parent / ".nojekyll").touch()
    return path


def print_plan(state, cfg, limit=30):
    tz = runner.Slots(cfg).tz
    rows = project(state, cfg, now_utc(), limit=limit)
    total = len(runner.queue(state))
    if not rows:
        print("Nothing queued.")
        return
    print(f"{total} videos queued. First {len(rows)} slots (Morocco time):")
    for item, s in rows:
        cap = (item.get("text") or "(no caption)").replace("\n", " ")[:60]
        print(f"  {s.astimezone(tz):%a %d %b %Y %H:%M}  IG {item['timestamp'][:10]}  {cap}")
    last = project(state, cfg, now_utc())[-1][1]
    print(f"Queue finishes on {last.astimezone(tz):%a %d %b %Y %H:%M}.")


def refresh_tokens(state):
    ig, th = clients()
    out = {}
    for name, client, secret in (("instagram", ig, "IG_ACCESS_TOKEN"), ("threads", th, "THREADS_ACCESS_TOKEN")):
        try:
            res = client.refresh_token()
        except MetaError as e:
            runner.alert(state, f"Could not refresh the {name} token: {e}")
            continue
        token, expires = res["access_token"], int(res.get("expires_in", 5184000))
        state["tokens"][f"{name}_expires_at"] = iso(now_utc() + timedelta(seconds=expires))
        out[secret] = token
    pat, repo = os.environ.get("GH_PAT"), os.environ.get("GITHUB_REPOSITORY")
    if out and pat and repo and shutil.which("gh"):
        for secret, token in out.items():
            subprocess.run(["gh", "secret", "set", secret, "--repo", repo], input=token.encode(), check=True,
                           env={**os.environ, "GH_TOKEN": pat}, stdout=subprocess.DEVNULL)
        print(f"Refreshed and stored: {', '.join(out)}")
    elif out and not os.environ.get("GITHUB_ACTIONS"):
        for secret, token in out.items():
            print(f"\n{secret} (paste into your GitHub secret):\n{token}")
    elif out:
        runner.alert(state, "Tokens were refreshed but GH_PAT is not set, so the new values could not be "
                            "saved. Old tokens still work until their expiry date; add GH_PAT (SETUP.md step 5).")


OAUTH = {
    "instagram": dict(app="IG", authorize="https://www.instagram.com/oauth/authorize",
                      scope="instagram_business_basic", short="https://api.instagram.com/oauth/access_token",
                      long="https://graph.instagram.com/access_token", grant="ig_exchange_token",
                      secret="IG_ACCESS_TOKEN"),
    "threads": dict(app="THREADS", authorize="https://threads.net/oauth/authorize",
                    scope="threads_basic,threads_content_publish", short="https://graph.threads.net/oauth/access_token",
                    long="https://graph.threads.net/access_token", grant="th_exchange_token",
                    secret="THREADS_ACCESS_TOKEN"),
}


def connect(kind: str):
    o = OAUTH[kind]
    app_id = _env(f"{o['app']}_APP_ID", f"{kind.title()} app ID: ")
    app_secret = _env(f"{o['app']}_APP_SECRET", f"{kind.title()} app secret (hidden): ", secret=True)
    redirect = _env("OAUTH_REDIRECT_URI", "Redirect URI registered in the app (https://…): ")
    csrf = secrets.token_urlsafe(16)
    url = o["authorize"] + "?" + urllib.parse.urlencode(
        {"client_id": app_id, "redirect_uri": redirect, "scope": o["scope"], "response_type": "code", "state": csrf})
    print(f"\n1) Open this link while logged in to the ABM Media {kind} account and click Allow:\n\n{url}\n")
    back = input("2) Paste the full address you were redirected to: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(back).query)
    if q.get("state", [""])[0] != csrf:
        raise SystemExit("The 'state' value does not match. Start again (this protects against forged redirects).")
    code = q.get("code", [""])[0].split("#")[0]
    if not code:
        raise SystemExit("No ?code= in that address.")
    short = _request("POST", o["short"], {"client_id": app_id, "client_secret": app_secret,
                                         "grant_type": "authorization_code", "redirect_uri": redirect, "code": code})
    long = _request("GET", o["long"], {"grant_type": o["grant"], "client_secret": app_secret,
                                       "access_token": short["access_token"]})
    days = int(long.get("expires_in", 0)) // 86400
    print(f"\nLong-lived {kind} token (valid ~{days} days). Store it as the GitHub secret {o['secret']},"
          f" then clear this terminal:\n\n{long['access_token']}\n")


def demo():
    import tempfile
    from pathlib import Path
    from .fakes import FakeInstagram, FakeThreads
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp())
    cfg = {**cfg, "state_path": str(tmp / "state.json")}
    ig, th = FakeInstagram(14), FakeThreads()
    ig.no_url.add(ig.media[5]["id"])
    state = runner.empty_state()
    state["accounts"] = {"instagram": "abm.media (demo)", "threads": "abm.media (demo)"}
    runner.sync(state, cfg, ig)
    runner.approve(state, cfg)
    # Pretend the first slots have already passed.
    for i, (item, s) in enumerate(project(state, cfg, now_utc(), limit=6)):
        if i == 4:
            break
        runner.publish_one(state, cfg, ig, th, item, s)
    for item in list(runner.queue(state))[:1]:
        runner._fail(state, cfg, item, "create container: HTTP 500: Service temporarily unavailable", False)
    out = runner.ROOT / "docs" / "demo.html"
    out.write_text(dashboard.build(state, cfg, now_utc()), encoding="utf-8")
    print(f"Demo dashboard written to {out}")


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd in ("connect-instagram", "connect-threads"):
        return connect(cmd.split("-")[1])
    if cmd == "demo":
        return demo()
    cfg = load_config()
    state = load_state(cfg)
    try:
        if cmd == "verify":
            ig, th = clients()
            igp, thp = ig.profile(), th.profile()
            state["accounts"] = {"instagram": igp.get("username"), "threads": thp.get("username")}
            print(f"Instagram: @{igp.get('username')}   Threads: @{thp.get('username')}")
            runner.sync(state, cfg, ig)
            print_plan(state, cfg, limit=10)
            try:
                print("Threads publishing quota:", th.publishing_limit())
            except MetaError as e:
                print("Could not read Threads quota:", e)
        elif cmd == "sync":
            runner.sync(state, cfg, clients()[0])
        elif cmd == "plan":
            print_plan(state, cfg, limit=int(args[0]) if args else 30)
        elif cmd == "approve":
            preview = runner.approve(state, cfg)
            print(f"Approved. First post at {preview[0]['slot']} (Morocco time).")
        elif cmd == "unapprove":
            runner.unapprove(state)
            print("Publishing paused.")
        elif cmd == "run":
            runner.run(state, cfg, *clients())
        elif cmd == "retry-failed":
            print(f"{runner.retry_failed(state)} video(s) re-queued.")
        elif cmd in ("exclude", "include"):
            for ig_id in args:
                runner.set_excluded(state, ig_id, cmd == "exclude")
            print(f"{cmd}d: {', '.join(args)}")
        elif cmd == "refresh-tokens":
            refresh_tokens(state)
        elif cmd != "dashboard":
            raise SystemExit(f"Unknown command {cmd!r}. Try: python -m migrator help")
    finally:
        save_state(cfg, state)
        print(f"Dashboard: {write_dashboard(cfg, state)}")


if __name__ == "__main__":
    main()
