"""Izvlačenje oglasa iz različitih vrsta izvora.

Svaki handler vraća listu dict-ova: {key, title, url, company, location}.
`key` je stabilan identifikator oglasa (obično normalizovan URL).
"""
import hashlib
import json
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode, quote_plus

import feedparser
from bs4 import BeautifulSoup

from .fetch import get, post_json, FetchError

log = logging.getLogger("radar.extract")

MAX_ITEMS = 400
LAST_HTML = {}  # id izvora -> zadnji dohvaćeni HTML (za reports/debug)

_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
             "fbclid", "gclid", "ref", "refid", "trackingid", "trk", "position", "pagenum"}

_SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".ico",
             ".zip", ".mp4", ".mp3")

_NAV_TEXT = re.compile(
    r"^(pocetna|naslovna|home|kontakt\w*|contact\w*|o nama|about( us)?|prijava|prijavi se|login|"
    r"log in|registr\w*|sign (in|up)|privatnost|privacy\w*|politika privatnosti|cookie\w*|"
    r"kolacic\w*|uslovi\w*|uvjeti\w*|terms\w*|impressum|faq|pomoc|help|blog|vijesti|novosti|"
    r"news|galerija|gallery|mapa sajta|sitemap|nazad|back|vise|more|procitaj vise|read more|"
    r"detaljnije|saznaj vise|pogledaj|pogledaj sve|sve|svi oglasi|all jobs|next|prev\w*|"
    r"sljedec\w*|prethodn\w*|\d+|\W*)$"
)
_NAV_HREF = re.compile(
    r"(facebook\.com|instagram\.com|twitter\.com|x\.com/|youtube\.com|tiktok\.com|"
    r"linkedin\.com/(company|in|school)/|wa\.me|viber:|/kontakt|/contact|/privacy|/politika|"
    r"/cookie|/login|/prijava$|/register|/registracija|/wp-login|/feed/?$|/tag/|/author/|"
    r"/category/(?!javni-oglasi)|\?share=|/page/\d+|[?&]page=\d+|/search)",
    re.I,
)


def clean_url(url: str) -> str:
    """Ukloni tracking parametre i fragment, da isti oglas uvijek ima isti ključ."""
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in _TRACKING]
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(q), ""))
    except Exception:
        return url


