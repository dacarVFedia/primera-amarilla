"""
Por qué acierta y por qué falla el pronóstico de la primera amarilla.

Recorre las temporadas en orden: para cada una entrena solo con las anteriores y pronostica sus partidos,
como si fuera en directo. Luego cuenta los aciertos según el tipo de partido y prueba variantes del modelo.

    python analisis.py                  descarga (o lee de la caché) y analiza
    python analisis.py partidos.json    usa una historia ya guardada

No usa cuotas ni The Odds API.
"""

import json
import math
import sys
from collections import defaultdict

sys.dont_write_bytecode = True

import amarillas as am

PRUEBA = (2024, 2025, 2026)  # temporadas que se pronostican (2023 solo sirve para empezar)


# ---------------------------------------------------------------- variantes del estado

class EstadoReciente(am.Estado):
    """Añade la forma reciente: media con olvido de amarillas y de primeras amarillas de cada equipo."""

    OLVIDO = 0.85  # cada partido nuevo pesa como ~6-7 partidos

    def __init__(self):
        super().__init__()
        self.rec = {}

    def nueva_temporada(self, t):
        super().nueva_temporada(t)

    def _r(self, i):
        g = self.g
        return self.rec.get(i, {"am": g["am"] / g["n"], "pri": g["pri"] / g["n"]})

    def rasgos(self, id_l, id_v, arbitro):
        x = super().rasgos(id_l, id_v, arbitro)
        l, v = self._r(id_l), self._r(id_v)
        return x + [l["am"] - v["am"], l["pri"] - v["pri"]]

    def anotar(self, p):
        for i, am_, pri in ((p["id_l"], p["am_l"], p["primera"] == "L"), (p["id_v"], p["am_v"], p["primera"] == "V")):
            r = dict(self._r(i))
            r["am"] = self.OLVIDO * r["am"] + (1 - self.OLVIDO) * am_
            r["pri"] = self.OLVIDO * r["pri"] + (1 - self.OLVIDO) * pri
            self.rec[i] = r
        super().anotar(p)


LIGAS_LISTA = list(am.LIGAS)


def con_liga(x, comp):
    """Un término de local propio para cada liga (en unas el local ve antes la amarilla que en otras)."""
    return x + [1.0 if comp == c else 0.0 for c in LIGAS_LISTA[1:]]


# ---------------------------------------------------------------- recorrido

def recorrer(partidos, clase=am.Estado, extra=None):
    """Filas con rasgos calculados solo con lo anterior, más datos del partido para el análisis."""
    estado, filas = clase(), []
    for p in sorted(partidos, key=lambda x: x["fecha"]):
        estado.nueva_temporada(p["temporada"])
        el, ev = estado.equipos.get(p["id_l"]) or {}, estado.equipos.get(p["id_v"]) or {}
        n_min = min(el.get("n", 0), ev.get("n", 0))
        if p["primera"] and not p["empate_primera"] and p["comp"] in am.LIGAS:
            x = estado.rasgos(p["id_l"], p["id_v"], p.get("arbitro"), am.titulares(p.get("al")))
            if extra:
                x = extra(x, p["comp"])
            filas.append({"x": x, "y": int(p["primera"] == "L"), "temporada": p["temporada"], "comp": p["comp"],
                          "n_min": n_min, "n_arb": estado.arbitro(p.get("arbitro"))["n"], "p": p,
                          "pts": x[5] if len(x) > 5 else 0.0})
        estado.anotar(p)
    return filas


def evaluar(filas, l2=2.0, minimo=am.MIN_PARTIDOS):
    """Entrena con las temporadas anteriores y pronostica cada temporada de prueba."""
    indices = list(range(len(filas[0]["x"])))
    salida = []
    for t in PRUEBA:
        entreno = [f for f in filas if f["temporada"] < t and f["n_min"] >= minimo]
        pesos = am.ajustar(entreno, indices, l2=l2)
        for f in filas:
            if f["temporada"] == t and f["n_min"] >= minimo:
                salida.append(dict(f, prob=am.probabilidad(pesos, indices, f["x"])))
    return salida


def notas(res):
    n = len(res)
    aciertos = sum((r["prob"] >= 0.5) == bool(r["y"]) for r in res)
    perdida = -sum(math.log(r["prob"] if r["y"] else 1 - r["prob"]) for r in res) / n
    seguros = [r for r in res if max(r["prob"], 1 - r["prob"]) >= 0.6]
    acs = sum((r["prob"] >= 0.5) == bool(r["y"]) for r in seguros)
    return {"n": n, "acierto": aciertos / n, "perdida": perdida, "n60": len(seguros),
            "acierto60": acs / max(len(seguros), 1)}


