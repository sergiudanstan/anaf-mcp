#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Descarca asset-ul compact in timpul build-ului Vercel."""

import os
import urllib.request


URL = (
    "https://github.com/sergiudanstan/anaf-mcp/releases/download/"
    "latest/anaf-remote-index.sqlite.xz"
)
OUTPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "anaf-remote-index.sqlite.xz",
)
MIN_BYTES = 10 * 1024 * 1024


def main():
    if os.path.exists(OUTPUT) and os.path.getsize(OUTPUT) >= MIN_BYTES:
        print("index remote deja prezent: %s" % OUTPUT)
        return
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    tmp = OUTPUT + ".tmp"
    request = urllib.request.Request(URL, headers={"User-Agent": "anaf-mcp-build/1.3"})
    written = 0
    with urllib.request.urlopen(request, timeout=180) as response, open(tmp, "wb") as target:
        expected = int(response.headers.get("Content-Length") or 0) or None
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
            written += len(chunk)
    if written < MIN_BYTES or (expected and written != expected):
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("Index remote incomplet: %d bytes" % written)
    os.replace(tmp, OUTPUT)
    print("index remote descarcat: %.1f MB" % (written / 1048576.0))


if __name__ == "__main__":
    main()