def _txt(s) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _norm_ascii(s: str) -> str:
    from .keywords import normalize
    return normalize(s)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _base_domain(host: str) -> str:
    parts = host.split(".")
    # .com.ba / .gov.ba / .co.ba zadrži tri dijela
    if len(parts) >= 3 and parts[-2] in {"com", "gov", "co", "org", "edu", "net"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


_GENERIC_TEXT = re.compile(
    r"^(detaljnije|vise|opsirnije|saznaj vise|procitaj vise|pogledaj( oglas| vise)?|"
    r"read more|more|more info|details|view( job| details)?|apply( now)?|prijavi se|"
    r"apliciraj|otvori|link|ovdje|here|klikni ovdje|preuzmi|download)\W*$"
)


def _anchor_text(a) -> str:
    text = _txt(a.get_text(" "))
    if len(text) < 6:
        text = _txt(a.get("title") or a.get("aria-label") or text)
    if len(text) < 6 or _GENERIC_TEXT.match(_norm_ascii(text)):
        # naslov oglasa je često u kartici oko linka ("Detaljnije" dugme)
        node = a
        for _ in range(4):
            node = node.parent
            if node is None or node.name in ("body", "main", "html"):
                break
            if len(node.find_all("a", href=True)) > 3:
                break
            head = node.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
            if head and len(_txt(head.get_text(" "))) >= 6:
                return _txt(head.get_text(" "))
    return text


def links_from_html(html: str, base_url: str, src: dict) -> list:
    """Generički izvlač: linkovi koji liče na pojedinačne oglase.

    Opcije izvora:
      pattern   regex nad apsolutnim URL-om koji oglas MORA zadovoljiti
      exclude   regex nad URL-om ili tekstom koji isključuje link
      selector  CSS selektor koji ograničava dio stranice
      any_domain  dozvoli linkove na druge domene (npr. ATS)
    """
    soup = BeautifulSoup(html, "lxml")
    scope = soup
    if src.get("selector"):
        found = soup.select(src["selector"])
        if found:
            scope = BeautifulSoup("".join(str(x) for x in found), "lxml")
    for bad in scope.select("nav, header, footer, script, style, noscript"):
        if not src.get("keep_nav"):
            bad.decompose()

    pattern = re.compile(src["pattern"], re.I) if src.get("pattern") else None
    exclude = re.compile(src["exclude"], re.I) if src.get("exclude") else None
    listing = clean_url(base_url)
    base_dom = _base_domain(_domain(base_url))
    min_len = src.get("min_title", 6)
    strip_query = src.get("strip_query", False)
    key_rx = re.compile(src["key_pattern"], re.I) if src.get("key_pattern") else None

    found = {}
    for a in scope.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = clean_url(urljoin(base_url, href))
        if strip_query:
            url = url.split("?", 1)[0]
        if not url.startswith("http"):
            continue
        if url == listing or url.lower().endswith(_SKIP_EXT):
            continue
        text = _anchor_text(a)
        if _GENERIC_TEXT.match(_norm_ascii(text)) or len(text) < min_len:
            slug = _slug_title(url)
            if pattern and slug:
                text = slug
        if exclude and (exclude.search(url) or exclude.search(text)):
            continue
        if pattern:
            if not pattern.search(url):
                continue
        else:
            if not _looks_like_job(url, text, base_dom, src, min_len):
                continue
        if len(text) < min_len and not url.lower().endswith(".pdf"):
            continue
        if url.lower().endswith(".pdf") and len(text) < min_len:
            text = urlparse(url).path.rsplit("/", 1)[-1]
        key = url
        if key_rx:
            m = key_rx.search(url)
            key = m.group(1) if m else url
        prev = found.get(key)
        # isti oglas zna imati više linkova (slika, naslov, dugme): zadrži najbolji naslov
        if not prev or _title_score(text) > _title_score(prev["title"]):
            found[key] = {"key": key, "title": text[:240], "url": url}
        if len(found) >= MAX_ITEMS:
            break
    return list(found.values())


_HEXISH = re.compile(r"^[0-9a-f]{6,}([- ][0-9a-f]{3,})*$", re.I)


def _title_score(text: str) -> int:
    """Pravi naslov je bolji od slug-a ili UUID-a, duži je bolji od kraćeg."""
    if not text or _HEXISH.match(text.strip()):
        return 0
    return len(text) + (100 if " " in text else 0)


def _slug_title(url: str) -> str:
    """'/posao/strucni-saradnik-ikt-99/' -> 'Strucni saradnik ikt'."""
    segs = [s for s in urlparse(url).path.split("/") if s]
    for seg in reversed(segs):
        if _HEXISH.match(seg):
            continue
        words = re.sub(r"\.(html?|php|aspx?)$", "", seg)
        words = re.sub(r"[-_+]+", " ", words)
        words = re.sub(r"\b\d+\b", "", words).strip()
        if len(words) >= 6:
            return words[:1].upper() + words[1:]
    return ""


def _looks_like_job(url, text, base_dom, src, min_len) -> bool:
    if _NAV_HREF.search(url):
        return False
    if _NAV_TEXT.match(_norm_ascii(text)):
        return False
    if len(text) < min_len or len(text) > 220:
        return False
    if not src.get("any_domain") and _base_domain(_domain(url)) != base_dom:
        return False
    path = urlparse(url).path
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    return (len(segs) >= 2 or bool(re.search(r"\d", path)) or len(segs[-1]) >= 18
            or path.lower().endswith(".pdf"))


def handle_links(src, browser):
    url = src["url"]
    if src.get("js"):
        html = browser.html(url, wait_selector=src.get("wait_for"))
    else:
        html = get(url, insecure=src.get("insecure", False)).text
    LAST_HTML[src["id"]] = html
    return links_from_html(html, url, src)


def handle_rss(src, browser):
    r = get(src.get("feed") or src["url"], insecure=src.get("insecure", False))
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise FetchError(f"RSS nije validan: {feed.bozo_exception}")
    items = []
    for e in feed.entries[:MAX_ITEMS]:
        link = clean_url(e.get("link") or "")
        title = _txt(e.get("title"))
        if not link or not title:
            continue
        items.append({"key": link, "title": title, "url": link,
                      "company": _txt(e.get("job_listing_company") or e.get("author") or "")})
    return items


def _walk_jsonld(node, out):
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, out)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if "JobPosting" in types:
        out.append(node)
    for key in ("@graph", "itemListElement", "item", "mainEntity"):
        if key in node:
            _walk_jsonld(node[key], out)
    if "ListItem" in types and "url" in node and "item" not in node:
        out.append({"title": node.get("name"), "url": node.get("url")})


