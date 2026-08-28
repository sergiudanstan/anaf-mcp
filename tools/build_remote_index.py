#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construieste indexul compact folosit de deployment-ul MCP remote.

Pastreaza numele normalizat + CUI pentru cautarea firmelor si tabelul financiar
pentru clasamente. Campurile ONRC complete raman in indexul local; dupa cautarea
remote, anaf_firma furnizeaza denumirea si datele oficiale live.
"""

import argparse
import lzma
import os
import shutil
import sqlite3


def build(source, output, log=print):
    db_output = output[:-3] if output.endswith(".xz") else output + ".sqlite"
    output_dir = os.path.dirname(os.path.abspath(output))
    os.makedirs(output_dir, exist_ok=True)
    for path in (db_output, output):
        if os.path.exists(path):
            os.remove(path)

    con = sqlite3.connect(db_output)
    try:
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("ATTACH DATABASE ? AS src", (os.path.abspath(source),))
        log("copiez indexul compact de nume...")
        con.execute(
            "CREATE TABLE firme (nume_norm TEXT NOT NULL, cui INTEGER NOT NULL, "
            "PRIMARY KEY(nume_norm, cui)) WITHOUT ROWID"
        )
        con.execute(
            "INSERT OR IGNORE INTO firme "
            "SELECT nume_norm, coalesce(cui, 0) FROM src.firme WHERE nume_norm <> ''"
        )
        log("copiez situatiile financiare...")
        con.execute("CREATE TABLE financiar AS SELECT * FROM src.financiar")
        con.execute("CREATE TABLE meta AS SELECT * FROM src.meta")
        con.execute("INSERT INTO meta VALUES ('remote_schema', '1')")
        con.commit()
    finally:
        con.close()

    log("comprim indexul remote...")
    with open(db_output, "rb") as source_file, lzma.open(output, "wb", preset=6) as target:
        shutil.copyfileobj(source_file, target, 1024 * 1024)
    os.remove(db_output)
    log("index remote: %s (%.1f MB)" % (output, os.path.getsize(output) / 1048576.0))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sursa", default=os.path.expanduser("~/.cache/anaf-mcp/anaf-index.sqlite"))
    parser.add_argument("--iesire", default="anaf-remote-index.sqlite.xz")
    args = parser.parse_args()
    build(args.sursa, args.iesire)


if __name__ == "__main__":
    main()
