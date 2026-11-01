"""Glavni program: obiđi sve izvore, pronađi nove oglase, pošalji notifikacije.

Pokretanje:  python -m radar.main            (koristi NTFY_TOPIC iz okruženja)
             python -m radar.main --only szks,bhtelecom
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import logging
import os
import re
import sys
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from . import notify
from .extract import extract, page_text
from .fetch import Browser, FetchError, get, pdf_text
from .keywords import find_it_match, is_entry_level, normalize, snippet_around

JOBISH = re.compile(
    r"(oglas|konkurs|natjecaj|pozicij|radno mjesto|radna mjesta|izvrsil|prijem|zaposljava|"
    r"zaposlenj|praks|pripravni|stipendij|position|opening|\bjobs?\b|vacanc|hiring|career|"
    r"intern|apply|prijav|rok za|deadline)"
)

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
REPORTS = ROOT / "reports"
SEEN_CAP = 5000
HISTORY_DAYS = 30
FAIL_ALERT_AFTER = 2          # uzastopnih neuspjeha prije alarma
MAX_INDIVIDUAL_PER_SOURCE = 5  # više od ovoga -> jedna grupna poruka
MAX_MESSAGES_PER_RUN = 45      # zaštita od poplave ako se neki sajt promijeni
DEEP_LIMIT = 25

log = logging.getLogger("radar")
TZ = dt.timezone(dt.timedelta(hours=1))  # Sarajevo (zimsko vrijeme; za prikaz je dovoljno)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_json(name, default):
    p = STATE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            log.warning("state/%s je oštećen, krećem ispočetka", name)
    return default


def save_json(name, data):
    STATE.mkdir(exist_ok=True)
    (STATE / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_sources(only=None):
    data = yaml.safe_load((ROOT / "sources.yaml").read_text(encoding="utf-8"))
    sources = [s for s in data["sources"] if s.get("enabled", True)]
    ids = [s["id"] for s in sources]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise SystemExit(f"Duplikat id u sources.yaml: {dup}")
    if only:
        sources = [s for s in sources if s["id"] in only]
    return sources


# ---------------------------------------------------------------- dubinska pretraga

def deep_text(item, src) -> str:
    """Tekst samog oglasa (HTML stranica + do 3 PDF priloga, ili PDF direktno)."""
    r = get(item["url"], insecure=src.get("insecure", False), timeout=40, retries=1)
    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or item["url"].lower().endswith(".pdf"):
        return pdf_text(r.content)
    soup = BeautifulSoup(r.text, "lxml")
    pdfs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower() and href not in pdfs:
            pdfs.append(href)
    text = "\n".join(page_text(r.text))
    from urllib.parse import urljoin
    for href in pdfs[:3]:
        try:
            pr = get(urljoin(item["url"], href), insecure=src.get("insecure", False),
                     timeout=40, retries=1)
            text += "\n" + pdf_text(pr.content)
        except FetchError:
            continue
    return text


# ---------------------------------------------------------------- klasifikacija

def classify(item, src, body_text=""):
    title_ctx = " ".join(x for x in [item.get("title"), item.get("company")] if x)
    match = find_it_match(title_ctx, is_title=True)
    snippet = ""
    if not match and body_text:
        match = find_it_match(body_text)
        if match:
            snippet = snippet_around(body_text, match)
    is_it = bool(match) or src.get("scope") == "it"
    entry = is_entry_level(title_ctx) or (is_it and bool(body_text) and is_entry_level(body_text[:3000]))
    item["it"] = is_it
    item["entry"] = entry
    item["match"] = match or ""
    item["snippet"] = snippet
    if is_it and entry:
        item["priority"] = 5
    elif is_it:
        item["priority"] = 4
    elif src.get("scope") == "broad":
        item["priority"] = 2
    else:
        item["priority"] = 3
    return item


# ---------------------------------------------------------------- poruke

def item_message(item, src):
    lines = []
    if item.get("company"):
        lines.append(f"🏢 {item['company']}")
    if item.get("location"):
        lines.append(f"📍 {item['location']}")
    if item.get("snippet"):
        lines.append(f"🔎 U oglasu: {item['snippet']}")
    elif item.get("match") and item["match"] != "IT":
        lines.append(f"🔎 Pogodak: {item['match']}")
    lines.append(f"Izvor: {src['name']}")
    tags = []
    if item["priority"] == 5:
        tags = ["rotating_light", "computer"]
    elif item["it"]:
        tags = ["computer"]
    else:
        tags = ["briefcase"]
    return {"title": item["title"], "message": "\n".join(lines), "priority": item["priority"],
            "tags": tags, "click": item["url"]}


def group_message(items, src, quiet=False):
    items = sorted(items, key=lambda i: -i["priority"])
    shown = items[:20]
    lines = []
    for i in shown:
        mark = "🚨 " if i["priority"] == 5 else ("💻 " if i["it"] else "• ")
        extra = f" ({i['company']})" if i.get("company") else ""
        lines.append(f"{mark}{i['title']}{extra}")
    if len(items) > len(shown):
        lines.append(f"... i još {len(items) - len(shown)} (vidi OGLASI.md)")
    label = "ostalih oglasa" if quiet else "novih oglasa"
    return {"title": f"{src['name']}: {len(items)} {label}", "message": "\n".join(lines),
            "priority": max(i["priority"] for i in items),
            "tags": ["card_index_dividers"] if quiet else ["briefcase"],
            "click": src.get("link") or src["url"]}


def plan_messages(src, new_items):
    if src.get("type") == "page_hash":
        return []
    if src.get("scope") == "broad":
        loud = [i for i in new_items if i["it"]]
        quiet = [i for i in new_items if not i["it"]]
    else:
        loud, quiet = new_items, []
    msgs = []
    if len(loud) > MAX_INDIVIDUAL_PER_SOURCE:
        msgs.append(group_message(loud, src))
    else:
        msgs.extend(item_message(i, src) for i in loud)
    if quiet:
        msgs.append(group_message(quiet, src, quiet=True))
    return msgs


# ---------------------------------------------------------------- izvještaji

def write_status(results, sources, run_started):
    REPORTS.mkdir(exist_ok=True)
    when = run_started.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    ok = sum(1 for r in results.values() if r["ok"])
    out = [f"# Status izvora\n", f"Zadnje pokretanje: **{when}** (Sarajevo)  ",
           f"Izvora: **{len(results)}**, ispravnih: **{ok}**, s greškom: **{len(results) - ok}**\n",
           "| Izvor | Status | Oglasa | Novih | Napomena |", "|---|---|---|---|---|"]
    by_id = {s["id"]: s for s in sources}
    for sid, r in sorted(results.items(), key=lambda kv: (kv[1]["ok"], kv[0])):
        status = "✅" if r["ok"] else "❌"
        note = (r.get("error") or "").replace("|", "/")[:120]
        if r.get("seeded"):
            note = "prvo pokretanje (zapamćeno, bez notifikacija)"
        out.append(f"| [{by_id[sid]['name']}]({by_id[sid]['url']}) | {status} | {r['count']} | "
                   f"{r['new']} | {note} |")
    out.append("\n## Uzorak izvučenih oglasa po izvoru\n")
    for sid, r in sorted(results.items()):
        out.append(f"<details><summary>{sid} ({r['count']})</summary>\n")
        for it in r.get("sample", []):
            out.append(f"- [{it['title']}]({it['url']})")
        out.append("\n</details>\n")
    (REPORTS / "status.md").write_text("\n".join(out), encoding="utf-8")


def write_oglasi(history):
    rows = sorted(history, key=lambda h: h["ts"], reverse=True)
    out = ["# Oglasi (zadnjih 30 dana)\n",
           "🚨 IT + početnička pozicija · 💻 IT · • ostalo · ⏺ bio otvoren kad je radar pokrenut\n",
           "| Vrijeme | Oglas | Izvor |", "|---|---|---|"]
    for h in rows[:1500]:
        t = dt.datetime.fromisoformat(h["ts"]).astimezone(TZ).strftime("%d.%m. %H:%M")
        mark = "🚨" if h.get("priority") == 5 else ("💻" if h.get("it") else "•")
        if h.get("initial"):
            mark += "⏺"
        title = h["title"].replace("|", "/")
        comp = f" · {h['company']}".replace("|", "/") if h.get("company") else ""
        out.append(f"| {t} | {mark} [{title}]({h['url']}){comp} | {h['source_name']} |")
    (ROOT / "OGLASI.md").write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- glavna petlja

def run_source(src, browser):
    try:
        items = extract(src, browser)
        return src["id"], items, None
    except FetchError as e:
        return src["id"], [], str(e)
    except Exception as e:  # bug u parsiranju jednog izvora ne smije srušiti sve
        log.exception("Neočekivana greška u %s", src["id"])
        return src["id"], [], f"{e.__class__.__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="zarezom odvojeni id-evi izvora")
    ap.add_argument("--heartbeat", action="store_true", help="pošalji dnevni izvještaj odmah")
    ap.add_argument("--no-notify", action="store_true", help="ništa ne šalji (test)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.no_notify:
        notify.TOPIC = ""

    run_started = now_utc()
    only = set(args.only.split(",")) if args.only else None
    sources = load_sources(only)
    seen = load_json("seen.json", {})
    health = load_json("health.json", {})
    history = load_json("history.json", [])
    meta = load_json("meta.json", {"runs": 0})
    first_run = meta.get("runs", 0) == 0

    # --- dohvatanje: obične stranice paralelno, JS stranice redom u jednom browseru
    browser = Browser()
    fetched = {}
    plain = [s for s in sources if not s.get("js")]
    js = [s for s in sources if s.get("js")]
    with cf.ThreadPoolExecutor(max_workers=12) as pool:
        for sid, items, err in pool.map(lambda s: run_source(s, None), plain):
            fetched[sid] = (items, err)
    for s in js:
        sid, items, err = run_source(s, browser)
        fetched[sid] = (items, err)
    browser.close()

    # --- obrada
    results, outgoing, health_msgs = {}, [], []
    seeded_sources, seeded_items = 0, 0
    ts = run_started.isoformat(timespec="seconds")
    for src in sources:
        sid = src["id"]
        items, err = fetched[sid]
        h = health.setdefault(sid, {"fail_streak": 0, "alerted": False})
        min_items = src.get("min_items", 1)
        if not err and len(items) < min_items:
            err = "0 oglasa: stranica je možda promijenila dizajn ili je prazna"
        res = {"ok": not err, "count": len(items), "new": 0, "error": err,
               "sample": [{"title": i["title"], "url": i["url"]} for i in items[:8]]}
        results[sid] = res

        if err:
            h["fail_streak"] += 1
            h["last_error"] = err
            if h["fail_streak"] >= FAIL_ALERT_AFTER and not h["alerted"]:
                h["alerted"] = True
                health_msgs.append({
                    "title": f"⚠️ Monitor ne radi: {src['name']}",
                    "message": f"{err}\nNe radi već {h['fail_streak']} provjere zaredom. "
                               f"Do popravke provjeravaj ručno:",
                    "priority": 3, "tags": ["warning"], "click": src.get("link") or src["url"]})
            continue

        if h.get("alerted"):
            health_msgs.append({"title": f"✅ Monitor ponovo radi: {src['name']}",
                                "message": f"Pronađeno {len(items)} oglasa.", "priority": 2,
                                "tags": ["white_check_mark"], "click": src.get("link") or src["url"]})
        h.update({"fail_streak": 0, "alerted": False, "last_ok": ts, "last_count": len(items)})
        h.pop("last_error", None)

        entry = seen.get(sid)
        # --- stranice koje pratimo po promjeni teksta
        if src.get("type") == "page_hash":
            page = items[0]
            if entry is None:
                seen[sid] = {"keys": [page["key"]], "lines": page["_lines"]}
                res["seeded"] = True
                seeded_sources += 1
            elif page["key"] not in entry["keys"]:
                old = set(entry.get("lines", []))
                added = [l for l in page["_lines"] if l not in old][:8]
                entry["keys"] = (entry["keys"] + [page["key"]])[-50:]
                entry["lines"] = page["_lines"]
                if added:
                    res["new"] = 1
                    body = "\n".join(f"+ {l[:140]}" for l in added)
                    joined = " ".join(added)
                    match = find_it_match(joined)
                    # glasno samo ako nova linija liči na oglas; ostale promjene (banneri,
                    # datumi, tekst) idu tiho da ne smetaju
                    jobish = JOBISH.search(normalize(joined))
                    pr = 4 if match and jobish else (3 if jobish else 2)
                    outgoing.append({"title": f"🔄 Promjena: {src['name']}",
                                     "message": f"Novo na stranici:\n{body}", "priority": pr,
                                     "tags": ["arrows_counterclockwise"], "click": src["url"]})
                    if pr >= 3:
                        history.append({"ts": ts, "source": sid, "source_name": src["name"],
                                        "title": f"Promjena na stranici: {added[0][:100]}",
                                        "url": src["url"], "it": bool(match), "priority": pr})
            continue

        # --- obični izvori: novi = ključ koji još nismo vidjeli
        if entry is None:
            seen[sid] = {"keys": [i["key"] for i in items][-SEEN_CAP:]}
            res["seeded"] = True
            seeded_sources += 1
            seeded_items += len(items)
            for i in items:
                classify(i, src)
                history.append({"ts": ts, "source": sid, "source_name": src["name"],
                                "title": i["title"], "url": i["url"], "company": i.get("company", ""),
                                "it": i["it"], "priority": i["priority"], "initial": True})
            continue

        known = set(entry["keys"])
        new_items = [i for i in items if i["key"] not in known]
        res["new"] = len(new_items)
        if not new_items:
            continue

        for n, i in enumerate(new_items):
            body = ""
            if src.get("deep") and n < DEEP_LIMIT:
                try:
                    body = deep_text(i, src)
                except FetchError as e:
                    log.info("%s: oglas %s se ne može otvoriti: %s", sid, i["url"], e)
            classify(i, src, body)
            history.append({"ts": ts, "source": sid, "source_name": src["name"],
                            "title": i["title"], "url": i["url"], "company": i.get("company", ""),
                            "it": i["it"], "priority": i["priority"], "match": i["match"],
                            "snippet": i["snippet"]})
        outgoing.extend(plan_messages(src, new_items))
        entry["keys"] = (entry["keys"] + [i["key"] for i in new_items])[-SEEN_CAP:]

    # --- slanje
    if len(outgoing) > MAX_MESSAGES_PER_RUN:
        log.warning("%d poruka je previše, šaljem sažetak", len(outgoing))
        total = sum(r["new"] for r in results.values())
        top = [m for m in outgoing if m["priority"] >= 4][:25]
        lines = [f"{'🚨' if m['priority'] == 5 else '💻'} {m['title']}" for m in top]
        outgoing = [{"title": f"{total} novih oglasa u ovoj provjeri",
                     "message": ("Najvažniji:\n" + "\n".join(lines) if lines else "") +
                                "\nSve je u OGLASI.md na GitHubu.",
                     "priority": 4, "tags": ["briefcase"],
                     "click": f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'FarisKarkelja/job-radar')}/blob/main/OGLASI.md"}]
    outgoing.sort(key=lambda m: -m["priority"])

    if notify.TOPIC and not meta.get("announced") and not only:
        ok = sum(1 for r in results.values() if r["ok"])
        remembered = sum(1 for h in history if h.get("initial"))
        meta["announced"] = True
        notify.send("📡 Job Radar je pokrenut",
                    f"Pratim {len(sources)} izvora ({ok} radi odmah).\n"
                    f"Zapamtio sam {remembered} oglasa koji su trenutno otvoreni. "
                    f"Od sada dobijaš samo nove.\nSvi trenutni oglasi su u OGLASI.md.",
                    priority=3, tags=["satellite_antenna"],
                    click=f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'FarisKarkelja/job-radar')}/blob/main/OGLASI.md")
    for m in outgoing + health_msgs:
        notify.send(m["title"], m["message"], priority=m["priority"], tags=m["tags"],
                    click=m.get("click"))

    # --- dnevni izvještaj (jutarnja provjera, 05-10h UTC; cron zna kasniti)
    today = run_started.date().isoformat()
    if args.heartbeat or (5 <= run_started.hour < 10 and meta.get("last_heartbeat") != today
                          and not first_run and not only):
        day_ago = run_started - dt.timedelta(hours=24)
        recent = [h for h in history if not h.get("initial")
                  and dt.datetime.fromisoformat(h["ts"]) >= day_ago]
        it_recent = sum(1 for h in recent if h.get("it"))
        broken = [s["name"] for s in sources if not results[s["id"]]["ok"]]
        msg = (f"Provjereno {len(sources)} izvora, ispravno {len(sources) - len(broken)}.\n"
               f"Zadnja 24h: {len(recent)} novih oglasa, od toga {it_recent} IT.")
        if broken:
            msg += "\nNe rade: " + ", ".join(broken[:15]) + (" ..." if len(broken) > 15 else "")
        notify.send("☀️ Job Radar dnevni izvještaj", msg, priority=2, tags=["sunny"],
                    click=f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'FarisKarkelja/job-radar')}/blob/main/reports/status.md")
        meta["last_heartbeat"] = today

    # --- čuvanje stanja
    cutoff = run_started - dt.timedelta(days=HISTORY_DAYS)
    history = [h for h in history if dt.datetime.fromisoformat(h["ts"]) >= cutoff][-6000:]
    meta["runs"] = meta.get("runs", 0) + 1
    meta["last_run"] = ts
    meta["last_sent"] = notify.sent_count
    save_json("seen.json", seen)
    save_json("health.json", health)
    save_json("history.json", history)
    save_json("meta.json", meta)
    write_status(results, sources, run_started)
    write_oglasi(history)

    new_total = sum(r["new"] for r in results.values())
    log.info("Gotovo: %d izvora, %d novih, %d poruka poslano, %d izvora zapamćeno prvi put",
             len(sources), new_total, notify.sent_count, seeded_sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
