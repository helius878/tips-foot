#!/usr/bin/env python3
"""Sonde Highlightly : appelle UN endpoint et affiche le début de la réponse.

Sert à voir le vrai format des réponses avant d'écrire le script final.
Variables d'environnement : HIGHLIGHTLY_KEY, PROBE_PATH, PROBE_PARAMS
Chaque appel consomme au plus 1 requête du quota gratuit (100 par jour).
La clé n'est jamais affichée : seulement sa longueur.
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

key = os.environ.get("HIGHLIGHTLY_KEY", "").strip()
print(f"Longueur de la clé lue dans le secret : {len(key)} caractères")
if not key:
    sys.exit("Le secret HIGHLIGHTLY_KEY est vide ou mal nommé.")

# En-têtes d'authentification possibles : on essaie jusqu'à ce que l'un soit accepté.
VARIANTS = [
    ("x-rapidapi-key", {"x-rapidapi-key": key}),
    ("x-api-key", {"x-api-key": key}),
    ("Authorization: Bearer", {"Authorization": f"Bearer {key}"}),
]

print(f"URL : {BASE}{path}  params={params}")
for label, headers in VARIANTS:
    r = requests.get(BASE + path, params=params, headers=headers, timeout=30)
    print(f"[{label}] -> statut HTTP {r.status_code}")
    if r.status_code != 401:
        break

print(f"En-tête retenu : {label}")
for name, value in r.headers.items():
    if "limit" in name.lower() or "remaining" in name.lower():
        print(f"{name}: {value}")
print("--- Début de la réponse ---")
print(r.text[:MAX_CHARS])
if len(r.text) > MAX_CHARS:
    print(f"\n[... coupé, {len(r.text)} caractères au total]")
