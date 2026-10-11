#!/usr/bin/env python3
"""Veille Octavia Exclusive Selection : met à jour data.json.

Principes
- Aucune dépendance externe (bibliothèque standard uniquement).
- Collecte respectueuse : User-Agent standard, pauses, nouvelles tentatives avec temporisation
  croissante, respect de robots.txt. Aucun contournement de protection (CAPTCHA, WAF, empreintes).
- Deux niveaux de couverture par concessionnaire : « site propre » et « portail Škoda »
  (<concession>.skoda-auto.cz), afin que la couverture affichée reste honnête.
- Les règles de tri (liftback, BVM6 / DSG7, exclusion des Combi, vendues, réparées) sont en code.

Usage : python crawler.py [--data data.json] [--max-sites N] [--only MOT]
"""
import argparse, copy, datetime as dt, json, re, sys, time, urllib.error, urllib.parse, urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 veille-octavia/1.0")
PAUSE = 1.2            # secondes entre deux requêtes vers un même hôte
RETRIES = 3
TIMEOUT = 25
MAX_LINKS_PER_SITE = 5
MAX_DETAIL_PER_SITE = 8

# --- Règles de tri -------------------------------------------------------------------------
# Octavia IV : 4e caractère du VIN. A = liftback, J = Combi (heuristique issue des VIN observés).
LIFTBACK_VIN = re.compile(r"^TMBA[A-Z0-9]{13}$", re.I)
COMBI_VIN = re.compile(r"^TMBJ[A-Z0-9]{13}$", re.I)
VIN_RE = re.compile(r"\bTMB[A-HJ-NPR-Z0-9]{14}\b", re.I)
STOCK_LINK = re.compile(r"/(skladove-vozy|ojete-vozy|predvadeci)/([a-z0-9\-]+)/([a-z0-9]{17})/(\d+)", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --- Réseau --------------------------------------------------------------------------------
def safe_url(url):
    """Encode correctement chemin et requête (corrige l'erreur « ascii codec » : Užitkové…)."""
    p = urllib.parse.urlsplit(url.strip())
    host = p.hostname.encode("idna").decode("ascii") if p.hostname else ""
    netloc = host + (f":{p.port}" if p.port else "")
    return urllib.parse.urlunsplit((
        p.scheme or "https", netloc,
        urllib.parse.quote(urllib.parse.unquote(p.path), safe="/%:@!$&'()*+,;=-._~"),
        urllib.parse.quote(urllib.parse.unquote(p.query), safe="=&%:@!$'()*+,;/?-._~[]"),
        ""))


_last = {}


def fetch(url, retries=RETRIES):
    """Retourne (code, texte, erreur). Respecte une pause par hôte et retente sur 429/5xx/réseau."""
    try:
        u = safe_url(url)
    except Exception as e:                          # URL invalide
        return 0, "", f"URL invalide : {e}"
    host = urllib.parse.urlsplit(u).hostname
    code, body, err = 0, "", ""
    for i in range(retries):
        wait = _last.get(host, 0) + PAUSE - time.time()
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(u, headers={
            "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json;q=0.8",
            "Accept-Language": "cs-CZ,cs;q=0.9,fr;q=0.6,en;q=0.5"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read(3_000_000)
                cs = r.headers.get_content_charset() or "utf-8"
                code, body, err = r.status, raw.decode(cs, "replace"), ""
        except urllib.error.HTTPError as e:
            code, err = e.code, f"HTTP {e.code}"
        except Exception as e:
            code, err = 0, f"{type(e).__name__}: {e}"[:140]
        _last[host] = time.time()
        if code == 200 or code in (401, 403, 404, 410, 451):   # inutile de réessayer
            break
        time.sleep(2 * (i + 1))                                   # 2 s, 4 s, 6 s
    return code, body, err


_robots = {}


def allowed(url):
    p = urllib.parse.urlsplit(url)
    origin = f"{p.scheme}://{p.netloc}"
    if origin not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            c, txt, _ = fetch(origin + "/robots.txt", retries=1)
            rp.parse(txt.splitlines() if c == 200 else [])
        except Exception:
            rp.parse([])
        _robots[origin] = rp
    try:
        return _robots[origin].can_fetch(UA, url)
    except Exception:
        return True


# --- Extraction ----------------------------------------------------------------------------
def strip_html(h):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()


def title_of(h):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", h)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def to_int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def parse_offer(url, html, dealer):
    """Analyse une fiche d'annonce. Retourne un dict d'offre ou None si ce n'est pas une Octavia Exclusive."""
    text = strip_html(html)
    low = text.lower()
    if "octavia" not in low or "exclusive" not in low:
        return None
    vin = next((v.upper() for v in VIN_RE.findall(html + " " + url)), None)
    # carrosserie : VIN d'abord, puis texte
    if vin and COMBI_VIN.match(vin):
        body = "combi"
    elif vin and LIFTBACK_VIN.match(vin):
        body = "liftback"
    else:
        body = "combi" if re.search(r"\bcombi\b", low) else "inconnue"
    if body == "combi":
        return None
    sold = bool(re.search(r"prodáno|prodan[oý]|rezervov|vyprodán|sold\b", low))
    repaired = bool(re.search(r"po (opravě|nehodě)|havarovan|bourané|accident", low))
    fuel = "TDI" if re.search(r"\btdi\b|nafta|diesel", low) else "TSI" if re.search(r"\btsi\b|benzín|benzin", low) else "?"
    auto = bool(re.search(r"\bdsg\b|automat|7[\s-]?stup", low))
    manual = bool(re.search(r"manuáln|manual|6[\s-]?stup", low)) and not re.search(r"\bdsg\b", low)
    if fuel == "TSI" and manual:
        tag = "BVM6"
    elif fuel == "TDI" and auto:
        tag = "TDI DSG7"
    elif fuel == "TSI" and auto:
        return None                       # essence automatique : hors périmètre
    else:
        tag = "à préciser"
    if body == "inconnue":
        tag += " · à préciser"
    if repaired:
        tag += " · réparée"
    pm = re.search(r"(\d{1,3}(?:[   .]\d{3})+|\d{5,7})\s*(?:Kč|CZK)", text)
    em = re.search(r"(\d{1,3}(?:[   .]\d{3})+)\s*€", text)
    km = re.search(r"(\d{1,3}(?:[   .]\d{3})*|\d{1,6})\s*km\b", text, re.I)
    equip = []
    for label, rx in [("HUD", r"\bhud\b|head[- ]?up"), ("360°", r"360|area view|kamer"), ("Canton", r"canton"),
                      ("Matrix", r"matrix"), ("DCC", r"\bdcc\b"), ("tri-zone", r"3[- ]?zón|tri[- ]?zone|klima paket"),
                      ("attelage", r"tažn|trailer"), ("ventilation", r"ventilac|ventilov"),
                      ("massage", r"mas[áa]ž"), ("pare-brise chauffant", r"vyhřívan\w* (čelní|přední) sklo")]:
        if re.search(rx, low):
            equip.append(label)
    return {
        "tag": tag, "dealer": dealer,
        "price": to_int(pm.group(1)) if pm else (to_int(em.group(1)) if em else None),
        "currency": "CZK" if pm or not em else "EUR",
        "km": to_int(km.group(1)) if km else None,
        "status": "Vendue ou réservée (à vérifier)" if sold else "Annonce publiée",
        "vin": vin,
        "details": ("Équipements détectés dans l'annonce : " + ", ".join(equip) + ". " if equip else "")
                   + "Détection automatique : DCC, Canton, attelage et tri-zone à confirmer par VIN auprès du vendeur.",
        "url": url, "_sold": sold, "_body": body,
    }


def stock_links(base, html):
    """Liens d'annonces du portail Škoda (le VIN est dans l'URL) ; filtre Octavia Exclusive Selection."""
    out = []
    for m in STOCK_LINK.finditer(html):
        slug = m.group(2).lower()
        if "octavia" in slug and "exclusive" in slug and "combi" not in slug:
            out.append(urllib.parse.urljoin(base, m.group(0)))
    return list(dict.fromkeys(out))


def candidate_links(base, html):
    out = []
    for h in re.findall(r'href=["\']([^"\'#]+)["\']', html):
        u = urllib.parse.urljoin(base, h)
        if urllib.parse.urlsplit(u).hostname != urllib.parse.urlsplit(base).hostname:
            continue
        if re.search(r"skladov|ojet|nabidk|stock|vuz|vozy|octavia|predvadeci", u, re.I) and not re.search(r"\.(jpg|png|pdf|css|js)(\?|$)", u, re.I):
            out.append(u)
    return list(dict.fromkeys(out))[:MAX_LINKS_PER_SITE]


# --- Un concessionnaire --------------------------------------------------------------------
def portal_host(site):
    for p in site.get("checked", []):
        h = urllib.parse.urlsplit(p["url"]).hostname or ""
        if h.endswith(".skoda-auto.cz"):
            return h
    return None


def crawl_site(site):
    row = {"name": site["name"], "url": site["url"], "checked": [], "pages": 0}
    offers, own_ok, portal_ok = [], False, False
    home_url = site["url"]

    def visit(u):
        if not allowed(u):
            row["checked"].append({"url": u, "title": "exclu par robots.txt", "result": "robots"})
            return None
        c, body, err = fetch(u)
        row["checked"].append({"url": u, "title": title_of(body) or u, "result": str(c) if c == 200 else (err or str(c))})
        if c == 200:
            row["pages"] += 1
            return body
        return None

    # niveau 1 : site propre
    home = visit(home_url)
    if home:
        own_ok = True
        for u in candidate_links(home_url, home):      # pages de stock du site propre (journal de couverture)
            visit(u)
    # niveau 2 : portail Škoda (stock officiel, VIN dans l'URL)
    ph = portal_host(site)
    if ph:
        seen = []
        for path in ("/skladove-vozy", "/ojete-vozy"):
            u = f"https://{ph}{path}"
            h = visit(u)
            if h:
                portal_ok = True
                seen += stock_links(u, h)
        for u in list(dict.fromkeys(seen))[:MAX_DETAIL_PER_SITE]:
            h = visit(u)
            if h:
                o = parse_offer(u, h, site["name"])
                if o:
                    offers.append(o)
    if own_ok and portal_ok:
        row["status"] = "Site propre et portail Škoda accessibles · contrôle partiel du stock"
    elif own_ok:
        row["status"] = "Site propre accessible · contrôle partiel du stock"
    elif portal_ok:
        row["status"] = "Portail Škoda accessible, site propre injoignable · contrôle partiel du stock"
    else:
        row["status"] = "Non vérifiable · échec d’accès"
    row["reachable"] = own_ok or portal_ok
    row["own_site_ok"], row["portal_ok"] = own_ok, portal_ok
    return row, offers


# --- Fusion, changements -------------------------------------------------------------------
def key(o):
    return o.get("vin") or o.get("url")


def merge_changes(prev, new):
    pk, nk = {key(o): o for o in prev}, {key(o): o for o in new}
    ch = {"new": [], "gone": [], "price": []}
    for k, o in nk.items():
        if k not in pk:
            ch["new"].append({k_: o[k_] for k_ in ("dealer", "vin", "url", "price", "currency")})
        elif pk[k].get("price") != o.get("price") and o.get("price") and pk[k].get("price"):
            ch["price"].append({"dealer": o["dealer"], "vin": o.get("vin"), "url": o.get("url"),
                                "before": pk[k]["price"], "after": o["price"], "currency": o["currency"]})
    for k, o in pk.items():
        if k not in nk:
            ch["gone"].append({k_: o.get(k_) for k_ in ("dealer", "vin", "url")})
    return ch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data.json")
    ap.add_argument("--max-sites", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()

    prev = json.load(open(a.data, encoding="utf-8"))
    sites = prev["sites"]
    if a.only:
        sites = [s for s in sites if a.only.lower() in s["name"].lower()]
    if a.max_sites:
        sites = sites[:a.max_sites]

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:     # un fil par concessionnaire ; pause par hôte
        results = list(ex.map(crawl_site, sites))
    rows = [r for r, _ in results]
    found = [o for _, os_ in results for o in os_]

    # offres : on conserve la fiche précédente (détails relus à la main) quand le VIN est déjà connu
    prev_by = {key(o): o for o in prev.get("offers", [])}
    offers = []
    for o in found:
        if o.pop("_sold", False):
            continue
        o.pop("_body", None)
        old = prev_by.get(key(o))
        if old:
            o["details"] = old.get("details") or o["details"]
            o["tag"] = old.get("tag") or o["tag"]
        offers.append(o)
    # offres connues non retrouvées sur les sites contrôlés cette fois-ci : conservées « à revalider »
    # seulement si leur dépôt n'a pas été re-contrôlé (évite de les déclarer disparues à tort)
    checked = {r["name"] for r in rows}
    for o in prev.get("offers", []):
        if key(o) not in {key(x) for x in offers} and o.get("dealer") and not any(o["dealer"].split(" · ")[0] in n for n in checked):
            offers.append(o)
    offers.sort(key=lambda o: (o["tag"].startswith("TDI"), o.get("price") or 10**9))

    # journal : on remplace les lignes recontrôlées, on garde les autres
    by_name = {r["name"]: r for r in rows}
    sites_out = [by_name.get(s["name"], s) for s in prev["sites"]]
    out = copy.deepcopy(prev)
    out["sites"] = sites_out
    out["offers"] = offers
    out["generated_at"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=2))).isoformat(timespec="seconds")
    out["subtitle"] = re.sub(r"Actualisation du [^—]+—", "Actualisation du " + dt.date.today().strftime("%d/%m/%Y") + " —", prev.get("subtitle", ""))
    ok = sum(1 for s in sites_out if s.get("reachable"))
    out["coverage"] = {"domains": len(sites_out), "ok": ok, "ko": len(sites_out) - ok,
                       "pages": sum(s.get("pages", 0) for s in sites_out),
                       "own_site_ok": sum(1 for s in sites_out if s.get("own_site_ok")),
                       "portal_ok": sum(1 for s in sites_out if s.get("portal_ok"))}
    out["coverage_note"] = prev.get("coverage_note", "")
    out["changes"] = merge_changes(prev.get("offers", []), offers)
    json.dump(out, open(a.data, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"{len(rows)} sites contrôlés en {time.time() - t0:.0f} s · {len(offers)} offres · "
        f"nouvelles {len(out['changes']['new'])} · disparues {len(out['changes']['gone'])} · prix {len(out['changes']['price'])}")


if __name__ == "__main__":
    main()
