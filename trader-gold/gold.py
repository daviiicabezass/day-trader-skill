#!/usr/bin/env python3
"""trader-gold: day trading de oro (XAUUSD) para MetaTrader 5. Solo biblioteca estandar.

    python3 gold.py clock                                   # mercado del oro y sesiones de Londres / Nueva York
    python3 gold.py orb --session london                    # ruptura del rango de apertura de Londres (08:00)
    python3 gold.py orb --session ny --write                # Nueva York (08:20, COMEX) -> trader/gold-entry.json
    python3 gold.py orb --session london --csv XAUUSDM5.csv # con las velas exportadas de tu MT5
    python3 gold.py plan --stop or-mid --target 2R --write  # -> trader/gold-plan.json
    python3 gold.py replay --write                          # 10.000 repeticiones + historial -> trader/gold-replay.json
    python3 gold.py size --write                            # lotes -> trader/gold-order.json
    python3 gold.py ticket                                  # la orden explicada para colocarla a mano en MT5

Nada de esto coloca una orden. El ticket se coloca a mano en MetaTrader 5, si quieres.

Datos:
  - CSV exportado de MT5 (recomendado: son los precios de TU broker). En MT5: Ver > Simbolos >
    Barras, XAUUSD, M5 o M1, Exportar. La hora del CSV es la del servidor del broker: --server-tz
    "ny+7" (por defecto, la convencion GMT+2 / GMT+3 que cierra la semana a las 17:00 de Nueva
    York), o un desfase fijo como "+2", "0" o "-5".
  - Yahoo Finance GC=F (futuros del oro en COMEX, gratuito y no oficial). El futuro cotiza unos
    dolares por encima del spot: --basis -25 resta 25 a todos los precios para acercarlo al XAUUSD
    de tu broker. Las distancias (riesgo, objetivo) valen igual; los niveles exactos, mejor del CSV.
Las velas de 1 minuto se agrupan en velas de 5 minutos.

Sesiones (configurables en trader/gold.json, "sessions"):
  london  apertura 08:00 hora de Londres, fuera a las 16:00
  ny      apertura 08:20 hora de Nueva York (COMEX), fuera a las 13:25
  tarde   apertura 09:30 hora de Nueva York (15:30 Espana), fuera a las 13:55
Sin hora limite de entrada: vale la primera ruptura hasta la ultima vela antes del cierre forzoso.
Para poner una, "no_entries_after": "HH:MM" en la sesion de trader/gold.json.
El horario de verano de Londres (ultimo domingo de marzo a ultimo domingo de octubre, 01:00 UTC)
y de Nueva York (segundo domingo de marzo a primer domingo de noviembre) se calcula aqui.
"""

import argparse
import bisect
import datetime
import gzip
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "day-trader-skill/2.0 (trader-gold)"
FIVE = datetime.timedelta(minutes=5)
TRADER = "trader"
DIG = 2          # decimales del precio del simbolo activo (2 en el oro, 5 en EURUSD/GBPUSD)
PREFIX = "gold"  # prefijo de los archivos en trader/: gold-entry.json, eurusd-entry.json...

# Simbolos que sabe operar. Un lote de oro son 100 onzas; uno de EURUSD o GBPUSD, 100.000 de la divisa base,
# y cada pip (0,0001) vale 10 $ por lote. Spread y comision: FTMO (cuenta Standard, noviembre de 2026).
SYMBOLS = {
    "XAUUSD": {"symbol": "XAUUSD", "yahoo_symbol": "GC=F", "contract_size": 100, "digits": 2, "spread": 0.30,
               "commission_per_lot": 0.0, "leverage": 50, "unit": "onzas", "futures": True, "prefix": "gold"},
    "EURUSD": {"symbol": "EURUSD", "yahoo_symbol": "EURUSD=X", "contract_size": 100000, "digits": 5,
               "spread": 0.00003, "commission_per_lot": 5.0, "leverage": 100, "unit": "unidades", "futures": False,
               "basis": 0.0, "prefix": "eurusd"},
    "GBPUSD": {"symbol": "GBPUSD", "yahoo_symbol": "GBPUSD=X", "contract_size": 100000, "digits": 5,
               "spread": 0.00005, "commission_per_lot": 5.0, "leverage": 100, "unit": "unidades", "futures": False,
               "basis": 0.0, "prefix": "gbpusd"},
}


def fp(x):
    """Un precio con los decimales del simbolo."""
    return "%.*f" % (DIG, x)


def rp(x):
    return round(x, DIG)


def apply_symbol(cfg, name):
    """Mezcla el preajuste del simbolo con la configuracion (lo de 'symbols' en gold.json manda)."""
    global DIG, PREFIX
    name = (name or "XAUUSD").upper()
    if name not in SYMBOLS:
        raise GoldError("simbolo desconocido: %s (usa %s)" % (name, ", ".join(sorted(SYMBOLS))))
    base = dict(SYMBOLS[name])
    if name == "XAUUSD":   # el oro conserva lo que ya tuviera la configuracion de nivel superior
        for k in ("symbol", "yahoo_symbol", "contract_size", "spread", "leverage", "commission_per_lot"):
            if k in cfg.get("_raw", {}):
                base[k] = cfg[k]
    cfg.update(base)
    cfg.update((cfg.get("symbols") or {}).get(name, {}))
    DIG, PREFIX = int(cfg["digits"]), cfg["prefix"]
    return cfg

DEFAULTS = {
    "symbol": "XAUUSD",          # el nombre en tu MT5 (XAUUSD, GOLD, XAUUSD.r...)
    "contract_size": 100,        # onzas por lote (casi siempre 100; compruebalo en la especificacion del simbolo)
    "lot_min": 0.01,
    "lot_step": 0.01,
    "lot_max": 5.0,
    "balance": 5000.0,
    "max_loss_usd": 50.0,
    "leverage": 100,
    "spread": 0.30,              # dolares por onza
    "cash_buffer_pct": 10,
    "min_reward_risk": 1.0,
    "or_minutes": 15,
    "server_tz": "ny+7",
    "yahoo_symbol": "GC=F",
    "basis": 0.0,
    "sessions": {},
    "prop": None,                # reglas de una cuenta de fondeo (FTMO...): ver PROP_FTMO_2STEP y gold.py prop
}

# FTMO 2-Step (Challenge y Verification), segun ftmo.com en noviembre de 2026. Compruebalo en tu panel.
PROP_FTMO_2STEP = {
    "name": "FTMO 2-Step",
    "initial_balance": 10000.0,
    "phase": 1,                  # 1 = FTMO Challenge (objetivo 10 %), 2 = Verification (5 %), 3 = cuenta FTMO
    "start_date": None,          # AAAA-MM-DD: solo cuentan las operaciones del diario desde ese dia
    "profit_target_pct": {"1": 10.0, "2": 5.0, "3": None},
    "max_daily_loss_pct": 5.0,   # del saldo inicial, desde el saldo a medianoche (hora de Praga = Espana)
    "max_loss_pct": 10.0,        # estatico: el equity nunca por debajo del 90 % del saldo inicial
    "min_trading_days": 4,
    # tus frenos, mas estrictos que FTMO, para no acercarte nunca a sus limites
    "daily_stop_usd": 150.0,     # deja de operar hoy si pierdes esto
    "max_losses_per_day": 2,     # o despues de 2 stops
    "safety_usd": 50.0,          # margen extra frente a los limites de FTMO
    "risk_pct": None,            # riesgo por operacion en % del saldo inicial (1 = 100 $ en 10k); manda sobre max_loss_usd
    "halve_below_pct": 5.0,      # si el saldo cae un 5 % o mas, el riesgo se reduce a la mitad hasta recuperarse
    "friday_no_entries_after_cet": "19:30",  # el oro cierra el viernes a las 22:00 de Espana: nada nuevo a menos de 2 h
    "commission_per_lot": 0.0,   # ida y vuelta, en $ por lote: miralo en MT5 (Especificacion del simbolo)
    "max_open_trades": 1,        # una sola operacion a la vez: nada de doblar el riesgo en la misma vela
}

SESSIONS = {
    "london": {"name": "Londres", "tz": "london", "open": "08:00", "no_entries_after": None, "flat_by": "16:00",
               "target": "1R", "trend_filter": True, "width_filter": False},
    "ny": {"name": "Nueva York", "tz": "ny", "open": "08:20", "no_entries_after": None, "flat_by": "13:25",
           "target": "2R", "trend_filter": True, "width_filter": False},
    # la tarde europea: de la apertura de la bolsa de EE. UU. (15:30 en Espana) a las 20:00 de Espana
    "tarde": {"name": "Tarde (15:30-20:00 Espana)", "tz": "ny", "open": "09:30", "no_entries_after": None,
              "flat_by": "13:55", "target": "1R", "trend_filter": True, "width_filter": False},
}
TZ_NAMES = {"ny": "Nueva York", "london": "Londres", "utc": "UTC", "cet": "Espana"}


class GoldError(Exception):
    pass


# ---------------------------------------------------------------- tiempo

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() - 6) % 7)


def ny_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    return -4 if start <= utc < end else -5


def london_offset(utc):
    y = utc.year
    start = datetime.datetime.combine(_last_sunday(y, 3), datetime.time(1, 0))
    end = datetime.datetime.combine(_last_sunday(y, 10), datetime.time(1, 0))
    return 1 if start <= utc < end else 0