def tabla(titulo, res, clave):
    grupos = defaultdict(list)
    for r in res:
        grupos[clave(r)].append(r)
    print(f"\n{titulo}")
    for k in sorted(grupos, key=lambda k: (str(type(k)), k)):
        g = grupos[k]
        ac = sum((r["prob"] >= 0.5) == bool(r["y"]) for r in g) / len(g)
        print(f"  {str(k):<28} {len(g):>5} partidos  {ac:6.1%}")


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            partidos = json.load(f)
    else:
        import registro
        partidos, _ = am.descargar_historia(registro.clave_football_data())

    base = evaluar(recorrer(partidos))
    print("MODELO ACTUAL (cada temporada pronosticada solo con las anteriores)")
    print("  ", {k: round(v, 4) for k, v in notas(base).items()})

    # ------------------------------------------------ dónde acierta y dónde falla
    def elegido_local(r):
        return r["prob"] >= 0.5

    tabla("Por liga", base, lambda r: r["comp"])
    tabla("Por temporada", base, lambda r: r["temporada"])
    tabla("Por seguridad del pronóstico", base,
          lambda r: f"{int(max(r['prob'], 1 - r['prob']) * 20) * 5}%+")
    tabla("Por equipo elegido", base, lambda r: "local" if elegido_local(r) else "visitante")
    tabla("Por árbitro conocido (partidos suyos en la historia)", base,
          lambda r: "sin árbitro" if not r["p"].get("arbitro") else "0-9" if r["n_arb"] < 10 else "10-29" if r["n_arb"] < 30 else "30+")
    tabla("Por minuto de la primera amarilla", base,
          lambda r: "0-15'" if r["p"]["minuto"] <= 15 else "16-30'" if r["p"]["minuto"] <= 30
          else "31-45'" if r["p"]["minuto"] <= 45 else "segunda parte")
    tabla("Por diferencia de puntos por partido (local - visitante)", base,
          lambda r: "local mucho mejor (>+0.5)" if r["pts"] > 0.5 else "local mucho peor (<-0.5)" if r["pts"] < -0.5 else "parejos")
    tabla("Por resultado final", base,
          lambda r: "ganó local" if r["p"]["gl"] > r["p"]["gv"] else "empate" if r["p"]["gl"] == r["p"]["gv"] else "ganó visitante")
    tabla("Por amarillas totales del partido", base,
          lambda r: "1-2" if r["p"]["am_l"] + r["p"]["am_v"] <= 2 else "3-4" if r["p"]["am_l"] + r["p"]["am_v"] <= 4 else "5+")

    # ------------------------------------------------ variantes
    print("\nVARIANTES (misma prueba: solo datos anteriores a cada temporada)")
    variantes = {"actual": base}
    variantes["local por liga"] = evaluar(recorrer(partidos, extra=con_liga))
    variantes["forma reciente"] = evaluar(recorrer(partidos, clase=EstadoReciente))
    variantes["forma reciente + local por liga"] = evaluar(recorrer(partidos, clase=EstadoReciente, extra=con_liga))
    for l2 in (0.5, 10.0, 50.0):
        variantes[f"regularización {l2}"] = evaluar(recorrer(partidos), l2=l2)
    originales = (am.K_EQUIPO, am.K_ARBITRO, am.PESO_TEMPORADA_ANTERIOR)
    for nombre, k_eq, k_arb, peso in (("K equipo 3", 3, 20, 0.3), ("K equipo 12", 12, 20, 0.3),
                                      ("K árbitro 8", 6, 8, 0.3), ("K árbitro 50", 6, 50, 0.3),
                                      ("temporada anterior 0.1", 6, 20, 0.1), ("temporada anterior 0.6", 6, 20, 0.6)):
        am.K_EQUIPO, am.K_ARBITRO, am.PESO_TEMPORADA_ANTERIOR = k_eq, k_arb, peso
        variantes[nombre] = evaluar(recorrer(partidos))
    am.K_EQUIPO, am.K_ARBITRO, am.PESO_TEMPORADA_ANTERIOR = originales
    for nombre, res in variantes.items():
        n = notas(res)
        print(f"  {nombre:<34} acierto {n['acierto']:6.2%}  pérdida {n['perdida']:.4f}  "
              f"≥60%: {n['n60']:>4} con {n['acierto60']:6.2%}")


if __name__ == "__main__":
    main()
