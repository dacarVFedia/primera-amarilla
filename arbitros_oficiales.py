"""
Árbitros designados antes de cada partido, leídos de las fuentes oficiales de cada liga.

  Serie A      legaseriea.it   (página de designaciones de la jornada)
  Bundesliga   DFB Datencenter (Schiedsrichter-Ansetzungen)
  Ligue 1      ligue1.com      (artículo "Les arbitres de la Ne journée", localizado por su sitemap)
  LaLiga       rfef.es         (PDF de designaciones del CTA, uno por día de partido)
  Eredivisie   knvb.nl         (PDF semanal de aanstellingen)

La Premier League no tiene aquí lector: su artículo de designaciones no se puede localizar sin la API interna.
Lo leído se guarda una hora en la caché para no pedir las páginas en cada actualización.
Cada fuente que falle se avisa y no tumba el resto.
"""

import html
import io
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import amarillas as am

AGENTE = "Mozilla/5.0 (compatible; primera-amarilla/1.0; +https://dacarvfedia.github.io/primera-amarilla/)"
DURACION_CACHE = 3600
ARCHIVO_CACHE = "arbitros_oficiales.json"


def _get(url, binario=False):
    req = urllib.request.Request(url, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(req, timeout=40) as r:
        datos = r.read()
    return datos if binario else datos.decode("utf-8", "replace")


def _texto(pagina):
    """HTML a texto con los bloques separados por " | "."""
    t = html.unescape(re.sub(r"<[^>]+>", "|", pagina)).replace("\xa0", " ")
    return re.sub(r"\s*\|[\s|]*", " | ", t)


def _pdf(url):
    import pypdf  # solo hace falta para LaLiga y Eredivisie
    lector = pypdf.PdfReader(io.BytesIO(_get(url, binario=True)))
    return "\n".join(p.extract_text() or "" for p in lector.pages)


# ---------------------------------------------------------------- lectores: lista de {"l", "v", "a", "dia"?}
# "l" y "v" son los equipos tal como los escribe la fuente; con "lv" la fuente los da juntos en una línea.

def serie_a(jornadas):
    salida = []
    for j in sorted(jornadas):
        try:
            t = _texto(_get(f"https://www.legaseriea.it/serie-a/news/le-designazioni-arbitrali-della-{j}a"))
        except Exception:
            continue
        for l, v, a in re.findall(r"\|\s*([A-ZÀ-Ü0-9' .]+?)\s*[–-]\s*([A-ZÀ-Ü0-9' .]+?)\s*\|\s*\([^)]*\)\s*\|\s*([A-ZÀ-Ü' .]+?)\s*\(", t):
            salida.append({"l": l, "v": v, "a": a.title()})
    return salida


def bundesliga(_jornadas):
    t = _texto(_get("https://datencenter.dfb.de/global_referee_schedule"))
    return [{"l": l, "v": v, "a": re.sub(r"^(Dr\.|Prof\.)\s*", "", a)}
            for l, v, a in re.findall(r"\|\s*([^|]+?)\s*\|\s*-\s*:\s*-\s*\|\s*([^|]+?)\s*\|\s*([^|(]+?)\s*\(SR\)", t)]


def ligue_1(jornadas):
    mapa = _get("https://ligue1.com/api/sitemap/sitemap-articles-l1-fr")
    urls = {}
    for m in re.finditer(r"(https://ligue1\.com/fr/articles/l1_article_\d+-les-arbitres-de-la-(\d+)e-journee[^<\s\"]*)", mapa):
        urls.setdefault(int(m.group(2)), m.group(1))
    salida = []
    for j in sorted(jornadas):
        if j not in urls:
            continue
        t = _texto(_get(urls[j]))
        for l, v, a in re.findall(r"\|\s*([^|]+?)\s*[–-]\s*\|\s*([^|]+?)\s*\|\s*\([^)]*\)\s*\|\s*Arbitre principal\s*:\s*([^|]+?)\s*\|", t):
            salida.append({"l": l, "v": v, "a": " ".join(p if p[:1].isupper() and not p.isupper() else p.title() for p in a.split())})
    return salida


def laliga(_jornadas):
    lista = _get("https://rfef.es/es/noticias/competiciones-masculinas/primera-division")
    articulos = sorted(set(re.findall(r'href="(/es/noticias/designaciones-estos-son-los-arbitros-para-[^"]+)"', lista)))
    salida = []
    for art in articulos[:8]:
        try:
            pagina = _get("https://rfef.es" + art)
        except Exception:
            continue
        for pdf in set(re.findall(r'href="(/sites/default/files/designaciones_1a_division_masculina[^"]+\.pdf)', pagina)):
            try:
                texto = _pdf("https://rfef.es" + pdf)
            except Exception:
                continue
            lineas = [x.strip() for x in texto.splitlines()]
            for i, linea in enumerate(lineas):
                m = re.match(r"(\d\d)-(\d\d)-(\d{4}) (.+?) \d\d:\d\d$", linea)
                if m and i + 1 < len(lineas):
                    a = re.match(r"Árbitro:\s*(.+?)\s+4º", lineas[i + 1])
                    if a:
                        salida.append({"lv": m.group(4), "a": a.group(1), "dia": f"{m.group(3)}-{m.group(2)}-{m.group(1)}"})
    return salida


MESES_NL = {m: i for i, m in enumerate(("januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus",
                                         "september", "oktober", "november", "december"), 1)}


def eredivisie(_jornadas):
    cat = _get("https://www.knvb.nl/knvb_node/categorie/scheidsrechters/aanstellingen")
    ids = sorted({(int(i), u) for u, i in re.findall(r'href="(/nieuws/scheidsrechters/aanstellingen/(\d+)/[^"]+)"', cat)}, reverse=True)
    pdfs = []
    for _, u in ids[:6]:
        try:
            pagina = _get("https://www.knvb.nl" + u)
        except Exception:
            continue
        pdfs += [p for p in re.findall(r'href="(/downloads/bestand/\d+/aanstellingen[^"]*)"', pagina) if p not in pdfs]
        if pdfs:
            break
    salida = []
    for p in pdfs[:2]:
        texto = _pdf("https://www.knvb.nl" + p)
        dentro, dia = False, None
        for linea in (x.strip() for x in texto.splitlines()):
            if "Eredivisie" in linea:
                dentro = True
                continue
            if dentro and (linea.startswith("Wnr") or linea.startswith("Competitie")):
                dentro = False
            if not dentro:
                continue
            f = re.match(r"(?:maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag) (\d+) (\w+) (\d{4})", linea)
            if f and f.group(2) in MESES_NL:
                dia = f"{f.group(3)}-{MESES_NL[f.group(2)]:02d}-{int(f.group(1)):02d}"
                continue
            m = re.match(r"(.+?) - (.+?)\s((?:mevr\.?\s)?(?:[A-Z]{1,2}\.)+[A-Z]?\.?\s?\S.*)$", linea)
            if m and dia:
                salida.append({"l": m.group(1), "v": m.group(2), "a": re.sub(r"^mevr\.?\s", "", m.group(3)), "dia": dia})
    return salida


LECTORES = {"Serie A": serie_a, "Bundesliga": bundesliga, "Ligue 1": ligue_1, "LaLiga": laliga, "Eredivisie": eredivisie}


# ---------------------------------------------------------------- nombres

def _fichas_nombre(nombre):
    """Palabras de un nombre sin iniciales (R.W.M.) ni partículas."""
    return [t for t in am._simple(nombre).split() if len(t) > 1 and t not in {"de", "van", "der", "di", "la", "le", "da", "del", "dr"}]


def nombre_modelo(nombre, conocidos):
    """El nombre con el que el modelo conoce al árbitro, o el de la fuente si no se reconoce con seguridad."""
    f = _fichas_nombre(nombre)
    if not f:
        return nombre
    mejores, nota_max = [], 0
    for c in conocidos:
        fc = _fichas_nombre(c)
        comun = len(set(f) & set(fc))
        # Vale si comparten el apellido de la fuente (su última palabra) o al menos dos palabras
        if comun and (f[-1] in fc or comun >= 2):
            nota = comun + (f[-1] == fc[-1])
            if nota > nota_max:
                mejores, nota_max = [c], nota
            elif nota == nota_max:
                mejores.append(c)
    return mejores[0] if len(mejores) == 1 else nombre


# ---------------------------------------------------------------- unir con el calendario

APODOS = {"losc": "lille"}  # nombres de la fuente que no se parecen a los del calendario


def _equipo(nombre):
    s = am._simple(nombre)
    return APODOS.get(s, nombre)


def _encaja(d, c):
    if "lv" in d:
        return am.parecido(c["local"], d["lv"]) + am.parecido(c["visitante"], d["lv"])
    return am.parecido(c["local"], _equipo(d["l"])) + am.parecido(c["visitante"], _equipo(d["v"]))


def poner(calendarios, conocidos, ahora, carpeta_cache, fallos):
    """Pone c["arbitro"] (y c["oficial"] = True) en los partidos próximos de liga que aún no lo tienen."""
    ruta = os.path.join(carpeta_cache, ARCHIVO_CACHE)
    guardado = {}
    try:
        with open(ruta, encoding="utf-8") as f:
            guardado = json.load(f)
    except (OSError, ValueError):
        pass
    proximos = {}
    for comp in LECTORES:
        proximos[comp] = [c for c in calendarios.get(comp, []) if c.get("fecha") and not c.get("arbitro")
                          and ahora - timedelta(hours=3) <= am_fecha(c["fecha"]) <= ahora + timedelta(days=8)]
    puestos = 0
    for comp, lector in LECTORES.items():
        if not proximos[comp]:
            continue
        jornadas = {c["jornada"] for c in proximos[comp] if c.get("jornada")}
        g = guardado.get(comp)
        if not g or time.time() - g["t"] > DURACION_CACHE or set(g.get("j", [])) != jornadas:
            try:
                g = {"t": time.time(), "j": sorted(jornadas), "d": lector(jornadas)}
                guardado[comp] = g
            except Exception as e:  # una fuente caída no debe tumbar la página
                fallos.append(f"Árbitros oficiales {comp}: {e}")
                continue
        for c in proximos[comp]:
            dia = am_fecha(c["fecha"]).date()
            mejor, nota = None, 0.0
            for d in g["d"]:
                if d.get("dia") and abs((datetime.fromisoformat(d["dia"]).date() - dia).days) > 1:
                    continue
                n = _encaja(d, c)
                if n > nota:
                    mejor, nota = d, n
            # Con la fecha confirmada basta con reconocer un equipo (cada uno juega una vez ese día);
            # sin fecha hacen falta los dos, porque la fuente puede traer filiales u otras categorías
            if mejor and (nota >= 1.5 or (nota >= 1 and mejor.get("dia"))):
                c["arbitro"] = nombre_modelo(mejor["a"], conocidos)
                c["oficial"] = True
                puestos += 1
    try:
        os.makedirs(carpeta_cache, exist_ok=True)
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(guardado, f, ensure_ascii=False)
    except OSError:
        pass
    return puestos


def am_fecha(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00")).astimezone(timezone.utc)
