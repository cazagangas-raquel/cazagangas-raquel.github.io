#!/usr/bin/env python3
"""Cazagangas Raquel: actualizador automático de deals.json.

Lo ejecuta GitHub Actions (.github/workflows/actualizar-ofertas.yml) cada 2 horas.
No necesita a Claude. Pasos:
  1. Lee deals.json (lista anterior) y data/state.json (memoria del script).
  2. Comprueba el precio actual de las ofertas guardadas en las fichas de las
     tiendas que se dejan leer (JSON-LD / Shopify) y quita las agotadas.
  3. Recoge ofertas nuevas de: Chollómetro (RSS), selecciones del día de
     Xataka, Applesfera, 3DJuegos, Compradicción... (RSS + artículo),
     ofertasenjuguetes, y tiendas de Canarias (MediaMarkt Canarias, Druni,
     Jugueterías Nikki, Worten Canarias, Mandatelo).
  4. Si existe GEMINI_API_KEY, Gemini lee los artículos y páginas de tienda y
     devuelve las ofertas en JSON. Todo lo que devuelve se valida: la URL y la
     foto tienen que aparecer literalmente en la página leída.
  5. Guarda deals.json. El workflow hace el commit.
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEALS = os.path.join(ROOT, "deals.json")
STATE = os.path.join(ROOT, "data", "state.json")
END = dt.datetime(2026, 12, 3, 0, 0, tzinfo=dt.timezone.utc)   # tras el 2-dic no hace nada
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
TODAY = NOW.date().isoformat()

MAX_DEALS = int(os.environ.get("MAX_DEALS", "1000"))
STALE_DAYS = float(os.environ.get("STALE_DAYS", "4"))        # sin verse en ninguna fuente
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "220"))
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "18"))
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL", "").strip(),
                             "gemini-flash-latest", "gemini-2.5-flash",
                             "gemini-flash-lite-latest", "gemini-2.5-flash-lite"] if m]

CATS = ["Gaming", "Móviles", "Apple", "TV y audio", "Tecnología", "Cocina", "Hogar",
        "Electrodomésticos", "Cuidado personal", "Deporte", "Juguetes", "Moda"]
SUBS = ["LEGO", "Playmobil", "Juegos de mesa", "Funko", "Nerf", "Hot Wheels", "Play-Doh", "Otros"]
CANARY = {"Worten Canarias", "MediaMarkt Canarias", "El Corte Inglés Canarias", "Mandatelo",
          "Jugueterías Nikki", "Druni Canarias", "Carrefour Canarias"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})

LOG = []
STATS = {"fuentes_ok": 0, "fuentes_fallo": 0, "paginas": 0, "gemini": 0, "nuevas": 0,
         "quitadas": 0, "bajadas": 0, "subidas": 0, "comprobadas": 0}


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOG.append(msg)


def get(url, **kw):
    kw.setdefault("timeout", 25)
    try:
        r = S.get(url, **kw)
        STATS["paginas"] += 1
        return r
    except requests.RequestException as e:
        log("  ! error", url, type(e).__name__)
        return None


# ---------------------------------------------------------------- utilidades
def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("€", "").replace("EUR", "").replace("\xa0", "").strip()
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif s.count(".") == 1 and len(s.split(".")[1]) == 3:
        s = s.replace(".", "")  # 1.299 -> 1299
    try:
        return round(float(s), 2)
    except ValueError:
        return None


ASIN_RE = re.compile(r"(?:/dp/|/gp/product/|/gp/aw/d/|/exec/obidos/asin/)([A-Z0-9]{10})", re.I)


def clean_url(u):
    """Enlace directo sin afiliados. Amazon -> https://www.amazon.es/dp/ASIN."""
    if not u:
        return None
    u = html.unescape(u.strip())
    if u.startswith("//"):
        u = "https:" + u
    p = urllib.parse.urlparse(u)
    q = urllib.parse.parse_qs(p.query)
    for k in ("url", "u", "murl", "ued", "dest", "destination", "redirect"):   # redirectores
        if k in q and q[k][0].startswith("http"):
            return clean_url(q[k][0])
    host = p.netloc.lower()
    if "amzn.to" in host or "amzn.eu" in host:
        try:
            r = S.head(u, allow_redirects=False, timeout=10)
            loc = r.headers.get("Location")
            if loc:
                return clean_url(loc)
        except requests.RequestException:
            return None
    if "amazon." in host:
        m = ASIN_RE.search(p.path)
        return f"https://www.amazon.es/dp/{m.group(1).upper()}" if m else None
    drop = re.compile(r"^(utm_|tag$|ref|aff|affiliate|clickid|irclickid|gclid|fbclid|mc_|srsltid|"
                      r"sv_|awc|cjevent|ranMID|ranEAID|ranSiteID|tduid|_ga)", re.I)
    keep = [(k, v) for k, vs in q.items() for v in vs if not drop.match(k)]
    return urllib.parse.urlunparse((p.scheme or "https", p.netloc, p.path, "",
                                    urllib.parse.urlencode(keep), ""))


def big_img(u):
    if not u:
        return u
    u = html.unescape(u.strip())
    if u.startswith("//"):
        u = "https:" + u
    u = re.sub(r"\._(?:SL|SX|SY|AC_SL|AC_SX|AC_SY|SS)\d+_", "._SL400_", u)
    u = re.sub(r"/re/\d+x\d+/", "/re/300x300/", u)
    return u


