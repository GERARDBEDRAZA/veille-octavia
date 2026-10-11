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
import argparse, copy, datetime as dt, html as htmllib, json, re, sys, time, urllib.error, urllib.parse, urllib.request
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36 veille-octavia/1.0")
PAUSE = 1.2            # secondes entre deux requêtes vers un même hôte
RETRIES = 3
TIMEOUT = 25
MAX_LINKS_PER_SITE = 5
MAX_DETAIL_PER_SITE = 20

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
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"[\s\u00a0\u2009\u202f]+", " ", t).strip()


def title_of(h):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", h)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


def to_int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


PRICE_RE = re.compile(r"(\d{1,3}(?: \d{3})+|\d{5,7})\s*(?:Kč|CZK)(?!\s*bez\s*DPH)", re.I)
EUR_RE = re.compile(r"(\d{1,3}(?: \d{3})+)\s*€")
KM_RE = re.compile(r"(\d{1,3}(?: \d{3})+|\d{1,6})\s*km\b", re.I)
SOLD_RE = re.compile(r"prodáno|vyprodáno|již prodán|rezervováno|sold\b")


def window(text, vin, r):
    """Texte autour du VIN de la fiche : évite de lire les autres annonces affichées sur la même page."""
    i = text.upper().find(vin) if vin else -1
    return text[max(0, i - r): i + r] if i >= 0 else text[: 2 * r]


def field(text, vin, labels, n=40):
    """Valeur qui suit un libellé de la fiche (« Převodovka : … »), en prenant l'occurrence la plus proche du VIN."""
    pos = text.upper().find(vin) if vin else 0
    best = None
    for m in re.finditer(rf"(?:{labels})\s*[:\-]?\s*(.{{1,{n}}})", text, re.I):
        d = abs(m.start() - max(pos, 0))
        if best is None or d < best[0]:
            best = (d, m.group(1).lower())
    return best[1] if best else ""


def parse_offer(url, html, dealer):
    """Analyse une fiche. Retourne une offre, ou None si hors périmètre (Combi, essence auto, TDI manuelle…)."""
    text = strip_html(html)
    low = text.lower()
    if "octavia" not in low or "exclusive" not in low:
        return None
    vin = next(iter(VIN_RE.findall(url) or VIN_RE.findall(text)), None)
    vin = vin.upper() if vin else None
    if vin:
        if vin[3] == "J":
            return None                                   # Combi
        if not re.match(r"TMBA[RJ]", vin):
            return None                                   # hors périmètre (autre modèle ou motorisation)
        body = "liftback"
    else:
        if re.search(r"\bcombi\b", low[:3000]):
            return None
        body = "inconnue"
    # carburant : le VIN (AR = 1.5 TSI, AJ = 2.0 TDI) fait foi ; boîte : valeur du libellé le plus proche du VIN
    vin_fuel = {"AR": "TSI", "AJ": "TDI"}.get(vin[3:5]) if vin else None
    fuel_txt = field(text, vin, r"palivo|typ paliva|druh paliva|fuel|carburant")
    gear_txt = field(text, vin, r"převodovka|typ převodovky|gearbox|transmission|boîte")
    t_diesel = bool(re.search(r"\btdi\b|nafta|diesel", fuel_txt))
    t_petrol = bool(re.search(r"\btsi\b|benz[ií]n|e-tec|hybrid", fuel_txt))
    txt_fuel = "TDI" if t_diesel and not t_petrol else "TSI" if t_petrol and not t_diesel else None
    fuel = vin_fuel or txt_fuel or "?"
    conflict = bool(vin_fuel and txt_fuel and vin_fuel != txt_fuel)
    auto = bool(re.search(r"\bdsg\b|automat|7[\s-]?stup", gear_txt))
    manual = bool(re.search(r"manu[aá]ln|\bmanual|6[\s-]?stup", gear_txt))
    gear = "auto" if auto and not manual else "manuel" if manual and not auto else "?"
    if fuel == "TSI" and gear == "auto":
        return None                                       # essence automatique : hors périmètre
    if fuel == "TDI" and gear == "manuel":
        return None                                       # TDI manuelle : hors périmètre
    base = ("BVM6" if fuel == "TSI" and gear == "manuel" else "TDI DSG7" if fuel == "TDI" and gear == "auto"
            else "BVM6" if fuel == "TSI" else "TDI" if fuel == "TDI" else "à préciser")
    uncertain = gear == "?" or fuel == "?" or conflict or body == "inconnue"
    tag = base + (" · à préciser" if uncertain and base != "à préciser" else "")
    w2 = window(text, vin, 1500).lower()
    if re.search(r"po opravě|po nehodě|havarovan|accident", w2):
        tag += " · réparée"
    sold = bool(SOLD_RE.search(w2))
    pm, em, km = PRICE_RE.search(text), EUR_RE.search(text), KM_RE.search(text)
    w3 = window(text, vin, 2500).lower()
    equip = [lab for lab, rx in [
        ("HUD", r"\bhud\b|head[- ]?up"), ("360°", r"360|area view"), ("Canton", r"canton"), ("Matrix", r"matrix"),
        ("DCC", r"\bdcc\b"), ("tri-zone", r"3[- ]?zón|tri[- ]?zone|klima paket"), ("attelage", r"tažn|trailer"),
        ("ventilation", r"ventilac|ventilov"), ("massage", r"mas[áa]ž")] if re.search(rx, w3)]
    note = " Incohérence VIN / texte sur la motorisation." if conflict else ""
    return {
        "tag": tag, "dealer": dealer,
        "price": to_int(pm.group(1)) if pm else (to_int(em.group(1)) if em else None),
        "currency": "CZK" if pm or not em else "EUR",
        "km": to_int(km.group(1)) if km else None,
        "status": "Vendue ou réservée (à vérifier)" if sold else "Annonce publiée",
        "vin": vin,
        "details": ("Équipements détectés : " + ", ".join(equip) + ". " if equip else "")
                   + "Détection automatique : équipements, prix et kilométrage à confirmer par VIN auprès du vendeur." + note,
        "url": url, "_sold": sold, "_body": body,
    }


