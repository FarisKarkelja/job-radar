"""Proba novih izvora, bez slanja notifikacija i bez diranja stanja.

1) Svaki izvor iz probe.yaml se pokrene i u reports/probe.md se upiše koliko je
   oglasa našao i uzorak naslova, lokacija i odluka filtera.
2) ATS sonda: za listu imena firmi proba Workable, Lever, Breezy, Teamtailor,
   Recruitee, Greenhouse, Ashby i Personio i zapiše gdje firma ima javnu listu poslova.

Pokretanje: python -m radar.probe
"""
import concurrent.futures as cf
import json
import logging
from pathlib import Path

import yaml

from .extract import extract
from .fetch import Browser, FetchError, get
from .location import decide

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("radar.probe")


def probe_source(src, browser):
    try:
        items = extract(src, browser)
        return src, items, None
    except FetchError as e:
        return src, [], str(e)
    except Exception as e:
        return src, [], f"{e.__class__.__name__}: {e}"


ATS = [
    ("workable", "https://apply.workable.com/api/v1/widget/accounts/{s}",
     lambda d: len(d.get("jobs", [])), lambda d: [j.get("title", "") + " @ " + (j.get("city") or "") for j in d.get("jobs", [])]),
    ("lever", "https://api.lever.co/v0/postings/{s}?mode=json",
     lambda d: len(d) if isinstance(d, list) else 0,
     lambda d: [j.get("text", "") + " @ " + ((j.get("categories") or {}).get("location") or "") for j in d]),
    ("lever_eu", "https://api.eu.lever.co/v0/postings/{s}?mode=json",
     lambda d: len(d) if isinstance(d, list) else 0,
     lambda d: [j.get("text", "") + " @ " + ((j.get("categories") or {}).get("location") or "") for j in d]),
    ("breezy", "https://{s}.breezy.hr/json",
     lambda d: len(d) if isinstance(d, list) else 0,
     lambda d: [j.get("name", "") + " @ " + str((j.get("location") or {}).get("name", "")) for j in d]),
    ("recruitee", "https://{s}.recruitee.com/api/offers/",
     lambda d: len(d.get("offers", [])), lambda d: [j.get("title", "") + " @ " + (j.get("location") or "") for j in d.get("offers", [])]),
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{s}/jobs",
     lambda d: len(d.get("jobs", [])), lambda d: [j.get("title", "") + " @ " + (j.get("location") or {}).get("name", "") for j in d.get("jobs", [])]),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{s}",
     lambda d: len(d.get("jobs", [])), lambda d: [j.get("title", "") + " @ " + (j.get("location") or "") for j in d.get("jobs", [])]),
]


def ats_check(slug):
    found = []
    for name, tpl, count, sample in ATS:
        url = tpl.format(s=slug)
        try:
            r = get(url, timeout=15, retries=0, accept_json=True)
            data = r.json()
        except (FetchError, ValueError):
            continue
        n = count(data)
        if n or name in ("workable", "breezy", "recruitee"):
            # ovi vraćaju 200 samo kad račun postoji, pa i 0 oglasa znači "postoji"
            found.append({"ats": name, "slug": slug, "url": url, "jobs": n, "sample": sample(data)[:6]})
    # Teamtailor (RSS)
    try:
        r = get(f"https://{slug}.teamtailor.com/jobs.rss", timeout=15, retries=0)
        if "<rss" in r.text[:500]:
            found.append({"ats": "teamtailor", "slug": slug, "url": r.url,
                          "jobs": r.text.count("<item>"), "sample": []})
    except FetchError:
        pass
    return slug, found


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = yaml.safe_load((ROOT / "probe.yaml").read_text(encoding="utf-8"))
    out = ["# Proba izvora\n"]

    # --- 1) izvori
    sources = cfg.get("sources", [])
    browser = Browser()
    plain = [s for s in sources if not s.get("js")]
    js = [s for s in sources if s.get("js")]
    results = []
    with cf.ThreadPoolExecutor(max_workers=10) as pool:
        results += list(pool.map(lambda s: probe_source(s, None), plain))
    for s in js:
        results.append(probe_source(s, browser))
    browser.close()
    out.append("## Izvori\n")
    out.append("| id | status | oglasa |\n|---|---|---|")
    for src, items, err in results:
        out.append(f"| {src['id']} | {'✅' if not err and items else '❌'} | {len(items)} {err or ''} |")
    for src, items, err in results:
        out.append(f"\n### {src['id']} ({len(items)})\n")
        if err:
            out.append(f"Greška: {err}\n")
        for i in items[:12]:
            ok, why = decide(src, i)
            out.append(f"- {i['title'][:90]} | {i.get('company', '')[:30]} | {i.get('location', '')[:40]} "
                       f"| lok: {why} | {i['url'][:110]}")

    # --- 2) ATS sonda
    slugs = cfg.get("ats_slugs", [])
    if slugs:
        with cf.ThreadPoolExecutor(max_workers=16) as pool:
            ats = list(pool.map(ats_check, slugs))
        hits = [(s, f) for s, f in ats if f]
        out.append(f"\n## ATS sonda: {len(hits)} od {len(slugs)} imena ima javnu listu\n")
        for slug, found in hits:
            for f in found:
                out.append(f"- **{slug}** → {f['ats']} ({f['jobs']} oglasa) {f['url']}")
                for smp in f["sample"]:
                    out.append(f"  - {smp[:100]}")
        (ROOT / "reports").mkdir(exist_ok=True)
        (ROOT / "reports" / "ats.json").write_text(json.dumps(hits, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")

    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / "probe.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print("Gotovo: reports/probe.md")


if __name__ == "__main__":
    main()
