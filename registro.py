"""
Registro de la primera amarilla: todas las jornadas de la temporada por competición.

Genera registro_amarillas.html con, para cada partido, quién vio la primera amarilla, el árbitro
y qué equipo era más probable según lo ocurrido ANTES de ese partido (no mira el resultado).

    python registro.py             genera la página y la abre
    python registro.py --no-abrir  solo la genera

No usa The Odds API, así que no gasta créditos.
"""

import csv
import json
import math
import os
import re
import sys
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True

import amarillas as am
import arbitros_oficiales

CARPETA = os.path.dirname(os.path.abspath(__file__))
PLANTILLA = os.path.join(CARPETA, "registro_plantilla.html")
CONFIG = os.path.join(CARPETA, "config.json")
ARBITROS_MANUAL = os.path.join(CARPETA, "arbitros.csv")
ARBITROS_CONFIRMADOS = os.path.join(CARPETA, "arbitros_confirmados.csv")
REPOSITORIO = os.environ.get("GITHUB_REPOSITORY") or "dacarVFedia/primera-amarilla"
SALIDA = os.path.join(CARPETA, "registro_amarillas.html")
DIAS_CON_PREDICCION = 21  # más lejos no se predice: el estado de forma de hoy ya no diría mucho
ORDEN = ["LaLiga", "Premier League", "Serie A", "Bundesliga", "Ligue 1", "Eredivisie",
         "Champions League", "Europa League", "Conference League", *am.SELECCIONES]


def clave_football_data():
    """En GitHub la clave llega como secreto; en el PC, desde config.json."""
    key = os.environ.get("FOOTBALL_DATA_KEY")
    if key:
        return key
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)["football_data_key"]
    sys.exit("Falta la clave de football-data: pon FOOTBALL_DATA_KEY o crea config.json.")


def arbitros_manual():
    """Filas de arbitros.csv (local;visitante;arbitro). Es opcional: en GitHub no existe."""
    if not os.path.exists(ARBITROS_MANUAL):
        return []
    with open(ARBITROS_MANUAL, encoding="utf-8-sig", newline="") as f:
        return [fila for fila in csv.DictReader(f, delimiter=";") if (fila.get("arbitro") or "").strip()]


def arbitros_confirmados(temporada):
    """Árbitros confirmados desde la página, por competición y equipos. Se guardan en el repositorio."""
    if not os.path.exists(ARBITROS_CONFIRMADOS):
        return {}
    with open(ARBITROS_CONFIRMADOS, encoding="utf-8-sig", newline="") as f:
        return {clave_confirmado(fila["competicion"], fila["local"], fila["visitante"]): fila["arbitro"].strip()
                for fila in csv.DictReader(f, delimiter=";")
                if fila.get("temporada") == str(temporada) and (fila.get("arbitro") or "").strip()}


def clave_confirmado(comp, local, visitante):
    return comp, am._simple(local), am._simple(visitante)


def clave(c):
    return c.get("fecha"), c.get("local"), c.get("visitante")


def fecha_de(texto):
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def arbitro_manual(manual, local, visitante):
    for f in manual:
        if am.parecido(local, f.get("local", "")) >= 1 and am.parecido(visitante, f.get("visitante", "")) >= 1:
            return (f.get("arbitro") or "").strip() or None
    return None


def previas_sin_mirar_resultado(partidos, actual):
    """Probabilidad de que el local reciba la primera, calculada antes de cada partido de esta temporada.

    Los pesos del modelo salen solo de temporadas anteriores, así que el acierto del registro es una prueba real.
    """
    filas, _ = am.construir_filas(partidos)
    pesos = am.ajustar([f for f in filas if f["comp"] in am.LIGAS and f["n_min"] >= am.MIN_PARTIDOS
                        and f["temporada"] < actual], am.RASGOS_TODOS)
    estado, previas = am.Estado(), {}
    for p in sorted(partidos, key=lambda x: x["fecha"]):
        estado.nueva_temporada(p["temporada"])
        if p["temporada"] == actual:
            n = min((estado.equipos.get(p["id_l"]) or {}).get("n", 0), (estado.equipos.get(p["id_v"]) or {}).get("n", 0))
            if n >= am.MIN_PARTIDOS:
                previas[p["id"]] = am.ajuste_copa(am.probabilidad(pesos, am.RASGOS_TODOS, estado.rasgos(
                    p["id_l"], p["id_v"], p.get("arbitro"), am.titulares(p.get("al")))), p["comp"])
        estado.anotar(p)
    return previas


