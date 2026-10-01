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


def call(key, path, params=None):
    return requests.get(BASE + path, params=params or {}, timeout=30,
                        headers={"x-rapidapi-key": key})


def find_players(x):
    """Trouve les petits blocs qui ressemblent à un joueur (id, name, position ou number)."""
    if isinstance(x, dict):
        if isinstance(x.get("id"), int) and "name" in x and ("position" in x or "number" in x):
            yield x
        for v in x.values():
            yield from find_players(v)
    elif isinstance(x, list):
        for v in x:
            yield from find_players(v)


def chain(key, params):
    """3 requêtes d'affilée : matchs d'un championnat un jour donné, compos du premier match terminé,
    stats du premier joueur de ces compos."""
    lid, date = params.get("leagueId"), params.get("date")
    if not lid or not date:
        sys.exit("chain : indique leagueId=... et date=AAAA-MM-JJ dans les paramètres.")
    r = call(key, "/football/matches", {"leagueId": lid, "date": date})
    print(f"[1] /football/matches leagueId={lid} date={date} -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        return
    items = r.json().get("data", [])
    mine = [m for m in items if str((m.get("league") or {}).get("id")) == str(lid)]
    print(f"{len(items)} match(s) renvoyé(s), dont {len(mine)} de la compétition {lid}")
    for m in mine[:15]:
        st = m.get("state") or {}
        score = (st.get("score") or {}).get("current")
        print(f"  id={m.get('id')} {m.get('date')} {(m.get('homeTeam') or {}).get('name')} - "
              f"{(m.get('awayTeam') or {}).get('name')} [{st.get('description')}] {score}")
    done = [m for m in mine if (m.get("state") or {}).get("description") == "Finished"]
    if not done:
        print("Aucun match terminé trouvé : relance avec une autre date.")
        return
    mid = done[0]["id"]
    r = call(key, f"/football/lineups/{mid}")
    print(f"\n[2] /football/lineups/{mid} -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:500])
        return
    js = r.json()
    print(compact(js)[:3500])
    players = list(find_players(js))
    if not players:
        print("Aucun joueur identifié dans les compos.")
        return
    pid = players[0]["id"]
    r = call(key, f"/football/players/{pid}/statistics")
    print(f"\n[3] /football/players/{pid}/statistics -> HTTP {r.status_code}")
    print(compact(r.json())[:3500] if r.status_code == 200 else r.text[:500])


def main():
    path = os.environ.get("PROBE_PATH", "").strip()
    if path != "chain" and (not path.startswith("/football/") or ".." in path):
        sys.exit("Le chemin doit être 'chain' ou commencer par /football/ (ex. /football/matches).")
    params = {}
    for item in os.environ.get("PROBE_PARAMS", "").split():
        if "=" in item:
            k, v = item.split("=", 1)
            params[k] = v
    key = os.environ.get("HIGHLIGHTLY_KEY", "").strip()
    if not key:
        sys.exit("Le secret HIGHLIGHTLY_KEY est vide ou mal nommé.")

    if path == "chain":
        chain(key, params)
        return
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