def short(s, n=60):
    s = re.sub(r"\s+", " ", html.unescape(s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip(" ,.-·|") + "…"


def hid(s):
    return hashlib.md5(s.encode()).hexdigest()[:8]


def store_from_url(u):
    h = urllib.parse.urlparse(u or "").netloc.lower()
    table = [("canarias.mediamarkt", "MediaMarkt Canarias"), ("canarias.worten", "Worten Canarias"),
             ("canarias.druni", "Druni Canarias"), ("mandatelo", "Mandatelo"),
             ("jugueteriasnikki", "Jugueterías Nikki"), ("amazon.", "Amazon"),
             ("elcorteingles", "El Corte Inglés"), ("mediamarkt", "MediaMarkt"),
             ("pccomponentes", "PcComponentes"), ("decathlon", "Decathlon"), ("fnac", "Fnac"),
             ("aliexpress", "AliExpress"), ("miravia", "Miravia"), ("primor", "Primor"),
             ("druni", "Druni"), ("leroymerlin", "Leroy Merlin"), ("carrefour", "Carrefour"),
             ("worten", "Worten"), ("lidl", "Lidl"), ("toysrus", "Toys 'R' Us"),
             ("lego.com", "LEGO"), ("playmobil", "Playmobil"), ("xiaomi", "Xiaomi Store"),
             ("samsung", "Samsung"), ("store.google", "Google Store"), ("ebay", "eBay"),
             ("tiendanimal", "Tiendanimal"), ("zooplus", "Zooplus"), ("sprinter", "Sprinter"),
             ("chollometro", None)]
    for k, v in table:
        if k in h:
            return v
    return None


RULES = [
    (r"\blego\b", "Juguetes", "LEGO"), (r"playmobil", "Juguetes", "Playmobil"),
    (r"\bfunko\b", "Juguetes", "Funko"), (r"\bnerf\b", "Juguetes", "Nerf"),
    (r"hot ?wheels", "Juguetes", "Hot Wheels"), (r"play-?doh", "Juguetes", "Play-Doh"),
    (r"juego de mesa|catan|monopoly|dixit|cluedo|jenga|virus!|dobble|carcassonne|ticket to ride|"
     r"pictionary|trivial|exploding kittens|aventureros al tren|party & co", "Juguetes", "Juegos de mesa"),
    (r"juguete|muñec|peluche|barbie|puzzle|rompecabezas|pista de|hasbro|mattel|vtech|fisher|"
     r"clementoni|pinypon|nenuco|bebés llorones|baby born|scalextric|magnetic|cocinita|patinete infantil",
     "Juguetes", "Otros"),
    (r"iphone|ipad|macbook|airpods|apple watch|imac|mac mini|mac studio|airtag|apple tv|magic (mouse|keyboard|trackpad)|homepod",
     "Apple", None),
    (r"\bps5\b|playstation|xbox|nintendo|switch 2|\bswitch\b|steam deck|dualsense|mando |consola|"
     r"gaming|\brtx\b|juego (para|de) (ps|switch|xbox)|ps4|meta quest|rog ally|legion go",
     "Gaming", None),
    (r"smartphone|\bm[oó]vil\b|galaxy (s|a|z)\d|redmi|pixel \d|motorola|\boppo\b|realme|\bpoco\b|honor \d|oneplus|xiaomi \d",
     "Móviles", None),
    (r"\btv\b|televisor|smart tv|\boled\b|\bqled\b|mini ?led|barra de sonido|soundbar|auricular|altavoz|"
     r"sonos|\bjbl\b|\bbose\b|cascos|proyector|fire tv|chromecast|home cinema|tocadiscos", "TV y audio", None),
    (r"freidora|cafetera|robot de cocina|thermomix|mambo|batidora|sart[eé]n|\bolla\b|microondas|tostador|"
     r"hervidor|cuchillo|bateria de cocina|batería de cocina|exprimidor|panificadora|envasadora|"
     r"amasadora|kitchenaid|nespresso|dolce gusto|tassimo|airfryer|vaporera", "Cocina", None),
    (r"lavadora|frigor[ií]fico|secadora|lavavajillas|\bhorno\b|congelador|aire acondicionado|"
     r"calefactor|vitrocer|placa de inducci|campana extractora|termo eléctrico|radiador", "Electrodomésticos", None),
    (r"perfum|eau de|colonia|\bedp\b|\bedt\b|crema|s[eé]rum|maquillaje|m[aá]scara de pesta|labial|"
     r"afeitadora|depiladora|\bipl\b|cepillo (de dientes|el[eé]ctrico)|oral-?b|irrigador|secador|"
     r"plancha de pelo|moldeador|champ[uú]|cosm[eé]tica|estuche|cortapelos|recortadora|dyson airwrap|"
     r"b[aá]scula|tensi[oó]metro", "Cuidado personal", None),
    (r"zapatillas de running|bicicleta|\bbici\b|patinete el[eé]ctrico|fitness|garmin|mancuerna|cinta de correr|"
     r"el[ií]ptica|esterilla|pesas|raqueta|bal[oó]n|tienda de campa|saco de dormir|kayak|pulsómetro|polar ",
     "Deporte", None),
    (r"zapatillas|chaqueta|camiseta|pantal[oó]n|abrigo|sudadera|bolso|vaquero|jersey|botas|parka|"
     r"cartera|gafas de sol|sneakers|adidas|nike|new balance|levi|tommy|calvin klein", "Moda", None),
    (r"aspirador|roomba|roborock|dreame|conga|colch[oó]n|almohada|l[aá]mpara|sof[aá]|toalla|edred[oó]n|"
     r"ventilador|deshumid|purificador|taladro|atornillador|herramienta|bosch professional|makita|"
     r"dewalt|mascota|pienso|perro|gato|arena para|sábanas|nórdico|bombilla|enchufe inteligente|"
     r"cámara de seguridad|ring |blink|cortacésped|limpiacristales|vaporeta|plancha de ropa|"
     r"pañal|dodot|silla de coche|carrito de beb|cuna|biber", "Hogar", None),
]


def classify(name):
    n = (name or "").lower()
    for rx, cat, sub in RULES:
        if re.search(rx, n):
            return cat, sub
    return "Tecnología", None


def iso(s):
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------- Gemini
def gemini(prompt):
    if not GEMINI_KEY:
        return None
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json",
                                 "maxOutputTokens": 16384}}
    for model in list(GEMINI_MODELS):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                r = S.post(url, json=body, timeout=240, headers={"x-goog-api-key": GEMINI_KEY})
            except requests.RequestException as e:
                log("  ! Gemini", model, type(e).__name__)
                break
            if r.status_code == 429 or r.status_code >= 500:
                wait = 20 * (attempt + 1)
                log(f"  · Gemini {model} {r.status_code}, espero {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (400, 403, 404):
                log(f"  ! Gemini {model} {r.status_code}: {r.text[:200]}")
                if r.status_code == 404 and model in GEMINI_MODELS:
                    GEMINI_MODELS.remove(model)   # modelo inexistente: no reintentar
                break
            try:
                data = r.json()
                text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
                STATS["gemini"] += 1
                text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
                return json.loads(text)
            except (KeyError, IndexError, ValueError) as e:
                log("  ! Gemini respuesta no válida", model, type(e).__name__)
                break
    return None


EXTRACT_PROMPT = """Eres el editor de "Cazagangas Raquel", un buscador de chollos de Black Friday para una familia de Canarias.
Hoy es {today}. Abajo tienes el texto de una o varias páginas (artículos de ofertas o páginas de tienda).
Los enlaces aparecen como [texto](URL) y las fotos como [IMG URL].

Extrae TODAS las ofertas concretas de productos que estén vigentes hoy (con precio en euros). Ignora cupones genéricos,
suscripciones, productos sin precio y ofertas que el texto diga que ya han terminado. NO inventes nada: precio, precio
anterior, enlace y foto deben aparecer LITERALMENTE en el texto.

Devuelve SOLO un objeto JSON: {{"deals": [ ... ], "campaigns": [ ... ]}}
Cada deal:
{{"name": nombre corto y claro (máx. 60 caracteres, en español, marca + modelo),
 "cat": una de {cats},
 "sub": solo si cat es "Juguetes": una de {subs},
 "age": edad recomendada si aparece (p. ej. "8+"), si no null,
 "store": tienda que vende el producto (p. ej. "Amazon", "El Corte Inglés", "MediaMarkt", "PcComponentes"){store_hint},
 "price": precio actual (número),
 "was": precio anterior/PVP/recomendado (número) o null,
 "url": la URL del producto en la tienda (la del enlace del texto, copiada tal cual),
 "img": la URL de la foto DEL PRODUCTO (una [IMG ...] cercana al producto; nunca logos, banners, avatares ni el logo de Prime) o null,
 "min": true si el texto dice que es mínimo histórico / precio más bajo, si no false,
 "why": frase corta (máx. 90 caracteres) de por qué es buena oferta, o null,
 "coupon": código de cupón necesario o null}}
Cada campaign (solo si el texto da una fecha de fin de una campaña de una tienda): {{"name": "Tienda · Campaña", "until": "AAAA-MM-DD", "note": nota corta}}

TEXTO:
{text}
"""


# ------------------------------------------------------- lectura de páginas
def page_to_text(soup, base, limit=24000):
    """Texto de la página con enlaces e imágenes marcados para Gemini."""
    for t in soup(["script", "style", "noscript", "svg", "iframe", "form", "header", "footer", "nav"]):
        t.decompose()
    links, imgs = set(), set()
    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("data-original") or img.get("src") or ""
        if (not src or src.startswith("data:")) and img.get("srcset"):
            src = img["srcset"].split(",")[-1].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            img.decompose()
            continue
        src = urllib.parse.urljoin(base, src)
        imgs.add(src)
        img.replace_with(f" [IMG {src}] ")
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if not href.startswith(("http", "/")):
            a.unwrap()
            continue
        href = urllib.parse.urljoin(base, href)
        links.add(href)
        a.replace_with(f" [{a.get_text(' ', strip=True)}]({href}) ")
    text = re.sub(r"[ \t\r\f\v]+", " ", soup.get_text("\n"))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit], links, imgs