def cet_offset(utc):
    """Hora de Praga y de Espana: la de Londres mas una (mismo cambio de horario en la UE y el Reino Unido)."""
    return london_offset(utc) + 1


OFFSETS = {"ny": ny_offset, "london": london_offset, "utc": lambda utc: 0, "cet": cet_offset}
CANDIDATES = {"ny": (-4, -5), "london": (1, 0), "utc": (0,), "cet": (2, 1)}


def to_local(utc, tz):
    return utc + datetime.timedelta(hours=OFFSETS[tz](utc))


def local_to_utc(local, tz):
    for off in CANDIDATES[tz]:
        utc = local - datetime.timedelta(hours=off)
        if OFFSETS[tz](utc) == off:
            return utc
    return local - datetime.timedelta(hours=CANDIDATES[tz][-1])


def parse_hm(text):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise GoldError("hora no valida: %r (usa HH:MM)" % (text,))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_now(text):
    """'2026-10-19 10:15' (Nueva York), o con zona al final: 'london', 'ny', 'utc'."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    parts = text.split()
    zone = "ny"
    if parts and parts[-1].lower() in ("ny", "london", "londres", "utc", "cet", "espana"):
        zone = {"londres": "london", "espana": "cet"}.get(parts[-1].lower(), parts[-1].lower())
        parts = parts[:-1]
    try:
        local = datetime.datetime.strptime(" ".join(parts), "%Y-%m-%d %H:%M")
    except ValueError:
        raise GoldError("--now no valido: %r (usa \"AAAA-MM-DD HH:MM\", opcionalmente con london, ny o utc)" % text)
    return local_to_utc(local, zone)


def hm(dt):
    return dt.strftime("%H:%M")


def market_open(utc):
    """El oro al contado / CFD: domingo 18:00 a viernes 17:00 de Nueva York, pausa diaria de 17:00 a 18:00."""
    ny = to_local(utc, "ny")
    wd, m = ny.weekday(), ny.hour * 60 + ny.minute
    if wd == 5:
        return False
    if wd == 6:
        return m >= 18 * 60
    if wd == 4 and m >= 17 * 60:
        return False
    return not (17 * 60 <= m < 18 * 60)


# ---------------------------------------------------------------- configuracion

def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise GoldError("%s no es JSON valido: %s" % (path, e))


def load_config(path=None):
    cfg = dict(DEFAULTS)
    raw = load_json(path or os.path.join(TRADER, "gold.json"))
    if raw:
        cfg.update(raw)
    cfg["_raw"] = raw or {}
    return cfg


def session_cfg(name, cfg):
    if name not in SESSIONS:
        raise GoldError("sesion desconocida: %r (usa london o ny)" % name)
    s = dict(SESSIONS[name])
    s.update((cfg.get("sessions") or {}).get(name, {}))
    s["key"] = name
    if not s.get("no_entries_after"):
        # sin hora limite: vale cualquier ruptura hasta la ultima vela antes del cierre forzoso
        f = datetime.datetime.combine(datetime.date(2000, 1, 3), parse_hm(s["flat_by"])) - FIVE
        s["no_entries_after"] = f.strftime("%H:%M")
    for k in ("open", "no_entries_after", "flat_by"):
        parse_hm(s[k])
    return s


def bounds(day, sess):
    tz = sess["tz"]
    f = lambda k: local_to_utc(datetime.datetime.combine(day, parse_hm(sess[k])), tz)
    return f("open"), f("no_entries_after"), f("flat_by")


def n_slots(sess):
    o = parse_hm(sess["open"])
    f = parse_hm(sess["flat_by"])
    mins = (f.hour * 60 + f.minute) - (o.hour * 60 + o.minute)
    if mins <= 0 or mins % 5:
        raise GoldError("la sesion %s debe durar un multiplo de 5 minutos" % sess["key"])
    return mins // 5


# ---------------------------------------------------------------- datos

def parse_server_tz(text):
    t = str(text).strip().lower()
    m = re.match(r"^ny([+-]\d+(?:\.\d+)?)$", t)
    if m:
        return ("ny", float(m.group(1)))
    if t in ("utc", "gmt"):
        return ("fixed", 0.0)
    try:
        return ("fixed", float(t))
    except ValueError:
        raise GoldError("--server-tz no valido: %r (usa ny+7, +2, 0, -5...)" % text)


def server_to_utc(dt, tzspec):
    kind, off = tzspec
    if kind == "ny":
        return local_to_utc(dt - datetime.timedelta(hours=off), "ny")
    return dt - datetime.timedelta(hours=off)


_DATE = re.compile(r"^(\d{4})[.\-/](\d{2})[.\-/](\d{2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def parse_mt5_csv(text, server_tz="ny+7"):
    """Velas exportadas de MT5: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> ... separadas por tab, coma o ;."""
    tzspec = parse_server_tz(server_tz)
    bars = []
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("<") or line[0].isalpha():
            continue
        f = [x.strip() for x in re.split(r"[\t,;]", line) if x.strip() != ""]
        m = _DATE.match(f[0]) if f else None
        if not m:
            continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if m.group(4) is not None:
            hh, mm, rest = int(m.group(4)), int(m.group(5)), f[1:]
        else:
            t = _TIME.match(f[1]) if len(f) > 1 else None
            if not t:
                continue
            hh, mm, rest = int(t.group(1)), int(t.group(2)), f[2:]
        try:
            o, h, l, c = (float(x) for x in rest[:4])
        except ValueError:
            continue
        server = datetime.datetime(y, mo, d, hh, mm)
        bars.append((server_to_utc(server, tzspec), o, h, l, c))
    if not bars:
        raise GoldError("no encontre velas en el CSV (espero fecha, hora, apertura, maximo, minimo, cierre)")
    return to_five(bars)


def parse_yahoo(text, basis=0.0):
    try:
        doc = json.loads(text)
    except ValueError:
        raise GoldError("la respuesta de Yahoo no es JSON")
    chart = doc.get("chart") or {}
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise GoldError("Yahoo no devolvio velas: %s" % (err.get("description") or "sin datos"))
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(t))
        bars.append((utc, o + basis, h + basis, l + basis, c + basis))
    if not bars:
        raise GoldError("Yahoo devolvio un grafico sin velas")
    return to_five(bars)


def to_five(bars):
    """Agrupa velas (de 1 o 5 minutos) en velas de 5 minutos. Devuelve {inicio_utc: (t, o, h, l, c, fin_datos)}."""
    out = {}
    for t, o, h, l, c in sorted(bars, key=lambda b: b[0]):
        if t.second or t.microsecond:
            continue  # el tick en vivo de Yahoo no es una vela
        start = t - datetime.timedelta(minutes=t.minute % 5)
        if start in out:
            _, o0, h0, l0, _, _ = out[start]
            out[start] = (start, o0, max(h0, h), min(l0, l), c, t)
        else:
            out[start] = (start, o, h, l, c, t)
    return out


def yahoo_url(sym, host="query2"):
    return "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m" % (host, urllib.parse.quote(sym))


def fetch_yahoo(sym):
    last = None
    for host in ("query2", "query1"):
        for attempt in (0, 1):
            req = urllib.request.Request(yahoo_url(sym, host), headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = e.code
                if e.code == 429 and attempt == 0:
                    time.sleep(5)
                    continue
                break
            except (urllib.error.URLError, OSError) as e:
                raise GoldError("no pude conectar con Yahoo: %s" % getattr(e, "reason", e))
        time.sleep(0.5)
    raise GoldError("Yahoo respondio HTTP %s. Prueba mas tarde o usa --csv con las velas de tu MT5." % last)


def read_text(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def load_bars(a, cfg):
    """Devuelve (velas, descripcion de la fuente)."""
    basis = cfg["basis"] if a.basis is None else a.basis
    if a.csv:
        tz = a.server_tz or cfg["server_tz"]
        return parse_mt5_csv(read_text(a.csv), tz), "MT5 CSV %s (hora del servidor %s)" % (os.path.basename(a.csv), tz)
    sym = cfg["yahoo_symbol"]
    text = read_text(a.offline) if a.offline else fetch_yahoo(sym)
    note = "Yahoo %s, %s (gratuito, no oficial, puede ir con retraso)" % (sym, "futuros" if cfg.get("futures") else "contado")
    if basis:
        note += ", base %+.2f" % basis
    return parse_yahoo(text, basis), note


# ---------------------------------------------------------------- redondeo

def floor_c(x):
    m = 10 ** DIG
    return round(math.floor(x * m + 1e-6) / m, DIG)


def ceil_c(x):
    m = 10 ** DIG
    return round(math.ceil(x * m - 1e-6) / m, DIG)


def round_c(x):
    m = 10 ** DIG
    return round(math.floor(x * m + 0.5 + 1e-9) / m, DIG)


def floor_step(x, step):
    n = math.floor(x / step + 1e-9)
    return round(max(n, 0) * step, 2)


# ---------------------------------------------------------------- ruptura del rango de apertura

def opening_range(idx, open_utc, k):
    rng = [idx.get(open_utc + i * FIVE) for i in range(k)]
    if any(b is None for b in rng):
        return None
    return max(b[2] for b in rng), min(b[3] for b in rng)


def orb(idx, sess, day, now_utc, or_minutes=15, side="both"):
    if or_minutes % 5 or or_minutes <= 0:
        raise GoldError("--or-minutes debe ser multiplo de 5")
    k = or_minutes // 5
    open_utc, cutoff_utc, flat_utc = bounds(day, sess)
    doc = {"session": sess["key"], "session_name": sess["name"], "date": day.isoformat(), "side": side,
           "or_minutes": or_minutes, "tz": sess["tz"]}
    if day.weekday() >= 5:
        doc["status"] = "closed"
        return doc
    if now_utc < open_utc:
        doc.update(status="not_open", opens=hm(to_local(open_utc, sess["tz"])))
        return doc
    if now_utc < open_utc + k * FIVE:
        doc["status"] = "forming"
        return doc
    rg = opening_range(idx, open_utc, k)
    if rg is None:
        doc["status"] = "no_data"
        return doc
    hi, lo = rg
    doc["opening_range"] = {"high": rp(hi), "low": rp(lo), "minutes": or_minutes}
    i, last = k, None
    while True:
        t = open_utc + i * FIVE
        end = t + FIVE
        if end > now_utc or end > cutoff_utc:
            break
        b = idx.get(t)
        i += 1
        if b is None:
            continue
        last = b
        hit = None
        if side in ("long", "both") and b[4] > hi:
            hit = "long"
        elif side in ("short", "both") and b[4] < lo:
            hit = "short"
        if hit:
            doc.update(status="triggered", side=hit, entry=rp(b[4]), entry_bar=i - 1,
                       entry_time=hm(to_local(end, sess["tz"])), entry_utc=end.isoformat() + "Z")
            return doc
    if last is not None:
        doc["last_close"] = rp(last[4])
        doc["last_bar_time"] = hm(to_local(last[0], sess["tz"]))
    doc["status"] = "expired" if now_utc >= cutoff_utc else "waiting"
    doc["cutoff"] = hm(to_local(cutoff_utc, sess["tz"]))
    return doc


def orb_report(doc, source):
    sess, tzn = doc["session_name"], TZ_NAMES[doc["tz"]]
    head = doc.get("symbol", "XAUUSD") + "  sesion de %s, %s  rango de apertura de %d minutos, velas de 5 minutos (%s)" % (
        sess, doc["date"], doc["or_minutes"], source)
    st = doc["status"]
    if st == "closed":
        return head + "\nFin de semana: no hay sesion de %s." % sess
    if st == "not_open":
        return head + "\nLa sesion de %s todavia no ha abierto (abre a las %s hora de %s)." % (sess, doc["opens"], tzn)
    if st == "forming":
        return head + "\nEl rango de apertura todavia se esta formando."
    if st == "no_data":
        return head + "\nFaltan velas del rango de apertura en los datos: no puedo calcularlo."
    r = doc["opening_range"]
    lines = [head, "Rango de apertura: maximo %s, minimo %s (%s de ancho)." % (fp(r["high"]), fp(r["low"]), fp(r["high"] - r["low"]))]
    if st == "triggered":
        lado = "LARGO (compra)" if doc["side"] == "long" else "CORTO (venta)"
        rel = "por encima del maximo" if doc["side"] == "long" else "por debajo del minimo"
        lines.append("ACTIVADO %s a las %s (%s) en %s: la vela %d cerro %s del rango." % (
            lado, doc["entry_time"], tzn, fp(doc["entry"]), doc["entry_bar"], rel))
        lines.append("No se ha colocado nada. Siguiente: el stop y el objetivo ANTES de entrar: gold.py plan.")
    elif st == "waiting":
        if "last_close" in doc:
            lc = doc["last_close"]
            lines.append("ESPERANDO  ultimo cierre %s (vela de las %s): %s bajo el maximo, %s sobre el minimo." % (
                fp(lc), doc["last_bar_time"], fp(r["high"] - lc), fp(lc - r["low"])))
        else:
            lines.append("ESPERANDO  todavia no ha cerrado ninguna vela despues del rango.")
        lines.append("Entradas hasta las %s (%s)." % (doc["cutoff"], tzn))
    elif st == "filtered":
        lines.append("FILTRADO  hoy no hay operacion en esta sesion: %s." % doc.get("filter_reason", ""))
    else:
        lines.append("CADUCADO  ninguna vela cerro fuera del rango antes de las %s (%s). Hoy no hay entrada." % (doc["cutoff"], tzn))
    return "\n".join(lines)


# ---------------------------------------------------------------- plan

def stop_from_rule(rule, side, entry, hi, lo):
    long = side == "long"
    if rule == "or-mid":
        raw = (hi + lo) / 2.0
    elif rule == "or-opposite":
        raw = lo if long else hi
    elif rule.startswith("price:"):
        raw = float(rule[6:])
    elif rule.startswith("usd:"):
        d = float(rule[4:])
        raw = entry - d if long else entry + d
    else:
        raise GoldError("--stop no valido: %r (or-mid, or-opposite, price:X, usd:X)" % rule)
    return floor_c(raw) if long else ceil_c(raw)


STOP_LABELS = {"or-mid": "punto medio del rango de apertura", "or-opposite": "el otro extremo del rango",
               "price": "tu precio", "usd": "dolares desde la entrada"}


def make_plan(entry, stop_rule="or-mid", target="2R", cfg=None):
    cfg = cfg or DEFAULTS
    if entry.get("status") != "triggered":
        raise GoldError("la entrada no se ha activado; usa --entry y --side para planificar por adelantado")
    side, e = entry["side"], float(entry["entry"])
    r = entry.get("opening_range") or {}
    if stop_rule.startswith("or-") and not r:
        raise GoldError("un stop %s necesita el rango de apertura (--or-high y --or-low)" % stop_rule)
    stop = stop_from_rule(stop_rule, side, e, r.get("high"), r.get("low"))
    sign = 1 if side == "long" else -1
    risk = rp((e - stop) * sign)
    if risk <= 0:
        raise GoldError("el stop %s esta en el lado equivocado de la entrada %s" % (fp(stop), fp(e)))
    spread = float(cfg["spread"])
    if risk < 3 * spread:
        raise GoldError("stop demasiado ajustado: %s de riesgo con un spread de %s; el ruido normal te sacaria. "
                        "Usa un stop mas amplio (or-opposite o usd:X)." % (fp(risk), fp(spread)))
    t = str(target).upper()
    if t.endswith("R"):
        rr = float(t[:-1])
        tgt = round_c(e + sign * rr * risk)
    elif t.startswith("PRICE:"):
        tgt = rp(float(t[6:]))
        rr = round((tgt - e) * sign / risk, 2)
    else:
        raise GoldError("--target no valido: %r (2R, 1.5R, price:X)" % target)
    if rr <= 0:
        raise GoldError("el objetivo %s esta en el lado equivocado de la entrada %s" % (fp(tgt), fp(e)))
    plan = dict(entry)
    plan.update(created_by="trader-gold", symbol=cfg["symbol"], entry=e, stop=stop,
                stop_rule=stop_rule.split(":")[0], stop_arg=stop_rule, stop_label=STOP_LABELS[stop_rule.split(":")[0]],
                target=tgt, target_r=rr, risk_per_oz=risk, reward_risk=rr)
    return plan


def plan_report(p):
    tzn = TZ_NAMES[p["tz"]]
    lado = "LARGO" if p["side"] == "long" else "CORTO"
    return "\n".join([
        "Plan de salida: %s %s, %s, sesion de %s" % (p["symbol"], lado, p["date"], p["session_name"]),
        "  Entrada      %s a las %s (%s)" % (fp(p["entry"]), p.get("entry_time", "?"), tzn),
        "  Stop         %s  %s" % (fp(p["stop"]), p["stop_label"]),
        "  Objetivo     %s  %sR" % (fp(p["target"]), _num(p["target_r"])),
        "  Riesgo       %s por unidad%s" % (fp(p["risk_per_oz"]), " (%.1f pips)" % (p["risk_per_oz"] * 10000) if DIG >= 4 else ""),
        "  Fuera a las  %s (%s), se gane o se pierda" % (p.get("flat_by", "?"), tzn),
        "Un stop es un plan, no una garantia: con un gap o una noticia fuerte puede ejecutarse peor.",
    ])


def _num(x):
    return ("%.2f" % x).rstrip("0").rstrip(".")


# ---------------------------------------------------------------- maquina del tiempo

def past_sessions(idx, sess, before_day, n):
    """Sesiones completas (todas las velas de 5 minutos) anteriores a before_day."""
    if not idx:
        return []
    first = to_local(min(idx), sess["tz"]).date()
    out = []
    d = first
    while d < before_day:
        if d.weekday() < 5:
            o, _, _ = bounds(d, sess)
            bars = [idx.get(o + i * FIVE) for i in range(n)]
            if all(b is not None for b in bars):
                out.append((d, bars))
        d += datetime.timedelta(days=1)
    return out


def build_pool(sessions, n):
    pool = [[] for _ in range(n)]
    for _, bars in sessions:
        for j in range(1, n):
            pc = bars[j - 1][4]
            b = bars[j]
            pool[j].append((b[1] / pc, b[2] / pc, b[3] / pc, b[4] / pc))
    return pool


def _pct(a, q):
    s = sorted(a)
    return s[min(int(q * len(s)), len(s) - 1)]


def simulate(pool, entry, stop, target, start, n, side, rr, replays=10000, seed=19, window=2):
    rng = random.Random(seed)
    long = side == "long"
    risk = abs(entry - stop)
    counts = {"target": 0, "stop": 0, "neither": 0}
    green, rs, finals = 0, [], []
    for _ in range(replays):
        p, out = entry, None
        for i in range(start, n):
            j = min(max(i + rng.randint(-window, window), 1), n - 1)
            o, h, l, c = rng.choice(pool[j])
            hi, lo = p * h, p * l
            if out is None:
                if long:
                    if lo <= stop:
                        out = "stop"
                    elif hi >= target:
                        out = "target"
                else:
                    if hi >= stop:
                        out = "stop"
                    elif lo <= target:
                        out = "target"
            p *= c
        finals.append(p)
        if out is None:
            out = "neither"
            r = (p - entry) / risk * (1 if long else -1)
        else:
            r = rr if out == "target" else -1.0
        counts[out] += 1
        rs.append(r)
        if out == "target" or (out == "neither" and r > 0):
            green += 1
    pc = lambda k: 100.0 * counts[k] / replays
    return {"replays": replays, "seed": seed, "target": pc("target"), "stop": pc("stop"),
            "neither": pc("neither"), "green": 100.0 * green / replays, "avg_r": sum(rs) / replays,
            "median": _pct(finals, 0.5), "p5": _pct(finals, 0.05), "p95": _pct(finals, 0.95)}


def history(sessions, plan, k, n, cutoff_slot):
    side, long = plan["side"], plan["side"] == "long"
    rr = plan["target_r"]
    res = {"sessions": len(sessions), "fired": 0, "target": 0, "stop": 0, "neither": 0, "r": []}
    for _, bars in sessions:
        hi = max(b[2] for b in bars[:k])
        lo = min(b[3] for b in bars[:k])
        ent = None
        for i in range(k, min(cutoff_slot, n)):
            c = bars[i][4]
            if (long and c > hi) or (not long and c < lo):
                ent = i
                break
        if ent is None:
            continue
        e = bars[ent][4]
        rule = plan.get("stop_arg", "or-mid")
        if rule.startswith("price:"):
            rule = "usd:%s" % plan["risk_per_oz"]
        stop = stop_from_rule(rule, side, e, hi, lo)
        risk = abs(e - stop)
        if risk <= 0:
            continue
        tgt = e + (rr * risk if long else -rr * risk)
        res["fired"] += 1
        out = None
        for b in bars[ent + 1:]:
            if long:
                if b[3] <= stop:
                    out = "stop"
                elif b[2] >= tgt:
                    out = "target"
            else:
                if b[2] >= stop:
                    out = "stop"
                elif b[3] <= tgt:
                    out = "target"
            if out:
                break
        if out is None:
            out = "neither"
            r = (bars[-1][4] - e) / risk * (1 if long else -1)
        else:
            r = rr if out == "target" else -1.0
        res[out] += 1
        res["r"].append(r)
    rl = res.pop("r")
    res["avg_r"] = sum(rl) / len(rl) if rl else None
    return res


def verdict(mc, rr):
    hit = mc["target"] + mc["stop"]
    if hit <= 0:
        return "Ninguna repeticion toco el stop ni el objetivo: el cierre lo decide todo."
    share = 100.0 * mc["target"] / hit
    be = 100.0 / (1 + rr)
    gap = share - be
    a = "objetivo primero %.1f %% frente a stop primero %.1f %%" % (mc["target"], mc["stop"])
    if abs(gap) < 5:
        return ("Casi como lanzar una moneda: " if abs(rr - 1) < 1e-9 else "Cerca del punto de equilibrio: ") + a + "."
    side = "el objetivo" if gap > 0 else "el stop"
    return ("Se inclina hacia %s: " if abs(gap) < 15 else "Claramente hacia %s: ") % side + a + "."


def run_replay(idx, plan, cfg, replays=10000, seed=19, window=2):
    sess = session_cfg(plan["session"], cfg)
    n = n_slots(sess)
    k = int(plan.get("or_minutes", 15)) // 5
    day = datetime.date.fromisoformat(plan["date"])
    sessions = past_sessions(idx, sess, day, n)
    if len(sessions) < 20:
        raise GoldError("solo hay %d sesiones completas de %s antes del %s; necesito al menos 20 para un conjunto justo"
                        % (len(sessions), sess["name"], plan["date"]))
    start = int(plan["entry_bar"]) + 1
    if start >= n:
        raise GoldError("la entrada es en la ultima vela de la sesion: no queda nada que simular")
    pool = build_pool(sessions, n)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], start, n, plan["side"], plan["target_r"],
                  replays, seed, window)
    o, cut, _ = bounds(day, sess)
    cutoff_slot = int((cut - o).total_seconds() // 300)
    hist = history(sessions, plan, k, n, cutoff_slot)
    return {"plan": plan, "mc": mc, "history": hist, "verdict": verdict(mc, plan["target_r"]),
            "bars_left": n - start, "flat_by": sess["flat_by"]}


def replay_report(doc):
    mc, h, p = doc["mc"], doc["history"], doc["plan"]
    tzn = TZ_NAMES[p["tz"]]
    lines = [
        "%s repeticiones del resto de la sesion de %s, %s a %s (%s, %d velas de 5 minutos), semilla %d." % (
            "{:,}".format(mc["replays"]).replace(",", "."), p["session_name"], p.get("entry_time", "?"),
            doc["flat_by"], tzn, doc["bars_left"], mc["seed"]),
        "  objetivo primero  %5.1f %%" % mc["target"],
        "  stop primero      %5.1f %%" % mc["stop"],
        "  ninguno           %5.1f %%   todavia abierta a las %s" % (mc["neither"], doc["flat_by"]),
        "  en verde          %5.1f %%   objetivo primero, o a favor a las %s" % (mc["green"], doc["flat_by"]),
        "  Precio a las %s: mediana %s, 90 %% de las repeticiones entre %s y %s." % (
            doc["flat_by"], fp(mc["median"]), fp(mc["p5"]), fp(mc["p95"])),
        "  R media: %+.3f" % mc["avg_r"],
    ]
    if h["fired"]:
        lines.append("Historial: la misma regla en %d sesiones reales se activo %d veces: %d objetivo, %d stop, %d ninguno. "
                     "R media %+.3f." % (h["sessions"], h["fired"], h["target"], h["stop"], h["neither"], h["avg_r"]))
    else:
        lines.append("Historial: la misma regla no se activo en ninguna de las %d sesiones reales." % h["sessions"])
    lines.append(doc["verdict"])
    if mc["neither"] > 40:
        lines.append("El %.1f %% de las repeticiones no toco ninguno: el precio a las %s decide esas." % (mc["neither"], doc["flat_by"]))
    lines.append("Es una simulacion con las sesiones recientes, no una prediccion. El spread no esta incluido. "
                 "Datos de empleo (NFP), IPC, la Fed o un titular pueden hacer lo que esas sesiones nunca hicieron.")
    return "\n".join(lines)


# ---------------------------------------------------------------- filtros y backtest

class Trend(object):
    """Tendencia de 1 hora: media exponencial de 20 frente a la de 50, con velas de 1 hora ya cerradas."""

    def __init__(self, idx, fast=20, slow=50, min_hours=60):
        hours = {}
        for t in sorted(idx):
            hours[t.replace(minute=0)] = idx[t][4]
        kf, ks = 2.0 / (fast + 1), 2.0 / (slow + 1)
        ef = es = None
        self.ends, self.vals = [], []
        for n, h in enumerate(sorted(hours), 1):
            c = hours[h]
            ef = c if ef is None else c * kf + ef * (1 - kf)
            es = c if es is None else c * ks + es * (1 - ks)
            self.ends.append(h + datetime.timedelta(hours=1))
            self.vals.append((ef, es, n))
        self.min_hours = min_hours

    def at(self, t):
        """'long', 'short', o None si aun no hay historia suficiente."""
        i = bisect.bisect_right(self.ends, t) - 1
        if i < 0:
            return None
        ef, es, n = self.vals[i]
        if n < self.min_hours:
            return None
        return "long" if ef > es else "short"


def median(xs):
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def width_ok(width, prior, lo=0.5, hi=2.0, lookback=20, min_prior=10):
    """El rango de hoy frente a la mediana de los ultimos 20: ni la mitad ni el doble."""
    prior = prior[-lookback:]
    if len(prior) < min_prior:
        return True, None
    m = median(prior)
    return lo * m <= width <= hi * m, m


def range_widths(idx, sess, k, before_day):
    """Anchura del rango de apertura de cada dia anterior a before_day con el rango completo."""
    if not idx:
        return []
    out = []
    d = to_local(min(idx), sess["tz"]).date()
    while d < before_day:
        if d.weekday() < 5:
            o, _, _ = bounds(d, sess)
            rg = opening_range(idx, o, k)
            if rg:
                out.append(rg[0] - rg[1])
        d += datetime.timedelta(days=1)
    return out


def trade_session(bars, k, cutoff_slot, side, stop_rule, rr):
    """La regla en una sesion completa: (lado, entrada, stop, resultado, R bruta) o None si no se activa."""
    n = len(bars)
    hi = max(b[2] for b in bars[:k])
    lo = min(b[3] for b in bars[:k])
    ent, s = None, None
    for i in range(k, min(cutoff_slot, n)):
        c = bars[i][4]
        if side in ("long", "both") and c > hi:
            ent, s = i, "long"
        elif side in ("short", "both") and c < lo:
            ent, s = i, "short"
        if ent is not None:
            break
    if ent is None:
        return None
    long = s == "long"
    e = bars[ent][4]
    stop = stop_from_rule(stop_rule, s, e, hi, lo)
    risk = abs(e - stop)
    if risk <= 0:
        return None
    tgt = e + (rr * risk if long else -rr * risk)
    out = None
    for b in bars[ent + 1:]:
        if long:
            if b[3] <= stop:
                out = "stop"
            elif b[2] >= tgt:
                out = "target"
        else:
            if b[2] >= stop:
                out = "stop"
            elif b[3] <= tgt:
                out = "target"
        if out:
            break
    if out is None:
        out = "neither"
        r = (bars[-1][4] - e) / risk * (1 if long else -1)
    else:
        r = rr if out == "target" else -1.0
    return s, e, risk, out, r


def backtest(idx, sess, cfg, rr=1.0, stop_rule="or-mid", trend=False, width=False, k=3, before_day=None):
    """La regla en todas las sesiones completas de los datos. R neta = R menos el spread (en R)."""
    n = n_slots(sess)
    spread = float(cfg["spread"])
    tr = Trend(idx) if trend else None
    days = sorted({to_local(t, sess["tz"]).date() for t in idx})
    widths = []
    res = {"sessions": 0, "fired": 0, "target": 0, "stop": 0, "neither": 0, "skipped_width": 0,
           "skipped_trend": 0, "r": [], "trades": []}
    for d in days:
        if d.weekday() >= 5 or (before_day and d >= before_day):
            continue
        o, cut, _ = bounds(d, sess)
        rg = opening_range(idx, o, k)
        if rg is None:
            continue
        w = rg[0] - rg[1]
        prior = list(widths)
        widths.append(w)
        bars = [idx.get(o + i * FIVE) for i in range(n)]
        if any(b is None for b in bars):
            continue
        res["sessions"] += 1
        if width and not width_ok(w, prior)[0]:
            res["skipped_width"] += 1
            continue
        side = "both"
        if trend:
            side = tr.at(o)
            if side is None:
                res["skipped_trend"] += 1
                continue
        cutoff_slot = int((cut - o).total_seconds() // 300)
        t = trade_session(bars, k, cutoff_slot, side, stop_rule, rr)
        if t is None:
            continue
        s, e, risk, out, r = t
        net = r - spread / risk
        res["fired"] += 1
        res[out] += 1
        res["r"].append(net)
        res["trades"].append({"date": d.isoformat(), "side": s, "entry": rp(e), "outcome": out, "r": round(net, 3)})
    rl = res.pop("r")
    res["total_r"] = sum(rl)
    res["avg_r"] = sum(rl) / len(rl) if rl else None
    res["win_pct"] = 100.0 * sum(1 for x in rl if x > 0) / len(rl) if rl else None
    # peor racha: maxima caida de la curva de R acumulada
    eq = peak = dd = 0.0
    for x in rl:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    res["max_drawdown_r"] = dd
    return res


def compare(idx, sess, cfg, k=3, stop_rule="or-mid", targets=(1.0, 1.5, 2.0)):
    rows = []
    for label, trend, width in (("sin filtros", False, False), ("tendencia", True, False),
                                ("anchura", False, True), ("tendencia+anchura", True, True)):
        for rr in targets:
            b = backtest(idx, sess, cfg, rr, stop_rule, trend, width, k)
            rows.append((label, rr, b))
    return rows


def compare_report(rows, sess):
    lines = ["Backtest de la sesion de %s (R neta de spread, stop en %s):" % (sess["name"], "el punto medio del rango"),
             "  filtros             objetivo  operaciones  objetivo/stop/ninguno  acierto   R total  R media  peor racha"]
    for label, rr, b in rows:
        if not b["fired"]:
            lines.append("  %-19s %4sR     0" % (label, _num(rr)))
            continue
        lines.append("  %-19s %4sR     %3d          %2d / %2d / %2d          %4.0f %%  %+7.2f  %+6.2f   %5.2fR" % (
            label, _num(rr), b["fired"], b["target"], b["stop"], b["neither"], b["win_pct"], b["total_r"],
            b["avg_r"], b["max_drawdown_r"]))
    n = rows[0][2]["sessions"] if rows else 0
    lines.append("Sesiones completas en los datos: %d. Con tan pocas operaciones, una diferencia pequena puede ser "
                 "suerte: confirma con mas historia (el CSV de tu MT5 puede tener anos)." % n)
    return "\n".join(lines)


# ---------------------------------------------------------------- tamano en lotes

def prop_cfg(cfg):
    pr = cfg.get("prop")
    if not pr:
        return None
    out = dict(PROP_FTMO_2STEP)
    out.update(pr)
    return out


def prop_status(trades, cfg, now):
    """Estado de la cuenta de fondeo a partir del diario: saldo, limite diario, perdida maxima, objetivo, dias."""
    pr = prop_cfg(cfg)
    if pr is None:
        return None
    init = float(pr["initial_balance"])
    start = pr.get("start_date")
    contract, spread = float(cfg["contract_size"]), float(cfg["spread"])
    today = to_local(now, "cet").date()
    mine = [t for t in trades if not start or t["date"] >= start]
    closed = [t for t in mine if t["status"] != "abierta" and "usd" in t]
    day_of = lambda t: to_local(_utc(t["entry_utc"]), "cet").date()
    before = sum(t["usd"] for t in closed if day_of(t) < today)
    today_closed = [t for t in closed if day_of(t) == today]
    today_pnl = sum(t["usd"] for t in today_closed)
    open_risk = sum(t["lots"] * ((t["risk_per_oz"] + t.get("spread", spread)) * t.get("contract", contract)
                                 + t.get("commission_per_lot", 0)) for t in mine
                    if t["status"] == "abierta" and t.get("lots"))
    balance = init + before + today_pnl
    midnight = init + before
    daily_floor = midnight - init * float(pr["max_daily_loss_pct"]) / 100.0
    total_floor = init * (1 - float(pr["max_loss_pct"]) / 100.0)
    tgt_pct = pr["profit_target_pct"].get(str(pr["phase"])) if isinstance(pr["profit_target_pct"], dict) else pr["profit_target_pct"]
    target = init * (1 + tgt_pct / 100.0) if tgt_pct else None
    days = sorted(set(day_of(t) for t in mine))
    losses_today = sum(1 for t in today_closed if t["usd"] < 0)
    safety = float(pr["safety_usd"])
    # lo que aun puedes perder hoy sin tocar ni el limite de FTMO ni tu propio freno, contando las abiertas al stop
    room_ftmo = balance - open_risk - max(daily_floor, total_floor) - safety
    room_own = float(pr["daily_stop_usd"]) + min(today_pnl, 0) - open_risk if pr.get("daily_stop_usd") else room_ftmo
    room = max(0.0, min(room_ftmo, room_own))
    blocks = []
    if pr.get("max_losses_per_day") and losses_today >= int(pr["max_losses_per_day"]):
        blocks.append("ya llevas %d stops hoy (tu freno: %d). Hoy no se opera mas." % (losses_today, pr["max_losses_per_day"]))
    if pr.get("daily_stop_usd") and -today_pnl >= float(pr["daily_stop_usd"]) - 1e-9:
        blocks.append("hoy pierdes %.2f $ (tu freno diario: %.2f $). Hoy no se opera mas." % (-today_pnl, pr["daily_stop_usd"]))
    if target and balance >= target:
        blocks.append("objetivo de la fase cumplido (%.2f $). No arriesgues mas: solo te faltan los dias minimos." % target)
    n_open = sum(1 for t in mine if t["status"] == "abierta")
    if pr.get("max_open_trades") and n_open >= int(pr["max_open_trades"]):
        blocks.append("ya tienes %d operacion abierta (maximo %d a la vez). Espera a que se cierre." % (n_open, pr["max_open_trades"]))
    risk_usd = None
    if pr.get("risk_pct"):
        risk_usd = init * float(pr["risk_pct"]) / 100.0
        if pr.get("halve_below_pct") and balance <= init * (1 - float(pr["halve_below_pct"]) / 100.0):
            risk_usd /= 2.0
    wd = to_local(now, "cet")
    if wd.weekday() == 4 and pr.get("friday_no_entries_after_cet") and \
            wd.time() >= parse_hm(pr["friday_no_entries_after_cet"]):
        blocks.append("viernes despues de las %s (Espana): el oro cierra a las 22:00 para el fin de semana; FTMO "
                      "prohibe abrir a menos de 2 horas de un cierre largo (gap trading)." % pr["friday_no_entries_after_cet"])
    return {"name": pr["name"], "phase": pr["phase"], "initial": init, "balance": round(balance, 2),
            "risk_usd": round(risk_usd, 2) if risk_usd else None,
            "halved": bool(risk_usd and pr.get("risk_pct") and risk_usd < init * float(pr["risk_pct"]) / 100.0 - 1e-9),
            "midnight_balance": round(midnight, 2), "today": today.isoformat(), "today_pnl": round(today_pnl, 2),
            "losses_today": losses_today, "open_risk": round(open_risk, 2),
            "daily_floor": round(daily_floor, 2), "total_floor": round(total_floor, 2),
            "target": round(target, 2) if target else None, "to_target": round(target - balance, 2) if target else None,
            "trading_days": len(days), "min_trading_days": pr["min_trading_days"],
            "room_today": round(room, 2), "blocks": blocks}


def prop_report(st):
    ph = {1: "fase 1, FTMO Challenge", 2: "fase 2, Verification", 3: "cuenta FTMO"}.get(st["phase"], "fase %s" % st["phase"])
    lines = ["%s (%s), %.0f $ iniciales. Dia FTMO: %s (empieza a medianoche, hora de Espana)." % (
        st["name"], ph, st["initial"], st["today"]),
        "  Saldo (segun el diario)  %10.2f $   hoy %+.2f $, %d stops" % (st["balance"], st["today_pnl"], st["losses_today"]),
        "  Limite diario FTMO       %10.2f $   el equity no puede bajar de aqui hoy (saldo a medianoche %.2f - 5 %%)" % (
            st["daily_floor"], st["midnight_balance"]),
        "  Perdida maxima FTMO      %10.2f $   estatica, nunca por debajo" % st["total_floor"]]
    if st["target"]:
        lines.append("  Objetivo                 %10.2f $   faltan %.2f $" % (st["target"], max(st["to_target"], 0)))
    lines.append("  Dias operados            %10d     minimo %d" % (st["trading_days"], st["min_trading_days"]))
    if st["open_risk"]:
        lines.append("  Abiertas, al stop        %10.2f $" % -st["open_risk"])
    if st.get("risk_usd"):
        lines.append("  Riesgo por operacion     %10.2f $   %s" % (st["risk_usd"], "REDUCIDO a la mitad: el saldo ha caido un 5 %"
                                                                   if st["halved"] else "el % del saldo inicial que elegiste"))
    lines.append("  Puedes arriesgar hoy     %10.2f $   como mucho, en la siguiente operacion" % st["room_today"])
    if st["phase"] == 3:
        lines.append("Cuenta FTMO: en XAUUSD nada puede ejecutarse (entrada, SL ni TP) de 2 minutos antes a 2 despues de "
                     "una noticia marcada en el calendario de FTMO. Miralo antes de cada operacion.")
    for b in st["blocks"]:
        lines.append("ALTO: " + b)
    lines.append("El saldo sale del diario (con spread, sin comisiones ni swap): compruebalo con el de tu panel de FTMO.")
    return "\n".join(lines)


def size_order(plan, cfg, room=None):
    e, stop, tgt = float(plan["entry"]), float(plan["stop"]), float(plan["target"])
    contract, spread = float(cfg["contract_size"]), float(cfg["spread"])
    step, lmin, lmax = float(cfg["lot_step"]), float(cfg["lot_min"]), float(cfg["lot_max"])
    if plan.get("reward_risk", 0) + 1e-9 < float(cfg["min_reward_risk"]):
        raise GoldError("recompensa:riesgo %.2f por debajo de tu minimo %.2f" % (plan["reward_risk"], cfg["min_reward_risk"]))
    commission = float(cfg.get("commission_per_lot") or 0)
    risk_oz = abs(e - stop) + spread
    risk_lot = risk_oz * contract + commission
    equity = float(cfg["balance"]) * (1 - float(cfg["cash_buffer_pct"]) / 100.0)
    margin_lot = contract * e / float(cfg["leverage"])
    limits = [
        ("perdida maxima", floor_step(float(cfg["max_loss_usd"]) / risk_lot, step),
         "%.2f $ / ((%s de riesgo + %s de spread) x %g %s%s)" % (float(cfg["max_loss_usd"]), fp(abs(e - stop)), fp(spread),
                                                             contract, cfg.get("unit", "onzas"),
                                                             " + %.2f $ de comision" % commission if commission else "")),
        ("margen", floor_step(equity / margin_lot, step),
         "%.2f $ disponibles / %.2f $ de margen por lote (apalancamiento 1:%g)" % (equity, margin_lot, float(cfg["leverage"]))),
        ("lote maximo", lmax, "lot_max en la configuracion"),
    ]
    if room is not None:
        limits.append(("limite FTMO hoy", floor_step(room / risk_lot, step),
                       "%.2f $ que aun puedes perder hoy / %.2f $ por lote al stop" % (room, risk_lot)))
    lots = min(x[1] for x in limits)
    decided = [x[0] for x in limits if x[1] == lots][0]
    if lots < lmin - 1e-9:
        if decided == "limite FTMO hoy":
            raise GoldError("no cabe ni el lote minimo dentro de lo que aun puedes perder hoy (%.2f $). "
                            "Salta la operacion: gold.py prop te dice por que." % room)
        raise GoldError("ni el lote minimo cabe: %.2f lotes arriesgan %.2f $ en el stop (con spread), mas que tu "
                        "perdida maxima de %.2f $. Usa un stop mas cercano o salta la operacion."
                        % (lmin, lmin * risk_lot, float(cfg["max_loss_usd"])))
    max_loss = round(lots * risk_lot, 2)
    reward = round(lots * ((abs(tgt - e) - spread) * contract - commission), 2)
    order = {"symbol": cfg["symbol"], "type": "buy" if plan["side"] == "long" else "sell", "volume": lots,
             "sl": stop, "tp": tgt, "entry_ref": e, "max_loss_usd": max_loss, "reward_usd": reward,
             "margin_usd": round(lots * margin_lot, 2), "decided_by": decided,
             "limits": [{"limit": n, "lots": l, "math": m} for n, l, m in limits],
             "flat_by": plan.get("flat_by"), "tz": plan.get("tz"), "date": plan.get("date"),
             "session": plan.get("session"), "contract": contract, "commission_per_lot": commission}
    return order


def size_report(o):
    lines = ["Tamano: %s" % o["symbol"]]
    for x in o["limits"]:
        mark = "  <- lo decide" if x["limit"] == o["decided_by"] else ""
        lines.append("  %-15s %6.2f lotes   %s%s" % (x["limit"], x["lots"], x["math"], mark))
    lines.append("%.2f lotes. Perdida maxima en el stop, con spread: %.2f $. En el objetivo: +%.2f $. Margen: %.2f $." % (
        o["volume"], o["max_loss_usd"], o["reward_usd"], o["margin_usd"]))
    return "\n".join(lines)


def ticket_report(o):
    tipo = "Buy (compra)" if o["type"] == "buy" else "Sell (venta)"
    tzn = TZ_NAMES.get(o.get("tz") or "ny", "")
    return "\n".join([
        "ORDEN para colocar A MANO en MetaTrader 5 (nada se ha enviado):",
        "  Simbolo      %s" % o["symbol"],
        "  Tipo         Ejecucion por mercado, %s" % tipo,
        "  Volumen      %.2f lotes" % o["volume"],
        "  Stop Loss    %s" % fp(o["sl"]),
        "  Take Profit  %s" % fp(o["tp"]),
        "  Perdida maxima si el stop se ejecuta en su precio: unos %.2f $ (puede ser peor en un mercado rapido)." % o["max_loss_usd"],
        "  Cierra lo que siga abierto a las %s (%s)." % (o.get("flat_by") or "?", tzn),
        "En MT5: F9 (Nueva orden) > Simbolo > Volumen > Stop Loss > Take Profit > %s." % (
            "Comprar por mercado" if o["type"] == "buy" else "Vender por mercado"),
        "Comprueba antes que el precio en MT5 sigue cerca de %s: si se ha ido lejos, es otra operacion." % fp(o["entry_ref"]),
        "Usa primero una cuenta demo.",
    ])


# ---------------------------------------------------------------- comandos

def _write(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
    print("Escrito %s." % path)


def _need(path, what):
    doc = load_json(path)
    if doc is None:
        raise GoldError("no existe %s: %s" % (path, what))
    return doc


def cmd_clock(a, cfg):
    now = parse_now(a.now)
    print("Ahora: %s UTC, %s Londres, %s Nueva York." % (
        now.strftime("%Y-%m-%d %H:%M"), to_local(now, "london").strftime("%H:%M"), to_local(now, "ny").strftime("%H:%M")))
    print("Oro (XAUUSD): %s." % ("ABIERTO" if market_open(now) else "CERRADO (fin de semana o pausa diaria de 17:00 a 18:00 Nueva York)"))
    for key in ("london", "ny", "tarde"):
        s = session_cfg(key, cfg)
        day = to_local(now, s["tz"]).date()
        o, cut, flat = bounds(day, s)
        if day.weekday() >= 5:
            st = "sin sesion hoy (fin de semana)"
        elif now < o:
            st = "abre a las %s" % s["open"]
        elif now < cut:
            st = "abierta: entradas hasta las %s" % s["no_entries_after"]
        elif now < flat:
            st = "sin entradas nuevas; fuera a las %s" % s["flat_by"]
        else:
            st = "terminada por hoy"
        print("  %-26s %s-%s hora de %s: %s" % (s["name"], s["open"], s["flat_by"], TZ_NAMES[s["tz"]], st))
    print("Festivos: el oro suele cotizar con horario reducido en festivos de EE. UU. y del Reino Unido; mira el calendario de tu broker.")
    return 0


def cmd_orb(a, cfg):
    sess = session_cfg(a.session, cfg)
    now = parse_now(a.now)
    idx, source = load_bars(a, cfg)
    day = to_local(now, sess["tz"]).date()
    om = a.or_minutes or int(cfg["or_minutes"])
    use_trend = sess.get("trend_filter", True) if a.trend is None else a.trend
    use_width = sess.get("width_filter", False) if a.width is None else a.width
    side, notes, blocked = a.side, [], None
    if use_trend:
        o, _, _ = bounds(day, sess)
        tside = Trend(idx).at(o)
        if tside is None:
            notes.append("Filtro de tendencia: no hay historia suficiente en 1 hora; no se aplica.")
        else:
            notes.append("Filtro de tendencia (1 hora, media de 20 frente a 50, al abrir la sesion): %s, solo %s." % (
                "alcista" if tside == "long" else "bajista", "largos" if tside == "long" else "cortos"))
            if side == "both":
                side = tside
            elif side != tside:
                blocked = "pediste %s pero la tendencia es %s" % (side, "alcista" if tside == "long" else "bajista")
    doc = orb(idx, sess, day, now, om, side)
    if use_width and doc.get("opening_range"):
        r = doc["opening_range"]
        ok, med = width_ok(r["high"] - r["low"], range_widths(idx, sess, om // 5, day))
        if med is not None:
            notes.append("Filtro de anchura: rango de %.2f frente a una mediana de %.2f (valido entre %.2f y %.2f)." % (
                r["high"] - r["low"], med, 0.5 * med, 2 * med))
        if not ok:
            blocked = "el rango de hoy es anormal comparado con los ultimos 20 dias"
    if blocked and doc["status"] in ("triggered", "waiting"):
        doc["status"] = "filtered"
        doc["filter_reason"] = blocked
    doc.update(flat_by=sess["flat_by"], source=source, symbol=cfg["symbol"], filter_notes=notes)
    print(json.dumps(doc, indent=2, ensure_ascii=False) if a.json else "\n".join([orb_report(doc, source)] + notes))
    if a.write and doc["status"] in ("triggered", "waiting"):
        _write(os.path.join(TRADER, PREFIX + "-entry.json"), doc)
    return 0 if doc["status"] == "triggered" else 1


def cmd_plan(a, cfg):
    if a.entry is not None:
        if a.side not in ("long", "short"):
            raise GoldError("con --entry di el lado: --side long o --side short")
        sess = session_cfg(a.session, cfg)
        entry = {"status": "triggered", "side": a.side, "entry": a.entry, "session": a.session,
                 "session_name": sess["name"], "tz": sess["tz"], "flat_by": sess["flat_by"],
                 "date": a.date or to_local(parse_now(a.now), sess["tz"]).date().isoformat(),
                 "or_minutes": a.or_minutes or int(cfg["or_minutes"]), "entry_bar": a.entry_bar,
                 "entry_time": a.entry_time}
        if a.or_high is not None and a.or_low is not None:
            entry["opening_range"] = {"high": a.or_high, "low": a.or_low, "minutes": entry["or_minutes"]}
    else:
        entry = _need(os.path.join(TRADER, PREFIX + "-entry.json"), "ejecuta antes gold.py orb --write, o pasa --entry")
    target = a.target or session_cfg(entry.get("session") or a.session, cfg).get("target") or "2R"
    plan = make_plan(entry, a.stop, target, cfg)
    print(json.dumps(plan, indent=2, ensure_ascii=False) if a.json else plan_report(plan))
    if a.write:
        _write(os.path.join(TRADER, PREFIX + "-plan.json"), plan)
    return 0


def cmd_replay(a, cfg):
    plan = _need(os.path.join(TRADER, PREFIX + "-plan.json"), "ejecuta antes gold.py plan --write")
    if plan.get("entry_bar") is None:
        raise GoldError("el plan no tiene entry_bar (la vela de entrada dentro de la sesion); pasalo con plan --entry-bar N")
    idx, _ = load_bars(a, cfg)
    doc = run_replay(idx, plan, cfg, a.replays, a.seed, a.window)
    print(json.dumps(doc, indent=2, ensure_ascii=False, default=str) if a.json else replay_report(doc))
    if a.write:
        _write(os.path.join(TRADER, PREFIX + "-replay.json"), doc)
    return 0


def cmd_size(a, cfg):
    plan = _need(os.path.join(TRADER, PREFIX + "-plan.json"), "ejecuta antes gold.py plan --write")
    for k in ("balance", "max_loss", "leverage", "spread"):
        v = getattr(a, k)
        if v is not None:
            cfg["max_loss_usd" if k == "max_loss" else k] = v
    pr = prop_cfg(cfg)
    if pr and pr.get("commission_per_lot") and not cfg.get("commission_per_lot"):
        cfg["commission_per_lot"] = pr["commission_per_lot"]
    st = prop_status((load_json(journal_path()) or {}).get("trades", []), cfg, parse_now(getattr(a, "now", None)))
    if st and st["blocks"]:
        raise GoldError("cuenta de fondeo: " + " ".join(st["blocks"]))
    if st and st.get("risk_usd") and a.max_loss is None:
        cfg["max_loss_usd"] = st["risk_usd"]
    o = size_order(plan, cfg, room=st["room_today"] if st else None)
    print(json.dumps(o, indent=2, ensure_ascii=False) if a.json else size_report(o))
    if a.write:
        _write(os.path.join(TRADER, PREFIX + "-order.json"), o)
    return 0


def cmd_backtest(a, cfg):
    sess = session_cfg(a.session, cfg)
    idx, source = load_bars(a, cfg)
    k = (a.or_minutes or int(cfg["or_minutes"])) // 5
    if a.compare:
        print(compare_report(compare(idx, sess, cfg, k, a.stop), sess))
        print("Datos: %s." % source)
        return 0
    t = a.target or sess.get("target") or "2R"
    if not t.upper().endswith("R"):
        raise GoldError("el backtest necesita un objetivo en R (1R, 1.5R, 2R)")
    use_trend = sess.get("trend_filter", True) if a.trend is None else a.trend
    use_width = sess.get("width_filter", False) if a.width is None else a.width
    b = backtest(idx, sess, cfg, float(t[:-1]), a.stop, use_trend, use_width, k)
    print(json.dumps(b, indent=2) if a.json else compare_report([(
        "+".join([x for x, on in (("tendencia", use_trend), ("anchura", use_width)) if on]) or "sin filtros",
        float(t[:-1]), b)], sess))
    return 0


def cmd_ticket(a, cfg):
    o = _need(os.path.join(TRADER, PREFIX + "-order.json"), "ejecuta antes gold.py size --write")
    print(ticket_report(o))
    return 0


# ---------------------------------------------------------------- diario (para probar en demo)

def journal_path():
    return os.path.join(TRADER, "gold-journal.json")


def _utc(text):
    return datetime.datetime.strptime(text.rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S")


def journal_add(doc, plan, order, cfg):
    if not plan.get("entry_utc"):
        raise GoldError("el plan no viene de gold.py orb (no tiene la hora de entrada); el diario necesita una senal de orb")
    trades = doc.setdefault("trades", [])
    for t in trades:
        if (t.get("symbol", "XAUUSD"), t["date"], t["session"], t["side"], t["entry"]) == (
                plan.get("symbol", "XAUUSD"), plan["date"], plan["session"], plan["side"], plan["entry"]):
            raise GoldError("esta senal ya esta en el diario (#%d)" % t["id"])
    sess = session_cfg(plan["session"], cfg)
    _, _, flat = bounds(datetime.date.fromisoformat(plan["date"]), sess)
    lots = None
    if order and abs(order.get("sl", 0) - plan["stop"]) < 1e-9 and abs(order.get("tp", 0) - plan["target"]) < 1e-9:
        lots = order["volume"]
    t = {"id": len(trades) + 1, "symbol": plan.get("symbol", cfg["symbol"]), "digits": DIG,
         "contract": float(cfg["contract_size"]), "spread": float(cfg["spread"]),
         "commission_per_lot": float(cfg.get("commission_per_lot") or 0), "date": plan["date"], "session": plan["session"], "session_name": plan["session_name"],
         "tz": plan["tz"], "side": plan["side"], "entry": plan["entry"], "entry_time": plan.get("entry_time"),
         "entry_utc": plan["entry_utc"], "stop": plan["stop"], "target": plan["target"], "target_r": plan["target_r"],
         "risk_per_oz": plan["risk_per_oz"], "lots": lots, "flat_by": sess["flat_by"],
         "flat_utc": flat.isoformat() + "Z", "status": "abierta"}
    trades.append(t)
    return t


def journal_check(doc, idx, now, cfg):
    """Cierra las operaciones abiertas del diario con las velas: objetivo, stop (primero, si una vela toca los dos)
    o la hora de cierre forzoso."""
    changed = []
    for t in doc.get("trades", []):
        if t["status"] != "abierta" or t.get("symbol", "XAUUSD") != cfg["symbol"]:
            continue
        contract = float(t.get("contract", cfg["contract_size"]))
        spread = float(t.get("spread", cfg["spread"]))
        long = t["side"] == "long"
        start, flat = _utc(t["entry_utc"]), _utc(t["flat_utc"])
        out, px, at, last = None, None, None, None
        b0 = start
        while b0 < flat and b0 + FIVE <= now:
            b = idx.get(b0)
            if b is not None:
                last = b
                if (long and b[3] <= t["stop"]) or (not long and b[2] >= t["stop"]):
                    out, px = "stop", t["stop"]
                elif (long and b[2] >= t["target"]) or (not long and b[3] <= t["target"]):
                    out, px = "objetivo", t["target"]
                if out:
                    at = b0 + FIVE
                    break
            b0 += FIVE
        if out is None:
            if now < flat or last is None:
                continue
            out, px, at = "cierre", last[4], last[0] + FIVE
        sign = 1 if long else -1
        r = t["target_r"] if out == "objetivo" else (-1.0 if out == "stop" else (px - t["entry"]) * sign / t["risk_per_oz"])
        t.update(status=out, exit=rp(px), exit_time=hm(to_local(at, t["tz"])), r=round(r, 3))
        if t.get("lots"):
            com = float(t.get("commission_per_lot", cfg.get("commission_per_lot") or 0) or 0)
            t["usd"] = round((px - t["entry"]) * sign * t["lots"] * contract - (spread * contract + com) * t["lots"], 2)
        changed.append(t)
    return changed


def journal_report(doc):
    trades = doc.get("trades", [])
    if not trades:
        return "El diario esta vacio. Apunta una senal con: gold.py journal add (despues de orb, plan y size)."
    lines = ["Diario (%d operaciones):" % len(trades),
             "  #  simbolo fecha       sesion   lado   entrada     stop        objetivo    lotes  resultado              R       $"]
    for t in trades:
        d = int(t.get("digits", 2))
        f = lambda x: "%.*f" % (d, x)
        res = t["status"] if t["status"] == "abierta" else "%s %s a %s" % (t["status"], t.get("exit_time", ""), f(t["exit"]))
        lines.append("  %-2d %-7s %s  %-7s  %-5s  %10s  %10s  %10s  %5s  %-21s %6s  %7s" % (
            t["id"], t.get("symbol", "XAUUSD"), t["date"], t["session"], "largo" if t["side"] == "long" else "corto",
            f(t["entry"]), f(t["stop"]), f(t["target"]), "%.2f" % t["lots"] if t.get("lots") else "-", res,
            "%+.2f" % t["r"] if "r" in t else "", "%+.2f" % t["usd"] if "usd" in t else ""))
    done = [t for t in trades if t["status"] != "abierta"]
    if done:
        wins = sum(1 for t in done if t["r"] > 0)
        lines.append("Cerradas %d: %d ganadoras, %d perdedoras (%.0f %% de acierto). R total %+.2f, R media %+.2f." % (
            len(done), wins, len(done) - wins, 100.0 * wins / len(done), sum(t["r"] for t in done),
            sum(t["r"] for t in done) / len(done)))
        usd = [t["usd"] for t in done if "usd" in t]
        if usd:
            lines.append("Resultado en dolares (con spread y comision, sin swap): %+.2f $." % sum(usd))
        if len(done) < 30:
            lines.append("Con menos de 30 operaciones el resultado dice poco: sigue apuntando.")
    return "\n".join(lines)


def cmd_journal(a, cfg):
    path = journal_path()
    doc = load_json(path) or {"trades": []}
    if a.action == "add":
        plan = _need(os.path.join(TRADER, PREFIX + "-plan.json"), "ejecuta antes gold.py orb --write y plan --write")
        order = load_json(os.path.join(TRADER, PREFIX + "-order.json"))
        t = journal_add(doc, plan, order, cfg)
        _write(path, doc)
        print("Apuntada #%d: %s %s %s en %s, stop %s, objetivo %s%s." % (
            t["id"], t["symbol"], t["session_name"], "largo" if t["side"] == "long" else "corto", fp(t["entry"]),
            fp(t["stop"]), fp(t["target"]),
            ", %.2f lotes" % t["lots"] if t["lots"] else " (sin lotes: ejecuta size --write antes para tenerlos)"))
        return 0
    if a.action == "check":
        if any(t["status"] == "abierta" for t in doc.get("trades", [])):
            idx, _ = load_bars(a, cfg)
            for t in journal_check(doc, idx, parse_now(a.now), cfg):
                print("#%d %s cerrada: %s a las %s en %s (%+.2fR)." % (t["id"], t.get("symbol", "XAUUSD"), t["status"],
                                                                    t["exit_time"], fp(t["exit"]), t["r"]))
            _write(path, doc)
    print(journal_report(doc))
    return 0


def cmd_prop(a, cfg):
    if not cfg.get("prop"):
        raise GoldError('no hay cuenta de fondeo en la configuracion: pon "prop": {"initial_balance": 10000, '
                        '"phase": 1, "start_date": "AAAA-MM-DD"} en trader/gold.json')
    st = prop_status((load_json(journal_path()) or {}).get("trades", []), cfg, parse_now(a.now))
    print(json.dumps(st, indent=2, ensure_ascii=False) if a.json else prop_report(st))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="gold.py", description="Day trading de oro (XAUUSD) para MetaTrader 5.")
    p.add_argument("--config", help="configuracion (por defecto trader/gold.json)")
    p.add_argument("--symbol", default="XAUUSD", type=str.upper, choices=sorted(SYMBOLS),
                   help="XAUUSD (por defecto), EURUSD o GBPUSD")
    sub = p.add_subparsers(dest="cmd")

    def data_opts(sp):
        sp.add_argument("--csv", help="velas exportadas de MT5 (M1 o M5)")
        sp.add_argument("--server-tz", help="hora del servidor del CSV: ny+7 (por defecto), +2, 0...")
        sp.add_argument("--offline", help="JSON de Yahoo guardado (.json o .json.gz)")
        sp.add_argument("--basis", type=float, help="suma esto a los precios de Yahoo (futuros -> spot)")

    def filter_opts(sp):
        sp.add_argument("--trend", dest="trend", action="store_true", default=None, help="filtro de tendencia de 1 hora")
        sp.add_argument("--no-trend", dest="trend", action="store_false")
        sp.add_argument("--width", dest="width", action="store_true", default=None, help="filtro de anchura del rango")
        sp.add_argument("--no-width", dest="width", action="store_false")

    sp = sub.add_parser("clock")
    sp.add_argument("--now")
    sp = sub.add_parser("orb")
    sp.add_argument("--session", default="london", choices=sorted(SESSIONS))
    sp.add_argument("--side", default="both", choices=["both", "long", "short"])
    sp.add_argument("--or-minutes", type=int)
    sp.add_argument("--now")
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    filter_opts(sp)
    data_opts(sp)
    sp = sub.add_parser("backtest")
    sp.add_argument("--session", default="london", choices=sorted(SESSIONS))
    sp.add_argument("--target")
    sp.add_argument("--stop", default="or-mid", choices=["or-mid", "or-opposite"])
    sp.add_argument("--or-minutes", type=int)
    sp.add_argument("--compare", action="store_true", help="todas las combinaciones de filtros y objetivos")
    sp.add_argument("--json", action="store_true")
    filter_opts(sp)
    data_opts(sp)
    sp = sub.add_parser("plan")
    sp.add_argument("--stop", default="or-mid")
    sp.add_argument("--target", help="2R, 1R, price:X (por defecto, el de la sesion: 2R Londres y NY, 1R tarde)")
    sp.add_argument("--entry", type=float)
    sp.add_argument("--side", choices=["long", "short"])
    sp.add_argument("--session", default="london", choices=sorted(SESSIONS))
    sp.add_argument("--or-high", type=float)
    sp.add_argument("--or-low", type=float)
    sp.add_argument("--or-minutes", type=int)
    sp.add_argument("--entry-bar", type=int)
    sp.add_argument("--entry-time")
    sp.add_argument("--date")
    sp.add_argument("--now")
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("replay")
    sp.add_argument("--replays", type=int, default=10000)
    sp.add_argument("--seed", type=int, default=19)
    sp.add_argument("--window", type=int, default=2)
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    data_opts(sp)
    sp = sub.add_parser("size")
    sp.add_argument("--balance", type=float)
    sp.add_argument("--max-loss", type=float)
    sp.add_argument("--leverage", type=float)
    sp.add_argument("--spread", type=float)
    sp.add_argument("--now")
    sp.add_argument("--write", action="store_true")
    sp.add_argument("--json", action="store_true")
    sub.add_parser("ticket")
    sp = sub.add_parser("prop")
    sp.add_argument("--now")
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("journal")
    sp.add_argument("action", choices=["add", "check", "show"])
    sp.add_argument("--now")
    data_opts(sp)

    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 2
    try:
        cfg = apply_symbol(load_config(a.config), a.symbol)
        return {"clock": cmd_clock, "orb": cmd_orb, "plan": cmd_plan, "replay": cmd_replay,
                "size": cmd_size, "ticket": cmd_ticket, "journal": cmd_journal,
                "backtest": cmd_backtest, "prop": cmd_prop}[a.cmd](a, cfg)
    except GoldError as e:
        print("No se pudo: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
