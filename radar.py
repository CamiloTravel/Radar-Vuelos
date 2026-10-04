#!/usr/bin/env python3
"""
RADAR DE VUELOS BARATOS · v2
============================
Se ejecuta automáticamente cada 3 horas en GitHub Actions:
  1. Consulta precios en Travelpayouts (caché de búsquedas de Aviasales).
  2. Verifica en Google Flights, con tu equipaje incluido, repartiendo las
     consultas entre 4 ventanas de antelación y según la prioridad de cada ruta.
  3. Calcula el "precio normal" de cada ruta en cada ventana.
  4. Puntúa cada oferta (0-100) y solo te avisa de las mejores.
  5. Respeta tus horas de silencio (salvo error fares).

No necesitas tocar este archivo: todo se ajusta en config.yaml.
"""

import csv
import html
import json
import os
import random
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

# ------------------------------------------------------------------
# Archivos y constantes
# ------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
TP_FILE = DATA / "historial_radar.csv"
GF_FILE = DATA / "historial_google.csv"
STATE_FILE = DATA / "estado.json"
LATEST_FILE = DATA / "ultimos_precios.json"

MADRID = ZoneInfo("Europe/Madrid")
NOW_MADRID = datetime.now(MADRID)
TODAY = NOW_MADRID.date()
NOW_UTC = datetime.now(timezone.utc)
NOW_ISO = NOW_UTC.isoformat(timespec="seconds")

TP_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
FORCE_ALL = os.environ.get("RADAR_TODAS_VENTANAS", "").strip() == "1"

TP_PRICES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
TP_CITIES_URL = "https://api.travelpayouts.com/data/en/cities.json"

EUROPA = set(
    "AL AD AT BA BE BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LI LT LU "
    "LV MC MD ME MK MT NL NO PL PT RO RS SE SI SK SM TR UA VA XK".split()
)
LATAM = set(
    "MX GT BZ SV HN NI CR PA CU DO HT PR JM CO VE EC PE BO CL AR UY PY BR GY SR GF "
    "TT BS AW CW BB LC GP MQ".split()
)
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

TP_FIELDS = ["ts", "clave", "mes_salida", "n", "minimo", "mediana"]
GF_FIELDS = ["ts", "clave", "tipo", "salida", "regreso", "precio"]
CAL_FILE = DATA / "historial_calendario.csv"   # Resumen del calendario de precios de Google
CAL_FIELDS = ["ts", "clave", "ventana", "n", "minimo", "p10", "mediana", "mejor_salida", "mejor_regreso"]
CAL_MAX_DIAS = 305      # Google no da precios de calendario más allá de ~305 días
PARCIAL_DIR = DATA / "parcial"   # Resultados de los barridos en paralelo (no se guardan en el repositorio)
CAL_TRAMO = 61          # Días por consulta de calendario


def log(msg):
    print(f"[{datetime.now(MADRID):%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------
# Utilidades generales
# ------------------------------------------------------------------
AJUSTES_FILE = DATA / "ajustes_bot.json"   # Cambios hechos desde Telegram


def load_overrides():
    if AJUSTES_FILE.exists():
        try:
            return json.loads(AJUSTES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def apply_overrides(cfg, ov):
    """Aplica encima de config.yaml los cambios hechos desde Telegram."""
    removed = set(ov.get("quitadas", []))
    routes = [dict(r) for r in cfg.get("rutas", []) if r["id"] not in removed]
    ids = {r["id"] for r in routes}
    routes += [dict(r) for r in ov.get("rutas_extra", []) if r["id"] not in ids and r["id"] not in removed]
    for r in routes:
        if r["id"] in ov.get("objetivos", {}):
            r["precio_objetivo"] = ov["objetivos"][r["id"]]
        if r["id"] in ov.get("prioridades", {}):
            r["prioridad"] = ov["prioridades"][r["id"]]
    cfg["rutas"] = routes
    cfg["_pausa_hasta"] = ov.get("pausa_hasta")
    return cfg


def load_config(raw=False):
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg if raw else apply_overrides(cfg, load_overrides())


def is_reverse(route):
    """Ruta que no sale de Madrid (p. ej. Berlín → Madrid → Berlín)."""
    return bool(route.get("origen_aeropuerto"))


def route_label(route):
    origen = route.get("origen_nombre") or "Madrid"
    return f"{origen} → {route['nombre']}"


def route_short(route):
    """Nombre corto para listas: 'Berlín' o 'Desde Berlín'."""
    return f"Desde {route['origen_nombre']}" if is_reverse(route) else route["nombre"]


def paused_until(cfg):
    p = cfg.get("_pausa_hasta")
    try:
        if p and datetime.fromisoformat(p) > NOW_UTC:
            return datetime.fromisoformat(p)
    except Exception:
        pass
    return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def months_between(d1, d2):
    out, y, m = [], d1.year, d1.month
    while (y, m) <= (d2.year, d2.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def fdate(d):
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]}"


def euros(x):
    return f"{x:,.0f} €".replace(",", ".")


def append_csv(path, fields, rows):
    if not rows:
        return
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def read_csv(path, max_age_days):
    if not path.exists():
        return []
    limit = NOW_UTC - timedelta(days=max_age_days)
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                if datetime.fromisoformat(r["ts"]) >= limit:
                    rows.append(r)
            except Exception:
                continue
    return rows


def prune_csv(path, fields, keep_days):
    if not path.exists():
        return
    rows = read_csv(path, keep_days)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def window_of(dep, windows, ref=None):
    """Devuelve el id de la ventana a la que pertenece una fecha de salida."""
    lead = (dep - (ref or TODAY)).days
    for wid, w in windows.items():
        if w["desde_dias"] <= lead <= w["hasta_dias"]:
            return wid
    return None


def is_weekend_trip(dep, ret):
    """Viaje que aprovecha un fin de semana: sale jueves, viernes o sábado y vuelve
    domingo, lunes o martes (como máximo 5 días). Pide pocos días de vacaciones."""
    return dep.weekday() in (3, 4, 5) and ret.weekday() in (6, 0, 1) and 1 <= (ret - dep).days <= 5


def in_quiet_hours(cfg_a):
    s = cfg_a.get("silencio") or {}
    if "desde" not in s or "hasta" not in s:
        return False
    h = NOW_MADRID.hour
    a, b = int(s["desde"]), int(s["hasta"])
    return a <= h < b if a < b else (h >= a or h < b)


# ------------------------------------------------------------------
# Travelpayouts
# ------------------------------------------------------------------
def tp_request(params):
    if not TP_TOKEN:
        return []
    p = dict(params, currency="eur", sorting="price", limit=1000, one_way="false")
    headers = {"X-Access-Token": TP_TOKEN, "Accept-Encoding": "gzip, deflate"}
    for intento in range(3):
        try:
            r = requests.get(TP_PRICES_URL, params=p, headers=headers, timeout=40)
            if r.status_code == 429:
                time.sleep(5 * (intento + 1))
                continue
            if r.status_code >= 400:
                log(f"  Travelpayouts respondió {r.status_code}")
                return []
            js = r.json()
            if js.get("success") is False:
                log(f"  Travelpayouts: {js.get('error')}")
                return []
            return js.get("data") or []
        except Exception as e:
            log(f"  Travelpayouts falló ({type(e).__name__}), reintentando…")
            time.sleep(3)
    return []


def tp_offer(item):
    try:
        dep = date.fromisoformat(str(item["departure_at"])[:10])
        ret = date.fromisoformat(str(item["return_at"])[:10]) if item.get("return_at") else None
        price = float(item["price"])
    except Exception:
        return None
    if ret is None or ret <= dep or price <= 0:
        return None
    link = item.get("link")
    return {
        "dep": dep, "ret": ret, "stay": (ret - dep).days, "price": price,
        "transfers": item.get("transfers"), "airline": item.get("airline"),
        "dest": item.get("destination"),
        "dest_airport": item.get("destination_airport") or item.get("destination"),
        "duration_to": item.get("duration_to"),
        "link": f"https://www.aviasales.com{link}" if link else None,
    }


def stay_ok(o, profile, max_dur=None):
    lo, hi = profile["estancia_dias"]
    if not (lo <= o["stay"] <= hi):
        return False
    if max_dur and o.get("duration_to"):
        try:
            if float(o["duration_to"]) > max_dur:
                return False
        except (TypeError, ValueError):
            pass
    return True


def dedupe_best(offers):
    best = {}
    for o in offers:
        k = (o["dep"], o["ret"])
        if k not in best or o["price"] < best[k]["price"]:
            best[k] = o
    return sorted(best.values(), key=lambda o: o["price"])


def load_cities():
    try:
        r = requests.get(TP_CITIES_URL, timeout=60)
        r.raise_for_status()
        out = {}
        for c in r.json():
            name = (c.get("name_translations") or {}).get("es") or c.get("name") or c.get("code")
            out[c.get("code")] = {"country": c.get("country_code"), "name": name}
        return out
    except Exception as e:
        log(f"No se pudo cargar la lista de ciudades ({type(e).__name__})")
        return {}


# ------------------------------------------------------------------
# Google Flights
# ------------------------------------------------------------------
def _hhmm(t):
    try:
        return f"{int(t[0] or 0):02d}:{int(t[1] or 0):02d}"
    except Exception:
        return "?"


def flight_details(best):
    """Horarios del vuelo de ida para que puedas encontrarlo rápido."""
    try:
        segs = best.flights
        first, last = segs[0], segs[-1]
        plus = ""
        try:
            d1 = date(*first.departure.date)
            d2 = date(*last.arrival.date)
            if (d2 - d1).days > 0:
                plus = f" (+{(d2 - d1).days})"
        except Exception:
            pass
        via = [s.to_airport.code for s in segs[:-1]]
        return {
            "sale": f"{_hhmm(first.departure.time)} {first.from_airport.code}",
            "llega": f"{_hhmm(last.arrival.time)} {last.to_airport.code}{plus}",
            "via": via,
        }
    except Exception:
        return None


def google_price(cfg, orig, dest, dep, ret, profile, max_dur):
    try:
        from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights
    except Exception as e:
        log(f"  No se pudo cargar la librería de Google Flights ({type(e).__name__})")
        return {"ok": False, "price": None, "url": None}

    q = create_query(
        flights=[
            FlightQuery(date=dep.isoformat(), from_airport=orig, to_airport=dest,
                        max_duration_minutes=max_dur),
            FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=orig,
                        max_duration_minutes=max_dur),
        ],
        trip="round-trip", seat="economy", passengers=Passengers(adults=1),
        language="en-US", currency="EUR",
        carry_on_bags=int(profile.get("equipaje_mano", 0)),
        checked_bags=int(profile.get("equipaje_facturado", 0)),
        hide_separate_and_self_transfer=bool(cfg.get("ocultar_autotransbordo", True)),
    )
    url = q.url().replace("hl=en-US", "hl=es")
    try:
        res = get_flights(q)
    except FlightsNotFound:
        return {"ok": True, "price": None, "url": url}
    except Exception as e:
        log(f"  Google Flights falló ({type(e).__name__})")
        return {"ok": False, "price": None, "url": url}
    valid = [f for f in res if getattr(f, "price", 0) and f.price > 0]
    if not valid:
        return {"ok": True, "price": None, "url": url}
    best = min(valid, key=lambda f: f.price)
    return {
        "ok": True, "price": float(best.price), "airlines": list(best.airlines or []),
        "stops": max(len(best.flights) - 1, 0), "url": url, "vuelo": flight_details(best),
    }