def extract_with_gemini(chunks, store_hint=None, force_store=None):
    """chunks: lista de (texto, links, imgs, origen). Devuelve candidatos validados."""
    out, camps = [], []
    batch, size = [], 0
    groups = []
    for c in chunks:
        if size + len(c[0]) > 45000 and batch:
            groups.append(batch)
            batch, size = [], 0
        batch.append(c)
        size += len(c[0])
    if batch:
        groups.append(batch)
    for g in groups:
        text = "\n\n=====\n\n".join(f"PÁGINA: {c[3]}\n{c[0]}" for c in g)
        links = set().union(*[c[1] for c in g])
        imgs = set().union(*[c[2] for c in g])
        hint = f'. Aquí la tienda es SIEMPRE "{force_store}"' if force_store else (store_hint or "")
        res = gemini(EXTRACT_PROMPT.format(today=TODAY, cats=CATS, subs=SUBS, store_hint=hint, text=text))
        if not isinstance(res, dict):
            continue
        clean_links = {}
        for l in links:
            cl = clean_url(l) if "amzn." not in l else None
            if cl:
                clean_links[cl] = l
        for d in res.get("deals") or []:
            try:
                c = validate(d, links, clean_links, imgs, g[0][3], force_store)
            except Exception as e:  # noqa: BLE001 - una oferta rara no debe parar el script
                log("  ! oferta descartada", type(e).__name__)
                c = None
            if c:
                out.append(c)
        for c in res.get("campaigns") or []:
            if isinstance(c, dict) and c.get("name") and c.get("until"):
                camps.append(c)
    return out, camps


