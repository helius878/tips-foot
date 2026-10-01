#!/usr/bin/env python3
"""Sonde Highlightly : appelle UN endpoint et affiche une version compacte de la réponse.

Variables d'environnement : HIGHLIGHTLY_KEY, PROBE_PATH, PROBE_PARAMS
Chaque appel consomme 1 requête du quota gratuit (100 par jour).
La clé n'est jamais affichée.
"""
import json
import os
import sys

import requests

BASE = "https://sports.highlightly.net"
MAX_CHARS = 3000      # réponses qui ne sont pas des listes
MAX_ITEMS = 30        # éléments affichés pour une liste
LINE_CHARS = 280      # longueur max d'un élément affiché


def strip_logos(x):
    """Retire les champs 'logo' (inutiles ici) pour raccourcir l'affichage."""
    if isinstance(x, dict):
        return {k: strip_logos(v) for k, v in x.items() if k != "logo"}
    if isinstance(x, list):
        return [strip_logos(v) for v in x]
    return x


def compact(x):
    return json.dumps(strip_logos(x), ensure_ascii=False, separators=(",", ":"))


def show(text):
    try:
        js = json.loads(text)
    except ValueError:
        print(text[:MAX_CHARS])
        return
    if isinstance(js, dict) and isinstance(js.get("data"), list):
        items = js["data"]
        print(f"{len(items)} élément(s) dans data")
        for it in items[:MAX_ITEMS]:
            print(compact(it)[:LINE_CHARS])
        if len(items) > MAX_ITEMS:
            print(f"[... {len(items) - MAX_ITEMS} autres éléments non affichés]")
        for k, v in js.items():
            if k != "data":
                print(f"{k} : {compact(v)[:300]}")
    else:
        out = compact(js)
        print(out[:MAX_CHARS])
        if len(out) > MAX_CHARS:
            print(f"[... coupé, {len(out)} caractères au total]")


def main():
    path = os.environ.get("PROBE_PATH", "").strip()
    if not path.startswith("/football/") or ".." in path:
        sys.exit("Le chemin doit commencer par /football/ (ex. /football/matches).")
    params = {}
    for item in os.environ.get("PROBE_PARAMS", "").split():
        if "=" in item:
            k, v = item.split("=", 1)
            params[k] = v
    key = os.environ.get("HIGHLIGHTLY_KEY", "").strip()
    if not key:
        sys.exit("Le secret HIGHLIGHTLY_KEY est vide ou mal nommé.")

    r = requests.get(BASE + path, params=params, timeout=30, headers={"x-rapidapi-key": key})
    print(f"URL : {BASE}{path}  params={params}")
    print(f"Statut HTTP : {r.status_code}")
    for name, value in r.headers.items():
        if "ratelimit-requests" in name.lower():
            print(f"{name}: {value}")
    print("--- Réponse ---")
    show(r.text)


if __name__ == "__main__":
    main()