# ------------------------------------------------------------------
# Calendario de precios de Google (todas las fechas de un tramo de una vez)
# ------------------------------------------------------------------
def calendar_chunk(orig, dest, start, end, stay, profile, max_dur):
    """Precio ida y vuelta (con tu equipaje) para cada fecha de salida entre start y end,
    con una estancia fija de `stay` días. Devuelve lista de (salida, regreso, precio)."""
    from fli.core import build_date_search_segments
    from fli.models import (Airport, BagsFilter, DateSearchFilters, MaxStops,
                            PassengerInfo, SeatType)
    from fli.search import SearchDates

    segs, trip = build_date_search_segments(Airport[orig], Airport[dest], start.isoformat(),
                                            trip_duration=stay, is_round_trip=True)
    bags = None
    if profile.get("equipaje_mano") or profile.get("equipaje_facturado"):
        bags = BagsFilter(checked_bags=int(profile.get("equipaje_facturado", 0)),
                          carry_on=bool(profile.get("equipaje_mano", 0)))
    f = DateSearchFilters(trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segs,
                          stops=MaxStops.ANY, seat_type=SeatType.ECONOMY, max_duration=max_dur,
                          bags=bags, from_date=start.isoformat(), to_date=end.isoformat(), duration=stay)
    res = SearchDates().search(f, currency="EUR", language="es", country="ES") or []
    out = []
    for dp in res:
        if dp.currency and dp.currency.upper() != "EUR":
            continue
        try:
            d1 = dp.date[0].date()
            d2 = dp.date[1].date() if len(dp.date) > 1 else d1 + timedelta(days=stay)
        except Exception:
            continue
        if start <= d1 <= end and dp.price and dp.price > 0:
            out.append((d1, d2, float(dp.price)))
    return out