def validate(d, links, clean_links, imgs, origin, force_store):
    if not isinstance(d, dict):
        return None
    price = num(d.get("price"))
    if not price or price <= 0 or price > 20000:
        return None
    was = num(d.get("was"))
    if was is not None and was <= price * 1.01:
        was = None
    raw = (d.get("url") or "").strip()
    url = None
    if raw in links:
        url = clean_url(raw)
    else:
        cu = clean_url(raw) if raw.startswith("http") else None
        if cu and cu in clean_links:
            url = cu
    if not url:
        return None
    img = (d.get("img") or "").strip()
    if img not in imgs or re.search(r"logo|banner|avatar|prime|sprite|icon|placeholder|blank", img, re.I):
        return None
    store = force_store or store_from_url(url) or short(d.get("store") or "", 30)
    if not store:
        return None
    if store == "Carrefour":           # su web no vende a Canarias
        return None
    name = short(d.get("name") or "", 60)
    if len(name) < 4:
        return None
    cat = d.get("cat") if d.get("cat") in CATS else classify(name)[0]
    sub = None
    if cat == "Juguetes":
        sub = d.get("sub") if d.get("sub") in SUBS else (classify(name)[1] or "Otros")
    c = {"name": name, "cat": cat, "store": store, "price": price, "was": was,
         "min": bool(d.get("min")), "url": url, "img": big_img(img), "others": {}}
    if sub:
        c["sub"] = sub
    if d.get("age"):
        c["age"] = short(str(d["age"]), 8)
    if d.get("why"):
        c["why"] = short(d["why"], 100)
    if d.get("coupon"):
        c["coupon"] = short(str(d["coupon"]), 30)
    if was:
        c["wasSrc"] = "precio anterior según " + short(urllib.parse.urlparse(origin).netloc.replace("www.", ""), 40)
    return c


# --------------------------------------------------------------- fuentes
WEBEDIA = ["xataka.com", "applesfera.com", "xatakandroid.com", "3djuegos.com", "vidaextra.com",
           "compradiccion.com", "xatakahome.com", "xatakamovil.com", "genbeta.com", "espinof.com"]
FEED_PATHS = ["/feedburner.xml", "/index.xml", "/rss2.xml", "/feed"]
OTHER_FEEDS = [
    ("ofertasenjuguetes", ["https://ofertasenjuguetes.com/feed/", "https://www.ofertasenjuguetes.com/feed/"]),
    ("elindependiente", ["https://www.elindependiente.com/de-tiendas/feed/"]),
]
DEAL_WORDS = re.compile(r"oferta|chollo|black friday|descuento|rebaj|precio|barat|mínimo|cyber monday|"
                        r"amazon|mediamarkt|corte ingl|pccomponentes|juguete|lego", re.I)


def parse_feed(content):
    try:
        from lxml import etree as LX       # tolera feeds con errores
        root = LX.fromstring(content, parser=LX.XMLParser(recover=True, resolve_entities=False))
    except Exception:  # noqa: BLE001
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return []
    if root is None:
        return []
    items = []
    for it in root.iter():
        if not isinstance(it.tag, str):
            continue
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        e = {"extra": {}}
        for ch in it:
            if not isinstance(ch.tag, str):
                continue
            t = ch.tag.split("}")[-1].lower()
            if t == "link":
                e["link"] = (ch.text or ch.get("href") or "").strip()
            elif t in ("title", "description", "summary", "pubdate", "published", "updated", "guid"):
                e[t] = (ch.text or "").strip()
            elif t == "encoded":
                e["content"] = ch.text or ""
            else:
                if ch.attrib:
                    e["extra"].setdefault(t, []).append(dict(ch.attrib))
                if ch.text and ch.text.strip():
                    e["extra"].setdefault(t + "_text", []).append(ch.text.strip())
        items.append(e)
    return items