def stock_links(base, html):
    """Liens d'annonces du portail Škoda (le VIN est dans l'URL). Les fiches « exclusive » passent en premier ;
    le tri définitif se fait à la lecture de la fiche."""
    out = []
    for m in STOCK_LINK.finditer(html):
        slug = m.group(2).lower()
        if "octavia" in slug and "combi" not in slug:
            out.append((0 if "exclusive" in slug else 1, urllib.parse.urljoin(base, m.group(0))))
    return [u for _, u in sorted(dict.fromkeys(out))]


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


def recheck_known(o):
    """Revisite l'URL d'une offre déjà suivie : met à jour prix / km, signale lien mort ou vente."""
    o = dict(o)
    u = o.get("url")
    if not u or not u.startswith("http"):
        return o, False
    c, body, _ = fetch(u)
    if c in (404, 410):
        first = not str(o.get("status", "")).startswith("Lien indisponible")
        o["status"] = f"Lien indisponible (HTTP {c}) · offre probablement retirée"
        return o, first
    if c != 200:
        return o, False                                   # dernier état connu conservé
    text = strip_html(body)
    if SOLD_RE.search(text.lower()):
        o["status"] = "Signalée vendue ou réservée (à vérifier)"
    elif str(o.get("status", "")).startswith(("Lien indisponible", "Signalée")):
        o["status"] = "Annonce publiée"
    m = (PRICE_RE if o.get("currency", "CZK") == "CZK" else EUR_RE).search(text)
    if m:
        new = to_int(m.group(1))
        old = o.get("price")
        if new and (not old or 0.7 * old <= new <= 1.3 * old):     # garde-fou contre une mauvaise lecture
            o["price"] = new
    k = KM_RE.search(text)
    if k and o.get("km") is not None:
        newkm = to_int(k.group(1))
        if newkm is not None and o["km"] * 0.9 <= newkm <= o["km"] + 30000:
            o["km"] = newkm
    return o, False


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

    # offres déjà suivies : on revisite leur URL (prix, km, lien mort) au lieu de les écarter
    known = prev.get("offers", [])
    rechecked, gone_now = [], []
    for o in known:
        o2, was_gone = recheck_known(o)
        rechecked.append(o2)
        if was_gone:
            gone_now.append(o2)
    by = {key(o): o for o in rechecked}
    for o in found:                                       # offres trouvées sur les portails Škoda
        if o.pop("_sold", False):
            continue
        o.pop("_body", None)
        k = key(o)
        if k in by:                                       # même annonce : mise à jour avec garde-fous
            old = by[k]
            if o.get("price") and o.get("currency") == old.get("currency") and (
                    not old.get("price") or 0.7 * old["price"] <= o["price"] <= 1.3 * old["price"]):
                old["price"] = o["price"]
            if o.get("km") is not None and (old.get("km") is None or old["km"] * 0.9 <= o["km"] <= old["km"] + 30000):
                old["km"] = o["km"]
        else:
            by[k] = o
    offers = list(by.values())
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
    out["changes"] = merge_changes(known, offers)
    for o in gone_now:
        out["changes"]["gone"].append({k_: o.get(k_) for k_ in ("dealer", "vin", "url")})
    json.dump(out, open(a.data, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"{len(rows)} sites contrôlés en {time.time() - t0:.0f} s · {len(offers)} offres · "
        f"nouvelles {len(out['changes']['new'])} · disparues {len(out['changes']['gone'])} · prix {len(out['changes']['price'])}")


if __name__ == "__main__":
    main()
