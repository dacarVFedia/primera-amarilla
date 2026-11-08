"""
Primera amarilla: qué equipo es más probable que la reciba, solo con datos de partidos y árbitros.

Fuentes (no usa cuotas):
  - ESPN (API pública sin key)  -> amarillas de cada partido con equipo y minuto, y faltas
  - football-data.org           -> calendario y árbitro de cada partido

Los partidos ya descargados se guardan en cache_amarillas/ y no se vuelven a pedir.
"""

import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher

CARPETA = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(CARPETA, "cache_amarillas")

# Competición -> (código en football-data, liga en ESPN)
LIGAS = {
    "LaLiga": ("PD", "esp.1"),
    "Premier League": ("PL", "eng.1"),
    "Serie A": ("SA", "ita.1"),
    "Bundesliga": ("BL1", "ger.1"),
    "Ligue 1": ("FL1", "fra.1"),
    "Eredivisie": ("DED", "ned.1"),
}
# Sin calendario propio en football-data (plan gratis) o solo como apoyo de la temporada actual
COPAS = {
    "Champions League": "uefa.champions",
    "Europa League": "uefa.europa",
    "Conference League": "uefa.europa.conf",
}
# Competición -> identificador en la web de la UEFA (árbitro y jornada de las copas)
COPAS_UEFA = {"Champions League": 1, "Europa League": 14, "Conference League": 2019}
TEMPORADAS_PASADAS = (2023, 2024, 2025)


def temporada_actual(hoy=None):
    hoy = hoy or datetime.now(timezone.utc).date()
    return hoy.year if hoy.month >= 7 else hoy.year - 1


# ---------------------------------------------------------------- red y caché

def _pedir(url, cabeceras=None, intentos=4):
    req = urllib.request.Request(url, headers={"User-Agent": "pronosticos/1.0", **(cabeceras or {})})
    for i in range(intentos):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < intentos - 1:
                time.sleep(65 if "football-data" in url else 5 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == intentos - 1:
                raise
            time.sleep(2 * (i + 1))


def _leer(nombre):
    ruta = os.path.join(CACHE, nombre)
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    return None


def _guardar(nombre, datos):
    os.makedirs(CACHE, exist_ok=True)
    ruta = os.path.join(CACHE, nombre)
    temporal = ruta + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)
    os.replace(temporal, ruta)


_ultima_fd = 0.0


def calendario_fd(codigo, temporada, key, refrescar=False):
    """Partidos de football-data con su árbitro. La temporada actual se refresca en cada ejecución."""
    global _ultima_fd
    nombre = f"fd_{codigo}_{temporada}.json"
    guardado = _leer(nombre)
    if guardado is not None and not refrescar:
        return guardado
    espera = 6.5 - (time.time() - _ultima_fd)
    if espera > 0:
        time.sleep(espera)
    _ultima_fd = time.time()
    datos = _pedir(f"https://api.football-data.org/v4/competitions/{codigo}/matches?season={temporada}",
                   {"X-Auth-Token": key})
    salida = [{
        "fecha": m["utcDate"], "local": m["homeTeam"].get("name"), "visitante": m["awayTeam"].get("name"),
        "estado": m.get("status"), "jornada": m.get("matchday"),
        "arbitro": next((r.get("name") for r in m.get("referees", []) if r.get("type") == "REFEREE"), None),
    } for m in datos.get("matches", [])]
    _guardar(nombre, salida)
    return salida


def _nombre_uefa(equipo):
    return (equipo or {}).get("internationalName") or ((equipo or {}).get("translations") or {}).get("displayName", {}).get("EN")


def _arbitro_uefa(partido):
    """Árbitro principal. La UEFA da nombre y apellidos dentro de person.translations."""
    lista = partido.get("referees") or []
    for r in lista:
        if (r.get("role") or "").upper() == "REFEREE":
            lista = [r]
            break
    else:
        return None
    t = (lista[0].get("person") or {}).get("translations") or {}
    nombre = (t.get("name") or {}).get("EN")
    if nombre:
        return nombre
    partes = [(t.get("firstName") or {}).get("EN"), (t.get("lastName") or {}).get("EN")]
    return " ".join(p for p in partes if p) or None