def item_date(e):
    from email.utils import parsedate_to_datetime
    for k in ("pubdate", "published", "updated"):
        v = e.get(k)
        if not v:
            continue
        try:
            d = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            d = iso(v)
        if d:
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    return None


def source_articles(state):
    """Artículos de ofertas recientes de las webs de tecnología y juguetes."""
    seen = state.setdefault("articles", {})
    arts = []
    feeds = [(s, [f"https://www.{s}{p}" for p in FEED_PATHS]) for s in WEBEDIA] + OTHER_FEEDS
    for name, urls in feeds:
        items = []
        for u in urls:
            r = get(u)
            if r is not None and r.status_code == 200 and b"<" in r.content[:200]:
                items = parse_feed(r.content)
                if items:
                    break
        if not items:
            log(f"  - {name}: feed no disponible")
            STATS["fuentes_fallo"] += 1
            continue
        STATS["fuentes_ok"] += 1
        n = 0
        for e in items:
            link = e.get("link") or ""
            d = item_date(e)
            if not link or (d and NOW - d > dt.timedelta(hours=30)):
                continue
            if link in seen:
                continue
            if not ("/seleccion/" in link or DEAL_WORDS.search(e.get("title", ""))):
                continue
            arts.append((d or NOW, name, link, e))
            n += 1
        log(f"  - {name}: {len(items)} entradas, {n} artículos de ofertas nuevos")
    arts.sort(key=lambda x: x[0], reverse=True)
    chunks = []
    for d, name, link, e in arts[:MAX_ARTICLES]:
        if e.get("content") and len(e["content"]) > 3000:
            soup = BeautifulSoup(e["content"], "html.parser")
        else:
            r = get(link)
            if r is None or r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            body = (soup.select_one(".article-content") or soup.select_one("article")
                    or soup.select_one(".entry-content") or soup.body or soup)
            soup = body
        text, links, imgs = page_to_text(soup, link)
        if len(text) < 300:
            continue
        chunks.append((text, links, imgs, link))
        seen[link] = TODAY
    # limpia memoria vieja
    for k in [k for k, v in seen.items() if v < (NOW - dt.timedelta(days=4)).date().isoformat()]:
        del seen[k]
    return chunks


def source_chollometro():
    """Chollómetro por RSS: no necesita IA (precio, tienda y foto vienen en el feed)."""
    out = []
    urls = ["https://www.chollometro.com/rss/hot", "https://www.chollometro.com/rss/destacados",
            "https://www.chollometro.com/rss/grupo/juegos-y-juguetes",
            "https://www.chollometro.com/rss/grupo/black-friday"]
    for u in urls:
        r = get(u)
        if r is None or r.status_code != 200:
            log(f"  - Chollómetro {u.rsplit('/', 1)[-1]}: {getattr(r, 'status_code', 'error')}")
            STATS["fuentes_fallo"] += 1
            continue
        items = parse_feed(r.content)
        STATS["fuentes_ok"] += 1
        n = 0
        for e in items:
            link = e.get("link") or ""
            m = re.search(r"-(\d{6,8})(?:$|[/?#])", link)
            if not m:
                continue
            d = item_date(e)
            if d and NOW - d > dt.timedelta(days=3):
                continue
            ex = e["extra"]
            merchant, price = None, None
            for t, vals in ex.items():
                if "merchant" in t and not t.endswith("_text"):
                    merchant = vals[0].get("name") or merchant
                    price = num(vals[0].get("price")) or price
            img = None
            for t in ("content", "thumbnail", "enclosure"):
                for v in ex.get(t, []):
                    if v.get("url") and re.search(r"\.(jpe?g|png|webp)", v["url"], re.I):
                        img = v["url"]
            if not img:
                mm = re.search(r'<img[^>]+src="([^"]+)"', e.get("description", "") + e.get("content", ""))
                img = mm.group(1) if mm else None
            if not price:
                mm = re.search(r"(\d+[.,]?\d*)\s?€", e.get("title", ""))
                price = num(mm.group(1)) if mm else None
            temp = None
            for t, vals in ex.items():
                if "temperature" in t and t.endswith("_text"):
                    temp = num(vals[0])
            if not (price and img and merchant):
                continue
            store = {"Amazon": "Amazon", "El Corte Ingles": "El Corte Inglés"}.get(merchant, merchant)
            if "carrefour" in store.lower() or price <= 0:
                continue
            title = re.sub(r"\s*[-–|]\s*\d+[.,]?\d*\s?€.*$", "", e.get("title", ""))
            cat, sub = classify(title)
            c = {"id": f"cm-{m.group(1)}", "name": short(title, 60), "cat": cat, "store": store,
                 "price": price, "was": None, "min": bool(re.search(r"m[ií]nimo", e.get("title", ""), re.I)),
                 "url": link.split("?")[0], "img": big_img(img), "others": {}, "src": "Chollómetro"}
            if sub:
                c["sub"] = sub
            if temp:
                c["deg"] = int(temp)
            desc = BeautifulSoup(e.get("description", ""), "html.parser").get_text(" ")
            mm = re.search(r"(?:antes|PVP|precio (?:habitual|anterior|original|recomendado))[^\d]{0,20}(\d+[.,]?\d*)\s?€",
                           desc, re.I)
            if mm and num(mm.group(1)) and num(mm.group(1)) > price * 1.05:
                c["was"] = num(mm.group(1))
                c["wasSrc"] = "precio de referencia en Chollómetro"
            out.append(c)
            n += 1
        log(f"  - Chollómetro {u.rsplit('/', 1)[-1]}: {len(items)} entradas, {n} válidas")
    uniq = {}
    for c in out:
        uniq[c["id"]] = c
    return list(uniq.values())


