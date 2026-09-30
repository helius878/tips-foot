#!/usr/bin/env python3
"""paris-foot : paris simples sur joueurs (buteur, passeur, décisif), 100 % gratuit.

Modes :
  check       vérifie les IDs de championnats et ce que l'API couvre (5 requêtes)
  test        envoie un message de test sur Telegram
  preselect   présélection du jour (à minuit, heure de Paris)
  confirm     confirme avec les compos officielles (toutes les 15 min les jours de match)
  mise P COTE BANKROLL   calcule la mise (ex. : mise 0.32 3.8 500)

Secrets attendus (variables d'environnement) :
  API_FOOTBALL_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import csv
import json
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- Configuration
# IDs API-Football : à vérifier avec `python paris_foot.py check`
LEAGUES = {172: "Bulgarie", 144: "Belgique", 210: "Croatie", 106: "Pologne", 203: "Turquie"}
SEASON = 2026                 # saison 2026-2027 = année de début
TZ = ZoneInfo("Europe/Paris")

EDGE = 0.08                   # value minimale exigée (8 %)
KELLY_FRACTION = 0.25         # quart de Kelly
STAKE_CAP = 0.015             # mise max : 1,5 % de la bankroll

MIN_APPS = 3                  # apparitions minimales cette saison
MIN_MINUTES = 200             # minutes minimales cette saison
MIN_START_RATE = 0.75         # part de matchs démarrés pour être « titulaire probable »
MIN_AVG_MIN = 60              # minutes moyennes par apparition
MIN_P = {"buteur": 0.20, "passeur": 0.15, "decisif": 0.30}   # probabilité minimale affichée
TOP_PER_MARKET = 3            # joueurs affichés par marché et par match

PRIOR_WEIGHT = 5              # poids (en matchs de 90') de la moyenne a priori
PRIORS = {                    # (buts/90, passes décisives/90) a priori par poste
    "Attacker": (0.40, 0.15),
    "Midfielder": (0.10, 0.12),
    "Defender": (0.04, 0.05),
    "Goalkeeper": (0.0, 0.0),
}
HOME, AWAY = 1.08, 0.92       # facteur domicile / extérieur

QUOTA_RESERVE = 25            # requêtes gardées pour les compos
CALL_DELAY = 6.5              # secondes entre 2 requêtes (limite par minute du plan gratuit)
CACHE_DAYS = 7                # durée de vie des stats joueurs en cache
LINEUP_TRIES = [75, 50, 35, 20]   # minutes avant coup d'envoi des essais de compo

API = "https://v3.football.api-sports.io"
DATA = "data"
MARKETS = {"buteur": "Buteur", "passeur": "Passeur", "decisif": "Décisif (but ou passe)"}


class QuotaLow(Exception):
    pass


# ---------------------------------------------------------------- API-Football
class Api:
    def __init__(self, key):
        self.key = key
        self.remaining = None
        self.last_call = 0.0

    def get(self, path, params=None, essential=True):
        if not essential and self.remaining is not None and self.remaining <= QUOTA_RESERVE:
            raise QuotaLow()
        wait = CALL_DELAY - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(2):
            r = requests.get(f"{API}/{path}", headers={"x-apisports-key": self.key},
                             params=params, timeout=30)
            self.last_call = time.time()
            rem = r.headers.get("x-ratelimit-requests-remaining")
            if rem is not None and rem.isdigit():
                self.remaining = int(rem)
            if r.status_code == 429 and attempt == 0:
                time.sleep(65)
                continue
            break
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"API-Football /{path} : {data['errors']}")
        return data


# ---------------------------------------------------------------- Fichiers
def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def journal(rows):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "journal.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(["date", "match", "joueur", "marche", "proba", "cote_min", "cote_prise", "resultat"])
        w.writerows(rows)


# ---------------------------------------------------------------- Telegram
def telegram(text):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    for i in range(0, len(text), 4000):
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": text[i:i + 4000]}, timeout=30)
        except requests.RequestException:
            # on ne relaie pas l'exception : elle contiendrait l'URL, donc le token
            raise RuntimeError("Telegram : erreur réseau") from None
        if not r.ok:
            raise RuntimeError(f"Telegram : HTTP {r.status_code} {r.text[:200]}")


# ---------------------------------------------------------------- Modèle
def min_odds(p):
    return (1 + EDGE) / p


def stake_fraction(p, odds):
    edge = p * odds - 1
    if edge < EDGE:
        return 0.0
    return min(STAKE_CAP, KELLY_FRACTION * edge / (odds - 1))


def rates(p):
    """Buts et passes décisives par 90', ramenés vers une moyenne par poste."""
    prior_g, prior_a = PRIORS.get(p["pos"], (0.10, 0.08))
    n90 = p["minutes"] / 90
    return ((p["goals"] + PRIOR_WEIGHT * prior_g) / (n90 + PRIOR_WEIGHT),
            (p["assists"] + PRIOR_WEIGHT * prior_a) / (n90 + PRIOR_WEIGHT))


