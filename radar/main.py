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
from .location import decide as location_decide

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
LOCATION_LOOKUPS = 40          # koliko oglasa po provjeri smije otvoriti radi lokacije
DEDUPE_DAYS = 60               # isti naslov iste firme ne šalje se ponovo ovoliko dana
GENERIC_TITLE = re.compile(r"^(javni |interni )?(oglas|konkurs|natjecaj|natjecaj|poziv)\w*\b.{0,25}$")

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
    for el in soup.select("a, aside, nav, footer, header, [class*=sidebar], [class*=widget], [id*=sidebar]"):
        el.decompose()   # sporedni blokovi i linkovi na druge objave nisu dio ovog oglasa
    text = "\n".join(page_text(str(soup), src.get("deep_selector")))
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
    scope = src.get("scope", "company")
    is_it = bool(match) or scope == "it"
    entry = is_entry_level(title_ctx) or (is_it and bool(body_text) and is_entry_level(body_text[:3000]))
    if scope == "program" and not entry:
        entry = True   # izvori studentskih programa: sve su prakse/programi
    item["it"] = is_it
    item["entry"] = entry
    item["match"] = match or ""
    item["snippet"] = snippet
    if is_it and entry:
        item["priority"] = 5
    elif is_it:
        item["priority"] = 4
    else:
        item["priority"] = 3
    return item


def relevant(item, src) -> tuple:
    """Da li oglas uopšte zaslužuje notifikaciju (prije provjere lokacije)."""
    scope = src.get("scope", "company")
    if src.get("always") and re.search(src["always"], item.get("title", ""), re.I):
        item["priority"] = 5
        return True, ""
    if scope == "it":
        return True, ""
    if scope == "program":
        # prakse vani: samo IT/tech (naslov ili tekst objave)
        return (True, "") if item["it"] else (False, "praksa nije IT")
    if item["it"]:
        return True, ""
    # prakse/pripravnici kod poslodavaca koje pratiš (banke, telekomi, javna preduzeća)
    if scope == "company" and item["entry"]:
        return True, ""
    return False, "nije IT"


def fingerprint(item, src) -> str:
    title = normalize(item.get("title", ""))
    company = normalize(item.get("company", ""))
    if company:
        return f"{title}|{company}"
    if GENERIC_TITLE.match(title) or len(title) < 12:
        return f"{title}|{src.get('dedupe_group', src['id'])}|{item['key']}"   # "JAVNI OGLAS": svaki je drugačiji
    return f"{title}|{src.get('dedupe_group', src['id'])}"


# ---------------------------------------------------------------- poruke

def item_message(item, src):
    lines = []
    if item.get("company"):
        lines.append(f"🏢 {item['company']}")
    if item.get("location"):
        lines.append(f"📍 {item['location']}")
    elif item.get("loc_reason") in ("sarajevo", "remote/hybrid"):
        lines.append(f"📍 {'Sarajevo' if item['loc_reason'] == 'sarajevo' else 'remote/hybrid'}")
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
        tags = ["mortar_board"]
    return {"title": item["title"], "message": "\n".join(lines), "priority": item["priority"],
            "tags": tags, "click": item["url"]}


def group_message(items, src, quiet=False):
    items = sorted(items, key=lambda i: -i["priority"])
    shown = items[:20]
    lines = []
    for i in shown:
        mark = "🚨 " if i["priority"] == 5 else ("💻 " if i["it"] else "🎓 ")
        extra = f" ({i['company']})" if i.get("company") else ""
        lines.append(f"{mark}{i['title']}{extra}")
    if len(items) > len(shown):
        lines.append(f"... i još {len(items) - len(shown)} (vidi OGLASI.md)")
    label = "ostalih oglasa" if quiet else "novih oglasa"
    return {"title": f"{src['name']}: {len(items)} {label}", "message": "\n".join(lines),
            "priority": max(i["priority"] for i in items),
            "tags": ["card_index_dividers"] if quiet else ["briefcase"],
            "click": src.get("link") or src["url"]}


def plan_messages(src, items):
    if not items:
        return []
    if len(items) > MAX_INDIVIDUAL_PER_SOURCE:
        return [group_message(items, src)]
    return [item_message(i, src) for i in items]


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