def source_mediamarkt_canarias():
    """MediaMarkt Canarias es una tienda Shopify: su catálogo sale en JSON (sin IA)."""
    out = []
    for coll in ("black-friday", "cyber-monday", "semanas-black", "ofertas"):
        got = 0
        for page in range(1, 5):
            r = get(f"https://canarias.mediamarkt.es/collections/{coll}/products.json",
                    params={"limit": 250, "page": page})
            if r is None or r.status_code != 200:
                break
            try:
                prods = r.json().get("products", [])
            except ValueError:
                break
            if not prods:
                break
            for p in prods:
                v = next((x for x in p.get("variants", []) if x.get("available", True)), None)
                if not v or not p.get("images"):
                    continue
                price, was = num(v.get("price")), num(v.get("compare_at_price"))
                if not price or not was or was < price * 1.12:       # solo descuentos de verdad
                    continue
                title = p.get("title") or ""
                if " - " in title:                      # "Freidora de aire - Cecotec X, 8 l, ..."
                    title = title.split(" - ", 1)[1]
                name = short(title.split(",")[0], 60)
                cat, sub = classify(name + " " + (p.get("product_type") or ""))
                c = {"id": "mmc-" + (str(p.get("id"))[-8:]), "name": name, "cat": cat,
                     "store": "MediaMarkt Canarias", "price": price, "was": was, "min": False,
                     "url": f"https://canarias.mediamarkt.es/products/{p['handle']}",
                     "img": p["images"][0]["src"].split("?")[0], "others": {},
                     "wasSrc": "precio anterior en MediaMarkt Canarias"}
                if sub:
                    c["sub"] = sub
                out.append(c)
                got += 1
        if got:
            STATS["fuentes_ok"] += 1
            log(f"  - MediaMarkt Canarias /{coll}: {got} productos con descuento")
    uniq = {c["url"]: c for c in out}
    best = sorted(uniq.values(), key=lambda c: c["price"] / c["was"])
    return best[:90]


CANARY_PAGES = [
    ("Druni Canarias", ["https://www.druni.es/black-friday/canarias", "https://canarias.druni.es/black-friday"]),
    ("Jugueterías Nikki", ["https://www.jugueteriasnikki.es/promociones", "https://www.jugueteriasnikki.es/ofertas",
                           "https://www.jugueteriasnikki.es/black-friday"]),
    ("Worten Canarias", ["https://canarias.worten.es/promocoes/black-friday", "https://canarias.worten.es/black-friday",
                         "https://canarias.worten.es/"]),
    ("Mandatelo", ["https://mandatelo.com/black-friday", "https://mandatelo.com/promociones", "https://mandatelo.com/"]),
]
NATIONAL_PAGES = [
    ("PcComTracker", ["https://pccomtracker.com/black-friday"]),
]


def source_store_pages():
    res, camps = [], []
    for store, urls in CANARY_PAGES + NATIONAL_PAGES:
        for u in urls:
            r = get(u)
            if r is None or r.status_code != 200 or len(r.text) < 2000:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            main = soup.select_one("main") or soup.body or soup
            text, links, imgs = page_to_text(main, r.url, limit=40000)
            if len(text) < 500:
                continue
            STATS["fuentes_ok"] += 1
            force = store if store in CANARY else None
            got, cp = extract_with_gemini([(text, links, imgs, r.url)], force_store=force)
            if store == "PcComTracker":
                for g in got:
                    g["src"] = "PcComTracker"
            log(f"  - {store}: {len(got)} ofertas ({r.url})")
            res += got
            camps += cp
            break
        else:
            log(f"  - {store}: no se pudo leer")
            STATS["fuentes_fallo"] += 1
    return res, camps


# --------------------------------------------- comprobación de precio actual
def jsonld_offer(text):
    """Precio y disponibilidad desde JSON-LD / microdatos / meta de una ficha."""
    price, avail = None, None
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            o = stack.pop()
            if isinstance(o, list):
                stack += o
                continue
            if not isinstance(o, dict):
                continue
            if "@graph" in o:
                stack += o["@graph"] if isinstance(o["@graph"], list) else [o["@graph"]]
            t = o.get("@type")
            t = t if isinstance(t, list) else [t]
            if "Product" in t or "ProductGroup" in t:
                offs = o.get("offers") or {}
                offs = offs if isinstance(offs, list) else [offs]
                for of in offs:
                    if not isinstance(of, dict):
                        continue
                    p = num(of.get("price") or of.get("lowPrice"))
                    if p and (price is None or p < price):
                        price = p
                        avail = str(of.get("availability") or "")
    if price is None:
        m = re.search(r'(?:product:price:amount|og:price:amount)"\s+content="([\d.,]+)"', text)
        if m:
            price = num(m.group(1))
    if price is None:
        m = re.search(r'itemprop="price"\s+content="([\d.,]+)"', text)
        if m:
            price = num(m.group(1))
    return price, avail