def opp_factor(teams, opp_id):
    """Faiblesse défensive de l'adversaire vs moyenne du championnat (1.0 = moyenne)."""
    tot_played = sum(t["played"] for t in teams.values())
    tot_ga = sum(t["ga"] for t in teams.values())
    o = teams.get(str(opp_id))
    if not o or not tot_played or not o["played"] or tot_ga <= 0:
        return 1.0
    league_avg = tot_ga / tot_played
    w = o["played"] / (o["played"] + 5)
    f = w * (o["ga"] / o["played"]) / league_avg + (1 - w)
    return min(1.4, max(0.7, f))


def evaluate(players, opp_f, home, team_name, starters=None, injured=()):
    """Probabilités par joueur. starters=None : titulaires probables ; sinon onze officiel."""
    out = []
    for pid, p in players.items():
        if p["pos"] == "Goalkeeper" or pid in injured:
            continue
        avg = p["minutes"] / p["apps"] if p["apps"] else 0
        if starters is not None:
            if int(pid) not in starters:
                continue
            exp = min(90, max(avg, 70))
        else:
            if p["apps"] < MIN_APPS or p["minutes"] < MIN_MINUTES:
                continue
            if p["starts"] / p["apps"] < MIN_START_RATE or avg < MIN_AVG_MIN:
                continue
            exp = min(90, avg)
        rg, ra = rates(p)
        f = opp_f * (HOME if home else AWAY)
        lg, la = rg * exp / 90 * f, ra * exp / 90 * f
        out.append({"id": int(pid), "name": p["name"], "team": team_name,
                    "buteur": 1 - math.exp(-lg), "passeur": 1 - math.exp(-la),
                    "decisif": 1 - math.exp(-(lg + la))})
    return out


def top_picks(cands):
    picks = []
    for m in MARKETS:
        best = sorted((c for c in cands if c[m] >= MIN_P[m]), key=lambda c: -c[m])[:TOP_PER_MARKET]
        picks += [{"market": m, "p": c[m], "id": c["id"], "name": c["name"], "team": c["team"]}
                  for c in best]
    return picks


def pick_line(pk):
    o = min_odds(pk["p"])
    return (f"• {MARKETS[pk['market']]} : {pk['name']} ({pk['team']}) "
            f"{pk['p']:.0%} → cote min {o:.2f} (mise {stake_fraction(pk['p'], o):.1%})")


# ---------------------------------------------------------------- Données
def fetch_fixtures(api, day):
    data = api.get("fixtures", {"date": day, "timezone": "Europe/Paris"})
    out = []
    for it in data["response"]:
        if it["league"]["id"] not in LEAGUES or it["fixture"]["status"]["short"] not in ("NS", "TBD"):
            continue
        out.append({"id": it["fixture"]["id"], "league": it["league"]["id"],
                    "ts": it["fixture"]["timestamp"],
                    "home": {"id": it["teams"]["home"]["id"], "name": it["teams"]["home"]["name"]},
                    "away": {"id": it["teams"]["away"]["id"], "name": it["teams"]["away"]["name"]}})
    return sorted(out, key=lambda x: x["ts"])


def league_table(api, cache, lid, today):
    entry = cache["standings"].get(str(lid))
    if entry and entry["date"] == today:
        return entry["teams"]
    data = api.get("standings", {"league": lid, "season": SEASON})
    teams = {}
    for resp in data["response"]:
        for group in resp["league"]["standings"]:
            for t in group:
                a = t["all"]
                teams[str(t["team"]["id"])] = {"played": a["played"], "gf": a["goals"]["for"],
                                               "ga": a["goals"]["against"]}
    cache["standings"][str(lid)] = {"date": today, "teams": teams}
    return teams


def fetch_injured(api, lid, today):
    data = api.get("injuries", {"league": lid, "season": SEASON, "date": today}, essential=False)
    return {str(it["player"]["id"]) for it in data["response"]}