def due_windows(cfg):
    """Ventanas que tocan en esta franja de 3 horas."""
    slot = NOW_UTC.hour // 3
    return [wid for wid, w in cfg["ventanas"].items()
            if FORCE_ALL or slot % max(int(w.get("cada_horas", 3)) // 3, 1) == 0]


def stays_for_run(profile, run_n):
    """Estancias a barrer: las fijas en cada ejecución y, además, una rotativa."""
    lo, hi = profile["estancia_dias"]
    base = profile.get("estancias_calendario") or list(range(lo, hi + 1))
    rot = profile.get("estancias_rotativas") or []
    return sorted(set(base) | ({rot[run_n % len(rot)]} if rot else set()))


def max_duration(route, profile):
    return (int(route["duracion_directo_min"] * profile.get("factor_duracion", 3))
            if route.get("duracion_directo_min") else None)


def calendar_tasks(cfg, due, run_n):
    """Lista ordenada de consultas de calendario: primero rutas con objetivo, luego
    prioritarias y después secundarias. Cada consulta = ruta + estancia + tramo de 61 días."""
    windows, profiles = cfg["ventanas"], cfg["perfiles"]
    routes = cfg.get("rutas", [])
    origin_ap = cfg["origen"]["aeropuerto"]
    ordered = sorted(routes, key=lambda r: (0 if r.get("precio_objetivo") else 1,
                                            0 if r.get("prioridad", "media") == "alta" else 1,
                                            (routes.index(r) - run_n) % max(len(routes), 1)))
    tasks = []
    for route in ordered:
        profile = profiles[route["perfil"]]
        wids = list(windows) if route.get("precio_objetivo") else list(due)
        start = TODAY + timedelta(days=min(windows[w]["desde_dias"] for w in wids))
        end = min(TODAY + timedelta(days=max(windows[w]["hasta_dias"] for w in wids)),
                  TODAY + timedelta(days=CAL_MAX_DIAS))
        for stay in stays_for_run(profile, run_n):
            cur = start
            while cur <= end:
                chunk_end = min(cur + timedelta(days=CAL_TRAMO - 1), end)
                tasks.append({"ruta": route["id"], "orig": route.get("origen_aeropuerto") or origin_ap,
                              "dest": route["aeropuerto"], "desde": cur, "hasta": chunk_end,
                              "estancia": stay, "perfil": route["perfil"],
                              "max_dur": max_duration(route, profile)})
                cur = chunk_end + timedelta(days=1)
    return tasks


def run_calendar_tasks(cfg, tasks, budget):
    """Ejecuta consultas de calendario. Se detiene si Google falla 6 veces seguidas."""
    results, calls, fails, consecutive = [], 0, 0, 0
    for t in tasks[:budget]:
        if consecutive >= 6:
            log("  El calendario de Google no responde: detengo el barrido")
            break
        calls += 1
        try:
            for d1, d2, p in calendar_chunk(t["orig"], t["dest"], t["desde"], t["hasta"], t["estancia"],
                                            cfg["perfiles"][t["perfil"]], t["max_dur"]):
                results.append([t["ruta"], d1.isoformat(), d2.isoformat(), p])
            consecutive = 0
        except Exception as e:
            fails += 1
            consecutive += 1
            log(f"  Calendario falló ({type(e).__name__}) en {t['ruta']}")
        time.sleep(random.uniform(1.0, 2.5))
    return {"resultados": results, "consultas": calls, "fallos": fails,
            "planificadas": len(tasks), "truncado": len(tasks) > budget}


def run_shard(part, parts):
    """Modo trabajo en paralelo: barre su parte de las consultas y guarda el resultado."""
    cfg = load_config()
    state = load_state()
    run_n = int(state.get("ejecuciones", 0))
    due = due_windows(cfg)
    ccfg = cfg.get("calendario", {})
    budget = int(state.get("calendario_presupuesto", ccfg.get("presupuesto_inicial_por_parte", 150)))
    tasks = calendar_tasks(cfg, due, run_n)[part::parts]
    log(f"Parte {part + 1}/{parts}: {len(tasks)} consultas planificadas, presupuesto {budget}")
    out = run_calendar_tasks(cfg, tasks, budget)
    out.update({"ts": NOW_ISO, "due": due, "parte": part})
    PARCIAL_DIR.mkdir(parents=True, exist_ok=True)
    (PARCIAL_DIR / f"barrido_{part}.json").write_text(json.dumps(out), encoding="utf-8")
    log(f"Parte {part + 1}: {out['consultas']} consultas, {out['fallos']} fallos, "
        f"{len(out['resultados'])} precios")


def load_shards():
    """Junta los resultados de los trabajos en paralelo (si los hay)."""
    if not PARCIAL_DIR.exists():
        return None
    parts = []
    for f in sorted(PARCIAL_DIR.glob("barrido_*.json")):
        try:
            parts.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return parts or None


def cal_stats(cal_hist, key, wid, cfg_a):
    """Lo habitual en esa ruta y ventana según los barridos de calendario anteriores:
    mediana = precio medio de las fechas · minimo = lo más barato que suele haber."""
    rows = [r for r in cal_hist if r["clave"] == key and r["ventana"] == wid]
    if len(rows) < int(cfg_a.get("min_barridos", 8)):
        return None
    days = {r["ts"][:10] for r in rows}
    if len(days) < int(cfg_a.get("min_dias_calendario", 4)):
        return None
    try:
        return {"mediana": statistics.median(float(r["mediana"]) for r in rows),
                "minimo": statistics.median(float(r["minimo"]) for r in rows),
                "minimo_historico": min(float(r["minimo"]) for r in rows), "n": len(rows)}
    except Exception:
        return None


# ------------------------------------------------------------------
# Precio normal y puntuación
# ------------------------------------------------------------------
TOLERANCIA_DEFECTO = {"ultimo_minuto": 3, "corto": 10, "medio": 25, "largo": 45}


def build_gf_index(gf_hist, windows):
    """Agrupa los precios de Google por (ruta, ventana): antelación, fechas y momento de la consulta."""
    idx = {}
    for r in gf_hist:
        if r.get("tipo") not in ("muestra", "candidato", "reconfirmacion") or not r.get("precio"):
            continue
        try:
            dep = date.fromisoformat(r["salida"])
            ts = datetime.fromisoformat(r["ts"])
            ref = ts.astimezone(MADRID).date()
            price = float(r["precio"])
        except Exception:
            continue
        wid = window_of(dep, windows, ref)
        if wid:
            idx.setdefault((r["clave"], wid), []).append(
                {"lead": (dep - ref).days, "par": (r["salida"], r["regreso"]), "ts": ts, "precio": price})
    return idx


def market_stats(gf_index, key, wid, windows, cfg_a, lead=None, exclude=None, recent_days=7):
    """Precio de mercado de una ruta en una ventana.
    Cada combinación de fechas cuenta UNA sola vez (con su precio más reciente), para que
    consultar muchas veces el mismo vuelo no distorsione el "precio normal".
    Devuelve (mediana, nº de fechas distintas, mejor precio reciente de OTRAS fechas)."""
    tol = windows[wid].get("tolerancia_dias", TOLERANCIA_DEFECTO.get(wid, 15))
    latest = {}
    for e in gf_index.get((key, wid), []):
        if lead is not None and abs(e["lead"] - lead) > tol:
            continue
        if exclude and e["par"] == exclude:
            continue
        if e["par"] not in latest or e["ts"] > latest[e["par"]]["ts"]:
            latest[e["par"]] = e
    if not latest:
        return None, 0, None
    prices = [e["precio"] for e in latest.values()]
    median = statistics.median(prices) if len(prices) >= cfg_a["min_observaciones"] else None
    cutoff = NOW_UTC - timedelta(days=recent_days)
    recent = [e["precio"] for e in latest.values() if e["ts"] >= cutoff]
    return median, len(prices), (min(recent) if recent else None)


def window_reference(gf_index, key, wid, windows, cfg_a):
    """Precio habitual de toda la ventana (para el resumen y el bot)."""
    median, _, _ = market_stats(gf_index, key, wid, windows, cfg_a)
    return median


def tp_baseline(tp_hist, key, month, cfg_a):
    """Mínimo habitual del radar para esa ruta, ventana y mes de salida."""
    vals = [float(r["minimo"]) for r in tp_hist if r["clave"] == key and r["mes_salida"] == month]
    if len(vals) >= cfg_a["min_observaciones"]:
        return statistics.median(vals)
    return None


def is_preferred(airlines, preferred):
    names = " | ".join(airlines or []).lower()
    return any(p.lower() in names for p in preferred)


def score(drop, stops, preferred):
    """0-100: 70 por descuento, 20 por comodidad, 10 por aerolínea preferida."""
    s = 70 * max(0.0, min(drop / 0.45, 1.0))
    s += {0: 20, 1: 12}.get(stops, 4) if stops is not None else 8
    s += 10 if preferred else 0
    return round(s)


# ------------------------------------------------------------------
# Telegram
# ------------------------------------------------------------------
def telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram no configurado; mensaje no enviado:\n" + text)
        return False
    ok = True
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900 and cur:
            chunks.append(cur)
            cur = ""
        cur += (("\n" if cur else "") + line)
    chunks.append(cur)
    for chunk in chunks:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=30,
            )
            if r.status_code != 200:
                log(f"Telegram respondió {r.status_code}: {r.text[:200]}")
                ok = False
        except Exception as e:
            log(f"Telegram falló ({type(e).__name__})")
            ok = False
    return ok


def bag_text(profile):
    if profile.get("equipaje_facturado"):
        return "con maleta de 23 kg"
    if profile.get("equipaje_mano"):
        return "con maleta de cabina"
    return "sin equipaje"


def build_message(a, cfg):
    """Construye el texto de una alerta a partir de sus datos."""
    profile = cfg["perfiles"][a["perfil"]]
    dep, ret = date.fromisoformat(a["salida"]), date.fromisoformat(a["regreso"])
    titles = {
        "error_fare": "🚨 <b>POSIBLE ERROR FARE</b>",
        "chollo": "🔥 <b>CHOLLO</b>",
        "objetivo": "🎯 <b>PRECIO OBJETIVO</b>",
    }
    head = titles[a["tipo"]]
    if a.get("ventana_nombre"):
        head += f" · {a['ventana_nombre']}"
    lines = [head, f"<b>{html.escape(a.get('ruta_txt') or 'Madrid → ' + a['nombre'])}</b>", ""]
    if a.get("verificado", True):
        lines.append(f"💶 <b>{euros(a['precio'])}</b> ida y vuelta ({bag_text(profile)})")
    else:
        lines.append(f"💶 <b>{euros(a['precio'])}</b> ida y vuelta (⚠️ equipaje no incluido, verifícalo)")
    if a.get("caida") is not None and a.get("normal"):
        lines.append(f"📉 {a['caida']:.0%} por debajo de lo normal (~{euros(a['normal'])})")
    tag = " · incluye fin de semana" if is_weekend_trip(dep, ret) else ""
    lines.append(f"📅 {fdate(dep)} → {fdate(ret)} ({(ret - dep).days} días{tag})")
    v = a.get("vuelo")
    if v:
        via = f" · escala en {', '.join(v['via'])}" if v.get("via") else " · directo"
        lines.append(f"🛫 Ida: sale {v['sale']} → llega {v['llega']}{via}")
    parts = []
    if a.get("aerolineas"):
        star = " ⭐" if a.get("preferida") else ""
        parts.append(html.escape(", ".join(a["aerolineas"][:2])) + star)
    if a.get("escalas") is not None and not v:
        parts.append("directo" if a["escalas"] == 0 else f"{a['escalas']} escala(s)")
    if parts:
        lines.append("✈️ " + " · ".join(parts))
    if a.get("puntuacion") is not None:
        lines.append(f"⭐ Puntuación: <b>{a['puntuacion']}/100</b>")
    if a.get("nota"):
        lines.append(f"ℹ️ {html.escape(a['nota'])}")
    links = []
    if a.get("url"):
        links.append(f'<a href="{html.escape(a["url"])}">Ver en Google Flights</a>')
    if a.get("tp_link"):
        links.append(f'<a href="{html.escape(a["tp_link"])}">Aviasales</a>')
    if links:
        lines.append("🔗 " + " | ".join(links))
    if a["tipo"] == "error_fare":
        lines.append("\n⏱ Estos precios suelen durar pocas horas.")
    return "\n".join(lines)


def _learned_text(cfg, gf_index, cal_hist=None):
    windows, cfg_a, routes = cfg["ventanas"], cfg["alertas"], cfg.get("rutas", [])
    learned = sum(1 for r in routes for wid in windows
                  if (cal_hist and cal_stats(cal_hist, r["id"], wid, cfg_a))
                  or window_reference(gf_index, r["id"], wid, windows, cfg_a))
    return f"🧠 Precio normal aprendido: {learned}/{len(routes) * len(windows)} combinaciones ruta-ventana"


def summary_messages(cfg, extra_rows=None, gf_index=None, titulo=None, solo_ventana=None, cal_extra=None):
    """Resumen de las últimas 24 h: un mensaje por ventana de antelación."""
    windows = cfg["ventanas"]
    cfg_a = cfg["alertas"]
    routes = cfg.get("rutas", [])
    if gf_index is None:
        gf_index = build_gf_index(read_csv(GF_FILE, cfg_a["dias_historial"]), windows)
    recent = read_csv(GF_FILE, 1) + (extra_rows or [])
    cal_recent = read_csv(CAL_FILE, 1) + (cal_extra or [])
    cal_hist = read_csv(CAL_FILE, int(cfg_a.get("dias_historial_calendario", 30)))
    titulo = titulo or "☀️ <b>Resumen diario</b>"
    wids = [solo_ventana] if solo_ventana else list(windows)
    messages = []
    for n, wid in enumerate(wids, 1):
        w = windows[wid]
        lines = [f"{titulo} · {fdate(TODAY)}",
                 f"<b>{n}/{len(wids)} · {w['nombre']}</b> (salidas en {w['desde_dias']}–{w['hasta_dias']} días)"
                 if len(wids) > 1 else
                 f"<b>{w['nombre']}</b> (salidas en {w['desde_dias']}–{w['hasta_dias']} días)"]
        missing = []
        for tier_name, in_group in (("Prioritarias", lambda r: not is_reverse(r) and r.get("prioridad", "media") == "alta"),
                                 ("Secundarias", lambda r: not is_reverse(r) and r.get("prioridad", "media") == "media"),
                                 ("Ida y vuelta desde otras ciudades", is_reverse)):
            tier_lines = []
            for route in [r for r in routes if in_group(r)]:
                # 1º el calendario (todas las fechas); si no hay, las consultas sueltas
                crow = [r for r in cal_recent if r["clave"] == route["id"] and r["ventana"] == wid]
                if crow:
                    b = min(crow, key=lambda r: float(r["minimo"]))
                    price = float(b["minimo"])
                    d1, d2 = date.fromisoformat(b["mejor_salida"]), date.fromisoformat(b["mejor_regreso"])
                    st = cal_stats(cal_hist, route["id"], wid, cfg_a)
                    typ = st["minimo"] if st else None
                else:
                    rows = [r for r in recent if r["clave"] == route["id"] and r["precio"]
                            and window_of(date.fromisoformat(r["salida"]), windows) == wid]
                    if not rows:
                        missing.append(route_short(route))
                        continue
                    b = min(rows, key=lambda r: float(r["precio"]))
                    price = float(b["precio"])
                    d1, d2 = date.fromisoformat(b["salida"]), date.fromisoformat(b["regreso"])
                    typ = window_reference(gf_index, route["id"], wid, windows, cfg_a)
                ref = ""
                if typ:
                    diff = price / typ - 1
                    arrow = "🟢" if diff <= -0.10 else "🔴" if diff >= 0.10 else "⚪"
                    ref = f" {arrow} suele ~{euros(typ)}"
                tgt = f" 🎯{euros(float(route['precio_objetivo']))}" if route.get("precio_objetivo") else ""
                tier_lines.append(f"• {html.escape(route_label(route) if is_reverse(route) else route['nombre'])}: <b>{euros(price)}</b> "
                                  f"({fdate(d1)} → {fdate(d2)}, {(d2 - d1).days} d){ref}{tgt}")
            if tier_lines:
                lines.append(f"\n<b>{tier_name}</b>")
                lines.extend(tier_lines)
        if missing:
            lines.append(f"\n<i>Sin datos hoy: {html.escape(', '.join(missing))}</i>")
        messages.append("\n".join(lines))
    footer = [_learned_text(cfg, gf_index, cal_hist)]
    p = paused_until(cfg)
    if p:
        footer.append(f"⏸ Alertas en pausa hasta el {fdate(p.astimezone(MADRID).date())}")
    footer.append("Precios más bajos vistos en las últimas 24 h, con tu equipaje incluido. "
                  "🟢 barato · ⚪ normal · 🔴 caro, frente a lo más barato que suele haber en esa ventana.")
    messages[-1] += "\n\n" + "\n".join(footer)
    return messages


def summary_text(cfg, extra_rows=None, gf_index=None, titulo=None):
    """Versión en un solo mensaje (mejor precio de cualquier ventana)."""
    windows = cfg["ventanas"]
    cfg_a = cfg["alertas"]
    routes = cfg.get("rutas", [])
    if gf_index is None:
        gf_index = build_gf_index(read_csv(GF_FILE, cfg_a["dias_historial"]), windows)
    recent = read_csv(GF_FILE, 1) + (extra_rows or [])
    lines = [titulo or f"☀️ <b>Resumen diario</b> · {fdate(TODAY)}"]
    for tier_name, in_group in (("Prioritarias", lambda r: not is_reverse(r) and r.get("prioridad", "media") == "alta"),
                                 ("Secundarias", lambda r: not is_reverse(r) and r.get("prioridad", "media") == "media"),
                                 ("Ida y vuelta desde otras ciudades", is_reverse)):
        tier_routes = [r for r in routes if in_group(r)]
        if not tier_routes:
            continue
        lines.append(f"\n<b>{tier_name}</b>")
        for route in tier_routes:
            rows = [r for r in recent if r["clave"] == route["id"] and r["precio"]]
            if not rows:
                lines.append(f"• {html.escape(route_short(route))}: sin datos hoy")
                continue
            b = min(rows, key=lambda r: float(r["precio"]))
            d1, d2 = date.fromisoformat(b["salida"]), date.fromisoformat(b["regreso"])
            wid = window_of(d1, windows)
            wtxt = f" · {windows[wid]['nombre']}" if wid else ""
            tgt = f" 🎯{euros(float(route['precio_objetivo']))}" if route.get("precio_objetivo") else ""
            lines.append(f"• {html.escape(route_label(route) if is_reverse(route) else route['nombre'])}: <b>{euros(float(b['precio']))}</b> "
                         f"({fdate(d1)} → {fdate(d2)}{wtxt}){tgt}")
    lines.append("\n" + _learned_text(cfg, gf_index))
    p = paused_until(cfg)
    if p:
        lines.append(f"⏸ Alertas en pausa hasta el {fdate(p.astimezone(MADRID).date())}")
    lines.append("Precios más bajos vistos en las últimas 24 h, con tu equipaje incluido.")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Programa principal
# ------------------------------------------------------------------
def main():
    DATA.mkdir(exist_ok=True)
    cfg = load_config()
    cfg_a = cfg["alertas"]
    windows = cfg["ventanas"]
    profiles = cfg["perfiles"]
    preferred = cfg.get("aerolineas_preferidas", [])
    state = load_state()
    run_n = int(state.get("ejecuciones", 0))
    state["ejecuciones"] = run_n + 1
    sent = state.setdefault("alertas_enviadas", {})
    queue = state.setdefault("cola_silencio", [])
    for k in list(sent):
        try:
            if date.fromisoformat(k.split("|")[1]) < TODAY:
                del sent[k]
        except Exception:
            del sent[k]

    if not state.get("inicializado"):
        if telegram("✅ <b>Radar de vuelos conectado</b>\n\nRevisaré precios cada 3 horas y te avisaré "
                    "aquí cuando encuentre un chollo."):
            state["inicializado"] = True
    if not state.get("v2_avisado"):
        if telegram("🆕 <b>Radar actualizado a la versión 2</b>\n\n"
                    "• 4 ventanas: último minuto, corto, medio y largo plazo\n"
                    "• 10 rutas prioritarias y 7 secundarias\n"
                    "• Puntuación 0-100: solo te aviso de lo mejor\n"
                    "• Silencio de 1:00 a 7:00 (salvo error fares)"):
            state["v2_avisado"] = True

    quiet = in_quiet_hours(cfg_a)
    shards = load_shards()
    due = due_windows(cfg)
    if shards:  # usar las mismas ventanas que barrieron los trabajos en paralelo
        due = [w for w in windows if any(w in p.get("due", []) for p in shards)] or due
    log(f"Ventanas en esta ejecución: {', '.join(windows[w]['nombre'] for w in due)}"
        + (" · horas de silencio" if quiet else ""))

    origin_city = cfg["origen"]["codigo_ciudad"]
    origin_ap = cfg["origen"]["aeropuerto"]
    gcfg = cfg.get("google", {})
    google_on = bool(gcfg.get("activado", True))
    google_budget = int(gcfg.get("max_consultas_por_ejecucion", 90))
    stats = {"google_ok": 0, "google_fail": 0, "alertas": 0, "en_cola": 0}

    tp_hist = read_csv(TP_FILE, cfg_a["dias_historial"])
    gf_hist = read_csv(GF_FILE, cfg_a["dias_historial"])
    gf_index = build_gf_index(gf_hist, windows)
    cal_hist = read_csv(CAL_FILE, int(cfg_a.get("dias_historial_calendario", 30)))
    cal_rows = []
    tp_rows, gf_rows = [], []
    latest = {"actualizado": NOW_ISO, "rutas": {}, "exploracion": {}}
    outgoing = []  # alertas listas para enviar

    min_lead = min(windows[w]["desde_dias"] for w in due)
    max_lead = max(windows[w]["hasta_dias"] for w in due)
    months = months_between(TODAY + timedelta(days=min_lead), TODAY + timedelta(days=max_lead))
    all_months = months_between(TODAY + timedelta(days=min(w["desde_dias"] for w in windows.values())),
                                TODAY + timedelta(days=max(w["hasta_dias"] for w in windows.values())))

    # ---------- utilidades internas ----------
    paused = paused_until(cfg)

    def register(alert):
        if paused:
            return
        k = f"{alert['clave']}|{alert['salida']}|{alert['regreso']}"
        prev = sent.get(k)
        if prev and alert["precio"] > prev["precio"] * (1 - cfg_a["realertar_si_baja"]):
            return
        if quiet and alert["tipo"] != "error_fare":
            if not any(q["clave"] == alert["clave"] and q["salida"] == alert["salida"]
                       and q["regreso"] == alert["regreso"] for q in queue):
                alert["en_cola_desde"] = NOW_ISO
                queue.append(alert)
                stats["en_cola"] += 1
            return
        sent[k] = {"precio": alert["precio"], "fecha": NOW_ISO}
        outgoing.append(alert)

    def run_google(key, dest_ap, dep, ret, profile, max_dur, tipo, orig=None):
        nonlocal google_budget
        if not google_on or google_budget <= 0:
            return None
        google_budget -= 1
        res = google_price(cfg, orig or origin_ap, dest_ap, dep, ret, profile, max_dur)
        stats["google_ok" if res["ok"] else "google_fail"] += 1
        if res["ok"] and res["price"]:
            gf_rows.append({"ts": NOW_ISO, "clave": key, "tipo": tipo, "salida": dep.isoformat(),
                            "regreso": ret.isoformat(), "precio": res["price"]})
        time.sleep(random.uniform(2, 4))
        return res

    def evaluate(route, wid, dep, ret, res, tp_o, tipo):
        """Decide si una oferta verificada merece alerta.
        Para ser chollo o error fare, el precio debe cumplir DOS condiciones:
          1. Estar muy por debajo del precio de mercado (fechas distintas, antelación parecida).
          2. Ser igual o mejor que cualquier otro precio visto en esa ruta y ventana en los
             últimos 7 días. Si hace poco vimos algo más barato, no es un chollo."""
        key = route["id"]
        profile = profiles[route["perfil"]]
        price = res["price"]
        month = dep.strftime("%Y-%m")
        pref = is_preferred(res.get("airlines"), preferred)
        weekend_ok = (not profile.get("fin_de_semana")) or is_weekend_trip(dep, ret)
        min_score = cfg_a["puntuacion_minima"] + (0 if weekend_ok else cfg_a["exigencia_extra_entre_semana"])
        base, n_pairs, recent_best = market_stats(
            gf_index, key, wid, windows, cfg_a, lead=(dep - TODAY).days,
            exclude=(dep.isoformat(), ret.isoformat()))
        is_new_low = recent_best is None or price <= recent_best
        kind = drop = pts = None
        note = normal = None
        if base:
            d = 1 - price / base
            pts = score(max(d, 0), res.get("stops"), pref)
            if d >= cfg_a["umbral_error_fare"] and (recent_best is None or price <= recent_best * 0.85):
                kind, drop, normal = "error_fare", d, base
            elif d >= cfg_a["umbral_chollo"] and is_new_low and pts >= min_score:
                kind, drop, normal = "chollo", d, base
        elif tp_o and is_new_low and price <= tp_o["price"] * 1.25:
            # Aún sin precio de mercado: usar la caída del radar, pero solo si Google
            # confirma un precio parecido al del radar y es lo más barato visto recientemente
            tb = tp_baseline(tp_hist, f"{key}@{wid}", month, cfg_a)
            if tb:
                d = 1 - tp_o["price"] / tb
                pts = score(max(d, 0), res.get("stops"), pref)
                if d >= cfg_a["umbral_chollo"] and pts >= min_score:
                    kind = "chollo"
                    note = f"El radar detectó una caída del {d:.0%}; precio confirmado en Google Flights"
        target = route.get("precio_objetivo")
        if not kind and target and price <= float(target):
            kind = "objetivo"
            note = f"Tu objetivo era {euros(float(target))}"
        if not kind:
            return
        if recent_best and kind in ("chollo", "error_fare"):
            extra = f"Lo más barato visto esta semana en esta ventana era {euros(recent_best)}"
            note = f"{note}. {extra}" if note else extra
        register({
            "tipo": kind, "clave": key, "nombre": route["nombre"], "perfil": route["perfil"],
            "ventana": wid, "ventana_nombre": windows[wid]["nombre"],
            "salida": dep.isoformat(), "regreso": ret.isoformat(), "precio": price,
            "normal": normal, "caida": drop, "puntuacion": pts, "aerolineas": res.get("airlines"),
            "preferida": pref, "escalas": res.get("stops"), "vuelo": res.get("vuelo"),
            "url": res.get("url"), "tp_link": tp_o["link"] if tp_o else None, "nota": note,
            "aeropuerto": route["aeropuerto"], "origen_aeropuerto": route.get("origen_aeropuerto"),
            "ruta_txt": route_label(route),
            "objetivo": float(target) if target else None,
        })

    # ---------- 1. Cola de silencio: reconfirmar y enviar a las 7:00 ----------
    if not quiet and queue:
        log(f"Revisando {len(queue)} alertas guardadas durante el silencio")
        pending, queue[:] = list(queue), []
        for a in pending:
            try:
                age = NOW_UTC - datetime.fromisoformat(a["en_cola_desde"])
                dep = date.fromisoformat(a["salida"])
            except Exception:
                continue
            if age > timedelta(hours=12) or dep <= TODAY:
                continue
            profile = profiles[a["perfil"]]
            route = next((r for r in cfg["rutas"] if r["id"] == a["clave"]), None)
            max_dur = (int(route["duracion_directo_min"] * profile.get("factor_duracion", 3))
                       if route and route.get("duracion_directo_min") else None)
            res = run_google(a["clave"], a["aeropuerto"], dep, date.fromisoformat(a["regreso"]),
                             profile, max_dur, "reconfirmacion", orig=a.get("origen_aeropuerto"))
            if res and res["ok"] and res["price"] and res["price"] <= a["precio"] * 1.05:
                a.update(precio=res["price"], url=res["url"], vuelo=res.get("vuelo") or a.get("vuelo"))
                a["nota"] = ((a.get("nota") + ". ") if a.get("nota") else "") + \
                    "Detectado de madrugada y reconfirmado ahora"
                register(a)
            elif res is None or not res["ok"]:
                register(a)  # Google no disponible: enviar sin reconfirmar

    # ---------- 2. Radar de rutas fijas por ventana ----------
    routes = cfg.get("rutas", [])
    tp_by_route = {}
    for route in routes:
        profile = profiles[route["perfil"]]
        max_dur = (int(route["duracion_directo_min"] * profile.get("factor_duracion", 3))
                   if route.get("duracion_directo_min") else None)
        has_target = bool(route.get("precio_objetivo"))
        route_windows = list(windows) if has_target else due  # objetivos: siempre las 4 ventanas
        offers = []
        for m in (all_months if has_target else months):
            for item in tp_request({"origin": route.get("origen_ciudad") or origin_city,
                                    "destination": route["codigo_ciudad"],
                                    "departure_at": m}):
                o = tp_offer(item)
                if o and stay_ok(o, profile, max_dur):
                    o["ventana"] = window_of(o["dep"], windows)
                    if o["ventana"] in route_windows:
                        offers.append(o)
            time.sleep(0.25)
        by_w = {}
        for o in dedupe_best(offers):
            by_w.setdefault(o["ventana"], []).append(o)
        tp_by_route[route["id"]] = by_w
        for wid, lst in by_w.items():
            per_month = {}
            for o in lst:
                per_month.setdefault(o["dep"].strftime("%Y-%m"), []).append(o["price"])
            for m, prices in per_month.items():
                tp_rows.append({"ts": NOW_ISO, "clave": f"{route['id']}@{wid}", "mes_salida": m,
                                "n": len(prices), "minimo": round(min(prices), 2),
                                "mediana": round(statistics.median(prices), 2)})
        log(f"Radar {route['nombre']}: " + ", ".join(
            f"{windows[w]['nombre']} {len(l)}" for w, l in by_w.items()) if by_w
            else f"Radar {route['nombre']}: sin datos en caché")

    # ---------- 2b. Barrido de calendario de Google ----------
    # Para cada ruta y estancia, una consulta devuelve el precio (con tu equipaje) de TODAS
    # las fechas de salida en un tramo de 61 días. Normalmente lo hacen 4 trabajos en paralelo
    # (cada uno en una máquina distinta) y aquí solo se juntan los resultados.
    ccfg = cfg.get("calendario", {})
    cal_on = bool(ccfg.get("activado", True))
    cal_done = set()          # (ruta, ventana) cubiertas por el calendario en esta ejecución
    cal_best = {}             # (ruta, ventana) -> lista de (salida, regreso, precio)
    scan = None
    if cal_on and shards:
        scan = {"resultados": [x for p in shards for x in p.get("resultados", [])],
                "consultas": sum(p.get("consultas", 0) for p in shards),
                "fallos": sum(p.get("fallos", 0) for p in shards),
                "planificadas": sum(p.get("planificadas", 0) for p in shards),
                "truncado": any(p.get("truncado") for p in shards), "partes": len(shards)}
        # Escalado automático: más consultas mientras Google responda bien, menos si falla
        budget = int(state.get("calendario_presupuesto", ccfg.get("presupuesto_inicial_por_parte", 150)))
        rate = scan["fallos"] / scan["consultas"] if scan["consultas"] else 0
        if rate > 0.10:
            budget = max(int(ccfg.get("presupuesto_minimo_por_parte", 60)), int(budget * 0.75))
        elif scan["truncado"] and rate < 0.02:
            budget = min(int(ccfg.get("presupuesto_maximo_por_parte", 350)), int(budget * 1.10) + 5)
        state["calendario_presupuesto"] = budget
    elif cal_on:  # sin trabajos en paralelo: barrido en este mismo proceso
        tasks = calendar_tasks(cfg, due, run_n)
        scan = run_calendar_tasks(cfg, tasks, int(ccfg.get("max_consultas_por_ejecucion", 200)))
        scan["partes"] = 1
    if scan:
        stats["cal"] = scan["consultas"]
        stats["cal_fallos"] = scan["fallos"]
        found = {}
        for rid, s1, s2, p in scan["resultados"]:
            d1, d2 = date.fromisoformat(s1), date.fromisoformat(s2)
            k = (rid, d1, d2)
            if k not in found or p < found[k]:
                found[k] = p
        route_by_id = {r["id"]: r for r in routes}
        for (rid, d1, d2), p in found.items():
            route = route_by_id.get(rid)
            if not route or d1 <= TODAY:
                continue
            wid = window_of(d1, windows)
            if wid and (wid in due or route.get("precio_objetivo")):
                cal_best.setdefault((rid, wid), []).append((d1, d2, p))
        for (rid, wid), lst in cal_best.items():
            cal_done.add((rid, wid))
            lst.sort(key=lambda x: x[2])
            prices = [p for _, _, p in lst]
            cal_rows.append({"ts": NOW_ISO, "clave": rid, "ventana": wid, "n": len(prices),
                             "minimo": prices[0], "p10": prices[int(len(prices) * 0.1)],
                             "mediana": statistics.median(prices),
                             "mejor_salida": lst[0][0].isoformat(), "mejor_regreso": lst[0][1].isoformat()})
        log(f"Calendario: {scan['consultas']} consultas en {scan['partes']} parte(s), {scan['fallos']} fallos, "
            f"{len(found)} combinaciones de fechas con precio"
            + (" · faltaron consultas por presupuesto" if scan["truncado"] else ""))

    def cal_evaluate(route, wid, d1, d2, cal_price, st, weekend_choice):
        """Evalúa la mejor fecha de una ventana. El precio se verifica en Google antes de avisar."""
        profile = profiles[route["perfil"]]
        target = route.get("precio_objetivo")
        mejora = float(cfg_a.get("mejora_sobre_minimo_historico", 0.05))
        kind = None
        if st:
            # Chollo: muy por debajo del precio medio Y más barato que lo más barato visto
            # en los últimos 30 días. Error fare: mitad del precio medio y muy por debajo del mínimo.
            d = 1 - cal_price / st["mediana"]
            if d >= cfg_a["umbral_error_fare"] and cal_price <= st["minimo_historico"] * 0.80:
                kind = "error_fare"
            elif d >= cfg_a["umbral_chollo"] and cal_price <= st["minimo_historico"] * (1 - mejora):
                kind = "chollo"
        if not kind and target and cal_price <= float(target):
            kind = "objetivo"
        if not kind:
            return
        max_dur = (int(route["duracion_directo_min"] * profile.get("factor_duracion", 3))
                   if route.get("duracion_directo_min") else None)
        res = run_google(route["id"], route["aeropuerto"], d1, d2, profile, max_dur, "verificacion",
                         orig=route.get("origen_aeropuerto"))
        price, note = cal_price, None
        if res and res["ok"] and res["price"]:
            if res["price"] > cal_price * 1.10:
                return  # el detalle no confirma el precio del calendario
            price = res["price"]
        elif res and res["ok"]:
            return      # Google ya no encuentra ese vuelo
        else:
            res = {}
            note = "Precio del calendario de Google con tu equipaje; no se pudo abrir el detalle"
        pref = is_preferred(res.get("airlines"), preferred)
        drop = pts = normal = None
        if kind in ("chollo", "error_fare"):
            drop = 1 - price / st["mediana"]
            pts = score(max(drop, 0), res.get("stops"), pref)
            weekend_ok = (not profile.get("fin_de_semana")) or weekend_choice
            min_score = cfg_a["puntuacion_minima"] + (0 if weekend_ok else cfg_a["exigencia_extra_entre_semana"])
            if kind == "chollo" and (drop < cfg_a["umbral_chollo"] or pts < min_score):
                kind = "objetivo" if (target and price <= float(target)) else None
            normal = st["mediana"]
            extra = (f"Lo más barato visto en 30 días en esta ventana: {euros(st['minimo_historico'])} "
                     f"(normalmente ~{euros(st['minimo'])})")
            note = f"{note}. {extra}" if note else extra
        if kind == "objetivo":
            if price > float(target):
                return
            tnote = f"Tu objetivo era {euros(float(target))}"
            note = f"{tnote}. {note}" if note else tnote
        if not kind:
            return
        register({
            "tipo": kind, "clave": route["id"], "nombre": route["nombre"], "perfil": route["perfil"],
            "ventana": wid, "ventana_nombre": windows[wid]["nombre"],
            "salida": d1.isoformat(), "regreso": d2.isoformat(), "precio": price,
            "normal": normal if kind != "objetivo" else None, "caida": drop if kind != "objetivo" else None,
            "puntuacion": pts, "aerolineas": res.get("airlines"), "preferida": pref,
            "escalas": res.get("stops"), "vuelo": res.get("vuelo"), "url": res.get("url"),
            "nota": note, "aeropuerto": route["aeropuerto"],
            "origen_aeropuerto": route.get("origen_aeropuerto"), "ruta_txt": route_label(route),
            "objetivo": float(target) if target else None,
        })

    for (key, wid), lst in cal_best.items():
        route = next(r for r in routes if r["id"] == key)
        profile = profiles[route["perfil"]]
        st = cal_stats(cal_hist, key, wid, cfg_a)
        picks = [(lst[0], is_weekend_trip(lst[0][0], lst[0][1]))]
        if profile.get("fin_de_semana"):
            wk = next((x for x in lst if is_weekend_trip(x[0], x[1])), None)
            if wk and wk is not lst[0]:
                picks.append((wk, True))
        for (d1, d2, p), wk_choice in picks:
            cal_evaluate(route, wid, d1, d2, p, st, wk_choice)

    # Fechas que el radar anuncia baratas pero Google dice que no (caché desactualizada):
    # se ignoran 24 h para no gastar consultas en ellas una y otra vez
    stale = state.setdefault("radar_obsoletos", {})
    for k in list(stale):
        try:
            if datetime.fromisoformat(stale[k]) < NOW_UTC:
                del stale[k]
        except Exception:
            del stale[k]

    # ---------- 3. Plan de consultas a Google (reparto justo) ----------
    # Una cola por (prioridad, ventana). Dentro de cada cola: primero la oferta más barata
    # de cada ruta, luego las muestras para aprender el precio normal y, por último, la mejor
    # opción viernes → lunes. Las colas se turnan: las prioritarias reciben 3 turnos por
    # cada turno de las secundarias (≈75% / 25%), y todas las ventanas reciben su parte.
    queues = {}
    rot_routes = routes[run_n % max(len(routes), 1):] + routes[:run_n % max(len(routes), 1)]
    for route in rot_routes:
        idx = routes.index(route)
        profile = profiles[route["perfil"]]
        tier = 0 if route.get("prioridad", "media") == "alta" else 1
        lo, hi = profile["estancia_dias"]
        for wid in due:
            w = windows[wid]
            q = queues.setdefault((tier, wid), {"cand": [], "muestra": [], "finde": []})
            offers = [o for o in tp_by_route.get(route["id"], {}).get(wid, [])
                      if f"{route['id']}|{o['dep']}|{o['ret']}" not in stale]
            if offers:
                o = offers[0]
                q["cand"].append((route, wid, "candidato", o["dep"], o["ret"], o))
            if profile.get("fin_de_semana"):
                wk = next((o for o in offers if is_weekend_trip(o["dep"], o["ret"])), None)
                if wk and wk is not (offers[0] if offers else None):
                    q["finde"].append((route, wid, "candidato", wk["dep"], wk["ret"], wk))
            start = TODAY + timedelta(days=w["desde_dias"])
            span = max(w["hasta_dias"] - w["desde_dias"], 1)
            off = ((run_n * 7) + idx * 3) % span
            dep = start + timedelta(days=off)
            if profile.get("fin_de_semana") and random.random() < 0.7:
                dep += timedelta(days=(4 - dep.weekday()) % 7)
                ret = dep + timedelta(days=3)
            else:
                ret = dep + timedelta(days=random.randint(lo, hi))
            if window_of(dep, windows) == wid:
                q["muestra"].append((route, wid, "muestra", dep, ret, None))
    ordered = {k: v["cand"] + v["muestra"] + v["finde"] for k, v in queues.items()}

    # Búsqueda de objetivos: en cada ventana, las fechas que el radar ve cerca de tu
    # precio objetivo se verifican en Google antes que cualquier otra consulta.
    target_tasks = []
    margen = float(cfg_a.get("margen_busqueda_objetivo", 0.15))
    for route in routes:
        target = route.get("precio_objetivo")
        if not target:
            continue
        for wid in windows:
            near = [o for o in tp_by_route.get(route["id"], {}).get(wid, [])
                    if o["price"] <= float(target) * (1 + margen)
                    and f"{route['id']}|{o['dep']}|{o['ret']}" not in stale][:2]
            for o in near:
                target_tasks.append((route, wid, "candidato", o["dep"], o["ret"], o))
    planned = {(t[0]["id"], t[3], t[4]) for t in target_tasks}
    target_tasks = [t for t in target_tasks if (t[0]["id"], t[1]) not in cal_done]
    for k in ordered:
        ordered[k] = [t for t in ordered[k] if (t[0]["id"], t[3], t[4]) not in planned
                      and (t[0]["id"], t[1]) not in cal_done]
    if target_tasks:
        log(f"Búsqueda de objetivos: {len(target_tasks)} fechas cerca de tus precios objetivo")
    total_planned = sum(len(v) for v in ordered.values())
    tasks = list(target_tasks[:google_budget])
    weights = {0: 3, 1: 1}
    while any(ordered.values()) and len(tasks) < google_budget:
        for wid in due:
            for tier in (0, 1):
                lst = ordered.get((tier, wid), [])
                for _ in range(weights[tier]):
                    if lst:
                        tasks.append(lst.pop(0))
    log(f"Consultas a Google: {len(tasks[:google_budget])} de {total_planned + len(target_tasks)} posibles")
    tasks = [(None, None) + t for t in tasks]

    best_seen = {}
    for tier, _, route, wid, tipo, dep, ret, tp_o in tasks:
        if google_budget <= 0:
            break
        profile = profiles[route["perfil"]]
        max_dur = (int(route["duracion_directo_min"] * profile.get("factor_duracion", 3))
                   if route.get("duracion_directo_min") else None)
        res = run_google(route["id"], route["aeropuerto"], dep, ret, profile, max_dur, tipo,
                         orig=route.get("origen_aeropuerto"))
        if tp_o and res and res["ok"] and (not res["price"] or res["price"] > tp_o["price"] * 1.5):
            stale[f"{route['id']}|{dep}|{ret}"] = (NOW_UTC + timedelta(hours=24)).isoformat()
        if res and res["ok"] and res["price"]:
            k = (route["id"], wid)
            if k not in best_seen or res["price"] < best_seen[k]["precio"]:
                best_seen[k] = {"precio": res["price"], "salida": dep.isoformat(),
                                "regreso": ret.isoformat(), "url": res["url"]}
            evaluate(route, wid, dep, ret, res, tp_o, tipo)
        elif tp_o and (res is None or not res["ok"]):
            # Google no disponible: alertar solo con el radar, marcando que falta verificar
            tb = tp_baseline(tp_hist, f"{route['id']}@{wid}", dep.strftime("%Y-%m"), cfg_a)
            if tb and 1 - tp_o["price"] / tb >= cfg_a["umbral_error_fare"]:
                register({"tipo": "error_fare", "clave": route["id"], "nombre": route["nombre"],
                          "perfil": route["perfil"], "ventana": wid,
                          "ventana_nombre": windows[wid]["nombre"], "salida": dep.isoformat(),
                          "regreso": ret.isoformat(), "precio": tp_o["price"], "normal": tb,
                          "caida": 1 - tp_o["price"] / tb, "tp_link": tp_o["link"],
                          "verificado": False, "aeropuerto": route["aeropuerto"]})

    for route in routes:
        latest.setdefault("calendario", {})[route["id"]] = {
            wid: {"precio": cal_best[(route["id"], wid)][0][2],
                  "salida": cal_best[(route["id"], wid)][0][0].isoformat(),
                  "regreso": cal_best[(route["id"], wid)][0][1].isoformat()}
            for wid in windows if (route["id"], wid) in cal_best}
        latest["rutas"][route["id"]] = {
            "nombre": route["nombre"], "prioridad": route.get("prioridad"),
            "ventanas": {wid: best_seen.get((route["id"], wid)) for wid in due},
        }

    # ---------- 4. Exploración: cualquier otro destino ----------
    ecfg = cfg.get("exploracion", {})
    if ecfg.get("activada"):
        cities = load_cities()
        regions = set(ecfg.get("regiones", []))
        fixed = {r["codigo_ciudad"] for r in routes if not is_reverse(r)} | {origin_city}
        found = {}
        for m in months:
            for item in tp_request({"origin": origin_city, "departure_at": m}):
                o = tp_offer(item)
                if not o or o["dest"] in fixed or o["dest"] not in cities:
                    continue
                country = cities[o["dest"]]["country"]
                region = "europa" if country in EUROPA else "latam" if country in LATAM else None
                if region not in regions:
                    continue
                pname = "europa" if region == "europa" else "largo"
                if stay_ok(o, profiles[pname]) and window_of(o["dep"], windows) in due:
                    o["perfil"] = pname
                    found.setdefault(o["dest"], []).append(o)
            time.sleep(0.25)
        log(f"Exploración: {len(found)} destinos con precios")
        umbral_x = float(ecfg.get("umbral_chollo", 0.35))
        for dest, offers in found.items():
            best = dedupe_best(offers)[0]
            key = f"X-{dest}"
            tp_rows.append({"ts": NOW_ISO, "clave": key, "mes_salida": best["dep"].strftime("%Y-%m"),
                            "n": len(offers), "minimo": best["price"],
                            "mediana": round(statistics.median(o["price"] for o in offers), 2)})
            name = cities[dest]["name"]
            latest["exploracion"][dest] = {"nombre": name, "precio": best["price"],
                                           "salida": best["dep"].isoformat(),
                                           "regreso": best["ret"].isoformat()}
            base = tp_baseline(tp_hist, key, best["dep"].strftime("%Y-%m"), cfg_a)
            if not base:
                continue
            d = 1 - best["price"] / base
            if d < umbral_x:
                continue
            kind = "error_fare" if d >= cfg_a["umbral_error_fare"] else "chollo"
            wid = window_of(best["dep"], windows)
            alert = {"tipo": kind, "clave": key, "nombre": name, "perfil": best["perfil"],
                     "ventana": wid, "ventana_nombre": windows[wid]["nombre"] if wid else None,
                     "salida": best["dep"].isoformat(), "regreso": best["ret"].isoformat(),
                     "precio": best["price"], "tp_link": best["link"], "aeropuerto": best["dest_airport"],
                     "nota": f"Destino descubierto por exploración ({d:.0%} por debajo de lo normal en el radar)"}
            res = None
            if ecfg.get("verificar_en_google", True):
                res = run_google(key, best["dest_airport"], best["dep"], best["ret"],
                                 profiles[best["perfil"]], None, "exploracion")
            if res and res["ok"] and res["price"] and res["price"] <= best["price"] * 1.3:
                alert.update(precio=res["price"], url=res["url"], aerolineas=res.get("airlines"),
                             escalas=res.get("stops"), vuelo=res.get("vuelo"),
                             preferida=is_preferred(res.get("airlines"), preferred))
                register(alert)
            # sin verificación en Google no se avisa: la caché del radar puede estar desactualizada

    # ---------- 5. Enviar alertas: una por ruta, la mejor ----------
    order = {"error_fare": 0, "objetivo": 1, "chollo": 2}
    outgoing.sort(key=lambda a: (order[a["tipo"]], -(a.get("puntuacion") or 0), a["precio"]))
    cooldown = state.setdefault("ultimo_aviso_ruta", {})
    for k in list(cooldown):
        try:
            if NOW_UTC - datetime.fromisoformat(cooldown[k]["fecha"]) > timedelta(hours=24):
                del cooldown[k]
        except Exception:
            del cooldown[k]
    target_cool = state.setdefault("ultimo_aviso_objetivo", {})
    horas_obj = float(cfg_a.get("repetir_objetivo_cada_horas", 72))
    for k in list(target_cool):
        try:
            if NOW_UTC - datetime.fromisoformat(target_cool[k]["fecha"]) > timedelta(hours=horas_obj):
                del target_cool[k]
        except Exception:
            del target_cool[k]
    groups = {}
    for a in outgoing:
        groups.setdefault(a["clave"], []).append(a)
    for key, group in groups.items():
        best = group[0]
        prev = cooldown.get(key)
        # Si ya te avisé de esta ruta en las últimas 24 h, solo vuelvo a avisar si el
        # precio mejora de forma clara o si ahora es un error fare
        upgrade = best["tipo"] == "error_fare" and prev and prev.get("tipo") != "error_fare"
        mejora = float(cfg_a.get("mejora_minima_para_repetir", 0.10))
        if not prev and best["tipo"] == "objetivo":
            prev = target_cool.get(key)   # objetivos: no repetir durante varios días
        if prev and not upgrade and best["precio"] > prev["precio"] * (1 - mejora):
            continue
        targets_met = [a for a in group if a["tipo"] == "objetivo" or
                       (a.get("objetivo") and a["precio"] <= a["objetivo"])]
        if targets_met:
            wnames = [windows[w]["nombre"] for w in windows
                      if any(a["ventana"] == w for a in targets_met)]
            txt = f"Por debajo de tu objetivo en: {', '.join(wnames)}"
            best["nota"] = f"{best['nota']}. {txt}" if best.get("nota") else txt
        others = len({(a["salida"], a["regreso"]) for a in group}) - 1
        if others > 0:
            extra = f"Hay {others} fecha{'s' if others > 1 else ''} más con precios muy bajos en esta ruta"
            best["nota"] = f"{best['nota']}. {extra}" if best.get("nota") else extra
        if telegram(build_message(best, cfg)):
            stats["alertas"] += 1
            cooldown[key] = {"precio": best["precio"], "fecha": NOW_ISO, "tipo": best["tipo"]}
            if best["tipo"] == "objetivo":
                target_cool[key] = {"precio": best["precio"], "fecha": NOW_ISO}
        time.sleep(1)

    # ---------- 6. Resumen diario ----------
    hour = cfg_a.get("resumen_diario_hora")
    if hour is not None and NOW_MADRID.hour >= int(hour) and state.get("ultimo_resumen") != TODAY.isoformat():
        if cfg_a.get("resumen_por_ventana", True):
            msgs = summary_messages(cfg, gf_rows, gf_index, cal_extra=cal_rows)
        else:
            msgs = [summary_text(cfg, gf_rows, gf_index)]
        total_g = stats["google_ok"] + stats["google_fail"]
        if total_g and stats["google_fail"] / total_g > 0.5:
            msgs[-1] += "\n⚠️ Google Flights está fallando en muchas consultas. Avísale a Claude."
        if not TP_TOKEN:
            msgs[-1] += "\n⚠️ Falta el secreto TRAVELPAYOUTS_TOKEN."
        ok = True
        for m in msgs:
            ok = telegram(m) and ok
            time.sleep(1)
        if ok:
            state["ultimo_resumen"] = TODAY.isoformat()

    state["ultima_ejecucion"] = {"fecha": NOW_ISO, "google_ok": stats["google_ok"],
                                 "google_fallos": stats["google_fail"], "alertas": stats["alertas"],
                                 "calendario_consultas": stats.get("cal", 0),
                                 "calendario_fallos": stats.get("cal_fallos", 0),
                                 "calendario_rutas": len({k for k, _ in cal_done}),
                                 "calendario_presupuesto": state.get("calendario_presupuesto")}

    # ---------- 7. Guardar ----------
    append_csv(TP_FILE, TP_FIELDS, tp_rows)
    append_csv(GF_FILE, GF_FIELDS, gf_rows)
    append_csv(CAL_FILE, CAL_FIELDS, cal_rows)
    if run_n % 8 == 0:
        prune_csv(TP_FILE, TP_FIELDS, 150)
        prune_csv(GF_FILE, GF_FIELDS, 150)
        prune_csv(CAL_FILE, CAL_FIELDS, 90)
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    save_state(state)
    log(f"Calendario: {stats.get('cal', 0)} consultas, {len({k for k, _ in cal_done})} rutas cubiertas")
    log(f"Listo. Google OK: {stats['google_ok']}, fallos: {stats['google_fail']}, "
        f"alertas: {stats['alertas']}, en cola de silencio: {stats['en_cola']}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--barrido":
        # python radar.py --barrido N --partes M
        n = int(sys.argv[2])
        m = int(sys.argv[4]) if len(sys.argv) >= 5 and sys.argv[3] == "--partes" else 4
        run_shard(n, m)
    else:
        main()
