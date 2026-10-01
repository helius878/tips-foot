#!/usr/bin/env python3
"""paris-foot (Highlightly, version gratuite réduite).

Paris simples sur joueurs : buteur, passeur, décisif (but ou passe).
Championnats : Jupiler Pro League (Belgique) et Süper Lig (Turquie).

Modes :
  test        envoie un message de test sur Telegram
  programme   programme du jour à minuit (heure de Paris), sans analyse
  confirm     toutes les 15 min : dès que la compo officielle est publiée, analyse du match
  mise P COTE BANKROLL   calcule la mise (ex. : mise 0.32 3.8 500)

Secrets attendus : HIGHLIGHTLY_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import csv
import json
import math
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- Configuration
BASE = "https://sports.highlightly.net"
LEAGUES = {123328: "Belgique", 173537: "Turquie"}   # identifiants Highlightly
TZ = ZoneInfo("Europe/Paris")

EDGE = 0.08                   # value minimale exigée (8 %)
KELLY_FRACTION = 0.25         # quart de Kelly
STAKE_CAP = 0.015             # mise max : 1,5 % de la bankroll

MAX_PER_TEAM = 5              # joueurs offensifs analysés par équipe (1 requête chacun)
MIN_P = {"buteur": 0.20, "passeur": 0.15, "decisif": 0.30}   # probabilité minimale affichée
TOP_PER_MARKET = 3            # joueurs affichés par marché et par match

PRIOR_WEIGHT = 5              # poids (en matchs de 90') de la moyenne a priori
PRIORS = {                    # (buts/90, passes décisives/90) a priori par poste
    "Forward": (0.40, 0.15),
    "Midfielder": (0.10, 0.12),
    "Defender": (0.04, 0.05),
    "Goalkeeper": (0.0, 0.0),
}
HOME, AWAY = 1.08, 0.92       # facteur domicile / extérieur
PREV_SEASON_WEIGHT = 0.5      # poids de la saison passée (même championnat)
PREV_OTHER_WEIGHT = 0.25      # poids de la saison passée (autre championnat)

QUOTA_RESERVE = 8             # requêtes gardées pour les compos
CALL_DELAY = 1.5              # secondes entre 2 requêtes
CACHE_DAYS = 10               # durée de vie des stats joueurs en cache
LINEUP_TRIES = [75, 50, 35, 20]   # minutes avant coup d'envoi des essais de compo
BAD_STATES = ("finish", "postpon", "cancel", "abandon", "suspend")

DATA = "data"
MARKETS = {"buteur": "Buteur", "passeur": "Passeur", "decisif": "Décisif (but ou passe)"}


class QuotaLow(Exception):
    pass


# ---------------------------------------------------------------- API Highlightly
class Api:
    def __init__(self, key):
        self.key = key
        self.remaining = None
        self.last_call = 0.0

    def get(self, path, params=None, essential=True):
        """Renvoie le JSON, ou None si 404 (ex. compo pas encore publiée)."""
        if not essential and self.remaining is not None and self.remaining <= QUOTA_RESERVE:
            raise QuotaLow()
        wait = CALL_DELAY - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(2):
            r = requests.get(BASE + path, params=params or {}, timeout=30,
                             headers={"x-rapidapi-key": self.key})
            self.last_call = time.time()
            rem = r.headers.get("x-ratelimit-requests-remaining")
            if rem is not None and rem.isdigit():
                self.remaining = int(rem)
            if r.status_code == 429 and attempt == 0:
                time.sleep(65)
                continue
            break
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


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


def probs(pos, prof, home):
    """Probabilités buteur / passeur / décisif pour un titulaire confirmé.
    Taux par 90' ramenés vers une moyenne par poste ; pas de facteur adversaire (v1)."""
    prior_g, prior_a = PRIORS.get(pos, (0.10, 0.08))
    n90 = prof["minutes"] / 90
    rg = (prof["goals"] + PRIOR_WEIGHT * prior_g) / (n90 + PRIOR_WEIGHT)
    ra = (prof["assists"] + PRIOR_WEIGHT * prior_a) / (n90 + PRIOR_WEIGHT)
    avg = prof["minutes"] / prof["apps"] if prof["apps"] > 0 else 0
    exp = min(90, max(avg, 70))
    f = HOME if home else AWAY
    lg, la = rg * exp / 90 * f, ra * exp / 90 * f
    return {"buteur": 1 - math.exp(-lg), "passeur": 1 - math.exp(-la),
            "decisif": 1 - math.exp(-(lg + la))}


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
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def season_labels(day):
    """('26/27', '25/26') pour une date AAAA-MM-JJ."""
    y, m = int(day[:4]), int(day[5:7])
    start = y if m >= 7 else y - 1
    return f"{start % 100:02d}/{(start + 1) % 100:02d}", f"{(start - 1) % 100:02d}/{start % 100:02d}"