def team_players(api, cache, team_id):
    entry = cache["teams"].get(str(team_id))
    if entry and time.time() - entry["ts"] < CACHE_DAYS * 86400:
        return entry["players"]
    players, page = {}, 1
    try:
        while True:
            data = api.get("players", {"team": team_id, "season": SEASON, "page": page}, essential=False)
            for it in data["response"]:
                agg = {"apps": 0, "starts": 0, "minutes": 0, "goals": 0, "assists": 0, "pos": None}
                for s in it["statistics"]:
                    if s["team"]["id"] != team_id:
                        continue
                    g = s["games"]
                    agg["apps"] += g.get("appearences") or 0
                    agg["starts"] += g.get("lineups") or 0
                    agg["minutes"] += g.get("minutes") or 0
                    agg["goals"] += s["goals"].get("total") or 0
                    agg["assists"] += s["goals"].get("assists") or 0
                    agg["pos"] = agg["pos"] or g.get("position")
                players[str(it["player"]["id"])] = {"name": it["player"]["name"], **agg}
            if page >= data["paging"]["total"]:
                break
            page += 1
    except QuotaLow:
        return entry["players"] if entry else None
    cache["teams"][str(team_id)] = {"ts": time.time(), "players": players}
    return players


def hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


# ---------------------------------------------------------------- Présélection
def run_preselect(api, force):
    now = datetime.now(TZ)
    if now.hour != 0 and not force:
        print("Ce n'est pas l'heure de la présélection (minuit, heure de Paris).")
        return
    today = now.strftime("%Y-%m-%d")
    cache = load("cache.json", {"teams": {}, "standings": {}})
    fixtures = fetch_fixtures(api, today)
    state = {"date": today, "fixtures": fixtures, "pre": {}, "tries": {}, "confirmed": [], "gaveup": []}
    if not fixtures:
        save("state.json", state)
        telegram(f"Aucun match aujourd'hui ({today}) dans tes championnats.")
        return

    tables, injured, notes = {}, {}, []
    for lid in sorted({f["league"] for f in fixtures}):
        try:
            tables[lid] = league_table(api, cache, lid, today)
        except (RuntimeError, requests.RequestException) as e:
            tables[lid] = {}
            notes.append(f"classement {LEAGUES[lid]} indisponible ({e})")
        try:
            injured[lid] = fetch_injured(api, lid, today)
        except (QuotaLow, RuntimeError, requests.RequestException):
            injured[lid] = set()
            notes.append(f"blessés {LEAGUES[lid]} non vérifiés")

    msgs = [f"🌙 Présélection du {today} : {len(fixtures)} match(s).\n"
            f"Cote min = cote à partir de laquelle le pari a {EDGE:.0%} de value selon le modèle. "
            f"La mise indiquée vaut pour une cote égale à la cote min (plus la cote réelle est haute, "
            f"plus la mise peut monter : commande « mise »). "
            f"Compos à confirmer environ 1 h avant chaque match."]
    for fx in fixtures:
        lid = fx["league"]
        lines = [f"⚽ {LEAGUES[lid]} · {fx['home']['name']} - {fx['away']['name']} ({hhmm(fx['ts'])})"]
        cands = []
        for side, other in (("home", "away"), ("away", "home")):
            t = fx[side]
            players = team_players(api, cache, t["id"])
            if players is None:
                lines.append(f"(stats indisponibles pour {t['name']} : quota API atteint)")
                continue
            cands += evaluate(players, opp_factor(tables[lid], fx[other]["id"]), side == "home",
                              t["name"], injured=injured[lid])
        picks = top_picks(cands)
        state["pre"][str(fx["id"])] = [{"id": p["id"], "name": p["name"]} for p in picks]
        lines += [pick_line(p) for p in picks] or ["Aucun joueur ne passe les filtres."]
        msgs.append("\n".join(lines))
    if notes:
        msgs.append("⚠️ " + " ; ".join(notes))
    if api.remaining is not None:
        msgs.append(f"Requêtes API restantes : {api.remaining}")
    save("cache.json", cache)
    save("state.json", state)
    telegram("\n\n".join(msgs))


