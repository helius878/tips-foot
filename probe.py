#!/usr/bin/env python3
"""Sonde Highlightly : appelle UN endpoint et affiche le début de la réponse.

Sert à voir le vrai format des réponses avant d'écrire le script final.
Variables d'environnement : HIGHLIGHTLY_KEY, PROBE_PATH, PROBE_PARAMS
Chaque appel consomme 1 requête du quota gratuit (100 par jour).
"""
import os
import sys

import requests

BASE = "https://sports.highlightly.net"
MAX_CHARS = 2500

path = os.environ.get("PROBE_PATH", "").strip()
if not path.startswith("/football/") or ".." in path:
    sys.exit("Le chemin doit commencer par /football/ (ex. /football/matches).")

params = {}
for item in os.environ.get("PROBE_PARAMS", "").split():
    if "=" in item:
        k, v = item.split("=", 1)
        params[k] = v

r = requests.get(BASE + path, params=params, timeout=30,
                 headers={"x-rapidapi-key": os.environ["HIGHLIGHTLY_KEY"]})

print(f"URL : {BASE}{path}  params={params}")
print(f"Statut HTTP : {r.status_code}")
for name, value in r.headers.items():
    if "limit" in name.lower() or "remaining" in name.lower():
        print(f"{name}: {value}")
print("--- Début de la réponse ---")
print(r.text[:MAX_CHARS])
if len(r.text) > MAX_CHARS:
    print(f"\n[... coupé, {len(r.text)} caractères au total]")
