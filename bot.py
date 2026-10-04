#!/usr/bin/env python3
"""
BOT DE TELEGRAM DEL RADAR DE VUELOS
===================================
GitHub lo ejecuta cada ~5 minutos: lee tus mensajes nuevos, responde
y guarda los cambios en data/ajustes_bot.json (que el radar lee).
Solo responde a tu Chat ID; ignora a cualquier otra persona.
"""

import csv
import html
import json
import re
import statistics
import unicodedata
from datetime import date, datetime, timedelta

import requests

import radar
from radar import (DATA, GF_FILE, MADRID, NOW_ISO, NOW_UTC, TG_CHAT, TG_TOKEN, TODAY,
                   AJUSTES_FILE, build_gf_index, euros, fdate, market_stats, window_reference, google_price,
                   load_config, load_overrides, read_csv, window_of)

BOT_STATE = DATA / "bot_estado.json"
COMPRAS_FILE = DATA / "compras.csv"
COMPRAS_FIELDS = ["fecha", "clave", "nombre", "precio", "normal", "ahorro", "salida"]
OBJETIVO_AHORRO_ANUAL = 250

ALIAS = {
    "ny": "NYC", "nueva york": "NYC", "new york": "NYC",
    "cdmx": "MEX", "mexico": "MEX", "ciudad de mexico": "MEX", "df": "MEX",
    "hvar": "SPU", "split": "SPU", "roma": "ROM", "rome": "ROM",
    "munich": "MUC", "berlin": "BER", "amsterdam": "AMS", "lisboa": "LIS", "lisbon": "LIS",
    "dublin": "DUB", "cracovia": "KRK", "krakow": "KRK", "bucarest": "BUH", "bucharest": "BUH",
    "barcelona": "BCN", "bcn": "BCN", "bogota": "BOG", "miami": "MIA", "ibiza": "IBZ",
    "mykonos": "JMK", "budapest": "BUD",
}

AYUDA = """🤖 <b>Comandos del radar</b>

<b>Consultar</b>
/precio <i>ciudad</i> · mejores precios recientes
/buscar <i>ciudad ida vuelta</i> · precio ahora mismo
   ej: /buscar lisboa 13/11 16/11
/rutas · tus rutas y objetivos
/resumen · el resumen por ventanas, ahora
   /resumen corto · solo una ventana (ultimo, corto, medio, largo)
/estado · cómo está funcionando el sistema

<b>Precios objetivo</b>
/objetivo <i>ciudad precio</i> · ej: /objetivo lisboa 90
   Desde otra ciudad: /objetivo desde berlin 150
/objetivos · ver todos
/borrar_objetivo <i>ciudad</i>
/sugerir <i>ciudad</i> · te propongo un objetivo con datos reales

<b>Rutas</b>
/anadir <i>código</i> · ej: /anadir OPO
/quitar <i>ciudad</i>
/prioridad <i>ciudad alta|media</i>

<b>Alertas</b>
/pausar <i>días</i> · ej: /pausar 3
/reanudar

<b>Ahorro</b>
/compre <i>ciudad precio [fecha ida]</i> · ej: /compre bogota 640 15/12
/ahorro · cuánto llevas ahorrado este año

Las respuestas pueden tardar unos minutos: reviso tus mensajes cada ~5 min."""


# ------------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------------
def norm(text):
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", t.lower()).strip()


def send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text[:3900], "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=30)
    except Exception as e:
        print(f"No se pudo responder ({type(e).__name__})")


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _match_city(q, code, name):
    return q.upper() == (code or "") or ALIAS.get(q) == code or (q and norm(name).startswith(q))


def find_route(cfg, query):
    """Madrid → ciudad: 'berlin' · Ciudad → Madrid: 'desde berlin', 'berlin madrid' o 'ber-mad'."""
    q = norm(query).replace("-", " ")
    if not q:
        return None
    reverse, forward = [r for r in cfg["rutas"] if radar.is_reverse(r)], \
        [r for r in cfg["rutas"] if not radar.is_reverse(r)]
    m = re.fullmatch(r"(?:desde|de) (.+)", q) or re.fullmatch(r"(.+?) (?:a )?(?:madrid|mad)", q)
    if m:
        city = m.group(1).strip()
        for r in reverse:
            if _match_city(city, r.get("origen_ciudad"), r.get("origen_nombre", "")) or \
                    _match_city(city, r.get("origen_aeropuerto"), r.get("origen_nombre", "")):
                return r
        return None
    code = ALIAS.get(q, q.upper())
    for r in forward:
        if code in (r["id"], r.get("codigo_ciudad"), r.get("aeropuerto")):
            return r
    for r in forward:
        if norm(r["nombre"]).startswith(q) or q in norm(r["nombre"]):
            return r
    return None