def write_debug(results):
    """Za izvore s greškom sačuvaj HTML koji je radar vidio, da se lakše popravi."""
    from .extract import LAST_HTML
    dbg = REPORTS / "debug"
    dbg.mkdir(parents=True, exist_ok=True)
    for sid, r in results.items():
        path = dbg / f"{sid}.html"
        if not r["ok"] and sid in LAST_HTML:
            path.write_text(LAST_HTML[sid][:400_000], encoding="utf-8")
        elif r["ok"] and path.exists():
            path.unlink()


def write_oglasi(history):
    rows = [h for h in history if h.get("sent") or (h.get("initial") and h.get("it"))]
    rows = sorted(rows, key=lambda h: h["ts"], reverse=True)
    out = ["# Oglasi (zadnjih 30 dana)\n",
           "Samo IT/tech poslovi i prakse: Sarajevo bilo kako, ostatak BiH remote/hybrid, "
           "plus studentski programi vani.\n",
           "🚨 IT + početnička pozicija · 💻 IT · 🎓 praksa/program · "
           "⏺ bio otvoren kad je izvor dodan\n",
           "| Vrijeme | Oglas | Izvor |", "|---|---|---|"]
    for h in rows[:1500]:
        t = dt.datetime.fromisoformat(h["ts"]).astimezone(TZ).strftime("%d.%m. %H:%M")
        mark = "🚨" if h.get("priority") == 5 else ("💻" if h.get("it") else "🎓")
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
    notified = load_json("notified.json", {})   # fingerprint -> kad je poslan
    if not notified and not meta.get("v2"):
        # prelazak na novu verziju: ranije poslani IT oglasi se ne šalju ponovo
        for h in history:
            if not h.get("initial") and h.get("it") and "sent" not in h:
                h["sent"] = True
                comp = normalize(h.get("company", ""))
                fp = f"{normalize(h['title'])}|{comp}" if comp else f"{normalize(h['title'])}|{h['source']}"
                notified.setdefault(fp, h["ts"])
    meta["v2"] = True
    lookups = {"n": 0}

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
            if entry is None or "lines" not in entry:
                # novi izvor (ili je ranije bio drugog tipa): samo zapamti stranicu
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
                    match = find_it_match(joined, is_title=True)  # kratke linije sa stranice karijera
                    jobish = JOBISH.search(normalize(joined))
                    entry_lvl = is_entry_level(joined)
                    scope = src.get("scope", "company")
                    send = bool(jobish) and (bool(match) or entry_lvl or scope in ("it", "program"))
                    if send:
                        pr = 5 if (match or scope in ("it", "program")) and entry_lvl else (4 if match or scope in ("it", "program") else 3)
                        outgoing.append({"title": f"🔄 Promjena: {src['name']}",
                                         "message": f"Novo na stranici:\n{body}", "priority": pr,
                                         "tags": ["arrows_counterclockwise"], "click": src["url"]})
                    history.append({"ts": ts, "source": sid, "source_name": src["name"],
                                    "title": f"Promjena na stranici: {added[0][:100]}",
                                    "url": src["url"], "it": bool(match) or scope in ("it", "program"),
                                    "priority": 4 if match else 3, "sent": send,
                                    "why": "" if send else "promjena ne liči na IT oglas/praksu"})
            continue

        # --- obični izvori: novi = ključ koji još nismo vidjeli
        if entry is None:
            seen[sid] = {"keys": [i["key"] for i in items][-SEEN_CAP:]}
            res["seeded"] = True
            seeded_sources += 1
            seeded_items += len(items)
            open_now = []
            for n, i in enumerate(items):
                body = ""
                if src.get("scope") == "program" and src.get("deep") and n < 40:
                    try:
                        body = deep_text(i, src)   # prakse vani: IT se vidi tek u tekstu objave
                    except FetchError:
                        pass
                classify(i, src, body)
                ok, _ = relevant(i, src)
                if ok:
                    ok, i["loc_reason"] = location_decide(src, i)
                if ok:
                    open_now.append(i)
                    notified.setdefault(fingerprint(i, src), ts)
                history.append({"ts": ts, "source": sid, "source_name": src["name"],
                                "title": i["title"], "url": i["url"], "company": i.get("company", ""),
                                "it": i["it"] or ok, "priority": i["priority"], "initial": True})
            # novi izvor: jedna poruka sa onim što je trenutno otvoreno
            if open_now and not first_run and not src.get("quiet_seed"):
                open_now.sort(key=lambda i: -i["priority"])
                lines = [f"{'🚨' if i['priority'] == 5 else '💻' if i['it'] else '🎓'} {i['title'][:90]}"
                         + (f" ({i['company']})" if i.get("company") and i["company"] != src["name"] else "")
                         for i in open_now[:15]]
                if len(open_now) > 15:
                    lines.append(f"... i još {len(open_now) - 15} (vidi OGLASI.md)")
                health_msgs.append({"title": f"📋 Novi izvor: {src['name']} ({len(open_now)} otvoreno)",
                                    "message": "\n".join(lines), "priority": 3,
                                    "tags": ["clipboard"], "click": src.get("link") or src["url"]})
            continue

        if src.get("strip_query") and any("?" in k for k in entry["keys"]):
            # stari ključevi su imali promjenjive parametre; svedi ih na isti oblik
            entry["keys"] = list(dict.fromkeys(k.split("?", 1)[0] for k in entry["keys"]))
        if src.get("key_pattern"):
            krx = re.compile(src["key_pattern"], re.I)
            converted = []
            for k in entry["keys"]:
                m = krx.search(k)
                converted.append(m.group(1) if m else k)
            entry["keys"] = list(dict.fromkeys(converted))
        known = set(entry["keys"])
        new_items = [i for i in items if i["key"] not in known]
        res["new"] = len(new_items)
        if not new_items:
            continue

        to_send = []
        for n, i in enumerate(new_items):
            body = ""
            if src.get("deep") and n < DEEP_LIMIT:
                try:
                    body = deep_text(i, src)
                except FetchError as e:
                    log.info("%s: oglas %s se ne može otvoriti: %s", sid, i["url"], e)
            classify(i, src, body)
            ok, why = relevant(i, src)
            if ok:
                ok_loc, loc_why = location_decide(src, i, body)
                # lokacija nepoznata i oglas nije otvaran: otvori ga i pogledaj
                if (ok_loc and loc_why == "nepoznato" and not body and src.get("type") != "linkedin"
                        and src.get("lookup", True) and lookups["n"] < LOCATION_LOOKUPS):
                    lookups["n"] += 1
                    try:
                        body = deep_text(i, src)
                        ok_loc, loc_why = location_decide(src, i, body)
                    except FetchError:
                        pass
                i["loc_reason"] = loc_why
                if not ok_loc:
                    ok, why = False, loc_why
            if ok:
                fp = fingerprint(i, src)
                last = notified.get(fp)
                if last and dt.datetime.fromisoformat(last) >= run_started - dt.timedelta(days=DEDUPE_DAYS):
                    ok, why = False, "već poslano"
                else:
                    notified[fp] = ts
            if ok:
                to_send.append(i)
            history.append({"ts": ts, "source": sid, "source_name": src["name"],
                            "title": i["title"], "url": i["url"], "company": i.get("company", ""),
                            "it": i["it"], "priority": i["priority"], "match": i["match"],
                            "snippet": i["snippet"], "sent": ok, "why": why,
                            "loc": i.get("loc_reason", "")})
        res["sent"] = len(to_send)
        outgoing.extend(plan_messages(src, to_send))
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

    # --- dnevni izvještaj (prva provjera poslije 06h po Sarajevu)
    today = run_started.date().isoformat()
    if args.heartbeat or (run_started.hour >= 5 and meta.get("last_heartbeat") != today
                          and not first_run and not only):
        day_ago = run_started - dt.timedelta(hours=24)
        recent = [h for h in history if not h.get("initial")
                  and dt.datetime.fromisoformat(h["ts"]) >= day_ago]
        sent_recent = sum(1 for h in recent if h.get("sent"))
        why = {}
        for h in recent:
            if not h.get("sent") and h.get("why"):
                why[h["why"]] = why.get(h["why"], 0) + 1
        broken = [s["name"] for s in sources if not results[s["id"]]["ok"]]
        msg = (f"Provjereno {len(sources)} izvora, ispravno {len(sources) - len(broken)}.\n"
               f"Zadnja 24h: poslano {sent_recent} oglasa.")
        if why:
            msg += "\nPreskočeno: " + ", ".join(f"{k} {v}" for k, v in sorted(why.items(), key=lambda x: -x[1]))
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
    keep = (run_started - dt.timedelta(days=DEDUPE_DAYS)).isoformat()
    save_json("notified.json", {k: v for k, v in notified.items() if v >= keep})
    write_status(results, sources, run_started)
    write_oglasi(history)
    write_debug(results)

    new_total = sum(r["new"] for r in results.values())
    log.info("Gotovo: %d izvora, %d novih, %d poruka poslano, %d izvora zapamćeno prvi put",
             len(sources), new_total, notify.sent_count, seeded_sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