# ---------------------------------------------------------------- Confirmation
def run_confirm(api):
    state = load("state.json", {})
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if state.get("date") != today:
        print("Pas de présélection pour aujourd'hui.")
        return
    cache = load("cache.json", {"teams": {}, "standings": {}})
    changed = False
    for fx in state["fixtures"]:
        fid = str(fx["id"])
        if fx["id"] in state["confirmed"] or fx["id"] in state["gaveup"]:
            continue
        mins = (fx["ts"] - now.timestamp()) / 60
        if mins < 0:
            state["gaveup"].append(fx["id"])
            changed = True
            continue
        tries = state["tries"].get(fid, 0)
        if mins > LINEUP_TRIES[tries]:
            continue
        label = f"{fx['home']['name']} - {fx['away']['name']} ({hhmm(fx['ts'])})"
        try:
            lineups = api.get("fixtures/lineups", {"fixture": fx["id"]})["response"]
        except (RuntimeError, requests.RequestException) as e:
            print(f"Compos {label} : {e}")
            lineups = []
        state["tries"][fid] = tries + 1
        changed = True
        ready = len(lineups) >= 2 and all(l.get("startXI") for l in lineups)
        if not ready:
            if tries + 1 >= len(LINEUP_TRIES):
                state["gaveup"].append(fx["id"])
                telegram(f"⏳ {label} : compo non publiée à temps. "
                         f"Ne joue que si tu as vu la compo toi-même.")
            continue

        xi = {l["team"]["id"]: {p["player"]["id"] for p in l["startXI"]} for l in lineups}
        table = cache["standings"].get(str(fx["league"]), {}).get("teams", {})
        cands = []
        for side, other in (("home", "away"), ("away", "home")):
            t = fx[side]
            entry = cache["teams"].get(str(t["id"]))
            if not entry or t["id"] not in xi:
                continue
            cands += evaluate(entry["players"], opp_factor(table, fx[other]["id"]), side == "home",
                              t["name"], starters=xi[t["id"]])
        picks = top_picks(cands)
        all_xi = set().union(*xi.values())
        out = sorted({p["name"] for p in state["pre"].get(fid, []) if p["id"] not in all_xi})
        lines = [f"✅ Compos confirmées · {label}"]
        lines += [pick_line(p) for p in picks] or ["Aucun joueur ne passe les filtres avec ce onze."]
        if out:
            lines.append("❌ Présélectionnés mais hors du onze : " + ", ".join(out))
        lines.append("Compare avec la cote de ton bookmaker : pas de pari si elle est sous la cote min.")
        telegram("\n".join(lines))
        journal([[today, label, p["name"], p["market"], f"{p['p']:.3f}", f"{min_odds(p['p']):.2f}", "", ""]
                 for p in picks])
        state["confirmed"].append(fx["id"])
    if changed:
        save("state.json", state)
    if api.remaining is not None:
        print(f"Requêtes API restantes : {api.remaining}")


# ---------------------------------------------------------------- Outils
def run_check(api):
    for lid, name in LEAGUES.items():
        data = api.get("leagues", {"id": lid, "season": SEASON})
        if not data["response"]:
            print(f"{lid} ({name}) : aucune réponse pour la saison {SEASON}")
            continue
        r = data["response"][0]
        cov = (r["seasons"][0].get("coverage") or {}) if r.get("seasons") else {}
        fx = cov.get("fixtures") or {}
        print(f"{lid} = {r['league']['name']} ({r['country']['name']}) | "
              f"compos: {fx.get('lineups')} | stats joueurs: {cov.get('players')} | "
              f"classement: {cov.get('standings')} | blessés: {cov.get('injuries')}")
    print(f"Requêtes API restantes : {api.remaining}")


def run_mise(p, odds, bankroll):
    edge = p * odds - 1
    f = stake_fraction(p, odds)
    if f == 0:
        print(f"Value {edge:+.1%} < seuil {EDGE:.0%} : pas de pari.")
    else:
        print(f"Value {edge:+.1%} · mise {f * bankroll:.2f} ({f:.2%} de la bankroll)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["check", "test", "preselect", "confirm", "mise"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    force = a.force or os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    if a.mode == "mise":
        run_mise(float(a.args[0]), float(a.args[1]), float(a.args[2]))
    elif a.mode == "test":
        telegram("✅ Test paris-foot : le bot fonctionne.")
    else:
        api = Api(os.environ["API_FOOTBALL_KEY"])
        {"check": lambda: run_check(api),
         "preselect": lambda: run_preselect(api, force),
         "confirm": lambda: run_confirm(api)}[a.mode]()


if __name__ == "__main__":
    main()