def fetch_fixtures(api, day):
    out = []
    for lid in LEAGUES:
        js = api.get("/football/matches", {"leagueId": lid, "date": day})
        for m in (js or {}).get("data", []):
            if str((m.get("league") or {}).get("id")) != str(lid):
                continue
            desc = ((m.get("state") or {}).get("description") or "").lower()
            if any(b in desc for b in BAD_STATES):
                continue
            ts = parse_ts(m["date"])
            if datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d") != day:
                continue
            out.append({"id": m["id"], "league": lid, "ts": ts,
                        "home": {"id": m["homeTeam"]["id"], "name": m["homeTeam"]["name"]},
                        "away": {"id": m["awayTeam"]["id"], "name": m["awayTeam"]["name"]}})
    return sorted(out, key=lambda x: x["ts"])


def ensure_day(api, today):
    state = load("state.json", {})
    if state.get("date") == today:
        return state
    state = {"date": today, "fixtures": fetch_fixtures(api, today), "tries": {},
             "confirmed": [], "gaveup": []}
    save("state.json", state)
    return state


def lineup_players(team):
    """Joueurs du onze, du plus avancé au plus reculé (la dernière ligne = attaquants)."""
    rows = (team or {}).get("initialLineup") or []
    return [p for row in reversed(rows) for p in row]


def lineup_ready(js):
    return (isinstance(js, dict)
            and len(lineup_players(js.get("homeTeam"))) >= 10
            and len(lineup_players(js.get("awayTeam"))) >= 10)


def attackers(team):
    pool = [p for p in lineup_players(team) if p.get("position") in ("Forward", "Midfielder")]
    return pool[:MAX_PER_TEAM]


def base_league(name):
    return (name or "").split(" Relegation")[0].split(" Play")[0].strip()


def valid_row(r):
    txt = f"{r.get('club', '')} {r.get('league', '')}"
    kind = str(r.get("type", "")).lower()
    return (not re.search(r"\bU(1[5-9]|2[0-3])\b", txt)) and ("league" in kind or "cup" in kind)


def player_profile(api, cache, pid, labels):
    """Matchs, minutes, buts et passes décisives cumulés (saison en cours + saison passée pondérée)."""
    e = cache["players"].get(str(pid))
    if e and time.time() - e["ts"] < CACHE_DAYS * 86400:
        return e
    try:
        js = api.get(f"/football/players/{pid}/statistics", essential=False)
    except QuotaLow:
        return e
    prof = js[0] if isinstance(js, list) and js else js
    if not isinstance(prof, dict):
        return e
    cur_label, prev_label = labels
    rows = [r for r in (prof.get("perCompetition") or []) if valid_row(r)]
    cur = [r for r in rows if r.get("season") == cur_label]
    prev = [r for r in rows if r.get("season") == prev_label]
    cur_leagues = {base_league(r.get("league")) for r in cur}
    agg = {"apps": 0.0, "minutes": 0.0, "goals": 0.0, "assists": 0.0}

    def add(r, w):
        agg["apps"] += w * (r.get("gamesPlayed") or 0)
        agg["minutes"] += w * (r.get("minutesPlayed") or 0)
        agg["goals"] += w * (r.get("goals") or 0)
        agg["assists"] += w * (r.get("assists") or 0)

    for r in cur:
        add(r, 1.0)
    for r in prev:
        add(r, PREV_SEASON_WEIGHT if base_league(r.get("league")) in cur_leagues else PREV_OTHER_WEIGHT)
    entry = {"ts": time.time(), "name": prof.get("name"), **agg}
    cache["players"][str(pid)] = entry
    return entry


