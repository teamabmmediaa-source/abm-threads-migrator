"""Static, self-contained HTML dashboard (works on GitHub Pages or opened locally)."""
from __future__ import annotations

import html
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .schedule import parse, project


def _row_status(item: dict, approved: bool) -> str:
    s = item["status"]
    if s == "queued":
        return "scheduled" if approved else "pending"
    if s == "processing":
        return "pending"
    return s  # published / failed / excluded


def build(state: dict, cfg: dict, now: datetime) -> str:
    tz = ZoneInfo(cfg["timezone"])
    approved = state["approval"]["approved"]
    plan = {i["ig_id"]: s for i, s in project(state, cfg, now)}

    def local(ts):
        dt = parse(ts) if isinstance(ts, str) else ts
        return dt.astimezone(tz).strftime("%a %d %b %Y · %H:%M") if dt else ""

    rows = []
    for it in state["items"].values():
        st = _row_status(it, approved)
        when = (it.get("published_at") if st == "published"
                else plan.get(it["ig_id"]) if it["status"] == "queued" else it.get("container_created_at"))
        note = it.get("last_error") or ""
        if it["status"] == "processing":
            note = "Threads is processing the video"
        elif it["status"] == "queued" and it.get("attempts"):
            note = f"Retry {it['attempts']}/{cfg['max_attempts']} · {note}"
        rows.append({
            "id": it["ig_id"], "status": st, "posted": local(it.get("timestamp")), "ts": it.get("timestamp") or "",
            "when": local(when), "caption": (it.get("text") or "")[:160], "trimmed": bool(it.get("caption_trimmed")),
            "ig": it.get("permalink"), "threads": it.get("threads_permalink"), "note": note,
            "kind": "Reel" if it.get("product") == "REELS" else "Video", "thumb": it.get("thumbnail"),
            "sort": (parse(when).isoformat() if isinstance(when, str) else when.isoformat()) if when else "",
        })
    counts = {k: sum(r["status"] == k for r in rows) for k in ("published", "scheduled", "pending", "failed", "excluded")}
    upcoming = sorted(plan.values())
    data = {
        "rows": rows, "counts": counts, "approved": approved,
        "approved_at": local(state["approval"].get("approved_at")),
        "generated": local(now), "last_sync": local(state.get("last_sync")),
        "finish": local(upcoming[-1]) if upcoming else "", "next": local(upcoming[0]) if upcoming else "",
        "alerts": [{"at": local(a["at"]), "message": a["message"]} for a in state.get("alerts", [])[:5]],
        "accounts": state.get("accounts", {}),
        "tokens": {k: local(v) for k, v in state.get("tokens", {}).items()},
        "slots": ", ".join(cfg["daily_slots"]),
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", payload).replace("__TITLE__", html.escape(cfg.get("dashboard_title", "Instagram → Threads migration")))


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>__TITLE__</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#16161a;--mute:#6b6b76;--line:#e6e6ea;
--pub:#1f8a4c;--sch:#2a62d9;--pen:#b7791f;--fail:#c53030;--exc:#8a8a94}
@media (prefers-color-scheme:dark){:root{--bg:#111114;--card:#1b1b20;--ink:#ececf1;--mute:#9a9aa6;--line:#2c2c34;
--pub:#4cc38a;--sch:#7aa2ff;--pen:#f0b44c;--fail:#ff7b7b;--exc:#77777f}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}.sub{color:var(--mute);font-size:13px}
.banner{margin:18px 0;padding:14px 16px;border-radius:10px;border:1px solid var(--line);background:var(--card)}
.banner.warn{border-color:var(--pen)}.banner.bad{border-color:var(--fail)}.banner b{display:block;margin-bottom:2px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:18px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;cursor:pointer;text-align:left;color:inherit;font:inherit}
.tile[aria-pressed=true]{outline:2px solid var(--ink)}.tile .n{font-size:30px;font-weight:650;font-variant-numeric:tabular-nums}
.tile .l{color:var(--mute);font-size:13px;display:flex;gap:6px;align-items:center}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block}
.meta{display:flex;flex-wrap:wrap;gap:8px 22px;color:var(--mute);font-size:13px;margin:6px 0 16px}.meta b{color:var(--ink);font-weight:600}
.tools{display:flex;gap:10px;margin:0 0 10px}input[type=search]{flex:1;padding:9px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink);font:inherit}
.wrap{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600;position:sticky;top:0;background:var(--card)}tr:last-child td{border-bottom:0}
.pill{display:inline-flex;gap:6px;align-items:center;font-size:12px;font-weight:600;white-space:nowrap}
.cap{max-width:420px;color:var(--ink)}.note{color:var(--mute);font-size:12px;margin-top:3px}.note.err{color:var(--fail)}
a{color:var(--sch)}td.when{white-space:nowrap;font-variant-numeric:tabular-nums}.thumb{width:42px;height:56px;object-fit:cover;border-radius:5px;background:var(--line)}
.empty{padding:30px;text-align:center;color:var(--mute)}
</style></head><body><main>
<h1>__TITLE__</h1><div class="sub" id="sub"></div>
<div id="banners"></div><div class="tiles" id="tiles"></div><div class="meta" id="meta"></div>
<div class="tools"><input type="search" id="q" placeholder="Search captions or IDs…" aria-label="Search"></div>
<div class="wrap"><table><thead><tr><th></th><th>Status</th><th>Threads time (Morocco)</th><th>Caption</th><th>Instagram post</th><th>Links</th></tr></thead><tbody id="rows"></tbody></table></div>
</main><script>
const D=__DATA__;
const C={published:["Published","--pub"],scheduled:["Scheduled","--sch"],pending:["Pending","--pen"],failed:["Failed","--fail"],excluded:["Excluded","--exc"]};
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const safe=u=>/^https:\/\//.test(u||"")?esc(u):"";
let filter=null;
document.getElementById("sub").textContent=`Updated ${D.generated} (Morocco time) · last Instagram check ${D.last_sync||"never"}`;
const b=[];
if(!D.approved)b.push(`<div class="banner warn"><b>Waiting for your approval — nothing will be published.</b>Review the schedule below, then run the “approve” action (see SETUP.md, step 6).</div>`);
else b.push(`<div class="banner"><b>Schedule approved ${esc(D.approved_at)}.</b>Posting at ${esc(D.slots)} Morocco time. Next: ${esc(D.next||"—")} · queue finishes ${esc(D.finish||"—")}.</div>`);
D.alerts.forEach(a=>b.push(`<div class="banner bad"><b>${esc(a.at)}</b>${esc(a.message)}</div>`));
document.getElementById("banners").innerHTML=b.join("");
const tiles=document.getElementById("tiles");
["published","scheduled","pending","failed"].concat(D.counts.excluded?["excluded"]:[]).forEach(k=>{
 const t=document.createElement("button");t.className="tile";t.setAttribute("aria-pressed","false");
 t.innerHTML=`<div class="n">${D.counts[k]}</div><div class="l"><span class="dot" style="background:var(${C[k][1]})"></span>${C[k][0]}</div>`;
 t.onclick=()=>{filter=filter===k?null:k;[...tiles.children].forEach(x=>x.setAttribute("aria-pressed","false"));if(filter)t.setAttribute("aria-pressed","true");render()};
 tiles.appendChild(t)});
const m=[];if(D.accounts.instagram)m.push(`Instagram <b>@${esc(D.accounts.instagram)}</b>`);if(D.accounts.threads)m.push(`Threads <b>@${esc(D.accounts.threads)}</b>`);
Object.entries(D.tokens).forEach(([k,v])=>m.push(`${esc(k.replace("_expires_at",""))} token valid until <b>${esc(v)}</b>`));
document.getElementById("meta").innerHTML=m.join("");
const order={processing:0,pending:1,scheduled:2,failed:3,published:4,excluded:5};
function render(){
 const q=document.getElementById("q").value.toLowerCase();
 const rs=D.rows.filter(r=>(!filter||r.status===filter)&&(!q||(r.caption+" "+r.id).toLowerCase().includes(q)))
  .sort((a,b)=>a.status==="published"&&b.status==="published"?b.sort.localeCompare(a.sort):(order[a.status]-order[b.status])||a.ts.localeCompare(b.ts));
 document.getElementById("rows").innerHTML=rs.length?rs.map(r=>`<tr>
 <td>${safe(r.thumb)?`<img class="thumb" loading="lazy" src="${safe(r.thumb)}" alt="" onerror="this.remove()">`:""}</td>
 <td><span class="pill" style="color:var(${C[r.status][1]})"><span class="dot" style="background:var(${C[r.status][1]})"></span>${C[r.status][0]}</span></td>
 <td class="when">${esc(r.when)||"—"}</td>
 <td><div class="cap">${esc(r.caption)||"<i>(no caption)</i>"}</div>${r.trimmed?'<div class="note">Caption shortened to fit Threads’ 500-character limit</div>':""}${r.note?`<div class="note ${r.status==="failed"?"err":""}">${esc(r.note)}</div>`:""}</td>
 <td class="when">${esc(r.kind)} · ${esc(r.posted)}<div class="note">${esc(r.id)}</div></td>
 <td>${safe(r.ig)?`<a href="${safe(r.ig)}" target="_blank" rel="noopener">Instagram</a>`:""}${safe(r.threads)?` · <a href="${safe(r.threads)}" target="_blank" rel="noopener">Threads</a>`:""}</td></tr>`).join(""):`<tr><td colspan="6" class="empty">No videos here.</td></tr>`}
document.getElementById("q").oninput=render;render();
</script></body></html>"""
