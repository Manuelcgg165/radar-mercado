#!/usr/bin/env python3
"""Radar de mercado semanal.

Lee universo.csv, descarga cotizaciones diarias de Yahoo Finance y compras de
directivos de la SEC (formulario 4), calcula señales y escribe en salida/:

  radar_mercado.csv    señales de la semana, ordenadas por puntuación
  radar_universo.csv   métricas de todos los valores
  estado.json          fecha de los datos y lo que ha fallado
  historial/           copia fechada de radar_universo.csv

Las rentabilidades se calculan en euros (cada serie se convierte con su tipo de
cambio diario); los máximos, mínimos y volúmenes, en la divisa de cotización.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ------------------------------------------------------------------ Configuración
SEMANA = 5                  # sesiones de la semana analizada
VENTANA_VOLUMEN = 50        # sesiones previas con las que se compara el volumen
VOLUMEN_SEMANA_X = 2.0      # volumen medio de la semana / volumen medio previo
VOLUMEN_DIA_X = 4.0         # mayor volumen de un día / mediana previa
MOVIMIENTO_PP = 5.0         # pp frente a la referencia que deben acompañar al volumen
FR_3M_PP = 15.0             # pp frente a la referencia a 3 meses en las señales de fuerza relativa
SIGMAS = 1.5                # además, el movimiento debe superar estas desviaciones típicas del valor
VENTANA_FR = 126            # sesiones del máximo o mínimo de fuerza relativa (6 meses)
COMPRAS_MIN_USD = 100_000   # compras de directivos en mercado, suma de la semana
DIAS_DIRECTIVOS = 7         # días hacia atrás en los formularios 4
MAX_SENALES = 12            # filas de radar_mercado.csv
PESOS = {                   # puntuación de cada señal; se divide por las semanas seguidas
    "Compras de directivos": 3,
    "Volumen anómalo": 2,
    "Ruptura de fuerza relativa": 2,
    "Máximo de 52 semanas": 1,
    "Hundimiento de fuerza relativa": 1,
    "Mínimo de 52 semanas": 1,
}
# ---------------------------------------------------------------------------------

S1M, S3M, S6M, S12M = 21, 63, 126, 252
RAIZ = Path(__file__).resolve().parent
SALIDA = Path(os.environ.get("RADAR_SALIDA", RAIZ / "salida"))
SEC_UA = os.environ.get("SEC_USER_AGENT") or "radar-mercado Manuelcgg165@users.noreply.github.com"
SUFIJOS = {"L": "GBp", "ST": "SEK", "OL": "NOK", "CO": "DKK", "SW": "CHF", "TO": "CAD",
           "T": "JPY", "KS": "KRW", "AX": "AUD", "HK": "HKD"}


# ------------------------------------------------------------------------- Datos
def moneda_por_sufijo(simbolo: str) -> str | None:
    if "=" in simbolo or simbolo.startswith("^"):
        return None
    if "." not in simbolo:
        return "USD"
    return SUFIJOS.get(simbolo.rsplit(".", 1)[1].upper(), "EUR")


def divisa_base(moneda: str | None) -> str | None:
    m = (moneda or "").strip()
    return {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(m, m.upper()) or None


def limpiar(df: pd.DataFrame) -> pd.DataFrame:
    df = df[["Close", "Volume"]].copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df["Close"] > 0]
    df["Volume"] = df["Volume"].fillna(0).astype(float)
    ahora = datetime.now(timezone.utc)
    if len(df) and df.index[-1].date() == ahora.date() and ahora.hour < 22:
        df = df.iloc[:-1]  # sesión en curso, incompleta
    return df


def descargar(simbolo: str):
    """Devuelve (DataFrame con Close y Volume, divisa) o (None, motivo del fallo)."""
    import yfinance as yf

    motivo = "sin datos"
    for intento in range(3):
        try:
            tk = yf.Ticker(simbolo)
            df = tk.history(period="2y", interval="1d", auto_adjust=True,
                            actions=False, raise_errors=True)
            if df is not None and not df.empty:
                try:
                    moneda = (tk.get_history_metadata() or {}).get("currency")
                except Exception:  # noqa: BLE001
                    moneda = None
                return limpiar(df), moneda or moneda_por_sufijo(simbolo)
        except Exception as e:  # noqa: BLE001
            motivo = f"{type(e).__name__}: {e}"[:200]
            if "RateLimit" in type(e).__name__ or "Too Many" in str(e) or "429" in str(e):
                time.sleep(30 * (intento + 1))
                continue
        time.sleep(2 + 3 * intento)
    return None, motivo


def alinear(serie: pd.Series, indice: pd.DatetimeIndex) -> pd.Series:
    return serie.reindex(serie.index.union(indice)).ffill().reindex(indice)


def en_euros(cierre: pd.Series, moneda: str | None, cambios: dict) -> tuple[pd.Series, bool]:
    c = divisa_base(moneda)
    if c in (None, "EUR"):
        return cierre, True
    if c not in cambios:
        return cierre, False
    return cierre * alinear(cambios[c], cierre.index), True


def cesta(series: list[pd.Series]) -> pd.Series | None:
    """Índice equiponderado (media de rentabilidades diarias) de varias series."""
    if len(series) < 2:
        return None
    df = pd.concat(series, axis=1).sort_index().ffill()
    r = (df / df.shift(1) - 1).mean(axis=1, skipna=True).fillna(0)
    return (1 + r).cumprod()


# --------------------------------------------------------------------- Métricas
def rent(s: pd.Series | None, k: int) -> float:
    """Rentabilidad en % de las últimas k sesiones."""
    if s is None or len(s) <= k:
        return np.nan
    x0, x1 = s.iat[-1 - k], s.iat[-1]
    if pd.isna(x0) or pd.isna(x1) or x0 <= 0:
        return np.nan
    return (x1 / x0 - 1) * 100


def rel(e: pd.Series, r: pd.Series | None, k: int) -> float:
    return rent(e, k) - rent(r, k) if r is not None else np.nan


def ratios_volumen(v: pd.Series) -> tuple[float, float]:
    base = v.iloc[-(SEMANA + VENTANA_VOLUMEN):-SEMANA]
    semana = v.iloc[-SEMANA:]
    if len(base) < 20 or base.mean() <= 0:
        return np.nan, np.nan
    med = base.median()
    return semana.mean() / base.mean(), (semana.max() / med if med > 0 else np.nan)


def sigma_diaria(e: pd.Series, r: pd.Series | None) -> float:
    """Desviación típica diaria, en %, de la rentabilidad frente a la referencia (último año)."""
    x = np.log(e / e.shift(1))
    if r is not None:
        x = x - np.log(r / r.shift(1))
    x = x.iloc[-S12M:].replace([np.inf, -np.inf], np.nan).dropna()
    return float(x.std()) * 100 if len(x) >= 40 else np.nan


def senales_tecnicas(c: pd.Series, e: pd.Series, r: pd.Series | None, v: pd.Series, k: int) -> dict:
    """Señales con los datos recortados k semanas (k=0 es la última semana)."""
    corte = len(c) - k * SEMANA
    if corte < 60:
        return {}
    c, e, v = c.iloc[:corte], e.iloc[:corte], v.iloc[:corte]
    r = r.iloc[:corte] if r is not None else None
    out = {}
    sd = sigma_diaria(e, r)
    sd5, sd63 = sd * np.sqrt(SEMANA), sd * np.sqrt(S3M)

    xs, xd = ratios_volumen(v)
    mov = rel(e, r, SEMANA)
    if np.isnan(mov):
        mov = rent(e, SEMANA)
    umbral = max(MOVIMIENTO_PP, SIGMAS * sd5) if not np.isnan(sd5) else MOVIMIENTO_PP
    if (xs >= VOLUMEN_SEMANA_X or xd >= VOLUMEN_DIA_X) and abs(mov) >= umbral:
        out["Volumen anómalo"] = {"dir": 1 if mov > 0 else -1, "xs": xs, "xd": xd, "mov": mov,
                                  "z": abs(mov) / sd5 if sd5 > 0 else np.nan}

    if len(c) >= S12M:  # solo si además va mejor (o peor) que su referencia en el último mes
        previo = c.iloc[-S12M:-SEMANA]
        rel1 = rel(e, r, S1M)
        if c.iloc[-SEMANA:].max() > previo.max() and not rel1 <= 0:
            out["Máximo de 52 semanas"] = {"dir": 1}
        if c.iloc[-SEMANA:].min() < previo.min() and not rel1 >= 0:
            out["Mínimo de 52 semanas"] = {"dir": -1}

    if r is not None:
        fr = (e / r).dropna()
        if len(fr) >= VENTANA_FR + SEMANA:
            rel3 = rel(e, r, S3M)
            umbral = max(FR_3M_PP, SIGMAS * sd63) if not np.isnan(sd63) else FR_3M_PP
            z = abs(rel3) / sd63 if sd63 > 0 else np.nan
            previo = fr.iloc[-(VENTANA_FR + SEMANA):-SEMANA]
            if fr.iloc[-SEMANA:].max() > previo.max() and rel3 >= umbral:
                out["Ruptura de fuerza relativa"] = {"dir": 1, "rel3": rel3, "z": z}
            if fr.iloc[-SEMANA:].min() < previo.min() and rel3 <= -umbral:
                out["Hundimiento de fuerza relativa"] = {"dir": -1, "rel3": rel3, "z": z}
    return out


# ---------------------------------------------------------------------- Textos
def n1(x: float, signo: bool = False) -> str:
    if x is None or pd.isna(x):
        return "n/d"
    return (f"{x:+.1f}" if signo else f"{x:.1f}").replace(".", ",")


def miles(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def texto_senal(nombre: str, d: dict, ref_txt: str, semanas: int) -> str:
    sig = f" ({n1(d['z'])} σ)" if not pd.isna(d.get("z", np.nan)) else ""
    if nombre == "Volumen anómalo":
        frente = f"pp frente a {ref_txt}" if ref_txt != "—" else "% en la semana"
        t = (f"volumen de la semana {n1(d['xs'])}× su media, pico diario {n1(d['xd'])}×, "
             f"con {n1(d['mov'], True)} {frente}{sig}")
    elif nombre == "Máximo de 52 semanas":
        t = "nuevo máximo de 52 semanas"
    elif nombre == "Mínimo de 52 semanas":
        t = "nuevo mínimo de 52 semanas"
    else:
        lado = "máximos" if nombre == "Ruptura de fuerza relativa" else "mínimos"
        t = f"fuerza relativa en {lado} de 6 meses: {n1(d['rel3'], True)} pp a 3 meses frente a {ref_txt}{sig}"
    return t + (f", {semanas}.ª semana seguida" if semanas > 1 else "")


def texto_compras(compradores: dict) -> str:
    total = sum(q["usd"] for q in compradores.values())
    if len(compradores) == 1:
        quien, q = next(iter(compradores.items()))
        return f"{quien.title()} ({q['rol']}) compró {miles(total)} $ en mercado"
    roles = ", ".join(sorted({q["rol"] for q in compradores.values()})[:4])
    return f"{len(compradores)} directivos compraron {miles(total)} $ en mercado ({roles})"


# ------------------------------------------------------------------------- SEC
def parsear_form4(contenido: bytes) -> tuple[str, str, float]:
    """Nombre, cargo y dólares de compras en mercado (código P) de un formulario 4."""
    contenido = re.sub(rb'\sxmlns(:\w+)?="[^"]*"', b"", contenido)
    raiz = ET.fromstring(contenido)

    def si(nodo, etiqueta):
        return (nodo.findtext(etiqueta) or "").strip().lower() in ("1", "true")

    duenos = raiz.findall("reportingOwner")
    nombre = (duenos[0].findtext("reportingOwnerId/rptOwnerName") or "").strip() if duenos else ""
    rol = "otro"
    for d in duenos:
        rel_ = d.find("reportingOwnerRelationship")
        if rel_ is None:
            continue
        titulo = (rel_.findtext("officerTitle") or "").strip()
        if si(rel_, "isOfficer"):
            rol = titulo or "directivo"
        elif si(rel_, "isDirector"):
            rol = "consejero"
        elif si(rel_, "isTenPercentOwner"):
            rol = "accionista >10 %"
        break
    usd = 0.0
    for tr in raiz.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if (tr.findtext("transactionCoding/transactionCode") or "").strip() != "P":
            continue
        if (tr.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "A").strip() != "A":
            continue
        try:
            usd += (float(tr.findtext("transactionAmounts/transactionShares/value"))
                    * float(tr.findtext("transactionAmounts/transactionPricePerShare/value")))
        except (TypeError, ValueError):
            continue
    return nombre, rol, usd


def compras_directivos(tickers: list[str]) -> dict:
    """{ticker: {persona: {"rol", "usd"}}} con las compras de los últimos DIAS_DIRECTIVOS días."""
    ses = requests.Session()
    ses.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
    ultimo = {"txt": "sin respuesta"}

    def get(url):
        for intento in range(3):
            time.sleep(0.15)  # la SEC admite 10 peticiones por segundo
            try:
                r = ses.get(url, timeout=30)
            except requests.RequestException as e:
                ultimo["txt"] = type(e).__name__
                time.sleep(3 * (intento + 1))
                continue
            ultimo["txt"] = f"HTTP {r.status_code}: " + re.sub(r"<[^>]+>|\s+", " ", r.text[:400]).strip()[:150]
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429, 500, 502, 503):
                time.sleep(5 * (intento + 1))
                continue
            return None
        return None

    r = get("https://www.sec.gov/files/company_tickers.json")
    if r is None:
        raise RuntimeError(f"company_tickers.json, {ultimo['txt']}")
    ciks = {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}
    desde = (date.today() - timedelta(days=DIAS_DIRECTIVOS)).isoformat()
    res = {}
    for t in tickers:
        cik = ciks.get(t.upper())
        if not cik:
            continue
        r = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if r is None:
            continue
        rec = r.json().get("filings", {}).get("recent", {})
        compradores, vistos = {}, set()
        for forma, fecha, acc, doc in zip(rec.get("form", []), rec.get("filingDate", []),
                                          rec.get("accessionNumber", []), rec.get("primaryDocument", [])):
            if forma != "4" or fecha < desde or acc in vistos:
                continue
            vistos.add(acc)
            x = get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc.split('/')[-1]}")
            if x is None:
                continue
            try:
                quien, rol, usd = parsear_form4(x.content)
            except ET.ParseError:
                continue
            if usd > 0:
                q = compradores.setdefault(quien, {"rol": rol, "usd": 0.0})
                q["usd"] += usd
        if compradores:
            res[t] = compradores
    return res


# ------------------------------------------------------------------- Principal
def leer_universo() -> pd.DataFrame:
    u = pd.read_csv(RAIZ / "universo.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    u.columns = [c.strip().lower() for c in u.columns]
    falta = {"ticker", "nombre", "tema", "referencia"} - set(u.columns)
    if falta:
        sys.exit(f"universo.csv: faltan las columnas {sorted(falta)}")
    for c in ("ticker", "nombre", "tema", "referencia"):
        u[c] = u[c].str.strip()
    return u[u["ticker"] != ""].drop_duplicates("ticker").reset_index(drop=True)


def main() -> int:
    t0 = time.time()
    universo = leer_universo()
    refs = sorted({r for r in universo["referencia"] if r})
    simbolos = list(dict.fromkeys(list(universo["ticker"]) + refs))

    datos, monedas, fallos = {}, {}, {}
    for i, s in enumerate(simbolos, 1):
        df, extra = descargar(s)
        if df is None or len(df) == 0:
            fallos[s] = extra if df is None else "sin sesiones cerradas"
        else:
            datos[s], monedas[s] = df, extra
        if i % 25 == 0:
            print(f"{i}/{len(simbolos)} descargados ({len(fallos)} fallos)", flush=True)
        time.sleep(0.3)

    cambios = {}
    for c in sorted({divisa_base(m) for m in monedas.values()} - {None, "EUR"}):
        df, _ = descargar(f"{c}EUR=X")
        if df is not None and len(df):
            cambios[c] = df["Close"]

    con_datos = [t for t in universo["ticker"] if t in datos]
    if len(con_datos) < len(universo) / 2:
        print(f"Solo hay datos de {len(con_datos)} de {len(universo)} valores. No se actualiza nada.")
        print(json.dumps(dict(list(fallos.items())[:10]), ensure_ascii=False, indent=1))
        return 1
    fecha_datos = max(datos[t].index[-1] for t in con_datos)

    eur, sin_cambio = {}, set()
    for s, df in datos.items():
        eur[s], ok = en_euros(df["Close"], monedas[s], cambios)
        if not ok:
            sin_cambio.add(divisa_base(monedas[s]))

    usa = [t for t in con_datos if "." not in t and "=" not in t and not t.startswith("^")]
    try:
        compras = compras_directivos(usa)
        estado_sec = f"ok ({len(compras)} valores con compras)"
    except Exception as e:  # noqa: BLE001
        compras, estado_sec = {}, f"error: {e}"[:200]

    filas, atrasados = [], []
    for _, f in universo.iterrows():
        t, tema, ref = f["ticker"], f["tema"], f["referencia"]
        fila = {"fecha_datos": fecha_datos.date().isoformat(), "ticker": t, "nombre": f["nombre"], "tema": tema}
        if t not in datos:
            filas.append({**fila, "referencia": ref, "estado": "sin datos"})
            continue
        df = datos[t]
        if (fecha_datos - df.index[-1]).days > 6:
            atrasados.append(t)
            filas.append({**fila, "referencia": ref, "estado": f"datos hasta {df.index[-1].date()}"})
            continue

        c, e, v = df["Close"], eur[t], df["Volume"]
        if ref and ref in eur:
            r, ref_txt = alinear(eur[ref], e.index), ref
        else:
            pares = universo[(universo["tema"] == tema) & (universo["referencia"] == ref)
                             & (universo["ticker"] != t)]["ticker"]
            b = cesta([eur[p] for p in pares if p in eur])
            r, ref_txt = (alinear(b, e.index), f"pares de {tema}") if b is not None else (None, "—")

        tecnicas = [senales_tecnicas(c, e, r, v, 0)]
        senales = []
        for nombre, d in tecnicas[0].items():
            semanas = 1
            for k in range(1, 9):
                if len(tecnicas) <= k:
                    tecnicas.append(senales_tecnicas(c, e, r, v, k))
                if nombre not in tecnicas[k]:
                    break
                semanas += 1
            senales.append((nombre, d["dir"], PESOS[nombre] / semanas, texto_senal(nombre, d, ref_txt, semanas), semanas))

        usd_compras = 0.0
        if t in compras:
            usd_compras = sum(q["usd"] for q in compras[t].values())
            if usd_compras >= COMPRAS_MIN_USD:
                extra = 1 if len(compras[t]) >= 2 else 0
                senales.append(("Compras de directivos", 1, PESOS["Compras de directivos"] + extra,
                                texto_compras(compras[t]), 1))

        senales.sort(key=lambda x: -x[2])
        dirs = {s[1] for s in senales}
        xs, xd = ratios_volumen(v)
        precio = float(c.iat[-1])
        filas.append({
            **fila,
            "referencia": ref_txt,
            "divisa": monedas[t],
            "precio": round(precio, 2 if precio >= 10 else 4),
            "ret_5d": rent(e, SEMANA),
            "rel_5d": rel(e, r, SEMANA),
            "rel_1m": rel(e, r, S1M),
            "rel_3m": rel(e, r, S3M),
            "rel_6m": rel(e, r, S6M),
            "mom_12_1": (e.iat[-1 - S1M] / e.iat[-1 - S12M] - 1) * 100 if len(e) > S12M else np.nan,
            "vol_semana_x": xs,
            "vol_pico_x": xd,
            "dist_max52": (c.iat[-1] / c.iloc[-S12M:].max() - 1) * 100,
            "compras_directivos_usd": round(usd_compras),
            "senales": "; ".join(s[0] for s in senales),
            "direccion": "" if not dirs else ("Mixta" if len(dirs) > 1 else ("Alcista" if 1 in dirs else "Bajista")),
            "puntuacion": round(sum(s[2] for s in senales), 2),
            "repetida": "" if not senales else ("sí" if all(s[4] > 1 for s in senales) else "no"),
            "detalle": "; ".join(s[3] for s in senales),
            "estado": "ok",
        })

    cols = ["fecha_datos", "ticker", "nombre", "tema", "referencia", "divisa", "precio", "ret_5d",
            "rel_5d", "rel_1m", "rel_3m", "rel_6m", "mom_12_1", "vol_semana_x", "vol_pico_x",
            "dist_max52", "compras_directivos_usd", "senales", "direccion", "puntuacion",
            "repetida", "detalle", "estado"]
    tabla = pd.DataFrame(filas).reindex(columns=cols)
    for col in ["ret_5d", "rel_5d", "rel_1m", "rel_3m", "rel_6m", "mom_12_1", "dist_max52"]:
        tabla[col] = pd.to_numeric(tabla[col]).round(1)
    for col in ["vol_semana_x", "vol_pico_x"]:
        tabla[col] = pd.to_numeric(tabla[col]).round(2)
    tabla["puntuacion"] = pd.to_numeric(tabla["puntuacion"]).fillna(0)
    tabla = tabla.sort_values(["puntuacion", "tema", "ticker"], ascending=[False, True, True])

    cols_radar = ["fecha_datos", "ticker", "nombre", "tema", "referencia", "senales", "direccion",
                  "puntuacion", "repetida", "detalle", "precio", "divisa", "ret_5d", "rel_5d",
                  "rel_1m", "rel_3m", "vol_semana_x", "dist_max52", "compras_directivos_usd"]
    radar = tabla[tabla["puntuacion"] > 0].head(MAX_SENALES)[cols_radar]

    (SALIDA / "historial").mkdir(parents=True, exist_ok=True)
    radar.to_csv(SALIDA / "radar_mercado.csv", index=False)
    tabla.to_csv(SALIDA / "radar_universo.csv", index=False)
    tabla.to_csv(SALIDA / "historial" / f"universo_{fecha_datos.date().isoformat()}.csv", index=False)
    estado = {
        "fecha_datos": fecha_datos.date().isoformat(),
        "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "valores": len(universo),
        "con_datos": len(con_datos),
        "senales": len(radar),
        "sin_datos": {t: fallos[t] for t in universo["ticker"] if t in fallos},
        "datos_atrasados": atrasados,
        "referencias_sin_datos": [r for r in refs if r not in datos],
        "divisas_sin_cambio": sorted(x for x in sin_cambio if x),
        "sec": estado_sec,
        "segundos": round(time.time() - t0),
    }
    (SALIDA / "estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in estado.items() if k != "sin_datos"}, ensure_ascii=False))
    print(f"Sin datos: {list(estado['sin_datos'])}")
    print(radar[["ticker", "senales", "puntuacion"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