def estado_de(c, ahora):
    e = (c.get("estado") or "").upper()
    if e in ("POSTPONED", "SUSPENDED", "CANCELLED", "CANCELED"):
        return "aplazado"
    if e in ("IN_PLAY", "PAUSED", "LIVE"):
        return "juego"
    if e == "FINISHED":
        return "sin_datos"
    if c.get("fecha") and timedelta(0) < ahora - fecha_de(c["fecha"]) < timedelta(hours=3):
        return "juego"
    return "pendiente"


def onces_cercanos(calendarios, ahora, fallos):
    """Once titular de los partidos que empiezan en las próximas 2 horas o se están jugando, si ESPN ya lo ha publicado.

    Devuelve [(comp, local, visitante, xi)] con los nombres de ESPN.
    """
    dias = set()
    for comp, cal in calendarios.items():
        for c in cal:
            if c.get("fecha") and timedelta(hours=-3) <= fecha_de(c["fecha"]) - ahora <= timedelta(hours=2):
                dias.add((comp, c["fecha"][:10]))
    salida = []
    for comp, dia in dias:
        liga = am.liga_espn(comp)
        try:
            datos = am._pedir(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{liga}/scoreboard?dates={dia.replace('-', '')}")
        except Exception as e:
            fallos.append(f"{comp} {dia} (alineaciones): {e}")
            continue
        for ev in datos.get("events", []):
            if (ev.get("status", {}).get("type") or {}).get("state") not in ("pre", "in"):
                continue
            equipos = {x["homeAway"]: x["team"]["displayName"] for x in ev["competitions"][0]["competitors"]}
            try:
                xi = am.titulares(am.alineacion_espn(liga, ev["id"], terminado=False))
            except Exception as e:
                fallos.append(f"{comp} {equipos.get('home')} (alineación): {e}")
                continue
            if xi:
                salida.append((comp, equipos.get("home"), equipos.get("away"), xi))
    return salida


def poner_directo(en_juego, fallos):
    """Marcador y primera amarilla de los partidos que se están jugando, desde ESPN.

    La primera amarilla no cambia al acabar el partido, así que ya se sabe si la predicción acertó.
    """
    pedir = {(ORDEN[f["c"]], f["f"][:10]) for f in en_juego}
    for comp, dia in pedir:
        try:
            datos = am._pedir(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{am.liga_espn(comp)}/scoreboard?dates={dia.replace('-', '')}")
        except Exception as e:  # sin directo la página sigue saliendo, con el partido como "En juego"
            fallos.append(f"{comp} {dia} (directo): {e}")
            continue
        for ev in datos.get("events", []):
            r = am._resumir_evento(ev, en_juego=True)
            if not r:
                continue
            for f in en_juego:
                if ORDEN[f["c"]] == comp and am.parecido(f["l"], r["local"]) >= 1 and am.parecido(f["v"], r["visitante"]) >= 1:
                    estado = ev.get("status", {})
                    descanso = (estado.get("type") or {}).get("name") == "STATUS_HALFTIME"
                    f.update(gl=r["gl"], gv=r["gv"], pr=r["primera"], m=r["minuto"], e=bool(r["empate_primera"]),
                             vivo="Descanso" if descanso else (estado.get("displayClock") or "En juego"))


ARCHIVO_MOSTRADOS = os.path.join(am.CACHE, "pronosticos_mostrados.json")


def clave_mostrado(comp, dia, local, visitante):
    return "|".join((comp, dia, am._simple(local), am._simple(visitante)))


SEMILLA_MOSTRADOS = os.path.join(CARPETA, "pronosticos_vistos_inicio.json")  # lo visto antes de existir esto


def vistos_en_pagina(datos):
    """Pronósticos de los partidos sin empezar de una página ya generada, tal como los enseñaba
    (con el árbitro confirmado a mano, la página recalcula igual que aquí)."""
    salida = {}
    for m in datos.get("partidos", []):
        if m.get("s") != "pendiente" or m.get("pl") is None:
            continue
        pl, pv = m["pl"], m["pv"]
        a = (datos.get("arbitros") or {}).get(m.get("cf") or "")
        if a and m.get("b") is not None:
            q = 1 / (1 + math.exp(-(m.get("k") or 1) * (m["b"] + datos["pesoLoc"] * a[0] * 5 + datos["pesoAm"] * a[1] * m["d"])))
            pl, pv = round(q * datos["resta"] * 1000) / 10, round((1 - q) * datos["resta"] * 1000) / 10
        salida[clave_mostrado(datos["comps"][m["c"]], m["f"][:10], m["l"], m["v"])] = [pl, pv]
    return salida


def leer_publicada():
    """Los datos de la página que está publicada ahora mismo, o {} si no se puede leer."""
    try:
        req = urllib.request.Request(f"https://{REPOSITORIO.split('/')[0].lower()}.github.io/{REPOSITORIO.split('/')[1]}/",
                                     headers={"User-Agent": "primera-amarilla/1.0", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as r:
            texto = r.read().decode("utf-8")
        return json.loads(re.search(r"const D = (\{.*?\});\n", texto, re.S).group(1))
    except Exception:
        return {}


def leer_mostrados():
    mostrados = {}
    for ruta in (SEMILLA_MOSTRADOS, ARCHIVO_MOSTRADOS):
        try:
            with open(ruta, encoding="utf-8") as f:
                mostrados.update(json.load(f))
        except (OSError, ValueError):
            pass
    # Lo que enseña ahora la página publicada manda: es exactamente lo que se ha visto
    mostrados.update(vistos_en_pagina(leer_publicada()))
    return mostrados


def guardar_mostrados(mostrados, ahora):
    """Guarda los pronósticos enseñados antes de cada partido; se olvidan a los 60 días."""
    limite = (ahora - timedelta(days=60)).date().isoformat()
    vivos = {k: v for k, v in mostrados.items() if k.split("|")[1] >= limite}
    try:
        os.makedirs(am.CACHE, exist_ok=True)
        with open(ARCHIVO_MOSTRADOS, "w", encoding="utf-8") as f:
            json.dump(vivos, f, ensure_ascii=False)
    except OSError:
        pass


def construir():
    key = clave_football_data()
    partidos, fallos = am.descargar_historia(key)
    actual = am.temporada_actual()
    ahora = datetime.now(timezone.utc)
    hoy_local = datetime.now().astimezone().date()
    manual = arbitros_manual()
    confirmados = arbitros_confirmados(actual)
    usados = set()
    calendarios = {c: am.calendario_fd(am.LIGAS[c][0], actual, key) for c in am.LIGAS}
    for c in am.COPAS_UEFA:
        try:
            calendarios[c] = am.calendario_uefa(c, actual)
        except Exception as e:
            fallos.append(f"{c} (calendario UEFA): {e}")
            calendarios[c] = []

    # Partidos cambiados de fecha: se unen por los dos equipos aunque no coincida el día
    # (en copas los equipos se repiten poco, pero se exige igualmente que coincidan los dos)
    for p in partidos:
        if p["temporada"] != actual:
            continue
        if not p.get("cal"):
            for c in calendarios.get(p["comp"], []):
                if am.parecido(p["local"], c["local"]) >= 1 and am.parecido(p["visitante"], c["visitante"]) >= 1:
                    p.update(cal=c, arbitro=c.get("arbitro"), jornada=c.get("jornada"), ronda=c.get("ronda"))
                    break
        # La fuente oficial manda. Si no tiene árbitro, vale el confirmado desde la página.
        if not p.get("arbitro"):
            k = clave_confirmado(p["comp"], p["local"], p["visitante"])
            if k in confirmados:
                p["arbitro"], p["confirmado"] = confirmados[k], True
                usados.add(k)
        # arbitros.csv solo vale para los partidos de hoy, que aún pueden no tener árbitro en la fuente
        if not p.get("arbitro") and fecha_de(p["fecha"]).astimezone().date() == hoy_local:
            p["arbitro"] = arbitro_manual(manual, p["local"], p["visitante"])

    pron = am.Pronosticador(partidos)
    # Árbitros designados que publican las ligas antes del partido (football-data no los da hasta después)
    oficiales = arbitros_oficiales.poner(calendarios, set(pron.estado.arbitros), ahora, am.CACHE, fallos)
    print(f"Árbitros oficiales leídos de las ligas: {oficiales}")
    previas = previas_sin_mirar_resultado(partidos, actual)
    resta = 1 - pron.sin_amarilla

    # Cada calendario escribe los equipos a su manera: los partidos ya unidos dicen cómo se llama cada uno en ESPN
    alias = {}
    for p in partidos:
        if p["temporada"] == actual and p.get("cal"):
            alias[(p["comp"], p["cal"]["local"])] = p["local"]
            alias[(p["comp"], p["cal"]["visitante"])] = p["visitante"]

    def nombre(texto, comp):
        if (comp, texto) in alias:
            return alias[(comp, texto)]
        i = pron.buscar_equipo(texto, comp)
        return pron.equipos[i]["nombre"] if i is not None else texto

    # El pronóstico que la página enseñaba justo antes de empezar cada partido. Es el que se califica después,
    # para que el acierto sea el de lo que se vio (y se apostó), no el de un cálculo rehecho al acabar.
    mostrados = leer_mostrados()

    def mostrado(comp, fecha, local, visitante):
        dia = fecha_de(fecha).date()
        for d in (dia, dia - timedelta(days=1), dia + timedelta(days=1)):
            r = mostrados.get(clave_mostrado(comp, d.isoformat(), local, visitante))
            if r:
                return r
        return None

    filas, unidos = [], set()
    for p in partidos:
        if p["temporada"] != actual or p["comp"] not in ORDEN:
            continue
        if p.get("cal"):
            unidos.add((p["comp"], *clave(p["cal"])))
        q = previas.get(p["id"])
        filas.append({
            "c": ORDEN.index(p["comp"]), "j": p.get("jornada"), "r": p.get("ronda"), "f": p["fecha"],
            "l": p["local"], "v": p["visitante"], "gl": p["gl"], "gv": p["gv"], "a": p.get("arbitro"),
            "s": "fin", "pr": p["primera"], "m": p["minuto"], "e": bool(p["empate_primera"]),
            "al": p["am_l"], "av": p["am_v"], "fl": p["faltas_l"], "fv": p["faltas_v"],
            "pl": None if q is None else round(q * resta * 100, 1), "pv": None if q is None else round((1 - q) * resta * 100, 1),
        })
        visto = mostrado(p["comp"], p["fecha"], p["local"], p["visitante"])
        if visto:
            filas[-1]["pl"], filas[-1]["pv"] = visto
        if p.get("confirmado"):
            filas[-1]["cf"] = p["arbitro"]

    onces = onces_cercanos(calendarios, ahora, fallos)

    def once_de(comp, local, visitante):
        for c, l, v, xi in onces:
            if c == comp and am.parecido(local, l) >= 1 and am.parecido(visitante, v) >= 1:
                return xi
        return None

    limite = ahora + timedelta(days=DIAS_CON_PREDICCION)
    for comp, cal in calendarios.items():
        for c in cal:
            if not c.get("fecha") or not c.get("local") or not c.get("visitante") or (comp, *clave(c)) in unidos:
                continue
            ini = fecha_de(c["fecha"])
            arb = c.get("arbitro")
            if not arb and ini.astimezone().date() == hoy_local:
                arb = arbitro_manual(manual, c["local"], c["visitante"])
            fila = {"c": ORDEN.index(comp), "j": c.get("jornada"), "r": c.get("ronda"), "f": c["fecha"],
                    "l": nombre(c["local"], comp), "v": nombre(c["visitante"], comp), "a": arb,
                    "s": estado_de(c, ahora), "pr": None, "pl": None, "pv": None}
            if not c.get("arbitro"):
                k = clave_confirmado(comp, fila["l"], fila["v"])
                if k in confirmados:
                    usados.add(k)
                    fila["cf"] = confirmados[k]
                    # Antes de empezar, la página parte del cálculo sin árbitro para poder cambiarlo o quitarlo
                    arb = None if fila["s"] == "pendiente" else confirmados[k]
                    fila["a"] = arb
            if ini <= limite and fila["s"] in ("pendiente", "juego"):
                xi = once_de(comp, fila["l"], fila["v"])
                if xi:
                    fila["xi"] = True
                a = pron.pronostico(fila["l"], fila["v"], comp, arb, xi)
                if a:
                    local_es_favorito = a["equipo"] == fila["l"]
                    fila["pl"] = round((a["prob"] if local_es_favorito else a["probOtro"]) * 100, 1)
                    fila["pv"] = round((a["probOtro"] if local_es_favorito else a["prob"]) * 100, 1)
                    # Sin árbitro publicado: la página recalcula sola con el que elija la persona
                    if not arb and fila["s"] == "pendiente":
                        x = pron.estado.rasgos(pron.buscar_equipo(fila["l"], comp), pron.buscar_equipo(fila["v"], comp), None, xi)
                        fila["b"] = round(sum(w * v for w, v in zip(pron.pesos, x)), 5)
                        fila["d"] = round(x[1], 5)
                        if am.es_copa(comp):
                            fila["k"] = am.FACTOR_COPAS  # la página rebaja la confianza igual que el modelo
                    k_m = clave_mostrado(comp, c["fecha"][:10], fila["l"], fila["v"])
                    if fila["s"] == "pendiente":
                        # Lo que ve la persona: con el árbitro confirmado a mano, la página recalcula con él
                        visto = a if not fila.get("cf") or arb else pron.pronostico(fila["l"], fila["v"], comp, fila["cf"], xi) or a
                        fav = visto["equipo"] == fila["l"]
                        mostrados[k_m] = [round((visto["prob"] if fav else visto["probOtro"]) * 100, 1),
                                          round((visto["probOtro"] if fav else visto["prob"]) * 100, 1)]
                    elif k_m in mostrados:
                        fila["pl"], fila["pv"] = mostrados[k_m]
            filas.append(fila)
    guardar_mostrados(mostrados, ahora)
    poner_directo([f for f in filas if f["s"] == "juego"], fallos)
    filas.sort(key=lambda f: f["f"])

    # Árbitros que se ofrecen en el desplegable: los que han dirigido esta temporada en cada competición
    arbitros, por_comp = {}, {c: set() for c in ORDEN}
    for p in partidos:
        if p["temporada"] == actual and p.get("arbitro") and p["comp"] in por_comp:
            a = pron.estado.arbitro(p["arbitro"])
            arbitros[p["arbitro"]] = [round(a["loc"], 5), round(a["am"], 5), a["n"]]
            por_comp[p["comp"]].add(p["arbitro"])
    for (comp, _, _), nom in confirmados.items():
        if comp in por_comp:
            a = pron.estado.arbitro(pron.buscar_arbitro(nom))
            arbitros.setdefault(nom, [round(a["loc"], 5), round(a["am"], 5), a["n"]])
            por_comp[comp].add(nom)
    for k in confirmados.keys() - usados:
        print("Aviso: árbitro confirmado sin partido:", *k, confirmados[k])

    datos = {
        "comps": ORDEN, "copas": [*am.COPAS, *am.SELECCIONES], "temporada": f"{actual}/{str(actual + 1)[2:]}",
        "anio": actual, "repo": REPOSITORIO,
        "generado": ahora.isoformat(), "sinAmarilla": round(pron.sin_amarilla * 100, 1), "partidos": filas,
        "resta": resta, "pesoLoc": pron.pesos[6], "pesoAm": pron.pesos[7], "arbitros": arbitros,
        "arbitrosComp": [sorted(por_comp[c], key=am._simple) for c in ORDEN],
    }
    if "--comprobar" in sys.argv:  # compara el cálculo de la página con el del modelo
        m = next(f for f in filas if f.get("b") is not None)
        comp = ORDEN[m["c"]]
        nombre_arb = datos["arbitrosComp"][m["c"]][0]
        r = pron.pronostico(m["l"], m["v"], comp, nombre_arb)
        print("COMPROBAR", m["l"], "-", m["v"], "|", nombre_arb, "|", r["equipo"], round(r["prob"] * 100, 1), "|", r["otro"], round(r["probOtro"] * 100, 1))
    return datos, fallos


def resumen(datos, fallos):
    print(f"\nTemporada {datos['temporada']}: {len(datos['partidos'])} partidos en el registro")
    for i, comp in enumerate(datos["comps"]):
        ps = [p for p in datos["partidos"] if p["c"] == i]
        fin = [p for p in ps if p["s"] == "fin"]
        # Sin amarillas o con las dos a la vez cuenta como fallo, igual que en la página
        con = [p for p in fin if p["pl"] is not None]

        def bien(p):
            return bool(p["pr"]) and not p["e"] and (p["pl"] >= p["pv"]) == (p["pr"] == "L")
        ok = sum(map(bien, con))
        altos = [p for p in con if max(p["pl"], p["pv"]) >= 60]
        ok_a = sum(map(bien, altos))
        print(f"  {comp:18} {len(fin):3} jugados de {len(ps):3} | árbitro {sum(bool(p['a']) for p in fin):3} | "
              f"sin jornada {sum(p['j'] is None for p in fin):2} | sin datos {sum(p['s'] == 'sin_datos' for p in ps):2} | "
              f"acierta {ok}/{len(con)} | >=60%: {ok_a}/{len(altos)}")
    for f in fallos[:15]:
        print("  aviso:", f)


def escribir(datos):
    with open(PLANTILLA, encoding="utf-8") as f:
        plantilla = f.read()
    texto = json.dumps(datos, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    pagina = plantilla.replace("/*DATOS*/null", texto)
    with open(SALIDA, "w", encoding="utf-8") as f:
        f.write('<!doctype html><html lang="es"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                '<meta name="robots" content="noindex"></head><body>\n')
        f.write(pagina)
        f.write("\n</body></html>\n")
    return pagina


def main():
    datos, fallos = construir()
    resumen(datos, fallos)
    # En la actualización automática, mejor conservar la última página buena que publicar una incompleta
    if "--estricto" in sys.argv:
        jugados = sum(p["s"] == "fin" for p in datos["partidos"])
        if not jugados or len(fallos) > 50 or any("UEFA" in f for f in fallos):
            sys.exit(f"Datos incompletos ({jugados} partidos jugados, {len(fallos)} fallos): no se genera la página.")
    pagina = escribir(datos)
    # Copia sin cabecera de documento, por si se quiere publicar la página
    for a in sys.argv[1:]:
        if a.startswith("--copia="):
            with open(a.split("=", 1)[1], "w", encoding="utf-8") as f:
                f.write(pagina)
    print("\nPágina:", SALIDA)
    if "--no-abrir" not in sys.argv:
        webbrowser.open("file:///" + SALIDA.replace("\\", "/"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
