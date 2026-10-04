#!/usr/bin/env python3
"""Diagnóstico del calendario de Google: ¿qué filtro reduce los resultados?"""
import time
from datetime import date, timedelta

from fli.core import build_date_search_segments
from fli.models import Airport, BagsFilter, DateSearchFilters, MaxStops, PassengerInfo, SeatType
from fli.search import SearchDates

HOY = date.today()


def prueba(nombre, orig, dest, desde, hasta, estancia, bolsas, max_dur, moneda="EUR"):
    segs, trip = build_date_search_segments(Airport[orig], Airport[dest], desde.isoformat(),
                                            trip_duration=estancia, is_round_trip=True)
    f = DateSearchFilters(trip_type=trip, passenger_info=PassengerInfo(adults=1), flight_segments=segs,
                          stops=MaxStops.ANY, seat_type=SeatType.ECONOMY, max_duration=max_dur, bags=bolsas,
                          from_date=desde.isoformat(), to_date=hasta.isoformat(), duration=estancia)
    t0 = time.time()
    try:
        kw = {"currency": moneda, "language": "es", "country": "ES"} if moneda else {}
        res = SearchDates().search(f, **kw) or []
        monedas = sorted({str(r.currency) for r in res})
        ej = ", ".join(f"{r.date[0]:%d/%m}:{r.price:.0f}" for r in sorted(res, key=lambda r: r.price)[:3])
        print(f"{nombre:42s} → {len(res):3d} precios · monedas {monedas} · más baratos {ej} "
              f"({time.time() - t0:.1f} s)", flush=True)
    except Exception as e:
        print(f"{nombre:42s} → ERROR {type(e).__name__}: {str(e)[:150]}", flush=True)
    time.sleep(2)


cabina = BagsFilter(checked_bags=0, carry_on=True)
d1, d2 = HOY + timedelta(days=50), HOY + timedelta(days=110)
print(f"Rango probado: {d1} → {d2} (61 fechas de salida), estancia 4 días\n")
prueba("1. MAD-MUC sin filtros", "MAD", "MUC", d1, d2, 4, None, None)
prueba("2. MAD-MUC + maleta de cabina", "MAD", "MUC", d1, d2, 4, cabina, None)
prueba("3. MAD-MUC + duración máx. 480 min", "MAD", "MUC", d1, d2, 4, None, 480)
prueba("4. MAD-MUC + cabina + duración (como el radar)", "MAD", "MUC", d1, d2, 4, cabina, 480)
prueba("5. MAD-MUC sin moneda/idioma/país", "MAD", "MUC", d1, d2, 4, None, None, moneda=None)
prueba("6. MAD-BCN sin filtros", "MAD", "BCN", d1, d2, 4, None, None)
prueba("7. MAD-BCN como el radar", "MAD", "BCN", d1, d2, 4, cabina, 255)
prueba("8. MAD-BOG sin filtros (16 días)", "MAD", "BOG", d1, d2, 16, None, None)
prueba("9. MAD-BOG como el radar (23 kg)", "MAD", "BOG", d1, d2, 16,
       BagsFilter(checked_bags=1, carry_on=True), 1280)
prueba("10. Repetición de la prueba 1 (¿límite?)", "MAD", "MUC", d1, d2, 4, None, None)
