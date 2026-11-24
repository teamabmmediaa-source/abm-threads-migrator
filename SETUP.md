# Setup guide: connecting ABM Media's Instagram and Threads accounts

Everything here is free: Meta's official APIs, GitHub Actions to run it, and GitHub Pages for the dashboard.
**Nothing is published until you run the "approve" step (step 6).**

How it works: the system reads your Instagram videos through the **Instagram API**, passes Instagram's original
video file URL to the **Threads API** (Threads downloads the actual file, so the post is a native video, not a link),
and posts one video per slot at 10:00, 14:00 and 20:00 Morocco time, oldest first.

---

## Security rules (read once)

* Access tokens are like passwords. Keep them only in **GitHub Secrets**. Never put them in a file, a commit, an email or a chat.
* The Meta apps stay in **Development mode**. In that mode only your own accounts (added as testers) can use them, and you don't need Meta's App Review.
* Turn on two-factor authentication for the Facebook, Instagram, Threads and GitHub accounts involved.
* Permissions requested are the minimum: `instagram_business_basic` (read only), `threads_basic` and `threads_content_publish`.
* To cut access at any time: Instagram → Settings → *Website permissions → Apps and websites* → remove the app. Do the same in Threads. Or delete the GitHub secrets.

---

## Step 1: Check the accounts

1. The ABM Media Instagram account must be a **Professional** account (Business or Creator). In Instagram, go to *Settings → Account type and tools*.
2. The ABM Media **Threads** profile must exist. It is created from the same Instagram account.

## Step 2: Put the code on GitHub

1. Create a free account at github.com if you don't have one. Then create a new repository, for example `abm-threads-migrator`.
   * **Public** (recommended): unlimited free Actions minutes and a free dashboard on GitHub Pages. Only `state/state.json` and the dashboard are visible: captions and links of posts that are already public. Tokens are never in the repository.
   * **Private**: also works. Runs use about 1,100 of the 2,000 free minutes a month. GitHub Pages isn't free for private repos, so you download the dashboard from each run's *Artifacts* section instead.
2. Upload the contents of this `threads-migrator` folder (keep the `.github` folder).
3. **Settings → Pages** → *Deploy from a branch* → `main` / `/docs` → Save. Your dashboard URL will be
   `https://<your-github-name>.github.io/abm-threads-migrator/`. You also use this URL as the **redirect URI** below.

## Step 3: Instagram app (read access)

1. Go to <https://developers.facebook.com/apps> → **Create app**.
2. Use case: **Manage messaging & content on Instagram** (this is the "Instagram API with Instagram Login").
3. In the app, open **Instagram → API setup with Instagram business login**.
4. Under *Generate access tokens*, click **Add account** and log in as ABM Media. Then click **Generate token** and copy the token. It is a long-lived token, valid for 60 days and refreshed automatically each week.
   * If the account isn't listed, go to **App roles → Roles → Add people → Instagram Tester**. Then accept the invite in Instagram (*Settings → Website permissions → Apps and websites → Tester invites*) and try again.
   * Alternative: under *Business login settings*, add your Pages URL as the redirect URL, then run the helper on your PC:
     ```powershell
     cd threads-migrator; python -m migrator connect-instagram
     ```
5. Save the token as a GitHub secret named **`IG_ACCESS_TOKEN`** (see step 5).

## Step 4: Threads app (publishing)

1. **Create app** again. Use case: **Access the Threads API**. If Meta lets you add it to the same app, that's fine too.
2. **Use cases → Access the Threads API → Customize → Permissions**: make sure `threads_basic` and `threads_content_publish` are added.
3. **Customize → Settings**: put your Pages URL in *Redirect Callback URLs*, *Uninstall Callback URL* and *Delete Callback URL*. Save.
4. **App roles → Roles → Add people → Threads Tester** → enter the ABM Media Threads username.
   Then accept it in the Threads app: *Settings → Account → Website permissions → Invites*.
5. Copy the **Threads App ID** and **Threads App Secret** (*App settings → Basic*, Threads section).
6. On your PC, in PowerShell:
   ```powershell
   cd threads-migrator; python -m migrator connect-threads
   ```
   It asks for the App ID, the secret (hidden while you type) and the redirect URI. It prints a link. Open it, log in as ABM Media and click **Allow**.
   You land on your Pages URL with `?code=…` in the address bar. Paste that whole address back into the terminal.
   It prints a 60-day token. Save it as the GitHub secret **`THREADS_ACCESS_TOKEN`**, then close the terminal.
   The app secret is used only on your PC and is never stored.

## Step 5: GitHub secrets

In the repository, go to **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `IG_ACCESS_TOKEN` | token from step 3 |
| `THREADS_ACCESS_TOKEN` | token from step 4 |
| `GH_PAT` *(optional, recommended)* | lets the weekly job save refreshed tokens. Create it at GitHub → Settings → Developer settings → **Fine-grained tokens**: only this repository, permission **Secrets: Read and write**, nothing else. |

Without `GH_PAT`, the dashboard warns you before the tokens expire, and you repeat steps 3 and 4 every ~55 days.

## Step 6: Check, review the schedule, approve

1. **Actions → Instagram → Threads → Run workflow → command `verify`**. This only reads: it confirms both accounts, loads every video into the queue and builds the dashboard.
   Open the dashboard. Every video shows as **Pending** with its planned Threads date and time.
2. To leave a video out: run the workflow with command `exclude` and the Instagram media ID shown in the dashboard.
3. When you're happy with the schedule: **Run workflow → command `approve`**. Publishing starts at the **next** slot after approval. Slots that already passed are never caught up.
4. To pause at any time: command `unapprove`. To try failed videos again: command `retry-failed`.

From then on, the workflow runs automatically every 15 minutes around each slot. New Reels you post on Instagram are added to the end of the queue. To have them wait for you instead, set `"auto_schedule_new": false` in `config.json`.

---

## Good to know

| Topic | Behaviour |
|---|---|
| Order | Oldest Instagram video first, one per slot, at 10:00 / 14:00 / 20:00 Africa/Casablanca time. Morocco's clock changes are handled automatically. |
| Duplicates | One record per Instagram media ID. A slot is used once. Before any retry, the system checks your Threads profile for the post. Runs never overlap. |
| Retries | Temporary errors are retried up to 5 times, waiting 30 min, 1 h, 2 h, 4 h… Other videos keep their slots in the meantime. After that the video is marked **Failed** with the reason. |
| Quality | The original file Instagram serves through its API is handed to Threads unchanged. Nothing is re-encoded on our side; Threads processes it like any upload. |
| Captions | Copied as they are. Threads allows 500 characters, so longer captions are shortened at a word boundary, and the dashboard flags them. |
| Limits | Threads accepts videos up to 5 minutes and 1 GB. Longer ones fail with Meta's message. Some Reels with licensed music have no downloadable file through the API; those are marked failed so you can post them by hand. |
| Timing | GitHub's scheduler can start runs 5–30 minutes late at busy times, so a post can appear a bit after the slot. A slot is skipped if nothing could run within 3 hours. |
| Scope | Reels and feed videos. Carousels, photos and Stories are not migrated. |

Commands you can also run on your PC (with the two tokens set as environment variables):

```powershell
cd threads-migrator; python -m migrator help
```

```powershell
cd threads-migrator; python -m unittest discover -s tests -t .
```
