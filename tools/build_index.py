#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Construieste indexul public anaf-mcp: lista firmelor de la Registrul Comertului
plus indicatorii din situatiile financiare anuale, intr-un singur SQLite.

Rezultatul se publica drept release asset pe GitHub, ca utilizatorii sa nu mai
descarce 650 MB de la data.gov.ro pe o conexiune care se rupe.

Rulare:
    python3 tools/build_index.py --iesire anaf-index.sqlite
    python3 tools/build_index.py --firme /cale/od_firme.csv --financiar 2024=/cale/uu2024.txt
"""

import argparse
import csv
import gzip
import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.parse
import urllib.request

CKAN = "https://data.gov.ro/api/3/action/"
UA = "anaf-mcp-build/1.0"
csv.field_size_limit(1 << 24)


def _get(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _descarca(url, cale, log=print):
    """data.gov.ro ignora Range, deci la trunchiere reluam de la zero."""
    for incercare in range(1, 4):
        scris, asteptat = 0, None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r:
                asteptat = int(r.headers.get("Content-Length") or 0) or None
                with open(cale, "wb") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        scris += len(b)
        except Exception as e:
            log("  incercarea %d a esuat dupa %d MB (%s)" % (incercare, scris >> 20, e))
            continue
        if asteptat and scris < asteptat:
            log("  incercarea %d: trunchiat, %d din %d MB" % (incercare, scris >> 20, asteptat >> 20))
            continue
        return cale
    raise RuntimeError("nu am putut descarca integral %s" % url)


STANDARD = ["CUI", "CAEN"] + ["I%d" % i for i in range(1, 21)]


def resurse_financiare(an, log=print):
    """Toate fisierele .txt din setul unui an, filtrate la cele cu indicatori standard.

    Setul e impartit pe tipuri de platitori: WEB_UU tine grosul firmelor, dar
    firmele mari sunt in WEB_BL_BS_SL. Bancile, asigurarile si ONG-urile au alte
    coloane, in care I13 nu mai inseamna cifra de afaceri, deci sunt sarite.
    """
    url = CKAN + "package_show?id=situatii_financiare_%s" % an
    try:
        pachet = json.loads(_get(url).decode("utf-8", "replace")).get("result") or {}
    except Exception:
        pachet = {}
    if not pachet.get("resources"):
        url = CKAN + "package_search?q=%s&rows=5" % urllib.parse.quote('"Situatii financiare %s"' % an)
        rez = json.loads(_get(url).decode("utf-8", "replace")).get("result", {}).get("results", [])
        pachet = rez[0] if rez else {}
    out = []
    for r in pachet.get("resources", []):
        nume = (r.get("name") or "").strip()
        if (r.get("format") or "").upper() != "TXT" or not nume:
            continue
        out.append((nume, r["url"]))
    return out


def antet_standard(cale):
    with open(cale, encoding="utf-8", errors="replace", newline="") as f:
        antet = (f.readline() or "").strip().split(",")
    return [c.strip().upper() for c in antet] == STANDARD


def cauta_resursa(q, sufix, log=print):
    """Gaseste in CKAN cea mai recenta resursa al carei URL se termina cu <sufix>."""
    url = CKAN + "package_search?q=%s&rows=5&sort=metadata_modified%%20desc" % urllib.parse.quote(q)
    pachete = json.loads(_get(url).decode("utf-8", "replace")).get("result", {}).get("results", [])
    for p in pachete:
        for res in p.get("resources", []):
            if (res.get("url") or "").lower().endswith(sufix):
                return res["url"], p.get("title", "")
    raise RuntimeError("nu am gasit resursa %s pentru %r" % (sufix, q))


def norm_nume(s):
    s = (s or "").lower()
    for a, b in (("ăâ", "aa"), ("î", "i"), ("șş", "ss"), ("țţ", "tt")):
        for i, ch in enumerate(a):
            s = s.replace(ch, b[i])
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _int(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def build(cale_db, firme_csv, financiar, log=print):
    if os.path.exists(cale_db):
        os.remove(cale_db)
    con = sqlite3.connect(cale_db)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("""CREATE TABLE firme (cui INTEGER, denumire TEXT, nume_norm TEXT,
                   nr_reg_com TEXT, data_inmatriculare TEXT, forma_juridica TEXT,
                   judet TEXT, localitate TEXT)""")
    con.execute("""CREATE TABLE financiar (cui INTEGER, an INTEGER, caen INTEGER,
                   judet TEXT, denumire TEXT, cifra_afaceri INTEGER, profit_net INTEGER,
                   pierdere_neta INTEGER, salariati INTEGER, active INTEGER,
                   datorii INTEGER, capitaluri INTEGER)""")

    # --- firme (ONRC)
    n = 0
    judet_de_cui, nume_de_cui = {}, {}
    with open(firme_csv, encoding="utf-8", errors="replace", newline="") as f:
        lot = []
        for row in csv.DictReader(f, delimiter="^"):
            den = (row.get("DENUMIRE") or row.get("﻿DENUMIRE") or "").strip()
            if not den:
                continue
            cui = _int(row.get("CUI")) or 0
            jud = (row.get("ADR_JUDET") or "").strip()
            lot.append((cui, den, norm_nume(den), (row.get("COD_INMATRICULARE") or "").strip(),
                        (row.get("DATA_INMATRICULARE") or "").strip(),
                        (row.get("FORMA_JURIDICA") or "").strip(), jud,
                        (row.get("ADR_LOCALITATE") or "").strip()))
            if cui:
                judet_de_cui[cui] = jud
                nume_de_cui[cui] = den
            n += 1
            if len(lot) >= 20000:
                con.executemany("INSERT INTO firme VALUES (?,?,?,?,?,?,?,?)", lot)
                lot = []
                if n % 500000 == 0:
                    log("  %d firme..." % n)
        if lot:
            con.executemany("INSERT INTO firme VALUES (?,?,?,?,?,?,?,?)", lot)
    log("firme indexate: %d" % n)

    # --- situatii financiare, pe ani
    total_fin = 0
    for an, cai in sorted(financiar.items()):
        if isinstance(cai, str):
            cai = [cai]
        m = 0
        for cale in cai:
          if not antet_standard(cale):
            log("  sar peste %s (alte coloane decat setul standard)" % os.path.basename(cale))
            continue
          with open(cale, encoding="utf-8", errors="replace", newline="") as f:
            lot = []
            for row in csv.DictReader(f):
                cui = _int(row.get("CUI") or row.get("cui"))
                if not cui:
                    continue
                g = lambda k: _int(row.get(k) or row.get(k.upper()))
                lot.append((cui, int(an), _int(row.get("CAEN") or row.get("caen")),
                            judet_de_cui.get(cui), nume_de_cui.get(cui),
                            g("I13"), g("I18"), g("I19"), g("I20"),
                            g("I1"), g("I7"), g("I10")))
                m += 1
                if len(lot) >= 20000:
                    con.executemany("INSERT INTO financiar VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", lot)
                    lot = []
            if lot:
                con.executemany("INSERT INTO financiar VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", lot)
          log("    %s: %d randuri" % (os.path.basename(cale), m))
        log("  situatii financiare %s: %d firme" % (an, m))
        total_fin += m

    log("creez indexii...")
    con.execute("CREATE INDEX idx_nume ON firme(nume_norm)")
    con.execute("CREATE INDEX idx_cui ON firme(cui)")
    con.execute("CREATE INDEX idx_fin_cui ON financiar(cui, an)")
    con.execute("CREATE INDEX idx_fin_top ON financiar(an, caen, cifra_afaceri DESC)")
    con.execute("CREATE INDEX idx_fin_jud ON financiar(an, judet, cifra_afaceri DESC)")
    con.execute("CREATE TABLE meta (cheie TEXT, valoare TEXT)")
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("firme", str(n)), ("financiar", str(total_fin)),
        ("ani", ",".join(str(a) for a in sorted(financiar))),
        ("schema", "1"),
    ])
    con.commit()
    con.close()
    return n, total_fin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iesire", default="anaf-index.sqlite")
    ap.add_argument("--firme", help="cale locala catre od_firme.csv (altfel se descarca)")
    ap.add_argument("--financiar", action="append", default=[],
                    help="an=cale sau doar an (ex. 2024=/tmp/uu.txt sau 2024)")
    ap.add_argument("--ani", default="2024,2023,2022", help="anii de descarcat daca nu se dau caile")
    ap.add_argument("--gzip", action="store_true", help="comprima rezultatul")
    ap.add_argument("--lucru", default=".cache-build")
    a = ap.parse_args()

    os.makedirs(a.lucru, exist_ok=True)
    firme = a.firme
    if not firme:
        url, titlu = cauta_resursa('"Firme inregistrate la Registrul Comertului"', "od_firme.csv")
        print("descarc lista ONRC (%s)..." % titlu)
        firme = _descarca(url, os.path.join(a.lucru, "od_firme.csv"))

    financiar = {}
    for spec in a.financiar:
        if "=" in spec:
            an, cale = spec.split("=", 1)
            financiar.setdefault(an, [])
            if isinstance(financiar[an], list):
                financiar[an].append(cale)
        else:
            financiar[spec] = None
    if not financiar:
        financiar = {an: None for an in a.ani.split(",") if an.strip()}
    for an in list(financiar):
        if financiar[an] is None:
            res = resurse_financiare(an)
            if not res:
                print("  %s: nu am gasit setul de situatii financiare, sar peste" % an)
                del financiar[an]
                continue
            cai = []
            for nume, url in res:
                cale = os.path.join(a.lucru, "%s_%s" % (an, nume))
                if not os.path.exists(cale):
                    print("descarc %s ..." % nume)
                    try:
                        _descarca(url, cale)
                    except Exception as e:
                        print("  %s: %s" % (nume, e))
                        continue
                cai.append(cale)
            financiar[an] = cai

    n, m = build(a.iesire, firme, financiar)
    dim = os.path.getsize(a.iesire)
    print("index: %s (%.0f MB) — %d firme, %d randuri financiare" % (a.iesire, dim / 1048576, n, m))

    if a.gzip:
        with open(a.iesire, "rb") as fi, gzip.open(a.iesire + ".gz", "wb", compresslevel=9) as fo:
            shutil.copyfileobj(fi, fo)
        print("comprimat: %s (%.0f MB)" % (a.iesire + ".gz", os.path.getsize(a.iesire + ".gz") / 1048576))


if __name__ == "__main__":
    main()
