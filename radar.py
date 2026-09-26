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
    return dep.weekday() == 4 and ret.weekday() == 0 and (ret - dep).days == 3


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
# Precio normal y puntuación
# ------------------------------------------------------------------
TOLERANCIA_DEFECTO = {"ultimo_minuto": 3, "corto": 10, "medio": 25, "largo": 45}


def build_gf_index(gf_hist, windows):
    """Agrupa los precios de Google por (ruta, ventana) con su antelación en días."""
    idx = {}
    for r in gf_hist:
        if r.get("tipo") not in ("muestra", "candidato") or not r.get("precio"):
            continue
        try:
            dep = date.fromisoformat(r["salida"])
            ref = datetime.fromisoformat(r["ts"]).astimezone(MADRID).date()
            price = float(r["precio"])
        except Exception:
            continue
        wid = window_of(dep, windows, ref)
        if wid:
            idx.setdefault((r["clave"], wid), []).append((r["tipo"], (dep - ref).days, price))
    return idx


def gf_baseline(gf_index, key, wid, tipo, lead, windows, cfg_a):
    """Precio habitual para el mismo tipo de búsqueda y una antelación parecida.
    - Candidatos (las fechas más baratas encontradas) se comparan con los candidatos
      habituales: "¿el mejor precio de hoy es mucho mejor que el mejor precio de siempre?"
    - Muestras (fechas al azar) se comparan con otras muestras."""
    tol = windows[wid].get("tolerancia_dias", TOLERANCIA_DEFECTO.get(wid, 15))
    vals = [p for t, l, p in gf_index.get((key, wid), []) if t == tipo and abs(l - lead) <= tol]
    if len(vals) >= cfg_a["min_observaciones"]:
        return statistics.median(vals)
    return None


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
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
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
    lines = [head, f"<b>Madrid → {html.escape(a['nombre'])}</b>", ""]
    if a.get("verificado", True):
        lines.append(f"💶 <b>{euros(a['precio'])}</b> ida y vuelta ({bag_text(profile)})")
    else:
        lines.append(f"💶 <b>{euros(a['precio'])}</b> ida y vuelta (⚠️ equipaje no incluido, verifícalo)")
    if a.get("caida") is not None and a.get("normal"):
        lines.append(f"📉 {a['caida']:.0%} por debajo de lo normal (~{euros(a['normal'])})")
    tag = " · viernes → lunes" if is_weekend_trip(dep, ret) else ""
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