def calendario_uefa(comp, temporada):
    """Partidos de una copa desde la fase de liga, con árbitro y jornada. Fuente: match.uefa.com (sin key)."""
    cid = COPAS_UEFA[comp]
    salida, offset = [], 0
    while True:
        # La UEFA nombra cada temporada por el año en que acaba: 2026/27 es 2027
        datos = _pedir(f"https://match.uefa.com/v5/matches?competitionId={cid}&seasonYear={temporada + 1}"
                       f"&limit=100&offset={offset}&order=ASC&phase=ALL")
        for m in datos or []:
            dia = m.get("matchday") or {}
            if dia.get("phase") == "QUALIFYING" or not _nombre_uefa(m.get("homeTeam")):
                continue
            ronda = ((m.get("round") or {}).get("metaData") or {}).get("name") or ""
            numero = dia.get("sequenceNumber")
            salida.append({
                "fecha": (m.get("kickOffTime") or {}).get("dateTime"), "local": _nombre_uefa(m.get("homeTeam")),
                "visitante": _nombre_uefa(m.get("awayTeam")), "estado": m.get("status"),
                "jornada": int(numero) if str(numero).isdigit() else None, "ronda": ronda,
                "arbitro": _arbitro_uefa(m),
            })
        if not datos or len(datos) < 100:
            return salida
        offset += 100


def _segundos(detalle):
    valor = (detalle.get("clock") or {}).get("value")
    if valor is not None:
        return float(valor)
    texto = (detalle.get("clock") or {}).get("displayValue") or ""
    numeros = re.findall(r"\d+", texto)
    return sum(int(n) for n in numeros[:2]) * 60.0


def _resumir_evento(ev, en_juego=False):
    """Resume un partido terminado. Con en_juego=True acepta también los que se están jugando."""
    c = ev["competitions"][0]
    tipo = ev.get("status", {}).get("type", {})
    if not tipo.get("completed") and not (en_juego and tipo.get("state") == "in"):
        return None
    equipos = {x["homeAway"]: x for x in c["competitors"]}
    if "home" not in equipos or "away" not in equipos:
        return None
    id_l, id_v = equipos["home"]["team"]["id"], equipos["away"]["team"]["id"]

    def faltas(x):
        for s in x.get("statistics", []):
            if s.get("name") == "foulsCommitted":
                try:
                    return float(s.get("displayValue"))
                except (TypeError, ValueError):
                    return None
        return None

    amarillas = []
    for d in c.get("details", []):
        texto = (d.get("type") or {}).get("text") or ""
        if d.get("yellowCard") or "Yellow" in texto:
            equipo = (d.get("team") or {}).get("id")
            if equipo in (id_l, id_v):
                amarillas.append((_segundos(d), "L" if equipo == id_l else "V"))
    amarillas.sort(key=lambda a: a[0])
    return {
        "fecha": ev["date"], "id": ev["id"],
        "id_l": id_l, "id_v": id_v,
        "local": equipos["home"]["team"]["displayName"], "visitante": equipos["away"]["team"]["displayName"],
        "gl": int(equipos["home"].get("score") or 0), "gv": int(equipos["away"].get("score") or 0),
        "am_l": sum(a[1] == "L" for a in amarillas), "am_v": sum(a[1] == "V" for a in amarillas),
        "primera": amarillas[0][1] if amarillas else None,
        "minuto": round(amarillas[0][0] / 60) if amarillas else None,
        # Dos en el mismo instante: no se sabe cuál fue antes
        "empate_primera": len(amarillas) > 1 and amarillas[0][0] == amarillas[1][0] and amarillas[0][1] != amarillas[1][1],
        "faltas_l": faltas(equipos["home"]), "faltas_v": faltas(equipos["away"]),
    }


