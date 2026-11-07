# Primera amarilla

Registro de la temporada: quién vio la primera amarilla en cada partido, con árbitro y el equipo más probable antes de jugarse. Cubre LaLiga, Premier League, Serie A, Bundesliga, Ligue 1, Eredivisie, Champions, Europa League y Conference.

GitHub Actions regenera la página varias veces al día y la publica en GitHub Pages. Los datos salen de ESPN, football-data.org y la UEFA.

Los árbitros confirmados desde la página se guardan en `arbitros_confirmados.csv`. Cada cambio regenera la página, y si la fuente oficial publica otro árbitro, manda la fuente.

Es una estimación y falla a menudo. No es una recomendación de apuesta.

Para generarla en local: `python registro.py`. Necesita la clave de football-data en `config.json` o en la variable `FOOTBALL_DATA_KEY`.