def summary_text(cfg, extra_rows=None, gf_index=None, titulo=None):
    """Mejores precios de las últimas 24 h por ruta (usado en el resumen y en /resumen)."""
    windows = cfg["ventanas"]
    cfg_a = cfg["alertas"]
    routes = cfg.get("rutas", [])
    if gf_index is None:
        gf_index = build_gf_index(read_csv(GF_FILE, cfg_a["dias_historial"]), windows)
    recent = read_csv(GF_FILE, 1) + (extra_rows or [])
    lines = [titulo or f"☀️ <b>Resumen diario</b> · {fdate(TODAY)}"]
    for tier_name, tier in (("Prioritarias", "alta"), ("Secundarias", "media")):
        tier_routes = [r for r in routes if r.get("prioridad", "media") == tier]
        if not tier_routes:
            continue
        lines.append(f"\n<b>{tier_name}</b>")
        for route in tier_routes:
            rows = [r for r in recent if r["clave"] == route["id"] and r["precio"]]
            if not rows:
                lines.append(f"• {html.escape(route['nombre'])}: sin datos hoy")
                continue
            b = min(rows, key=lambda r: float(r["precio"]))
            d1, d2 = date.fromisoformat(b["salida"]), date.fromisoformat(b["regreso"])
            wid = window_of(d1, windows)
            wtxt = f" · {windows[wid]['nombre']}" if wid else ""
            tgt = f" 🎯{euros(float(route['precio_objetivo']))}" if route.get("precio_objetivo") else ""
            lines.append(f"• {html.escape(route['nombre'])}: <b>{euros(float(b['precio']))}</b> "
                         f"({fdate(d1)} → {fdate(d2)}{wtxt}){tgt}")
    learned = sum(1 for r in routes for wid in windows
                  if sum(1 for t, _, _ in gf_index.get((r["id"], wid), []) if t == "candidato")
                  >= cfg_a["min_observaciones"])
    lines.append(f"\n🧠 Precio normal aprendido: {learned}/{len(routes) * len(windows)} combinaciones ruta-ventana")
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
    slot = NOW_UTC.hour // 3
    due = [wid for wid, w in windows.items()
           if FORCE_ALL or slot % max(int(w.get("cada_horas", 3)) // 3, 1) == 0]
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
    tp_rows, gf_rows = [], []
    latest = {"actualizado": NOW_ISO, "rutas": {}, "exploracion": {}}
    outgoing = []  # alertas listas para enviar

    min_lead = min(windows[w]["desde_dias"] for w in due)
    max_lead = max(windows[w]["hasta_dias"] for w in due)
    months = months_between(TODAY + timedelta(days=min_lead), TODAY + timedelta(days=max_lead))

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

    def run_google(key, dest_ap, dep, ret, profile, max_dur, tipo):
        nonlocal google_budget
        if not google_on or google_budget <= 0:
            return None
        google_budget -= 1
        res = google_price(cfg, origin_ap, dest_ap, dep, ret, profile, max_dur)
        stats["google_ok" if res["ok"] else "google_fail"] += 1
        if res["ok"] and res["price"]:
            gf_rows.append({"ts": NOW_ISO, "clave": key, "tipo": tipo, "salida": dep.isoformat(),
                            "regreso": ret.isoformat(), "precio": res["price"]})
        time.sleep(random.uniform(2, 4))
        return res

    def evaluate(route, wid, dep, ret, res, tp_o, tipo):
        """Decide si una oferta verificada merece alerta."""
        key = route["id"]
        profile = profiles[route["perfil"]]
        price = res["price"]
        month = dep.strftime("%Y-%m")
        pref = is_preferred(res.get("airlines"), preferred)
        weekend_ok = (not profile.get("fin_de_semana")) or is_weekend_trip(dep, ret)
        min_score = cfg_a["puntuacion_minima"] + (0 if weekend_ok else cfg_a["exigencia_extra_entre_semana"])
        base = gf_baseline(gf_index, key, wid, tipo, (dep - TODAY).days, windows, cfg_a)
        kind = drop = pts = None
        note = normal = None
        if base:
            d = 1 - price / base
            pts = score(max(d, 0), res.get("stops"), pref)
            if d >= cfg_a["umbral_error_fare"]:
                kind, drop, normal = "error_fare", d, base
            elif d >= cfg_a["umbral_chollo"] and pts >= min_score:
                kind, drop, normal = "chollo", d, base
        elif tp_o:  # aún sin precio normal en Google: usar la caída detectada por el radar
            tb = tp_baseline(tp_hist, f"{key}@{wid}", month, cfg_a)
            if tb:
                d = 1 - tp_o["price"] / tb
                pts = score(max(d, 0), res.get("stops"), pref)
                if d >= cfg_a["umbral_error_fare"] or (d >= cfg_a["umbral_chollo"] and pts >= min_score):
                    kind = "error_fare" if d >= cfg_a["umbral_error_fare"] else "chollo"
                    note = f"El radar detectó una caída del {d:.0%}; precio confirmado en Google Flights"
        target = route.get("precio_objetivo")
        if not kind and target and price <= float(target):
            kind = "objetivo"
            note = f"Tu objetivo era {euros(float(target))}"
        if not kind:
            return
        register({
            "tipo": kind, "clave": key, "nombre": route["nombre"], "perfil": route["perfil"],
            "ventana": wid, "ventana_nombre": windows[wid]["nombre"],
            "salida": dep.isoformat(), "regreso": ret.isoformat(), "precio": price,
            "normal": normal, "caida": drop, "puntuacion": pts, "aerolineas": res.get("airlines"),
            "preferida": pref, "escalas": res.get("stops"), "vuelo": res.get("vuelo"),
            "url": res.get("url"), "tp_link": tp_o["link"] if tp_o else None, "nota": note,
            "aeropuerto": route["aeropuerto"],
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
                             profile, max_dur, "reconfirmacion")
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
        offers = []
        for m in months:
            for item in tp_request({"origin": origin_city, "destination": route["codigo_ciudad"],
                                    "departure_at": m}):
                o = tp_offer(item)
                if o and stay_ok(o, profile, max_dur):
                    o["ventana"] = window_of(o["dep"], windows)
                    if o["ventana"] in due:
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

    # ---------- 3. Plan de consultas a Google ----------
    tasks = []  # (orden, ruta, ventana, tipo, salida, regreso, oferta_tp)
    for idx, route in enumerate(routes):
        profile = profiles[route["perfil"]]
        tier = 0 if route.get("prioridad", "media") == "alta" else 1
        lo, hi = profile["estancia_dias"]
        for wid in due:
            w = windows[wid]
            offers = tp_by_route.get(route["id"], {}).get(wid, [])
            if offers:  # la oferta más barata del radar
                o = offers[0]
                tasks.append((tier, 0, route, wid, "candidato", o["dep"], o["ret"], o))
            if profile.get("fin_de_semana") and tier == 0:  # la mejor de viernes → lunes
                wk = next((o for o in offers if is_weekend_trip(o["dep"], o["ret"])), None)
                if wk and (not offers or wk is not offers[0]):
                    tasks.append((tier, 0, route, wid, "candidato", wk["dep"], wk["ret"], wk))
            # muestra rotativa para aprender el precio normal de la ventana
            start = TODAY + timedelta(days=w["desde_dias"])
            span = max(w["hasta_dias"] - w["desde_dias"] - hi, 1)
            off = ((run_n * 7) + idx * 3) % span
            dep = start + timedelta(days=off)
            if profile.get("fin_de_semana") and random.random() < 0.7:
                dep += timedelta(days=(4 - dep.weekday()) % 7)
                ret = dep + timedelta(days=3)
            else:
                ret = dep + timedelta(days=random.randint(lo, hi))
            if window_of(dep, windows) == wid:
                tasks.append((tier, 1, route, wid, "muestra", dep, ret, None))
    rot = run_n % max(len(tasks), 1)
    tasks = tasks[rot:] + tasks[:rot]  # reparto justo entre ejecuciones
    tasks.sort(key=lambda t: (t[0], t[1]))
    log(f"Consultas planificadas en Google: {len(tasks)} (máximo {google_budget})")

    best_seen = {}
    for tier, _, route, wid, tipo, dep, ret, tp_o in tasks:
        if google_budget <= 0:
            break
        profile = profiles[route["perfil"]]
        max_dur = (int(route["duracion_directo_min"] * profile.get("factor_duracion", 3))
                   if route.get("duracion_directo_min") else None)
        res = run_google(route["id"], route["aeropuerto"], dep, ret, profile, max_dur, tipo)
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
        latest["rutas"][route["id"]] = {
            "nombre": route["nombre"], "prioridad": route.get("prioridad"),
            "ventanas": {wid: best_seen.get((route["id"], wid)) for wid in due},
        }

    # ---------- 4. Exploración: cualquier otro destino ----------
    ecfg = cfg.get("exploracion", {})
    if ecfg.get("activada"):
        cities = load_cities()
        regions = set(ecfg.get("regiones", []))
        fixed = {r["codigo_ciudad"] for r in routes} | {origin_city}
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
            if res and res["ok"] and res["price"]:
                alert.update(precio=res["price"], url=res["url"], aerolineas=res.get("airlines"),
                             escalas=res.get("stops"), vuelo=res.get("vuelo"),
                             preferida=is_preferred(res.get("airlines"), preferred))
                register(alert)
            elif res is None or not res["ok"]:
                alert.update(verificado=False, normal=base, caida=d)
                register(alert)

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
        if prev and not upgrade and best["precio"] > prev["precio"] * (1 - mejora):
            continue
        others = len({(a["salida"], a["regreso"]) for a in group}) - 1
        if others > 0:
            extra = f"Hay {others} fecha{'s' if others > 1 else ''} más con precios muy bajos en esta ruta"
            best["nota"] = f"{best['nota']}. {extra}" if best.get("nota") else extra
        if telegram(build_message(best, cfg)):
            stats["alertas"] += 1
            cooldown[key] = {"precio": best["precio"], "fecha": NOW_ISO, "tipo": best["tipo"]}
        time.sleep(1)

    # ---------- 6. Resumen diario ----------
    hour = cfg_a.get("resumen_diario_hora")
    if hour is not None and NOW_MADRID.hour >= int(hour) and state.get("ultimo_resumen") != TODAY.isoformat():
        text = summary_text(cfg, gf_rows, gf_index)
        total_g = stats["google_ok"] + stats["google_fail"]
        if total_g and stats["google_fail"] / total_g > 0.5:
            text += "\n⚠️ Google Flights está fallando en muchas consultas. Avísale a Claude."
        if not TP_TOKEN:
            text += "\n⚠️ Falta el secreto TRAVELPAYOUTS_TOKEN."
        if telegram(text):
            state["ultimo_resumen"] = TODAY.isoformat()

    state["ultima_ejecucion"] = {"fecha": NOW_ISO, "google_ok": stats["google_ok"],
                                 "google_fallos": stats["google_fail"], "alertas": stats["alertas"]}

    # ---------- 7. Guardar ----------
    append_csv(TP_FILE, TP_FIELDS, tp_rows)
    append_csv(GF_FILE, GF_FIELDS, gf_rows)
    if run_n % 8 == 0:
        prune_csv(TP_FILE, TP_FIELDS, 150)
        prune_csv(GF_FILE, GF_FIELDS, 150)
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    save_state(state)
    log(f"Listo. Google OK: {stats['google_ok']}, fallos: {stats['google_fail']}, "
        f"alertas: {stats['alertas']}, en cola de silencio: {stats['en_cola']}")


if __name__ == "__main__":
    main()