CHECKABLE = ("canarias.mediamarkt.es", "druni.es", "jugueteriasnikki.es", "mandatelo.com", "primor.eu",
             "leroymerlin.es", "decathlon.es", "pccomponentes.com", "mediamarkt.es", "elcorteingles.es",
             "worten.es", "fnac.es", "kinderkraft", "tiendanimal", "playmobil", "lego.com")


def check_prices(deals, state):
    chk = state.setdefault("checked", {})
    fails = state.setdefault("fails", {})
    cands = [d for d in deals if any(h in d.get("url", "") for h in CHECKABLE)]
    cands.sort(key=lambda d: chk.get(d["id"], ""))       # primero las menos comprobadas
    blocked = set()
    removed = set()
    gone = state.setdefault("gone", {})
    gone_now, per_host = [], {}
    for d in cands[:CHECKS_PER_RUN]:
        host = urllib.parse.urlparse(d["url"]).netloc
        if host in blocked:
            continue
        price, avail, status = None, None, None
        if "canarias.mediamarkt.es/products/" in d["url"]:
            r = get(d["url"].split("?")[0] + ".js", timeout=15)
            status = getattr(r, "status_code", None)
            if r is not None and status == 200:
                try:
                    j = r.json()
                    price = (j.get("price") or 0) / 100 or None
                    avail = "InStock" if j.get("available") else "OutOfStock"
                    cmp_ = (j.get("compare_at_price") or 0) / 100
                    if cmp_ and price and cmp_ > price:
                        d["was"] = round(cmp_, 2)
                except ValueError:
                    pass
        else:
            r = get(d["url"], timeout=15)
            status = getattr(r, "status_code", None)
            if r is not None and status == 200:
                price, avail = jsonld_offer(r.text)
        time.sleep(0.4)
        if status in (403, 429, 503) or status is None:
            fails[host] = fails.get(host, 0) + 1
            if fails[host] >= 3:
                blocked.add(host)
            continue
        fails[host] = 0
        chk[d["id"]] = NOW.isoformat()
        STATS["comprobadas"] += 1
        per_host.setdefault(host, [0, 0])[0] += 1
        if status in (404, 410) or (avail and re.search(r"OutOfStock|SoldOut|Discontinued", avail, re.I)):
            per_host[host][1] += 1
            gone_now.append(d)
            continue
        gone.pop(d["id"], None)
        d["seen"] = NOW.isoformat()
        if price and abs(price - d["price"]) >= 0.01 and 0.2 < price / d["price"] < 3:
            old = d["price"]
            d["prevDay"] = old
            d["price"] = price
            if d.get("was") and d["was"] <= price:
                d["was"] = None
            if price < old:
                STATS["bajadas"] += 1
            else:
                STATS["subidas"] += 1
            log(f"  ~ {d['name']}: {old} -> {price} €")
    # Seguridad: si una web devuelve "no existe" en más de la mitad de sus fichas, seguramente
    # nos está bloqueando, así que no se borra nada de esa web. Y una oferta solo se quita
    # cuando sale agotada/no existe en DOS pasadas seguidas.
    for d in gone_now:
        host = urllib.parse.urlparse(d["url"]).netloc
        total, bad = per_host[host]
        if total >= 4 and bad / total > 0.5:
            continue
        gone[d["id"]] = gone.get(d["id"], 0) + 1
        if gone[d["id"]] >= 2:
            removed.add(d["id"])
            gone.pop(d["id"], None)
            log(f"  x agotada/caducada: {d['name']} ({d['store']})")
    if blocked:
        log("  · webs que bloquean la comprobación:", ", ".join(sorted(blocked)))
    return removed


# -------------------------------------------------------------- fusión
def key_of(c):
    m = ASIN_RE.search(c.get("url", ""))
    return ("asin", m.group(1).upper()) if m else ("url", c.get("url", "").rstrip("/").lower())


def make_id(c):
    m = ASIN_RE.search(c["url"])
    if m:
        return "amz-" + m.group(1).lower()
    pre = {"MediaMarkt Canarias": "mmc", "Worten Canarias": "wtc", "Druni Canarias": "dru",
           "Jugueterías Nikki": "nk", "Mandatelo": "mdt", "El Corte Inglés": "eci",
           "El Corte Inglés Canarias": "ecic", "PcComponentes": "pc", "MediaMarkt": "mm",
           "Decathlon": "dec", "Primor": "prm", "Leroy Merlin": "lm"}.get(c["store"], "of")
    return f"{pre}-{hid(c['url'])}"


def score(d):
    disc = 1 - d["price"] / d["was"] if d.get("was") else 0.1
    s = disc + (0.25 if d.get("min") else 0) + min(d.get("deg") or 0, 1000) / 2000
    if d.get("store") in CANARY:
        s += 0.15
    if d.get("cat") == "Juguetes":
        s += 0.05
    return s