def parse_date(txt):
    txt = txt.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(txt, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", txt)
    if m:
        d = date(TODAY.year, int(m.group(2)), int(m.group(1)))
        return d if d >= TODAY else date(TODAY.year + 1, d.month, d.day)
    return None


def split_city_number(args):
    """'ciudad de mexico 800' -> ('ciudad de mexico', 800.0)"""
    parts = args.split()
    if len(parts) < 2:
        return None, None
    try:
        return " ".join(parts[:-1]), float(parts[-1].replace("€", "").replace(",", "."))
    except ValueError:
        return None, None


def gf_index_for(cfg, days=None):
    return build_gf_index(read_csv(GF_FILE, days or cfg["alertas"]["dias_historial"]), cfg["ventanas"])


def unique_prices(idx, route_id, windows, wid=None):
    """Precio más reciente de cada combinación de fechas distinta (sin repeticiones)."""
    latest = {}
    for (k, w), entries in idx.items():
        if k != route_id or (wid and w != wid):
            continue
        for e in entries:
            if e["par"] not in latest or e["ts"] > latest[e["par"]]["ts"]:
                latest[e["par"]] = e
    return [e["precio"] for e in latest.values()]


def route_prices(cfg, route_id, days):
    rows = read_csv(GF_FILE, days)
    return [r for r in rows if r["clave"] == route_id and r.get("precio")]


# ------------------------------------------------------------------
# Comandos
# ------------------------------------------------------------------
def cmd_rutas(cfg, ov, args):
    lines = ["🗺 <b>Tus rutas</b>"]
    groups = (("Prioritarias", lambda r: not radar.is_reverse(r) and r.get("prioridad", "media") == "alta"),
              ("Secundarias", lambda r: not radar.is_reverse(r) and r.get("prioridad", "media") == "media"),
              ("Ida y vuelta desde otras ciudades", radar.is_reverse))
    for title, in_group in groups:
        rs = [r for r in cfg["rutas"] if in_group(r)]
        if rs:
            lines.append(f"\n<b>{title}</b>")
            for r in rs:
                t = f" · 🎯 {euros(float(r['precio_objetivo']))}" if r.get("precio_objetivo") else ""
                name = radar.route_label(r) if radar.is_reverse(r) else f"{r['nombre']} ({r['id']})"
                lines.append(f"• {html.escape(name)}{t}")
    return "\n".join(lines)


def cmd_precio(cfg, ov, args):
    r = find_route(cfg, args)
    if not r:
        return "No encontré esa ruta. Escribe /rutas para ver las disponibles."
    windows = cfg["ventanas"]
    cal_recent = [x for x in read_csv(radar.CAL_FILE, 2) if x["clave"] == r["id"]]
    ver_recent = [x for x in read_csv(GF_FILE, 2) if x["clave"] == r["id"] and x.get("precio")
                  and x.get("tipo") in ("verificacion", "candidato", "muestra")]
    if cal_recent or ver_recent:
        gf30 = read_csv(GF_FILE, 30)
        lines = [f"💶 <b>{html.escape(radar.route_label(r))}</b> · últimas 48 h\n"]
        for wid, w in windows.items():
            rows = [x for x in ver_recent
                    if window_of(date.fromisoformat(x["salida"]), windows,
                                 datetime.fromisoformat(x["ts"]).astimezone(MADRID).date()) == wid]
            crow = [x for x in cal_recent if x["ventana"] == wid]
            if not rows and not crow:
                continue
            if rows:
                b = min(rows, key=lambda x: float(x["precio"]))
                d1, d2 = date.fromisoformat(b["salida"]), date.fromisoformat(b["regreso"])
                line = f"<b>{w['nombre']}</b>: {euros(float(b['precio']))} ({fdate(d1)} → {fdate(d2)}) ✅"
            else:
                b = min(crow, key=lambda x: float(x["minimo"]))
                d1, d2 = date.fromisoformat(b["mejor_salida"]), date.fromisoformat(b["mejor_regreso"])
                line = f"<b>{w['nombre']}</b>: ~{euros(float(b['minimo']))} ({fdate(d1)} → {fdate(d2)}) ⚠️ sin verificar"
            vst = radar.verified_stats(gf30, r["id"], wid, windows, cfg["alertas"])
            if vst:
                line += (f"\n   Lo habitual: ~{euros(vst['habitual'])} · "
                         f"lo más barato en 30 días: {euros(vst['minimo_historico'])}")
            lines.append(line)
        lines.append("\n✅ = verificado con tu equipaje, tu duración máxima y sin billetes separados")
        if r.get("precio_objetivo"):
            lines.append(f"🎯 Tu objetivo: {euros(float(r['precio_objetivo']))}")
        return "\n".join(lines)
    recent = route_prices(cfg, r["id"], 2)
    if not recent:
        return f"Aún no tengo precios recientes de {html.escape(r['nombre'])}. Prueba con /buscar."
    idx = gf_index_for(cfg)
    lines = [f"💶 <b>{html.escape(radar.route_label(r))}</b> · últimas 48 h\n"]
    for wid, w in windows.items():
        rows = [x for x in recent if window_of(date.fromisoformat(x["salida"]), windows) == wid]
        if not rows:
            continue
        b = min(rows, key=lambda x: float(x["precio"]))
        d1, d2 = date.fromisoformat(b["salida"]), date.fromisoformat(b["regreso"])
        typ = window_reference(idx, r["id"], wid, windows, cfg["alertas"])
        ref = f"\n   Precio habitual: ~{euros(typ)}" if typ else ""
        lines.append(f"<b>{w['nombre']}</b>: {euros(float(b['precio']))} ({fdate(d1)} → {fdate(d2)}){ref}")
    if r.get("precio_objetivo"):
        lines.append(f"\n🎯 Tu objetivo: {euros(float(r['precio_objetivo']))}")
    return "\n".join(lines)


def cmd_buscar(cfg, ov, args):
    parts = args.split()
    if len(parts) < 3:
        return "Uso: /buscar ciudad ida vuelta\nEj: /buscar lisboa 13/11 16/11"
    d1, d2 = parse_date(parts[-2]), parse_date(parts[-1])
    r = find_route(cfg, " ".join(parts[:-2]))
    if not r:
        return "No encontré esa ruta. Escribe /rutas para ver las disponibles."
    if not d1 or not d2 or d2 <= d1 or d1 <= TODAY:
        return "No entendí las fechas. Usa el formato día/mes, ej: 13/11 16/11"
    profile = cfg["perfiles"][r["perfil"]]
    max_dur = (int(r["duracion_directo_min"] * profile.get("factor_duracion", 3))
               if r.get("duracion_directo_min") else None)
    res = google_price(cfg, r.get("origen_aeropuerto") or cfg["origen"]["aeropuerto"], r["aeropuerto"],
                       d1, d2, profile, max_dur)
    if not res["ok"]:
        return "Google Flights no respondió ahora mismo. Inténtalo de nuevo en unos minutos."
    if not res["price"]:
        return f"No encontré vuelos que cumplan tus reglas para esas fechas ({html.escape(radar.route_label(r))})."
    lines = [f"🔎 <b>{html.escape(radar.route_label(r))}</b>",
             f"📅 {fdate(d1)} → {fdate(d2)}",
             f"💶 <b>{euros(res['price'])}</b> ida y vuelta ({radar.bag_text(profile)})"]
    wid = window_of(d1, cfg["ventanas"])
    if wid:
        base, _, recent_best = market_stats(gf_index_for(cfg), r["id"], wid, cfg["ventanas"], cfg["alertas"],
                                            lead=(d1 - TODAY).days, exclude=(d1.isoformat(), d2.isoformat()))
        if recent_best:
            lines.append(f"🔻 Lo más barato visto esta semana en esa ventana: {euros(recent_best)}")
        if base:
            diff = 1 - res["price"] / base
            word = "por debajo" if diff >= 0 else "por encima"
            lines.append(f"📊 {abs(diff):.0%} {word} de lo normal para fechas parecidas (~{euros(base)})")
    v = res.get("vuelo")
    if v:
        via = f" · escala en {', '.join(v['via'])}" if v.get("via") else " · directo"
        lines.append(f"🛫 Ida: sale {v['sale']} → llega {v['llega']}{via}")
    if res.get("airlines"):
        lines.append("✈️ " + html.escape(", ".join(res["airlines"][:2])))
    lines.append(f'🔗 <a href="{html.escape(res["url"])}">Ver en Google Flights</a>')
    return "\n".join(lines)


def cmd_objetivo(cfg, ov, args):
    city, price = split_city_number(args)
    if not city:
        return "Uso: /objetivo ciudad precio\nEj: /objetivo lisboa 90"
    r = find_route(cfg, city)
    if not r:
        return "No encontré esa ruta. Escribe /rutas para ver las disponibles."
    ov.setdefault("objetivos", {})[r["id"]] = price
    return (f"🎯 Listo. Te aviso si {html.escape(radar.route_label(r))} ida y vuelta baja de "
            f"<b>{euros(price)}</b> ({radar.bag_text(cfg['perfiles'][r['perfil']])}).")


def cmd_objetivos(cfg, ov, args):
    rs = [r for r in cfg["rutas"] if r.get("precio_objetivo")]
    if not rs:
        return "No tienes objetivos activos. Crea uno con /objetivo ciudad precio."
    return "🎯 <b>Tus objetivos</b>\n" + "\n".join(
        f"• {html.escape(radar.route_label(r))}: {euros(float(r['precio_objetivo']))}" for r in rs)


def cmd_borrar_objetivo(cfg, ov, args):
    r = find_route(cfg, args)
    if not r:
        return "No encontré esa ruta."
    ov.setdefault("objetivos", {})[r["id"]] = None
    return f"🗑 Objetivo de {html.escape(radar.route_label(r))} eliminado."


def cmd_sugerir(cfg, ov, args):
    r = find_route(cfg, args)
    if not r:
        return "No encontré esa ruta."
    cal = [float(x["minimo"]) for x in read_csv(radar.CAL_FILE, 30) if x["clave"] == r["id"]]
    prices = sorted(cal) if len(cal) >= 20 else sorted(unique_prices(gf_index_for(cfg, 30), r["id"], cfg["ventanas"]))
    if len(prices) < 20:
        return (f"Aún tengo pocos datos de {html.escape(r['nombre'])} ({len(prices)} precios). "
                "Pregúntame de nuevo en unos días.")
    p10 = prices[int(len(prices) * 0.10)]
    p25 = prices[int(len(prices) * 0.25)]
    step = 10 if r["perfil"] == "largo" else 5
    target = int(p10 // step * step)
    return (f"📊 <b>{html.escape(radar.route_label(r))}</b> · últimos 30 días ({len(prices)} fechas distintas)\n"
            f"• Mínimo visto: {euros(prices[0])}\n"
            f"• El 10% más barato: por debajo de {euros(p10)}\n"
            f"• El 25% más barato: por debajo de {euros(p25)}\n"
            f"• Precio típico: {euros(statistics.median(prices))}\n\n"
            f"Sugerencia: <b>/objetivo {('desde ' + norm(r['origen_nombre'])) if radar.is_reverse(r) else r['id'].lower()} {target}</b>\n"
            "Así solo te avisaré de precios que aparecen en muy pocas ocasiones.")


def cmd_anadir(cfg, ov, args):
    code = args.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        return "Uso: /anadir CÓDIGO (código de 3 letras del aeropuerto)\nEj: /anadir OPO"
    if any(r["id"] == code for r in cfg["rutas"]):
        return "Esa ruta ya está en tu lista."
    cities = radar.load_cities()
    info = cities.get(code)
    if not info:
        return "No reconozco ese código. Busca el código IATA del aeropuerto (ej: OPO para Oporto)."
    perfil = "europa" if info["country"] in radar.EUROPA else "largo"
    route = {"id": code, "nombre": info["name"], "codigo_ciudad": code, "aeropuerto": code,
             "perfil": perfil, "prioridad": "media", "duracion_directo_min": None,
             "precio_objetivo": None}
    ov.setdefault("rutas_extra", []).append(route)
    if code in ov.get("quitadas", []):
        ov["quitadas"].remove(code)
    return (f"✅ Añadida Madrid → {html.escape(info['name'])} ({code}) como secundaria, "
            f"perfil {'Europa (maleta de cabina)' if perfil == 'europa' else 'largo (maleta de 23 kg)'}.\n"
            "Empezará a vigilarse en la próxima ejecución del radar.")


def cmd_quitar(cfg, ov, args):
    r = find_route(cfg, args)
    if not r:
        return "No encontré esa ruta."
    extra = ov.get("rutas_extra", [])
    if any(x["id"] == r["id"] for x in extra):
        ov["rutas_extra"] = [x for x in extra if x["id"] != r["id"]]
    else:
        ov.setdefault("quitadas", []).append(r["id"])
    return f"🗑 {html.escape(r['nombre'])} ya no se vigilará. Puedes volver a añadirla con /anadir {r['id']}."


def cmd_prioridad(cfg, ov, args):
    parts = args.split()
    if len(parts) < 2 or norm(parts[-1]) not in ("alta", "media"):
        return "Uso: /prioridad ciudad alta|media"
    r = find_route(cfg, " ".join(parts[:-1]))
    if not r:
        return "No encontré esa ruta."
    ov.setdefault("prioridades", {})[r["id"]] = norm(parts[-1])
    return f"✅ {html.escape(r['nombre'])} ahora tiene prioridad {norm(parts[-1])}."


def cmd_pausar(cfg, ov, args):
    try:
        days = float(args.strip() or 1)
    except ValueError:
        return "Uso: /pausar días  (ej: /pausar 3)"
    until = NOW_UTC + timedelta(days=days)
    ov["pausa_hasta"] = until.isoformat(timespec="seconds")
    return (f"⏸ Alertas en pausa hasta el {fdate(until.astimezone(MADRID).date())} "
            f"a las {until.astimezone(MADRID):%H:%M}. El radar sigue aprendiendo precios.\n"
            "Usa /reanudar para volver antes.")


def cmd_reanudar(cfg, ov, args):
    ov.pop("pausa_hasta", None)
    return "▶️ Alertas reactivadas."


VENTANA_ALIAS = {"ultimo": "ultimo_minuto", "ultimo minuto": "ultimo_minuto", "um": "ultimo_minuto",
                 "corto": "corto", "medio": "medio", "largo": "largo"}


def cmd_resumen(cfg, ov, args):
    """/resumen → los 4 mensajes · /resumen corto → solo esa ventana · /resumen total → uno solo."""
    a = norm(args)
    if a in ("total", "general", "todo junto"):
        return radar.summary_text(cfg, titulo=f"📋 <b>Resumen</b> · {fdate(TODAY)}")
    wid = VENTANA_ALIAS.get(a) or (a if a in cfg["ventanas"] else None)
    if a and not wid:
        return "Uso: /resumen · /resumen ultimo · /resumen corto · /resumen medio · /resumen largo · /resumen total"
    msgs = radar.summary_messages(cfg, titulo="📋 <b>Resumen</b>", solo_ventana=wid)
    for m in msgs[:-1]:
        send(m)
    return msgs[-1]


def cmd_estado(cfg, ov, args):
    st = load_json(radar.STATE_FILE, {})
    last = st.get("ultima_ejecucion")
    lines = ["⚙️ <b>Estado del sistema</b>"]
    if last:
        t = datetime.fromisoformat(last["fecha"]).astimezone(MADRID)
        lines.append(f"• Última búsqueda: {fdate(t.date())} a las {t:%H:%M}")
        lines.append(f"• Calendario: {last.get('calendario_consultas', 0)} consultas "
                     f"({last.get('calendario_fallos', 0)} fallidas), {last.get('calendario_rutas', 0)} rutas cubiertas")
        if last.get("calendario_presupuesto"):
            lines.append(f"• Presupuesto actual: {last['calendario_presupuesto']} consultas por cada uno de los 4 trabajos")
        lines.append(f"• Consultas detalladas a Google: {last['google_ok']} correctas, {last['google_fallos']} fallidas")
    lines.append(f"• Rutas vigiladas: {len(cfg['rutas'])}")
    p = radar.paused_until(cfg)
    lines.append(f"• Alertas: {'en pausa' if p else 'activas'}")
    return "\n".join(lines)


def cmd_compre(cfg, ov, args):
    parts = args.split()
    dep = parse_date(parts[-1]) if len(parts) >= 3 else None
    if dep:
        parts = parts[:-1]
    city, price = split_city_number(" ".join(parts))
    if not city:
        return "Uso: /compre ciudad precio [fecha ida]\nEj: /compre bogota 640 15/12"
    r = find_route(cfg, city)
    if not r:
        return "No encontré esa ruta."
    windows = cfg["ventanas"]
    wid = window_of(dep, windows) if dep else None
    cal = [float(x["mediana"]) for x in read_csv(radar.CAL_FILE, 30)
           if x["clave"] == r["id"] and (not wid or x["ventana"] == wid)]
    vals = cal if len(cal) >= 5 else unique_prices(gf_index_for(cfg, 30), r["id"], windows, wid)
    normal = statistics.median(vals) if len(vals) >= (5 if cal else 10) else None
    ahorro = round(normal - price, 2) if normal else None
    new = not COMPRAS_FILE.exists()
    with open(COMPRAS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COMPRAS_FIELDS)
        if new:
            w.writeheader()
        w.writerow({"fecha": TODAY.isoformat(), "clave": r["id"], "nombre": r["nombre"], "precio": price,
                    "normal": round(normal, 2) if normal else "", "ahorro": ahorro if ahorro is not None else "",
                    "salida": dep.isoformat() if dep else ""})
    msg = f"🧾 Compra registrada: {html.escape(radar.route_label(r))} por {euros(price)}."
    if ahorro is not None:
        msg += (f"\nEl precio habitual era ~{euros(normal)}: "
                + (f"<b>ahorraste {euros(ahorro)}</b> 🎉" if ahorro > 0 else f"pagaste {euros(-ahorro)} más de lo habitual."))
    else:
        msg += "\nAún no tengo datos suficientes de esta ruta para calcular el ahorro."
    return msg


def cmd_ahorro(cfg, ov, args):
    if not COMPRAS_FILE.exists():
        return "Aún no has registrado compras. Usa /compre ciudad precio cuando compres un vuelo."
    with open(COMPRAS_FILE, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["fecha"][:4] == str(TODAY.year)]
    if not rows:
        return f"No hay compras registradas en {TODAY.year}."
    total = sum(float(r["ahorro"]) for r in rows if r["ahorro"])
    lines = [f"💰 <b>Ahorro {TODAY.year}</b>\n"]
    for r in rows:
        a = f" · ahorro {euros(float(r['ahorro']))}" if r["ahorro"] else ""
        lines.append(f"• {html.escape(r['nombre'])}: {euros(float(r['precio']))}{a}")
    pct = min(total / OBJETIVO_AHORRO_ANUAL, 1) if total > 0 else 0
    bar = "▓" * round(pct * 10) + "░" * (10 - round(pct * 10))
    lines.append(f"\n<b>Total ahorrado: {euros(total)}</b>")
    lines.append(f"{bar} {pct:.0%} de tu meta de {euros(OBJETIVO_AHORRO_ANUAL)}")
    return "\n".join(lines)


COMMANDS = {
    "start": lambda c, o, a: AYUDA, "ayuda": lambda c, o, a: AYUDA, "help": lambda c, o, a: AYUDA,
    "rutas": cmd_rutas, "precio": cmd_precio, "buscar": cmd_buscar,
    "objetivo": cmd_objetivo, "objetivos": cmd_objetivos, "borrar_objetivo": cmd_borrar_objetivo,
    "sugerir": cmd_sugerir, "anadir": cmd_anadir, "añadir": cmd_anadir, "quitar": cmd_quitar,
    "prioridad": cmd_prioridad, "pausar": cmd_pausar, "reanudar": cmd_reanudar,
    "resumen": cmd_resumen, "estado": cmd_estado, "compre": cmd_compre, "compré": cmd_compre,
    "ahorro": cmd_ahorro,
}


# ------------------------------------------------------------------
# Programa principal
# ------------------------------------------------------------------
def main():
    if not (TG_TOKEN and TG_CHAT):
        print("Faltan los secretos de Telegram.")
        return
    DATA.mkdir(exist_ok=True)
    st = load_json(BOT_STATE, {})
    offset = st.get("offset")
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"timeout": 0, **({"offset": offset} if offset else {})}, timeout=30)
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"No se pudieron leer mensajes ({type(e).__name__})")
        return
    if not updates:
        print("Sin mensajes nuevos.")
        return

    ov = load_overrides()
    for u in updates:
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != TG_CHAT:
            continue  # solo te responde a ti
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            send("Escribe /ayuda para ver lo que puedo hacer.")
            continue
        head, _, args = text[1:].partition(" ")
        name = norm(head.split("@")[0]).replace("-", "_")
        fn = COMMANDS.get(name) or COMMANDS.get(head.split("@")[0].lower())
        if not fn:
            send("No conozco ese comando. Escribe /ayuda para ver la lista.")
            continue
        cfg = radar.apply_overrides(load_config(raw=True), ov)
        try:
            reply = fn(cfg, ov, args.strip())
        except Exception as e:
            reply = f"Algo falló procesando el comando ({type(e).__name__}). Avísale a Claude."
        send(reply)
        print(f"Comando /{name} procesado")

    # Confirmar a Telegram que ya se leyeron (así no se repiten aunque algo falle después)
    try:
        requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                     params={"offset": st["offset"], "timeout": 0}, timeout=30)
    except Exception:
        pass
    save_json(AJUSTES_FILE, ov)
    save_json(BOT_STATE, st)


if __name__ == "__main__":
    main()