# ---------------------------------------------------------------- Programme du jour
def run_programme(api, force):
    now = datetime.now(TZ)
    if now.hour != 0 and not force:
        print("Ce n'est pas l'heure du programme (minuit, heure de Paris).")
        return
    today = now.strftime("%Y-%m-%d")
    fixtures = fetch_fixtures(api, today)
    save("state.json", {"date": today, "fixtures": fixtures, "tries": {},
                        "confirmed": [], "gaveup": []})
    if not fixtures:
        if force:
            telegram(f"Aucun match aujourd'hui ({today}) dans tes championnats.")
        return
    lines = [f"🌙 Programme du {today} : {len(fixtures)} match(s)."]
    lines += [f"• {hhmm(f['ts'])} {f['home']['name']} - {f['away']['name']} ({LEAGUES[f['league']]})"
              for f in fixtures]
    lines.append("L'analyse arrive environ 1 h avant chaque match, quand la compo officielle est publiée.")
    if api.remaining is not None:
        lines.append(f"Requêtes API restantes : {api.remaining}")
    telegram("\n".join(lines))


# ---------------------------------------------------------------- Confirmation
def run_confirm(api):
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    state = ensure_day(api, today)
    cache = load("cache.json", {"players": {}})
    labels = season_labels(today)
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
            js = api.get(f"/football/lineups/{fx['id']}")
        except requests.RequestException as e:
            print(f"Compos {label} : {e}")
            js = None
        state["tries"][fid] = tries + 1
        changed = True
        if not lineup_ready(js):
            if tries + 1 >= len(LINEUP_TRIES):
                state["gaveup"].append(fx["id"])
                telegram(f"⏳ {label} : compo non publiée à temps. "
                         f"Ne joue que si tu as vu la compo toi-même.")
            continue

        cands, missing = [], 0
        for side in ("home", "away"):
            team = js[f"{side}Team"]
            for pl in attackers(team):
                try:
                    prof = player_profile(api, cache, pl["id"], labels)
                except requests.RequestException as e:
                    print(f"Stats joueur {pl.get('name')} : {e}")
                    prof = None
                if not prof:
                    missing += 1
                    continue
                cands.append({"id": pl["id"], "name": pl["name"], "team": fx[side]["name"],
                              **probs(pl["position"], prof, side == "home")})
        save("cache.json", cache)
        picks = top_picks(cands)
        lines = [f"✅ Compos confirmées · {LEAGUES[fx['league']]} · {label}"]
        lines += [pick_line(p) for p in picks] or ["Aucun joueur ne passe les filtres avec ce onze."]
        if missing:
            lines.append(f"⚠️ Stats indisponibles pour {missing} joueur(s) (quota API ou données manquantes).")
        lines.append("Cote min = cote à partir de laquelle le pari a "
                     f"{EDGE:.0%} de value selon le modèle. Pas de pari si la cote de ton bookmaker est en dessous.")
        telegram("\n".join(lines))
        journal([[today, label, p["name"], p["market"], f"{p['p']:.3f}", f"{min_odds(p['p']):.2f}", "", ""]
                 for p in picks])
        state["confirmed"].append(fx["id"])
    if changed:
        save("state.json", state)
    if api.remaining is not None:
        print(f"Requêtes API restantes : {api.remaining}")


# ---------------------------------------------------------------- Outils
def run_mise(p, odds, bankroll):
    edge = p * odds - 1
    f = stake_fraction(p, odds)
    if f == 0:
        print(f"Value {edge:+.1%} < seuil {EDGE:.0%} : pas de pari.")
    else:
        print(f"Value {edge:+.1%} · mise {f * bankroll:.2f} ({f:.2%} de la bankroll)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["test", "programme", "confirm", "mise"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    force = a.force or os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch"

    if a.mode == "mise":
        run_mise(float(a.args[0]), float(a.args[1]), float(a.args[2]))
    elif a.mode == "test":
        telegram("✅ Test paris-foot : le bot fonctionne.")
    else:
        api = Api(os.environ["HIGHLIGHTLY_KEY"].strip())
        if a.mode == "programme":
            run_programme(api, force)
        else:
            run_confirm(api)


if __name__ == "__main__":
    main()