def partidos_dia_espn(liga, dia):
    """Partidos terminados de un día. Se guardan salvo los días recientes, que aún pueden cambiar."""
    nombre = f"espn_{liga}_{dia}.json"
    guardado = _leer(nombre)
    if guardado is not None:
        return guardado
    datos = _pedir(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/scoreboard?dates={dia.replace('-', '')}")
    salida = [r for r in (_resumir_evento(e) for e in datos.get("events", [])) if r]
    if date.fromisoformat(dia) < datetime.now(timezone.utc).date() - timedelta(days=2):
        _guardar(nombre, salida)
    return salida


def liga_espn(comp):
    return LIGAS[comp][1] if comp in LIGAS else COPAS.get(comp)


def alineacion_espn(liga, id_partido, terminado=True):
    """Jugadores de cada equipo con sus amarillas y faltas en ese partido. De la respuesta solo se guarda eso.

    Devuelve {"L": [...], "V": [...]} con {"id", "n" (nombre), "pos", "tit" (titular), "am", "fal"},
    o None si ESPN aún no tiene alineación. Solo se guarda en la caché cuando el partido ha terminado.
    """
    nombre = f"espn_al_{id_partido}.json"
    guardado = _leer(nombre)
    if guardado is not None:
        return guardado or None
    d = _pedir(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/summary?event={id_partido}")
    lados = {}
    for comp in (d.get("header") or {}).get("competitions", [])[:1]:
        for c in comp.get("competitors", []):
            lados[(c.get("team") or {}).get("id") or c.get("id")] = "L" if c.get("homeAway") == "home" else "V"
    salida = {}
    for r in d.get("rosters") or []:
        lado = lados.get((r.get("team") or {}).get("id")) or ("L" if r.get("homeAway") == "home" else "V")
        jugadores = []
        for a in r.get("roster") or []:
            st = {s.get("name"): s.get("value") for s in a.get("stats") or []}
            jugadores.append({"id": (a.get("athlete") or {}).get("id"), "n": (a.get("athlete") or {}).get("displayName"),
                              "pos": (a.get("position") or {}).get("abbreviation"), "tit": bool(a.get("starter")),
                              "am": st.get("yellowCards") or 0, "fal": st.get("foulsCommitted"),
                              "rec": st.get("foulsSuffered")})
        if any(j["tit"] for j in jugadores):
            salida[lado] = jugadores
    if len(salida) < 2:
        salida = {}
    if terminado:
        _guardar(nombre, salida)
    return salida or None


def descargar_alineaciones(partidos, hilos=8, aviso=print):
    """Pone p["al"] en cada partido terminado (de la caché o de ESPN). Un fallo deja el partido sin alineación."""
    pendientes = [p for p in partidos if "al" not in p and liga_espn(p["comp"])]
    nuevos = sum(not os.path.exists(os.path.join(CACHE, f"espn_al_{p['id']}.json")) for p in pendientes)
    if nuevos:
        aviso(f"Alineaciones: descargando {nuevos} partidos nuevos...")

    def uno(p):
        try:
            return alineacion_espn(liga_espn(p["comp"]), p["id"])
        except Exception:
            return None

    with ThreadPoolExecutor(hilos) as ex:
        for p, al in zip(pendientes, ex.map(uno, pendientes)):
            p["al"] = al
    return sum(1 for p in partidos if p.get("al"))


# ---------------------------------------------------------------- unir ESPN y football-data

ALIAS = {"internazionale": "inter", "lyonnais": "lyon", "munchen": "munich", "koln": "cologne", "saint germain": "sg",
         "wolverhampton wanderers": "wolves", "brighton hove": "brighton"}
VACIAS = {"fc", "cf", "afc", "ac", "as", "sc", "ssc", "cd", "ud", "sd", "rc", "rcd", "club", "de", "la", "the",
          "calcio", "sv", "vfb", "vfl", "tsg", "bv", "hotspur", "and", "albion", "1", "04", "05", "1899", "1900",
          "1909", "1846", "1913", "olympique", "real", "balompie", "futbol", "madrid", "united", "city", "town"}


def _simple(nombre):
    n = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    for a, b in ALIAS.items():
        n = n.replace(a, b)
    return n


def _fichas(nombre):
    return {t for t in _simple(nombre).split() if t not in VACIAS} or set(_simple(nombre).split())


def parecido(a, b):
    fa, fb = _fichas(a), _fichas(b)
    if not fa or not fb:
        return 0.0
    if fa & fb:
        return len(fa & fb) / min(len(fa), len(fb))
    # Abreviaturas y variantes: "atletico"/"atleti", "bayern"/"munchen"
    return max((1.0 if x[:5] == y[:5] and len(x) >= 5 else 0.0) for x in fa for y in fb)


# Nombres que se confunden con otro equipo o no se parecen: (pista en el nombre, nombre en ESPN)
PISTAS_EQUIPO = [
    (r"\binter\b(?!.*escaldes)", r"inter"),
    (r"espanyol", r"espanyol"),
    (r"alaves", r"alaves"),
    (r"lyon", r"lyon"),
    (r"coruna|^deportivo$", r"deportivo"),
    (r"truiden", r"sint truidense"),
    (r"craiova", r"csu craiova"),
    (r"gilloise", r"union st gilloise"),
    (r"copenhagen|benhavn", r".*benhavn.*"),
]


def poner_arbitros(partidos, calendario, minimo=1.5):
    """Añade árbitro y jornada del calendario a cada partido de ESPN del mismo día con equipos parecidos.

    En copas basta con reconocer un equipo (minimo=1): cada uno juega una vez esa semana y la UEFA abrevia
    nombres que no se parecen a los de ESPN ("Man City").
    """
    por_dia = {}
    for c in calendario:
        if c.get("fecha"):
            por_dia.setdefault(c["fecha"][:10], []).append(c)
    unidos = 0
    for p in partidos:
        dia = datetime.fromisoformat(p["fecha"].replace("Z", "+00:00")).date()
        candidatos = por_dia.get(dia.isoformat(), []) + por_dia.get((dia - timedelta(days=1)).isoformat(), []) \
            + por_dia.get((dia + timedelta(days=1)).isoformat(), [])
        mejor, nota = None, 0.0
        for c in candidatos:
            n = parecido(p["local"], c["local"]) + parecido(p["visitante"], c["visitante"])
            if n > nota:
                mejor, nota = c, n
        p["unido"] = bool(mejor and nota >= minimo)
        p["arbitro"] = mejor["arbitro"] if p["unido"] else None
        p["jornada"] = mejor.get("jornada") if p["unido"] else None
        p["ronda"] = mejor.get("ronda") if p["unido"] else None
        p["cal"] = mejor if p["unido"] else None
        unidos += p["unido"]
    if minimo < 1.5:
        # Copas: lo que quede se une entre partidos que empiezan a la misma hora, por el nombre más parecido
        usados = {id(p["cal"]) for p in partidos if p.get("cal")}
        for p in partidos:
            if p["unido"]:
                continue
            libres = [c for c in calendario if id(c) not in usados and c.get("fecha") and c.get("local") and c.get("visitante")
                      and abs(datetime.fromisoformat(c["fecha"].replace("Z", "+00:00"))
                              - datetime.fromisoformat(p["fecha"].replace("Z", "+00:00"))) <= timedelta(minutes=30)]
            if libres:
                def nota(c):
                    return SequenceMatcher(None, _simple(p["local"]), _simple(c["local"])).ratio() \
                        + SequenceMatcher(None, _simple(p["visitante"]), _simple(c["visitante"])).ratio()
                c = max(libres, key=nota)
                usados.add(id(c))
                p.update(unido=True, cal=c, arbitro=c.get("arbitro"), jornada=c.get("jornada"), ronda=c.get("ronda"))
                unidos += 1
    return unidos


def descargar_historia(fd_key, hilos=6, aviso=print):
    """Devuelve la lista de partidos con amarillas y árbitro: ligas (varias temporadas) y copas (la actual)."""
    actual = temporada_actual()
    hoy = datetime.now(timezone.utc).date()
    tareas, calendarios = [], {}
    for comp, (codigo, liga) in LIGAS.items():
        for t in (*TEMPORADAS_PASADAS, actual):
            if t in calendarios.get(comp, {}):
                continue
            cal = calendario_fd(codigo, t, fd_key, refrescar=t == actual)
            calendarios.setdefault(comp, {})[t] = cal
            dias = {c["fecha"][:10] for c in cal if c.get("fecha") and c["fecha"][:10] <= hoy.isoformat()}
            tareas += [(comp, liga, t, d) for d in dias]
    # Copas: temporada actual, todos los días de partido habituales (martes a jueves) desde septiembre
    inicio = date(actual, 9, 1)
    d = inicio
    while d <= hoy:
        if d.weekday() in (1, 2, 3):
            tareas += [(comp, liga, actual, d.isoformat()) for comp, liga in COPAS.items()]
        d += timedelta(days=1)

    aviso(f"Amarillas: consultando {len(tareas)} días de partido (los ya guardados no gastan red)...")

    def uno(t):
        comp, liga, temporada, dia = t
        try:
            return [dict(p, comp=comp, temporada=temporada) for p in partidos_dia_espn(liga, dia)], None
        except Exception as e:  # un día fallido no debe tumbar el informe
            return [], f"{comp} {dia}: {e}"

    partidos, fallos, vistos = [], [], set()
    with ThreadPoolExecutor(hilos) as ex:
        for lista, fallo in ex.map(uno, tareas):
            if fallo:
                fallos.append(fallo)
            for p in lista:
                if p["id"] not in vistos:
                    vistos.add(p["id"])
                    partidos.append(p)
    for comp in LIGAS:
        for t, cal in calendarios[comp].items():
            poner_arbitros([p for p in partidos if p["comp"] == comp and p["temporada"] == t], cal)
    for comp in COPAS:
        try:
            poner_arbitros([p for p in partidos if p["comp"] == comp], calendario_uefa(comp, actual), minimo=1)
        except Exception as e:  # sin la UEFA las copas siguen funcionando, solo sin árbitro
            fallos.append(f"{comp} (árbitros UEFA): {e}")
    descargar_alineaciones(partidos, hilos=hilos, aviso=aviso)
    partidos.sort(key=lambda p: p["fecha"])
    return partidos, fallos


# ---------------------------------------------------------------- estado de equipos y árbitros

K_EQUIPO = 6       # partidos "de media" que se suman a cada equipo para no exagerar con pocos datos
K_ARBITRO = 20     # ídem para árbitros
PESO_TEMPORADA_ANTERIOR = 0.3
MIN_PARTIDOS = 4   # por debajo no se da pronóstico
K_JUGADOR = 10    # ídem para cada jugador
NOMBRES_RASGOS = ("local", "amarillas", "provocadas", "primera", "faltas", "puntos", "arbitro_local", "arbitro_x_amarillas",
                  "amonestables_x_regateadores")
RASGOS_EQUIPOS = (0, 1, 2, 3, 4, 5)
RASGOS_LOCAL = (0,)
RASGOS_TODOS = (0, 1, 2, 3, 4, 5, 6, 7, 8)


def titulares(al):
    """{"L": [ids], "V": [ids]} de una alineación de ESPN, o None si no la hay."""
    if not al or not al.get("L") or not al.get("V"):
        return None
    xi = {lado: [j["id"] for j in al[lado] if j.get("tit")] for lado in ("L", "V")}
    return xi if xi["L"] and xi["V"] else None


class Estado:
    """Acumula lo ocurrido hasta cierto momento. Solo mira hacia atrás."""

    def __init__(self):
        self.equipos = {}
        self.arbitros = {}
        self.temporada = None
        # Jugadores: amarillas y faltas recibidas por partido como titular; ult = último once de cada equipo
        self.jugadores, self.ult = {}, {}
        self.gj = {"n": 50.0, "am": 50 * 0.2, "nr": 50.0, "rec": 50 * 1.1}
        # Medias generales, con valores de partida razonables
        self.g = {"n": 50.0, "am": 50 * 2.2, "pri": 50 * 0.48, "fal": 50 * 12.0, "pts": 50 * 1.35,
                  "con": 50.0, "loc": 50 * 0.44}

    def _eq(self, i):
        return self.equipos.setdefault(i, {"n": 0.0, "am": 0.0, "riv": 0.0, "pri": 0.0, "fal": 0.0, "nf": 0.0, "pts": 0.0})

    def nueva_temporada(self, t):
        if self.temporada is not None and t != self.temporada:
            for e in self.equipos.values():
                for k in e:
                    e[k] *= PESO_TEMPORADA_ANTERIOR
        self.temporada = t

    def media(self, i, campo, media_g, n_campo="n"):
        e = self.equipos.get(i)
        n = e[n_campo] if e else 0.0
        return ((e[campo] if e else 0.0) + K_EQUIPO * media_g) / (n + K_EQUIPO)

    def resumen(self, i):
        g = self.g
        m_am, m_pri, m_fal, m_pts = g["am"] / g["n"], g["pri"] / g["n"], g["fal"] / g["n"], g["pts"] / g["n"]
        return {"am": self.media(i, "am", m_am), "riv": self.media(i, "riv", m_am), "pri": self.media(i, "pri", m_pri),
                "fal": self.media(i, "fal", m_fal, "nf"), "pts": self.media(i, "pts", m_pts),
                "n": (self.equipos.get(i) or {}).get("n", 0.0)}

    def arbitro(self, nombre):
        g = self.g
        m_am, m_loc = g["am"] / g["n"], g["loc"] / g["con"]
        a = self.arbitros.get(nombre) if nombre else None
        if not a:
            return {"loc": 0.0, "am": 0.0, "n": 0, "am_abs": None, "loc_abs": None}
        loc = (a["loc"] + K_ARBITRO * m_loc) / (a["con"] + K_ARBITRO)
        am = (a["am"] + K_ARBITRO * m_am) / (a["n"] + K_ARBITRO)
        return {"loc": loc - m_loc, "am": am - m_am, "n": a["n"], "am_abs": am * 2, "loc_abs": loc}

    def _tasa(self, i, campo, n_campo):
        j = self.jugadores.get(i) or {}
        return (j.get(campo, 0.0) + K_JUGADOR * self.gj[campo] / self.gj[n_campo]) / (j.get(n_campo, 0.0) + K_JUGADOR)

    def cruce(self, id_l, id_v, xi=None):
        """Amonestables de un once frente a quienes reciben faltas en el otro, con el once dado o el último."""
        ids_l = (xi or {}).get("L") or self.ult.get(id_l)
        ids_v = (xi or {}).get("V") or self.ult.get(id_v)
        if not ids_l or not ids_v:
            return 0.0
        am_l, am_v = (sum(self._tasa(i, "am", "n") for i in ids) for ids in (ids_l, ids_v))
        rec_l, rec_v = (sum(self._tasa(i, "rec", "nr") for i in ids) for ids in (ids_l, ids_v))
        return (am_l * rec_v - am_v * rec_l) / 10

    def rasgos(self, id_l, id_v, arbitro, xi=None):
        l, v = self.resumen(id_l), self.resumen(id_v)
        a = self.arbitro(arbitro)
        d_am = l["am"] - v["am"]
        return [1.0, d_am, l["riv"] - v["riv"], l["pri"] - v["pri"], (l["fal"] - v["fal"]) / 5,
                l["pts"] - v["pts"], a["loc"] * 5, a["am"] * d_am, self.cruce(id_l, id_v, xi)]

    def anotar(self, p):
        g = self.g
        for lado, equipo in (("L", p["id_l"]), ("V", p["id_v"])):
            once = [j for j in ((p.get("al") or {}).get(lado) or []) if j.get("tit")]
            if not once:
                continue
            self.ult[equipo] = [j["id"] for j in once]
            gj = self.gj
            for j in once:
                e = self.jugadores.setdefault(j["id"], {})
                e["n"] = e.get("n", 0.0) + 1
                e["am"] = e.get("am", 0.0) + (j.get("am") or 0)
                gj["n"] += 1
                gj["am"] += j.get("am") or 0
                if j.get("rec") is not None:
                    e["nr"] = e.get("nr", 0.0) + 1
                    e["rec"] = e.get("rec", 0.0) + j["rec"]
                    gj["nr"] += 1
                    gj["rec"] += j["rec"]
        for i, am, riv, pri, fal, gf, gc in (
                (p["id_l"], p["am_l"], p["am_v"], p["primera"] == "L", p["faltas_l"], p["gl"], p["gv"]),
                (p["id_v"], p["am_v"], p["am_l"], p["primera"] == "V", p["faltas_v"], p["gv"], p["gl"])):
            e = self._eq(i)
            pts = 3 if gf > gc else 1 if gf == gc else 0
            e["n"] += 1
            e["am"] += am
            e["riv"] += riv
            e["pri"] += pri
            e["pts"] += pts
            g["n"] += 1
            g["am"] += am
            g["pri"] += pri
            g["pts"] += pts
            if fal is not None:
                e["fal"] += fal
                e["nf"] += 1
                g["fal"] += fal
        if p["primera"]:
            g["con"] += 1
            g["loc"] += p["primera"] == "L"
        arb = p.get("arbitro")
        if arb:
            a = self.arbitros.setdefault(arb, {"n": 0, "am": 0.0, "con": 0, "loc": 0})
            a["n"] += 1
            a["am"] += (p["am_l"] + p["am_v"]) / 2
            if p["primera"]:
                a["con"] += 1
                a["loc"] += p["primera"] == "L"


def construir_filas(partidos):
    """Recorre los partidos por orden de fecha: rasgos con lo anterior a cada partido, y luego lo anota."""
    estado, filas = Estado(), []
    for p in sorted(partidos, key=lambda x: x["fecha"]):
        estado.nueva_temporada(p["temporada"])
        n_min = min((estado.equipos.get(p["id_l"]) or {}).get("n", 0), (estado.equipos.get(p["id_v"]) or {}).get("n", 0))
        if p["primera"] and not p["empate_primera"]:
            filas.append({"x": estado.rasgos(p["id_l"], p["id_v"], p.get("arbitro"), titulares(p.get("al"))), "y": int(p["primera"] == "L"),
                          "temporada": p["temporada"], "comp": p["comp"], "n_min": n_min, "arbitro": p.get("arbitro"),
                          "n_arb": estado.arbitro(p.get("arbitro"))["n"]})
        estado.anotar(p)
    return filas, estado


# ---------------------------------------------------------------- regresión logística sin dependencias

def _resolver(A, b):
    n = len(b)
    M = [fila[:] + [b[i]] for i, fila in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda f: abs(M[f][c]))
        M[c], M[piv] = M[piv], M[c]
        for f in range(n):
            if f != c and M[c][c]:
                r = M[f][c] / M[c][c]
                for k in range(c, n + 1):
                    M[f][k] -= r * M[c][k]
    return [M[i][n] / M[i][i] if M[i][i] else 0.0 for i in range(n)]


def sigmoide(z):
    return 1 / (1 + 2.718281828459045 ** (-max(min(z, 30), -30)))


def ajustar(filas, indices, l2=2.0):
    """Newton con regularización L2 (no se regulariza el término de local)."""
    pesos = [0.0] * len(indices)
    for _ in range(12):
        g = [0.0] * len(indices)
        H = [[0.0] * len(indices) for _ in indices]
        for f in filas:
            x = [f["x"][i] for i in indices]
            p = sigmoide(sum(a * b for a, b in zip(pesos, x)))
            w = max(p * (1 - p), 1e-6)
            for i, xi in enumerate(x):
                g[i] += (f["y"] - p) * xi
                for j, xj in enumerate(x):
                    H[i][j] += w * xi * xj
        for i in range(1, len(indices)):
            g[i] -= l2 * pesos[i]
            H[i][i] += l2
        paso = _resolver(H, g)
        pesos = [a + b for a, b in zip(pesos, paso)]
        if max(abs(b) for b in paso) < 1e-7:
            break
    return pesos


def probabilidad(pesos, indices, x):
    return sigmoide(sum(p * x[i] for p, i in zip(pesos, indices)))


# ---------------------------------------------------------------- pronóstico para el informe

class Pronosticador:
    def __init__(self, partidos):
        filas, self.estado = construir_filas(partidos)
        self.pesos = ajustar([f for f in filas if f["comp"] in LIGAS and f["n_min"] >= MIN_PARTIDOS], RASGOS_TODOS)
        self.equipos = {}  # id -> {"nombre": ..., "comps": set}
        jugados = sin = 0
        for p in partidos:
            for i, nombre in ((p["id_l"], p["local"]), (p["id_v"], p["visitante"])):
                self.equipos.setdefault(i, {"nombre": nombre, "comps": set()})["comps"].add(p["comp"])
            jugados += 1
            sin += not p["primera"]
        self.sin_amarilla = sin / max(jugados, 1)

    def buscar_equipo(self, nombre, comp):
        """Busca por nombre, primero entre los equipos que juegan esa competición."""
        simple = _simple(nombre)
        for pista, objetivo in PISTAS_EQUIPO:
            if re.search(pista, simple):
                for i, e in self.equipos.items():
                    if re.fullmatch(objetivo, _simple(e["nombre"])):
                        return i
                return None
        for candidatos in ([i for i, e in self.equipos.items() if comp in e["comps"]], list(self.equipos)):
            mejor, nota = None, 0.0
            for i in candidatos:
                base = parecido(nombre, self.equipos[i]["nombre"])
                if base < 1:
                    continue
                n = base + SequenceMatcher(None, _simple(nombre), _simple(self.equipos[i]["nombre"])).ratio()
                if n > nota:
                    mejor, nota = i, n
            if mejor is not None:
                return mejor
        return None

    def buscar_arbitro(self, nombre):
        if not nombre:
            return None
        if nombre in self.estado.arbitros:
            return nombre
        mejor, nota = nombre, 0.0
        for a in self.estado.arbitros:
            n = SequenceMatcher(None, _simple(nombre), _simple(a)).ratio()
            if n > nota:
                mejor, nota = a, n
        return mejor if nota >= 0.8 else nombre

    def pronostico(self, local, visitante, comp, arbitro=None, xi=None):
        """Devuelve el equipo más probable o None si no hay datos suficientes de los dos.

        xi: once titular de cada equipo ({"L": [ids], "V": [ids]}) si ya se conoce; si no, se usa el último.
        """
        id_l, id_v = self.buscar_equipo(local, comp), self.buscar_equipo(visitante, comp)
        if id_l is None or id_v is None or id_l == id_v:
            return None
        l, v = self.estado.resumen(id_l), self.estado.resumen(id_v)
        if min(l["n"], v["n"]) < MIN_PARTIDOS:
            return None
        arb = self.buscar_arbitro(arbitro)
        pl = probabilidad(self.pesos, RASGOS_TODOS, self.estado.rasgos(id_l, id_v, arb, xi))
        pl_total, pv_total = pl * (1 - self.sin_amarilla), (1 - pl) * (1 - self.sin_amarilla)
        info_arb = self.estado.arbitro(arb)
        return {
            "equipo": local if pl >= 0.5 else visitante, "prob": max(pl_total, pv_total),
            "otro": visitante if pl >= 0.5 else local, "probOtro": min(pl_total, pv_total),
            "sinAmarilla": self.sin_amarilla,
            "statsL": {"am": l["am"], "fal": l["fal"], "pri": l["pri"]},
            "statsV": {"am": v["am"], "fal": v["fal"], "pri": v["pri"]},
            "arbitro": arb, "arbAmarillas": info_arb["am_abs"], "arbLocal": info_arb["loc_abs"], "arbPartidos": info_arb["n"],
        }