def jsonld_items(html, base_url):
    soup = BeautifulSoup(html, "lxml")
    raw = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            _walk_jsonld(json.loads(s.string or s.get_text() or ""), raw)
        except Exception:
            continue
    items = {}
    for j in raw:
        title = _txt(j.get("title") or j.get("name"))
        link = j.get("url") or j.get("sameAs") or ""
        if not title or not link:
            continue
        link = clean_url(urljoin(base_url, link))
        org = j.get("hiringOrganization") or {}
        company = _txt(org.get("name") if isinstance(org, dict) else str(org))
        loc = j.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        addr = loc.get("address", {}) if isinstance(loc, dict) else {}
        city = _txt(addr.get("addressLocality") if isinstance(addr, dict) else "")
        items[link] = {"key": link, "title": title[:240], "url": link,
                       "company": company, "location": city}
    return list(items.values())


def handle_jsonld(src, browser):
    url = src["url"]
    if src.get("js"):
        html = browser.html(url)
    else:
        html = get(url, insecure=src.get("insecure", False)).text
    LAST_HTML[src["id"]] = html
    items = jsonld_items(html, url)
    if not items:
        items = links_from_html(html, url, src)
    return items


def handle_workable(src, browser):
    acc = src["account"]
    r = get(f"https://apply.workable.com/api/v1/widget/accounts/{acc}", accept_json=True)
    data = r.json()
    items = []
    for j in data.get("jobs", []):
        link = clean_url(j.get("url") or j.get("application_url") or
                         f"https://apply.workable.com/{acc}/j/{j.get('shortcode')}")
        loc = ", ".join(x for x in [j.get("city"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc += " (remote)"
        items.append({"key": link, "title": _txt(j.get("title")), "url": link,
                      "company": data.get("name") or src["name"], "location": loc})
    return items


def handle_linkedin(src, browser):
    """LinkedIn javna (guest) pretraga, bez prijave. Oglasi iz zadnjih 7 dana.

    `searches` je lista {location, remote}: remote: true dodaje filter
    "Remote + Hybrid" (f_WT=2,3), pa takvi oglasi prolaze bez provjere lokacije.
    """
    items = {}
    searches = src.get("searches") or [{"location": src.get("location_query", "Bosnia and Herzegovina")}]
    for search in searches:
        wt = "&f_WT=2%2C3" if search.get("remote") else ""
        for kw in src["queries"]:
            url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                   f"?keywords={quote_plus(kw)}&location={quote_plus(search['location'])}"
                   f"&f_TPR=r604800&sortBy=DD&start=0{wt}")
            try:
                html = get(url, retries=1).text
            except FetchError as e:
                log.info("LinkedIn '%s' nije uspio: %s", kw, e)
                continue
            soup = BeautifulSoup(html, "lxml")
            for card in soup.select("li"):
                a = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
                if not a:
                    continue
                m = re.search(r"(\d{6,})", a["href"])
                if not m:
                    continue
                job_id = m.group(1)
                title = _txt((card.select_one("h3") or a).get_text(" "))
                h4 = card.select_one("h4")
                company = _txt(h4.get_text(" ")) if h4 else ""
                loc_el = card.select_one(".job-search-card__location")
                loc = _txt(loc_el.get_text(" ")) if loc_el else ""
                link = f"https://www.linkedin.com/jobs/view/{job_id}"
                prev = items.get(link)
                item = {"key": link, "title": title, "url": link, "company": company,
                        "location": loc + (" (remote/hybrid)" if search.get("remote") else "")}
                if search.get("remote") or "sarajev" in search["location"].lower():
                    item["loc_ok"] = True
                if not prev or item.get("loc_ok"):
                    items[link] = item
    if not items:
        raise FetchError("LinkedIn nije vratio nijedan oglas (moguća blokada)")
    return list(items.values())


# ---------------------------------------------------------------- ATS sistemi (JSON API)

def _job(link, title, company, location=""):
    link = clean_url(link)
    return {"key": link, "title": _txt(title)[:240], "url": link,
            "company": _txt(company), "location": _txt(location)}


def handle_greenhouse(src, browser):
    b = src["board"]
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{b}/jobs", accept_json=True).json()
    return [_job(j["absolute_url"], j["title"], src.get("company", src["name"]),
                 (j.get("location") or {}).get("name", "")) for j in data.get("jobs", [])]


def handle_lever(src, browser):
    c = src["company_id"]
    host = "api.eu.lever.co" if src.get("eu") else "api.lever.co"
    data = get(f"https://{host}/v0/postings/{c}?mode=json", accept_json=True).json()
    out = []
    for j in data:
        cat = j.get("categories") or {}
        loc = ", ".join(x for x in [cat.get("location"), j.get("workplaceType")] if x)
        out.append(_job(j["hostedUrl"], j["text"], src.get("company", src["name"]), loc))
    return out


def handle_ashby(src, browser):
    b = src["board"]
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{b}", accept_json=True).json()
    out = []
    for j in data.get("jobs", []):
        loc = j.get("location") or ""
        if j.get("isRemote"):
            loc += " (remote)"
        out.append(_job(j.get("jobUrl") or j.get("applyUrl"), j["title"],
                        src.get("company", src["name"]), loc))
    return out


def handle_smartrecruiters(src, browser):
    c = src["company_id"]
    data = get(f"https://api.smartrecruiters.com/v1/companies/{c}/postings?limit=100",
               accept_json=True).json()
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        where = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
        if loc.get("remote"):
            where += " (remote)"
        url = f"https://jobs.smartrecruiters.com/{c}/{j['id']}"
        out.append(_job(url, j["name"], (j.get("company") or {}).get("name") or src["name"], where))
    return out


def handle_recruitee(src, browser):
    c = src["company_id"]
    data = get(f"https://{c}.recruitee.com/api/offers/", accept_json=True).json()
    out = []
    for j in data.get("offers", []):
        loc = j.get("location") or ""
        if j.get("remote"):
            loc += " (remote)"
        out.append(_job(j.get("careers_url") or j.get("url"), j["title"],
                        j.get("company_name") or src["name"], loc))
    return out


def handle_personio(src, browser):
    c = src["company_id"]
    dom = src.get("domain", "de")
    r = get(f"https://{c}.jobs.personio.{dom}/xml")
    soup = BeautifulSoup(r.content, "xml")
    out = []
    for p in soup.find_all("position"):
        pid = p.find("id").get_text() if p.find("id") else ""
        name = p.find("name").get_text() if p.find("name") else ""
        office = p.find("office").get_text() if p.find("office") else ""
        out.append(_job(f"https://{c}.jobs.personio.{dom}/job/{pid}", name, src["name"], office))
    return out


def handle_breezy(src, browser):
    c = src["company_id"]
    data = get(f"https://{c}.breezy.hr/json", accept_json=True).json()
    out = []
    for j in data:
        loc = j.get("location") or {}
        where = loc.get("name", "") if isinstance(loc, dict) else str(loc)
        if (isinstance(loc, dict) and loc.get("is_remote")) or "remote" in str(j.get("type", "")).lower():
            where += " (remote)"
        out.append(_job(j.get("url") or f"https://{c}.breezy.hr/p/{j.get('friendly_id')}",
                        j.get("name", ""), (j.get("company") or {}).get("name") or src["name"], where))
    return out


def handle_workday(src, browser):
    """Workday javni CXS API (npr. Infobip). `site` = https://x.wdN.myworkdayjobs.com/{tenant}/{board}"""
    base = src["site"].rstrip("/")
    host = re.match(r"https://[^/]+", base).group(0)
    tenant = src.get("tenant") or re.match(r"https://([^.]+)\.", base).group(1)
    board = base.split("/")[-1]
    api = f"{host}/wday/cxs/{tenant}/{board}/jobs"
    out = {}
    for search in src.get("searches", [""]):
        data = post_json(api, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": search})
        for j in data.get("jobPostings", []):
            url = f"{host}/en-US/{board}{j.get('externalPath', '')}"
            out[url] = _job(url, j.get("title", ""), src.get("company", src["name"]),
                            j.get("locationsText", ""))
    return list(out.values())


def handle_teamtailor(src, browser):
    r = get(f"https://{src['company_id']}.teamtailor.com/jobs.rss")
    feed = feedparser.parse(r.content)
    out = []
    for e in feed.entries:
        loc = ""
        for key in ("tt_location", "location", "tt_locations"):
            if e.get(key):
                loc = str(e.get(key))
                break
        out.append(_job(e.get("link", ""), e.get("title", ""), src["name"], loc))
    return out


def page_text(html: str, selector: str = None) -> list:
    soup = BeautifulSoup(html, "lxml")
    for bad in soup.select("script, style, noscript, nav, header, footer, iframe"):
        bad.decompose()
    root = soup.select_one(selector) if selector else None
    root = root or soup.find("main") or soup.body or soup
    lines = [_txt(x) for x in root.get_text("\n").split("\n")]
    # izbaci prazne i očito dinamične linije (datumi, brojači)
    return [l for l in lines if len(l) > 2 and not re.fullmatch(r"[\d\W]+", l)]


def handle_page_hash(src, browser):
    """Za stranice bez liste oglasa: javi kad se tekst stranice promijeni."""
    url = src["url"]
    if src.get("js"):
        html = browser.html(url)
    else:
        html = get(url, insecure=src.get("insecure", False)).text
    LAST_HTML[src["id"]] = html
    lines = page_text(html, src.get("selector"))
    if not lines:
        raise FetchError("stranica je prazna")
    digest = hashlib.sha1("\n".join(lines).encode()).hexdigest()[:16]
    return [{"key": f"hash:{digest}", "title": "Stranica je promijenjena", "url": url,
             "_lines": lines[:400], "_page_hash": True}]


HANDLERS = {
    "links": handle_links,
    "rss": handle_rss,
    "jsonld": handle_jsonld,
    "workable": handle_workable,
    "linkedin": handle_linkedin,
    "page_hash": handle_page_hash,
    "greenhouse": handle_greenhouse,
    "lever": handle_lever,
    "ashby": handle_ashby,
    "smartrecruiters": handle_smartrecruiters,
    "recruitee": handle_recruitee,
    "personio": handle_personio,
    "teamtailor": handle_teamtailor,
    "breezy": handle_breezy,
    "workday": handle_workday,
}


def extract(src: dict, browser) -> list:
    """Pokreni handler i primijeni `include` filter (regex nad naslovom, firmom i lokacijom)."""
    items = _extract(src, browser)
    if src.get("include") and src.get("type") != "page_hash":
        rx = re.compile(src["include"], re.I)
        items = [i for i in items if rx.search(" ".join(
            [i.get("title", ""), i.get("company", ""), i.get("location", "")]))]
    return items


def _extract(src: dict, browser) -> list:
    """Pokreni handler; ako ne uspije ili vrati 0, probaj `fallback` konfiguraciju."""
    handler = HANDLERS[src.get("type", "links")]
    try:
        items = handler(src, browser)
        if items or not src.get("fallback"):
            return items
        log.info("%s: 0 oglasa, probam fallback", src["id"])
    except FetchError as e:
        if not src.get("fallback"):
            raise
        log.info("%s: %s, probam fallback", src["id"], e)
    fb = {**src, **src["fallback"]}
    fb.pop("fallback", None)
    return HANDLERS[fb.get("type", "links")](fb, browser)
