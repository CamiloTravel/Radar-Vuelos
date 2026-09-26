#!/usr/bin/env python3
"""
RADAR DE VUELOS BARATOS
=======================
Se ejecuta automáticamente cada 3 horas en GitHub Actions:
  1. Consulta precios en Travelpayouts (caché de búsquedas de Aviasales).
  2. Verifica los mejores candidatos en Google Flights, con tu equipaje incluido.
  3. Guarda el historial y calcula el "precio normal" de cada ruta.
  4. Te avisa por Telegram cuando detecta un chollo.

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
# Rutas de archivos y constantes
# ------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
TP_FILE = DATA / "historial_radar.csv"       # Resumen de precios del radar
GF_FILE = DATA / "historial_google.csv"      # Precios verificados en Google Flights
STATE_FILE = DATA / "estado.json"            # Memoria interna (alertas enviadas, etc.)
LATEST_FILE = DATA / "ultimos_precios.json"  # Foto de la última ejecución (para el panel)

MADRID = ZoneInfo("Europe/Madrid")
NOW_MADRID = datetime.now(MADRID)
TODAY = NOW_MADRID.date()
NOW_UTC = datetime.now(timezone.utc)
NOW_ISO = NOW_UTC.isoformat(timespec="seconds")

TP_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

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

TP_FIELDS = ["ts", "clave", "mes_salida", "n", "minimo", "mediana"]
GF_FIELDS = ["ts", "clave", "tipo", "salida", "regreso", "precio"]


def log(msg):
    print(f"[{datetime.now(MADRID):%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------------
def load_config():
    with open(BASE / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def months_ahead(n):
    first = TODAY.replace(day=1)
    out = []
    for i in range(n + 1):
        y = first.year + (first.month - 1 + i) // 12
        m = (first.month - 1 + i) % 12 + 1
        out.append(f"{y:04d}-{m:02d}")
    return out


def fdate(d):
    return f"{d.day} {MESES[d.month - 1]}"


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
    """Borra datos muy antiguos para que el repositorio no crezca sin límite."""
    if not path.exists():
        return
    rows = read_csv(path, keep_days)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------------
# Travelpayouts (radar amplio)
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
        "dep": dep,
        "ret": ret,
        "stay": (ret - dep).days,
        "price": price,
        "transfers": item.get("transfers"),
        "airline": item.get("airline"),
        "dest": item.get("destination"),
        "dest_airport": item.get("destination_airport") or item.get("destination"),
        "duration_to": item.get("duration_to"),
        "link": f"https://www.aviasales.com{link}" if link else None,
    }


def offer_ok(o, profile, horizon_days, max_dur=None):
    if o is None:
        return False
    lo, hi = profile["estancia_dias"]
    if not (lo <= o["stay"] <= hi):
        return False
    if o["dep"] <= TODAY or o["dep"] > TODAY + timedelta(days=horizon_days):
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
# Google Flights (verificación con equipaje)
# ------------------------------------------------------------------
def google_price(cfg, orig, dest, dep, ret, profile, max_dur):
    from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

    q = create_query(
        flights=[
            FlightQuery(date=dep.isoformat(), from_airport=orig, to_airport=dest,
                        max_duration_minutes=max_dur),
            FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=orig,
                        max_duration_minutes=max_dur),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
        language="en-US",
        currency="EUR",
        carry_on_bags=int(profile.get("equipaje_mano", 0)),
        checked_bags=int(profile.get("equipaje_facturado", 0)),
        hide_separate_and_self_transfer=bool(cfg.get("ocultar_autotransbordo", True)),
    )
    url = q.url()
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
        "ok": True,
        "price": float(best.price),
        "airlines": list(best.airlines or []),
        "stops": max(len(best.flights) - 1, 0),
        "url": url,
    }


# ------------------------------------------------------------------
# Precio "normal" (línea base)
# ------------------------------------------------------------------
def tp_baseline(tp_hist, key, month, cfg_a):
    rows = [r for r in tp_hist if r["clave"] == key]
    month_vals = [float(r["mediana"]) for r in rows if r["mes_salida"] == month]
    if len(month_vals) >= cfg_a["min_observaciones_mes"]:
        return statistics.median(month_vals)
    all_vals = [float(r["mediana"]) for r in rows]
    if len(all_vals) >= cfg_a["min_observaciones_ruta"]:
        return statistics.median(all_vals)
    return None


def gf_baseline(gf_hist, key, month, cfg_a):
    rows = [r for r in gf_hist if r["clave"] == key and r["tipo"] == "muestra" and r["precio"]]
    month_vals = [float(r["precio"]) for r in rows if r["salida"][:7] == month]
    if len(month_vals) >= cfg_a["min_observaciones_mes"]:
        return statistics.median(month_vals)
    all_vals = [float(r["precio"]) for r in rows]
    if len(all_vals) >= cfg_a["min_observaciones_ruta"]:
        return statistics.median(all_vals)
    return None


def classify(price, base, cfg_a):
    """Devuelve ('error_fare'|'chollo', % de bajada) o None."""
    if not price or not base:
        return None
    drop = 1 - price / base
    if drop >= cfg_a["umbral_error_fare"]:
        return "error_fare", drop
    if drop >= cfg_a["umbral_chollo"]:
        return "chollo", drop
    return None


# ------------------------------------------------------------------
# Telegram
# ------------------------------------------------------------------
def telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        log("Telegram no configurado; mensaje no enviado:\n" + text)
        return False
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    ok = True
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


def alert_message(kind, name, price, dep, ret, profile, base=None, drop=None,
                  airlines=None, stops=None, url=None, tp_link=None, verified=True, note=None):
    titles = {
        "error_fare": "🚨 <b>POSIBLE ERROR FARE</b>",
        "chollo": "🔥 <b>CHOLLO</b>",
        "objetivo": "🎯 <b>PRECIO OBJETIVO</b>",
    }
    lines = [f"{titles[kind]} · Madrid → {html.escape(name)}", ""]
    if verified:
        lines.append(f"💶 <b>{euros(price)}</b> ida y vuelta ({bag_text(profile)})")
    else:
        lines.append(f"💶 <b>{euros(price)}</b> ida y vuelta (⚠️ equipaje no incluido, verifícalo)")
    if drop is not None and base:
        lines.append(f"📉 {drop:.0%} por debajo de lo normal (~{euros(base)})")
    lines.append(f"📅 {fdate(dep)} → {fdate(ret)} ({(ret - dep).days} días)")
    if airlines or stops is not None:
        parts = []
        if airlines:
            parts.append(html.escape(", ".join(airlines[:2])))
        if stops is not None:
            parts.append("directo" if stops == 0 else f"{stops} escala{'s' if stops > 1 else ''}")
        lines.append("✈️ " + " · ".join(parts))
    if note:
        lines.append(f"ℹ️ {html.escape(note)}")
    links = []
    if url:
        links.append(f'<a href="{html.escape(url)}">Google Flights</a>')
    if tp_link:
        links.append(f'<a href="{html.escape(tp_link)}">Aviasales</a>')
    if links:
        lines.append("🔗 " + " | ".join(links))
    if kind == "error_fare":
        lines.append("\n⏱ Estos precios suelen durar pocas horas.")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Programa principal
# ------------------------------------------------------------------
def main():
    DATA.mkdir(exist_ok=True)
    cfg = load_config()
    cfg_a = cfg["alertas"]
    state = load_state()
    run_n = int(state.get("ejecuciones", 0))
    state["ejecuciones"] = run_n + 1
    sent = state.setdefault("alertas_enviadas", {})
    for k in list(sent):  # olvidar alertas de vuelos que ya pasaron
        try:
            if date.fromisoformat(k.split("|")[1]) < TODAY:
                del sent[k]
        except Exception:
            del sent[k]

    if not state.get("inicializado"):
        if telegram("✅ <b>Radar de vuelos conectado</b>\n\nTu sistema ya está funcionando. "
                    "Revisaré precios cada 3 horas y te avisaré aquí cuando encuentre un chollo. "
                    "Durante las primeras semanas estaré aprendiendo cuál es el precio normal de cada ruta."):
            state["inicializado"] = True

    origin_city = cfg["origen"]["codigo_ciudad"]
    origin_ap = cfg["origen"]["aeropuerto"]
    horizon_days = int(cfg["horizonte_meses"]) * 31
    months = months_ahead(int(cfg["horizonte_meses"]))
    factor = float(cfg.get("factor_duracion_maxima", 3))
    profiles = cfg["perfiles"]
    gcfg = cfg.get("google", {})
    google_on = bool(gcfg.get("activado", True))
    google_budget = int(gcfg.get("max_consultas_por_ejecucion", 70))
    stats = {"google_ok": 0, "google_fail": 0, "alertas": 0}

    tp_hist = read_csv(TP_FILE, cfg_a["dias_historial"])
    gf_hist = read_csv(GF_FILE, cfg_a["dias_historial"])
    tp_rows, gf_rows, latest = [], [], {"actualizado": NOW_ISO, "rutas": {}, "exploracion": {}}
    pending_alerts = []

    def maybe_alert(key, dep, ret, price, message):
        k = f"{key}|{dep.isoformat()}|{ret.isoformat()}"
        prev = sent.get(k)
        if prev and price > prev["precio"] * (1 - cfg_a["realertar_si_baja"]):
            return
        sent[k] = {"precio": price, "fecha": NOW_ISO}
        pending_alerts.append(message)

    def record_tp(key, offers):
        by_month = {}
        for o in offers:
            by_month.setdefault(o["dep"].strftime("%Y-%m"), []).append(o["price"])
        for m, prices in by_month.items():
            tp_rows.append({"ts": NOW_ISO, "clave": key, "mes_salida": m, "n": len(prices),
                            "minimo": round(min(prices), 2),
                            "mediana": round(statistics.median(prices), 2)})

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

    # ---------------- 1. Rutas fijas: radar ----------------
    routes = cfg.get("rutas", [])
    route_offers = {}
    for route in routes:
        profile = profiles[route["perfil"]]
        max_dur = int(route["duracion_directo_min"] * factor) if route.get("duracion_directo_min") else None
        offers = []
        for m in months:
            for item in tp_request({"origin": origin_city, "destination": route["codigo_ciudad"],
                                    "departure_at": m}):
                o = tp_offer(item)
                if offer_ok(o, profile, horizon_days, max_dur):
                    offers.append(o)
            time.sleep(0.3)
        offers = dedupe_best(offers)
        route_offers[route["id"]] = offers
        record_tp(route["id"], offers)
        log(f"Radar {route['nombre']}: {len(offers)} opciones válidas"
            + (f", mínimo {euros(offers[0]['price'])}" if offers else ""))

    # ---------------- 2. Rutas fijas: verificación en Google ----------------
    n_cand = int(gcfg.get("candidatos_por_ruta", 3))
    n_samp = int(gcfg.get("muestras_por_ruta", 2))
    for idx, route in enumerate(routes):
        key = route["id"]
        profile = profiles[route["perfil"]]
        max_dur = int(route["duracion_directo_min"] * factor) if route.get("duracion_directo_min") else None
        lo, hi = profile["estancia_dias"]
        tasks = [("candidato", o["dep"], o["ret"], o) for o in route_offers[key][:n_cand]]
        span = max(horizon_days - 10, 30)
        for i in range(n_samp):  # fechas rotativas para aprender el precio normal
            off = 7 + ((run_n * n_samp + i) * 11 + idx * 5) % span
            dep = TODAY + timedelta(days=off)
            ret = dep + timedelta(days=random.randint(lo, hi))
            tasks.append(("muestra", dep, ret, None))

        best_route = None
        for tipo, dep, ret, tp_o in tasks:
            res = run_google(key, route["aeropuerto"], dep, ret, profile, max_dur, tipo)
            month = dep.strftime("%Y-%m")
            g_price = res["price"] if res and res["ok"] else None
            tp_price = tp_o["price"] if tp_o else None
            tp_base = tp_baseline(tp_hist, key, month, cfg_a)

            if g_price:
                if not best_route or g_price < best_route["precio"]:
                    best_route = {"precio": g_price, "salida": dep.isoformat(), "regreso": ret.isoformat(),
                                  "url": res["url"]}
                g_base = gf_baseline(gf_hist, key, month, cfg_a)
                verdict = classify(g_price, g_base, cfg_a)
                base, note = g_base, None
                if not verdict and tp_price and not g_base:
                    verdict = classify(tp_price, tp_base, cfg_a)
                    if verdict:
                        base, note = None, (f"El radar detectó una caída del {verdict[1]:.0%}; "
                                            "precio confirmado en Google Flights")
                target = route.get("precio_objetivo")
                if verdict:
                    kind, drop = verdict
                    maybe_alert(key, dep, ret, g_price, alert_message(
                        kind, route["nombre"], g_price, dep, ret, profile,
                        base=base, drop=drop if base else None,
                        airlines=res.get("airlines"), stops=res.get("stops"),
                        url=res["url"], tp_link=tp_o["link"] if tp_o else None, note=note))
                elif target and g_price <= float(target):
                    maybe_alert(key, dep, ret, g_price, alert_message(
                        "objetivo", route["nombre"], g_price, dep, ret, profile,
                        airlines=res.get("airlines"), stops=res.get("stops"), url=res["url"],
                        tp_link=tp_o["link"] if tp_o else None,
                        note=f"Tu objetivo era {euros(float(target))}"))
            elif tp_o and (res is None or not res["ok"]):
                # Google no disponible: alertar solo con el radar, marcando que falta verificar
                verdict = classify(tp_price, tp_base, cfg_a)
                if verdict:
                    kind, drop = verdict
                    maybe_alert(key, dep, ret, tp_price, alert_message(
                        kind, route["nombre"], tp_price, dep, ret, profile, base=tp_base, drop=drop,
                        tp_link=tp_o["link"], verified=False))

        latest["rutas"][key] = {
            "nombre": route["nombre"],
            "radar_minimo": route_offers[key][0]["price"] if route_offers[key] else None,
            "google_mejor": best_route,
        }

    # ---------------- 3. Exploración: cualquier destino ----------------
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
                profile = profiles["europa" if region == "europa" else "largo"]
                if offer_ok(o, profile, horizon_days):
                    o["region"] = region
                    found.setdefault(o["dest"], []).append(o)
            time.sleep(0.3)
        log(f"Exploración: {len(found)} destinos con precios")

        for dest, offers in found.items():
            offers = dedupe_best(offers)
            key = f"X-{dest}"
            record_tp(key, offers)
            best = offers[0]
            name = cities[dest]["name"]
            profile = profiles["europa" if best["region"] == "europa" else "largo"]
            latest["exploracion"][dest] = {"nombre": name, "precio": best["price"],
                                           "salida": best["dep"].isoformat(),
                                           "regreso": best["ret"].isoformat()}
            base = tp_baseline(tp_hist, key, best["dep"].strftime("%Y-%m"), cfg_a)
            verdict = classify(best["price"], base, cfg_a)
            if not verdict:
                continue
            kind, drop = verdict
            res = None
            if ecfg.get("verificar_en_google", True):
                res = run_google(key, best["dest_airport"], best["dep"], best["ret"], profile, None, "exploracion")
            if res and res["ok"] and res["price"]:
                maybe_alert(key, best["dep"], best["ret"], res["price"], alert_message(
                    kind, name, res["price"], best["dep"], best["ret"], profile, airlines=res.get("airlines"),
                    stops=res.get("stops"), url=res["url"], tp_link=best["link"],
                    note=f"Destino descubierto por exploración: {drop:.0%} por debajo de lo normal en el radar"))
            elif res is None or not res["ok"]:
                maybe_alert(key, best["dep"], best["ret"], best["price"], alert_message(
                    kind, name, best["price"], best["dep"], best["ret"], profile, base=base, drop=drop,
                    tp_link=best["link"], verified=False, note="Destino descubierto por exploración"))

    # ---------------- 4. Enviar alertas ----------------
    for msg in pending_alerts:
        if telegram(msg):
            stats["alertas"] += 1
        time.sleep(1)

    # ---------------- 5. Resumen diario ----------------
    hour = cfg_a.get("resumen_diario_hora")
    if hour is not None and NOW_MADRID.hour >= int(hour) and state.get("ultimo_resumen") != TODAY.isoformat():
        lines = [f"☀️ <b>Resumen diario</b> · {fdate(TODAY)}", ""]
        recent = read_csv(GF_FILE, 1) + gf_rows
        for route in routes:
            rows = [r for r in recent if r["clave"] == route["id"] and r["precio"]]
            if rows:
                b = min(rows, key=lambda r: float(r["precio"]))
                d1, d2 = date.fromisoformat(b["salida"]), date.fromisoformat(b["regreso"])
                lines.append(f"• {html.escape(route['nombre'])}: <b>{euros(float(b['precio']))}</b> "
                             f"({fdate(d1)} → {fdate(d2)})")
            else:
                lines.append(f"• {html.escape(route['nombre'])}: sin datos verificados hoy")
        total_g = stats["google_ok"] + stats["google_fail"]
        if total_g and stats["google_fail"] / total_g > 0.5:
            lines.append("\n⚠️ Google Flights está fallando en muchas consultas. Avísale a Claude.")
        if not TP_TOKEN:
            lines.append("\n⚠️ Falta el secreto TRAVELPAYOUTS_TOKEN.")
        lines.append("\nPrecios más bajos vistos en las últimas 24 h, con tu equipaje incluido.")
        if telegram("\n".join(lines)):
            state["ultimo_resumen"] = TODAY.isoformat()

    # ---------------- 6. Guardar ----------------
    append_csv(TP_FILE, TP_FIELDS, tp_rows)
    append_csv(GF_FILE, GF_FIELDS, gf_rows)
    if run_n % 8 == 0:  # una vez al día, limpiar datos de más de 150 días
        prune_csv(TP_FILE, TP_FIELDS, 150)
        prune_csv(GF_FILE, GF_FIELDS, 150)
    LATEST_FILE.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")
    save_state(state)
    log(f"Listo. Google OK: {stats['google_ok']}, fallos: {stats['google_fail']}, "
        f"alertas enviadas: {stats['alertas']}")


if __name__ == "__main__":
    main()