def merge(prev, cands, removed):
    by_id = {d["id"]: d for d in prev if d["id"] not in removed}
    by_key = {key_of(d): d for d in by_id.values()}
    new = 0
    for c in cands:
        old = by_id.get(c.get("id")) or by_key.get(key_of(c))
        if old:
            old["seen"] = NOW.isoformat()
            if c["price"] and abs(c["price"] - old["price"]) >= 0.01:
                old["prevDay"] = old["price"]
                if c["price"] < old["price"]:
                    STATS["bajadas"] += 1
                else:
                    STATS["subidas"] += 1
                old["price"] = c["price"]
                if c.get("was"):
                    old["was"] = c["was"]
                elif old.get("was") and old["was"] <= c["price"]:
                    old["was"] = None
            for k in ("deg", "coupon"):
                if c.get(k):
                    old[k] = c[k]
            if c.get("min"):
                old["min"] = True
            continue
        c.setdefault("id", make_id(c))
        if c["id"] in by_id:
            c["id"] = c["id"] + "-" + hid(c["url"])[:4]
        c["seen"] = NOW.isoformat()
        c["added"] = TODAY
        by_id[c["id"]] = c
        by_key[key_of(c)] = c
        new += 1
    STATS["nuevas"] = new
    deals = list(by_id.values())
    # caducidad: no vistas en ninguna fuente ni comprobadas en STALE_DAYS días
    keep = []
    for d in deals:
        seen = iso(d.get("seen")) or NOW
        if NOW - seen > dt.timedelta(days=STALE_DAYS):
            STATS["quitadas"] += 1
            continue
        keep.append(d)
    STATS["quitadas"] += len(removed)
    if len(keep) > MAX_DEALS:
        keep.sort(key=score, reverse=True)
        STATS["quitadas"] += len(keep) - MAX_DEALS
        keep = keep[:MAX_DEALS]
    return keep


def fix_campaigns(camps, extra):
    names = {c["name"].lower() for c in camps}
    for c in extra:
        until = str(c.get("until"))[:16]
        if c["name"].lower() not in names and re.match(r"\d{4}-\d{2}-\d{2}", until):
            camps.append({"name": short(c["name"], 50), "until": until,
                          "note": short(c.get("note") or "", 80), "urgent": False})
            names.add(c["name"].lower())
    out = []
    for c in camps:
        u = str(c.get("until", ""))
        try:
            end = dt.datetime.fromisoformat(u if "T" in u else u + "T23:59").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if end < NOW:
            continue
        c["urgent"] = end - NOW < dt.timedelta(hours=24)
        out.append(c)
    return out


# ----------------------------------------------------------------- main
def main():
    if NOW >= END:
        log("Campaña terminada (después del 2 de diciembre): no hago nada.")
        return 0
    with open(DEALS, encoding="utf-8") as f:
        doc = json.load(f)
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    prev = doc["deals"]
    for d in prev:
        d.setdefault("seen", NOW.isoformat())      # primera ejecución: todas cuentan como vistas hoy
    log(f"Lista anterior: {len(prev)} ofertas (edición {doc.get('edition')})")
    log("Gemini:", "activado" if GEMINI_KEY else "SIN CLAVE (solo fuentes sin IA)")

    log("1) Comprobando precios actuales…")
    removed = check_prices(prev, state)

    log("2) Buscando ofertas nuevas…")
    cands, camps = [], []
    for fn in (source_chollometro, source_mediamarkt_canarias):
        try:
            cands += fn()
        except Exception as e:  # noqa: BLE001
            log(f"  ! {fn.__name__}: {type(e).__name__}: {e}")
    if GEMINI_KEY:
        try:
            chunks = source_articles(state)
            log(f"  · {len(chunks)} artículos para Gemini")
            got, cp = extract_with_gemini(chunks)
            log(f"  · artículos: {len(got)} ofertas extraídas")
            cands += got
            camps += cp
        except Exception as e:  # noqa: BLE001
            log(f"  ! artículos: {type(e).__name__}: {e}")
        try:
            got, cp = source_store_pages()
            cands += got
            camps += cp
        except Exception as e:  # noqa: BLE001
            log(f"  ! tiendas: {type(e).__name__}: {e}")

    deals = merge(prev, cands, removed)
    if not cands and not STATS["comprobadas"]:
        log("No se ha podido leer ninguna fuente: no cambio deals.json.")
        return 0

    deals.sort(key=score, reverse=True)
    doc["deals"] = deals
    doc["campaigns"] = fix_campaigns(doc.get("campaigns", []), camps)
    doc["updated"] = NOW.replace(second=0).isoformat()
    doc["edition"] = int(doc.get("edition", 0)) + 1
    with open(DEALS, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False, indent=0) + "\n")
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    state["last_run"] = NOW.isoformat()
    state["last_stats"] = STATS
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)

    can = sum(1 for d in deals if d["store"] in CANARY)
    log(f"\nRESUMEN: {len(deals)} ofertas ({can} de tiendas canarias) · nuevas {STATS['nuevas']} · "
        f"quitadas {STATS['quitadas']} · bajan {STATS['bajadas']} · suben {STATS['subidas']} · "
        f"comprobadas {STATS['comprobadas']} · páginas {STATS['paginas']} · llamadas a Gemini {STATS['gemini']}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Cazagangas\n\n```\n" + "\n".join(LOG[-80:]) + "\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
