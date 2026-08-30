#!/usr/bin/env python3
"""Web UI: sync watched anime from Plex to MyAnimeList.

Run: python3 plex_mal_sync.py   (then open http://localhost:5057)
State (tokens etc) persists in plex_mal_sync_config.json next to this file.
"""
import difflib
import json
import logging
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import requests
from flask import Flask, redirect, request, render_template_string

CONFIG_PATH = os.environ.get(
    "CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "plex_mal_sync_config.json"),
)
PORT = int(os.environ.get("PORT", 5057))
MATCH_THRESHOLD = 0.5  # ponytail: plain difflib ratio, raise/replace with rapidfuzz if mismatches show up

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("plex_mal_sync")

app = Flask(__name__)
sync_state = {"running": False, "results": None}


def redirect_uri():
    # MAL_REDIRECT_URI env overrides for reverse-proxy setups; otherwise match
    # whatever host/IP the browser is actually using so it lines up with
    # whatever was registered on myanimelist.net/apiconfig.
    return os.environ.get("MAL_REDIRECT_URI") or request.host_url.rstrip("/") + "/callback"


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---- pure logic (see test_plex_mal_sync.py) ----

def compute_status(leaf_count, viewed_leaf_count):
    """Return (status, episodes) for MAL, or (None, 0) if not started."""
    if leaf_count <= 0 or viewed_leaf_count <= 0:
        return None, 0
    if viewed_leaf_count >= leaf_count:
        return "completed", leaf_count
    return "watching", viewed_leaf_count


def candidate_names(c):
    """MAL's primary title is often Japanese romaji; check English/synonym titles too."""
    alt = c.get("alternative_titles") or {}
    names = [c["title"]]
    if alt.get("en"):
        names.append(alt["en"])
    if alt.get("ja"):
        names.append(alt["ja"])
    names.extend(alt.get("synonyms") or [])
    return names


def episode_count_compatible(num_episodes, total_episodes):
    """True if a candidate's own episode count could plausibly correspond to Plex's reported
    total: an exact single-season match, or an even divisor for a Plex-merged multi-season show
    (e.g. a 13-episode season legitimately divides a 26-episode merged total; a 12-episode one
    doesn't). Either side being unknown (0, or still-airing on MAL) can't be ruled out.
    A 1-episode candidate (movie/special) is excluded from the divisor case - "anything % 1 == 0"
    is mathematically true but not a meaningful signal, and would wrongly out-compete an ongoing
    TV series entry (0/unknown episodes) that happens to tie on title (e.g. a movie's alternate
    Japanese title matching the main series' title exactly)."""
    if not num_episodes or not total_episodes:
        return True
    if num_episodes >= total_episodes:
        return num_episodes == total_episodes
    if num_episodes <= 1:
        return False
    return total_episodes % num_episodes == 0


def best_match(title, candidates, total_episodes=0):
    """candidates: list of {"id":..,"title":..,"alternative_titles":..,"num_episodes":..}.
    Returns (candidate, ratio) or (None, 0). total_episodes (Plex's leaf_count for the show, if
    known) breaks ties among near-identical titles - e.g. a show's two seasons ("X" vs "X S")
    can score almost the same string-similarity, but only one has an episode count that's
    actually compatible with what Plex reports having watched."""
    t = title.lower()
    scored = []
    for c in candidates:
        ratio = max(difflib.SequenceMatcher(None, t, name.lower()).ratio() for name in candidate_names(c))
        scored.append((ratio, c))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda x: -x[0])
    top_ratio = scored[0][0]
    TIE_MARGIN = 0.05
    tied = [(r, c) for r, c in scored if top_ratio - r <= TIE_MARGIN]
    eligible = [(r, c) for r, c in tied if episode_count_compatible(c.get("num_episodes", 0), total_episodes)]
    best_ratio, best = (eligible or tied)[0]
    return best, best_ratio


# ---- Plex ----

def plex_get(cfg, path, **params):
    r = requests.get(
        cfg["plex_base_url"].rstrip("/") + path,
        headers={"X-Plex-Token": cfg["plex_token"], "Accept": "application/json"},
        params=params,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["MediaContainer"]


def plex_list_show_sections(cfg):
    """Return [{"key","title"}] for every TV-type library, for the library picker."""
    dirs = plex_get(cfg, "/library/sections").get("Directory", [])
    return [{"key": d["key"], "title": d["title"]} for d in dirs if d.get("type") == "show"]


def plex_watched_shows(cfg):
    """Yield {"title", "leaf_count", "viewed_leaf_count"} for shows in the selected libraries
    (or every TV library if none were picked)."""
    keys = cfg.get("plex_library_keys") or None
    sections = plex_get(cfg, "/library/sections").get("Directory", [])
    for section in sections:
        if section.get("type") != "show":
            continue
        if keys and section["key"] not in keys:
            continue
        shows = plex_get(cfg, f"/library/sections/{section['key']}/all").get("Metadata", [])
        for s in shows:
            yield {
                "title": s["title"],
                "leaf_count": s.get("leafCount", 0),
                "viewed_leaf_count": s.get("viewedLeafCount", 0),
            }


# ---- MAL ----

def mal_headers(cfg):
    return {"Authorization": f"Bearer {cfg['mal_access_token']}"}


def mal_ensure_token(cfg):
    if cfg.get("mal_token_expires_at", 0) > time.time() + 60:
        return
    log.info("MAL access token expired, refreshing")
    r = mal_request(
        requests.post, "https://myanimelist.net/v1/oauth2/token",
        data={
            "client_id": cfg["mal_client_id"],
            "client_secret": cfg.get("mal_client_secret") or "",
            "grant_type": "refresh_token",
            "refresh_token": cfg["mal_refresh_token"],
        },
        timeout=15,
    )
    tok = r.json()
    cfg["mal_access_token"] = tok["access_token"]
    cfg["mal_refresh_token"] = tok["refresh_token"]
    cfg["mal_token_expires_at"] = time.time() + tok["expires_in"]
    save_config(cfg)


def mal_request(method, url, **kwargs):
    # MAL's API is frequently slow/flaky (intermittent 15s+ read timeouts unrelated to the
    # request itself) - retry transient failures a couple times before giving up.
    last_exc = None
    for attempt in range(3):
        try:
            r = method(url, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            if attempt < 2:
                log.warning("MAL request to %s failed (attempt %d/3): %s - retrying", url, attempt + 1, e)
                time.sleep(2)
    raise last_exc


def mal_current_list(cfg):
    """Fetch the user's whole MAL list once: {anime_id: (status, num_episodes_watched, num_episodes_total)}.
    Lets the sync skip writing entries that already match, instead of PATCHing every show
    on every run. nsfw=true is required - MAL otherwise silently omits gray/black-flagged
    entries from this listing even though they're genuinely on the list."""
    current = {}
    url = "https://api.myanimelist.net/v2/users/@me/animelist"
    params = {"fields": "list_status,num_episodes", "limit": 100, "nsfw": "true"}  # limit=1000 reliably times out
    while url:
        r = mal_request(requests.get, url, headers=mal_headers(cfg), params=params, timeout=30)
        data = r.json()
        for entry in data.get("data", []):
            ls = entry.get("list_status", {})
            current[entry["node"]["id"]] = (
                ls.get("status"), ls.get("num_episodes_watched", 0), entry["node"].get("num_episodes", 0),
            )
        url = (data.get("paging") or {}).get("next")
        params = None  # already embedded in the next url
    return current


def mal_single_status(cfg, anime_id):
    """Fallback for an id missing from mal_current_list() (NSFW-filtered from the bulk listing) -
    the single anime endpoint still returns my_list_status correctly. Returns None on failure."""
    try:
        r = mal_request(
            requests.get, f"https://api.myanimelist.net/v2/anime/{anime_id}",
            headers=mal_headers(cfg), params={"fields": "num_episodes,my_list_status"}, timeout=15,
        )
    except requests.RequestException:
        return None
    d = r.json()
    ls = d.get("my_list_status") or {}
    return (ls.get("status"), ls.get("num_episodes_watched", 0), d.get("num_episodes", 0))


def clamp_episodes(episodes, total):
    """Never claim to have watched more episodes of a MAL entry than it actually has
    (e.g. Plex merges multi-cour shows MAL splits into separate entries) - MAL clamps this
    silently server-side anyway, so clamping here keeps our own diff-check able to converge."""
    return min(episodes, total) if total else episodes


def plan_update(cfg, current_list, mal_id, status, episodes, total_hint=0):
    """Return (send_status, send_episodes, already_up_to_date) for a target MAL entry.
    Plex may report progress against a merged, multi-cour view (e.g. 20/49 episodes) while
    the matched MAL entry only covers part of that (e.g. 20 total) - once clamped, if all of
    that entry's own episodes are watched it's "completed" on MAL, regardless of what the
    wider Plex-side total says."""
    entry = current_list.get(mal_id)
    if entry is None:
        entry = mal_single_status(cfg, mal_id)
    total = (entry[2] if entry else 0) or total_hint
    send_episodes = clamp_episodes(episodes, total)
    send_status = "completed" if total and send_episodes >= total else status
    already = entry is not None and entry[0] == send_status and entry[1] == send_episodes
    return send_status, send_episodes, already


def match_parts(cfg, title):
    """Plex can merge a multi-cour show (e.g. two seasons) into one title with more episodes
    than any single MAL entry has. mal_matches[title] is normally a plain MAL id (offset 0,
    the old/common case), or a list of {"id", "offset"} parts for shows that need a second
    (sequel) MAL entry once the viewer gets past the first part's episode count."""
    raw = cfg.get("mal_matches", {}).get(title)
    if raw is None:
        return []
    if isinstance(raw, int):
        return [{"id": raw, "offset": 0}]
    return raw


def add_match_part(cfg, title, mal_id, offset):
    matches = cfg.setdefault("mal_matches", {})
    parts = match_parts(cfg, title)
    parts.append({"id": mal_id, "offset": offset})
    matches[title] = parts


def next_match_offset(cfg, title):
    """Sum of the real episode totals of every part already tracked for this title -
    where a newly-added sequel part's episodes start counting from, in Plex's merged count."""
    offset = 0
    for part in match_parts(cfg, title):
        info = mal_single_status(cfg, part["id"])
        offset += (info[2] if info else 0)
    return offset


def mal_search(cfg, title):
    # MAL's search hard-rejects q over 64 chars (400 invalid_q) - there's no other title-based
    # lookup endpoint, so this is unavoidable. best_match() still compares against the full,
    # untruncated title (incl. alternative_titles), so this only shortens what's sent, not what's matched.
    r = mal_request(
        requests.get, "https://api.myanimelist.net/v2/anime",
        headers=mal_headers(cfg),
        params={"q": title[:64], "limit": 5, "fields": "id,title,alternative_titles,num_episodes"},
        timeout=15,
    )
    return [d["node"] for d in r.json().get("data", [])]


RESULT_SORT_ORDER = {"skip": 0, "err": 1, "ok": 2}  # unresolved matches surface at the top


def sort_results(results):
    results.sort(key=lambda r: RESULT_SORT_ORDER.get(r["css"], 2))


def mal_update(cfg, anime_id, status, episodes):
    mal_request(
        requests.patch, f"https://api.myanimelist.net/v2/anime/{anime_id}/my_list_status",
        headers=mal_headers(cfg),
        data={"status": status, "num_watched_episodes": episodes},
        timeout=15,
    )


PAGE = """
<!doctype html><html><head><title>Plex → MAL sync</title>
{% if syncing %}<meta http-equiv="refresh" content="3">{% endif %}
<style>
body{font-family:sans-serif;max-width:700px;margin:40px auto;color:#e0e0e0;background:#1a1a1a}
h2,h3{color:#fff}
a{color:#6cb4ff}
input[type="text"],input[type="url"],input[type="password"]{
  width:100%;padding:6px;margin:4px 0 12px;box-sizing:border-box;
  background:#2a2a2a;color:#e0e0e0;border:1px solid #444;border-radius:3px}
input[type="checkbox"]{width:auto;height:auto;margin:0 6px 0 0;padding:0}
label{display:flex;align-items:center;margin:2px 0;font-weight:normal}
button{background:#333;color:#e0e0e0;border:1px solid #555;border-radius:3px;padding:6px 14px;cursor:pointer}
button:hover{background:#3d3d3d}
table{border-collapse:collapse;width:100%;table-layout:fixed}
td,th{border:1px solid #444;padding:8px 10px;text-align:left;word-wrap:break-word;overflow-wrap:break-word}
code{background:#2a2a2a;padding:1px 4px;border-radius:3px}
.ok{color:#4caf50}.skip{color:#888}.err{color:#f44336}
a.edit{font-size:0.85em;margin-left:8px}</style></head><body>
<h2>Plex → MyAnimeList sync</h2>

<h3>Plex</h3>
{% if edit == 'plex' or not cfg.get('plex_base_url') or not cfg.get('plex_token') %}
<form method=post action="/setup/plex">
Plex server base URL:
<input type=text name=plex_base_url value="{{ cfg.get('plex_base_url','http://192.168.1.100:32400') }}" required>
Plex X-Plex-Token (from an existing app's config, or plex.tv/api/resources):
<input type=text name=plex_token value="{{ cfg.get('plex_token','') }}" required>
{% if sections %}
Libraries to sync (none checked = every TV library):
{% for s in sections %}
<label><input type=checkbox name=library_keys value="{{ s.key }}" {% if s.key in selected %}checked{% endif %}> {{ s.title }}</label>
{% endfor %}
{% elif sections_error %}
<p class=err>Could not load library list: {{ sections_error }}</p>
{% endif %}
<button type=submit>Save</button>
</form>
{% else %}
<p class=ok>Plex connected{% if selected %} ({{ selected|length }} librar{{ 'y' if selected|length == 1 else 'ies' }} selected){% endif %}.
<a class=edit href="/?edit=plex">Edit</a></p>
{% endif %}

<h3>MyAnimeList</h3>
{% if edit == 'mal' or not cfg.get('mal_client_id') %}
<form method=post action="/setup/mal">
MAL Client ID (from myanimelist.net/apiconfig):
<input type=text name=mal_client_id value="{{ cfg.get('mal_client_id','') }}" required>
MAL Client Secret (leave blank if app type is "other"/public):
<input type=text name=mal_client_secret value="{{ cfg.get('mal_client_secret','') }}">
Redirect URL (paste exactly into the MAL app's "App Redirect URL" field):
<input type=text readonly onclick="this.select()" value="{{ redirect_uri }}">
<button type=submit>Save</button>
</form>
{% else %}
{% if not cfg.get('mal_access_token') %}
<a href="/mal-connect"><button>Connect MyAnimeList</button></a>
<a class=edit href="/?edit=mal">Edit</a>
{% else %}
<p class=ok>MyAnimeList connected. <a class=edit href="/?edit=mal">Edit</a></p>
{% endif %}
{% endif %}

{% if cfg.get('plex_token') and cfg.get('mal_access_token') %}
<h3>Sync</h3>
{% if syncing %}
<p>Sync running… this page refreshes itself every few seconds. Tail <code>docker logs -f plex-mal-sync</code> to watch it live.</p>
{% else %}
<form method=post action="/sync"><button type=submit>Run sync now</button></form>
{% endif %}
{% endif %}

{% if error %}<p class=err>{{ error }}</p>{% endif %}

{% if results %}
<h3>Results</h3>
<table><tr><th>Plex title</th><th>MAL match</th><th>Status</th></tr>
{% for r in results %}
<tr><td>{{ r.plex_title }}</td><td>{{ r.mal_title or '-' }}</td>
<td class="{{ r.css }}">{{ r.note }}
{% if r.css == 'skip' %}
<form method=post action="/resolve" style="display:flex;gap:4px;margin-top:6px">
<input type=hidden name=plex_title value="{{ r.plex_title }}">
<input type=text name=mal_ref placeholder="MAL id or link" required style="width:auto;flex:1;margin:0;padding:4px">
<button type=submit style="padding:4px 8px">Add</button>
<button type=submit formaction="/ignore" formnovalidate style="padding:4px 8px">Ignore</button>
</form>
{% elif r.css == 'ok' %}
<form method=post action="/resolve" style="display:flex;gap:4px;margin-top:6px;font-size:0.85em">
<input type=hidden name=plex_title value="{{ r.plex_title }}">
<input type=text name=mal_ref placeholder="sequel MAL id/link (if Plex merges seasons)" style="width:auto;flex:1;margin:0;padding:3px">
<button type=submit style="padding:2px 8px">+ part</button>
</form>
{% endif %}
</td></tr>
{% endfor %}
</table>
{% endif %}
</body></html>
"""


def render_page(edit=None, error=None):
    cfg = load_config()
    show_plex_form = edit == "plex" or not cfg.get("plex_base_url") or not cfg.get("plex_token")
    sections, sections_error = None, None
    if show_plex_form and cfg.get("plex_base_url") and cfg.get("plex_token"):
        try:
            sections = plex_list_show_sections(cfg)
        except requests.RequestException as e:
            sections_error = str(e)
    return render_template_string(
        PAGE, cfg=cfg, redirect_uri=redirect_uri(), edit=edit,
        sections=sections, sections_error=sections_error,
        selected=set(cfg.get("plex_library_keys") or []),
        syncing=sync_state["running"], results=sync_state["results"], error=error,
    )


@app.route("/")
def index():
    return render_page(edit=request.args.get("edit"))


@app.route("/setup/plex", methods=["POST"])
def setup_plex():
    cfg = load_config()
    cfg["plex_base_url"] = request.form["plex_base_url"].strip()
    cfg["plex_token"] = request.form["plex_token"].strip()
    cfg["plex_library_keys"] = request.form.getlist("library_keys")
    save_config(cfg)
    return redirect("/")


@app.route("/setup/mal", methods=["POST"])
def setup_mal():
    cfg = load_config()
    cfg["mal_client_id"] = request.form["mal_client_id"].strip()
    cfg["mal_client_secret"] = request.form["mal_client_secret"].strip() or None
    save_config(cfg)
    return redirect("/")


@app.route("/mal-connect")
def mal_connect():
    cfg = load_config()
    verifier = secrets.token_urlsafe(64)[:128]
    cfg["mal_code_verifier"] = verifier
    save_config(cfg)
    params = urlencode({
        "response_type": "code",
        "client_id": cfg["mal_client_id"],
        "code_challenge": verifier,
        "code_challenge_method": "plain",
        "redirect_uri": redirect_uri(),
    })
    return redirect(f"https://myanimelist.net/v1/oauth2/authorize?{params}")


@app.route("/callback")
def mal_callback():
    cfg = load_config()
    code = request.args.get("code")
    r = requests.post(
        "https://myanimelist.net/v1/oauth2/token",
        data={
            "client_id": cfg["mal_client_id"],
            "client_secret": cfg.get("mal_client_secret") or "",
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": cfg["mal_code_verifier"],
            "redirect_uri": redirect_uri(),
        },
        timeout=15,
    )
    if r.status_code != 200:
        return render_page(edit="mal", error=f"MAL token exchange failed: {r.status_code} {r.text[:300]}")
    tok = r.json()
    cfg["mal_access_token"] = tok["access_token"]
    cfg["mal_refresh_token"] = tok["refresh_token"]
    cfg["mal_token_expires_at"] = time.time() + tok["expires_in"]
    cfg.pop("mal_code_verifier", None)
    save_config(cfg)
    return redirect("/")


def run_sync(cfg):
    results = []
    try:
        mal_ensure_token(cfg)
    except requests.RequestException as e:
        log.error("sync aborted: could not refresh MAL token: %s", e)
        results.append({"plex_title": "(sync aborted)", "mal_title": None,
                        "note": f"could not refresh MAL token: {e}", "css": "err"})
        sync_state["results"] = results
        sync_state["running"] = False
        return

    cfg.setdefault("mal_matches", {})  # plex title -> MAL id, once resolved never searched again
    ignored = set(cfg.get("mal_ignored") or [])

    try:
        current_list = mal_current_list(cfg)
        log.info("fetched %d entries from current MAL list", len(current_list))
    except requests.RequestException as e:
        log.warning("could not fetch current MAL list, will update everything unconditionally: %s", e)
        current_list = {}

    log.info("sync started")
    try:
        for show in plex_watched_shows(cfg):
            try:
                status, episodes = compute_status(show["leaf_count"], show["viewed_leaf_count"])
                if status is None:
                    log.info("skip %r: not started (viewed=%s/%s)",
                              show["title"], show["viewed_leaf_count"], show["leaf_count"])
                    continue
                if show["title"] in ignored:
                    log.info("skip %r: ignored by user", show["title"])
                    continue

                parts = match_parts(cfg, show["title"])
                if parts:
                    part_notes = []
                    for part in parts:
                        local_episodes = max(0, episodes - part["offset"])
                        if local_episodes == 0:
                            continue  # this part not reached yet (still on an earlier part)
                        send_status, send_episodes, already = plan_update(
                            cfg, current_list, part["id"], status, local_episodes)
                        if already:
                            log.info("skip %r: MAL id %s already up to date", show["title"], part["id"])
                            part_notes.append(f"MAL id {part['id']}: up to date")
                            continue
                        mal_update(cfg, part["id"], send_status, send_episodes)
                        log.info("synced %r -> MAL id %s (cached): %s (%d ep)",
                                  show["title"], part["id"], send_status, send_episodes)
                        part_notes.append(f"MAL id {part['id']}: set {send_status} ({send_episodes} ep)")
                    results.append({"plex_title": show["title"],
                                    "mal_title": ", ".join(f"MAL id {p['id']}" for p in parts) + " (cached)",
                                    "note": "; ".join(part_notes) if part_notes else "already up to date, no change sent",
                                    "css": "ok"})
                    continue

                candidates = mal_search(cfg, show["title"])
                match, ratio = best_match(show["title"], candidates, total_episodes=show["leaf_count"])
                if not match or ratio < MATCH_THRESHOLD:
                    log.info("skip %r: no confident MAL match (best=%r ratio=%.2f)",
                              show["title"], match["title"] if match else None, ratio)
                    results.append({"plex_title": show["title"], "mal_title": match["title"] if match else None,
                                    "note": "no confident MAL match, skipped", "css": "skip"})
                    continue
                cfg["mal_matches"][show["title"]] = match["id"]
                save_config(cfg)
                send_status, send_episodes, already = plan_update(cfg, current_list, match["id"], status, episodes,
                                                                   total_hint=match.get("num_episodes", 0))
                if already:
                    log.info("matched %r -> MAL %r: already up to date", show["title"], match["title"])
                    results.append({"plex_title": show["title"], "mal_title": match["title"],
                                    "note": "already up to date, no change sent", "css": "ok"})
                    continue
                mal_update(cfg, match["id"], send_status, send_episodes)
                log.info("synced %r -> MAL %r: %s (%d ep)", show["title"], match["title"], send_status, send_episodes)
                results.append({"plex_title": show["title"], "mal_title": match["title"],
                                "note": f"set {send_status} ({send_episodes} ep)", "css": "ok"})
            except requests.RequestException as e:
                log.error("error on %r: %s", show["title"], e)
                results.append({"plex_title": show["title"], "mal_title": None,
                                "note": f"MAL request failed: {e}", "css": "err"})
    except Exception as e:
        # Plex itself can fail mid-iteration (generator raises outside the per-show try above);
        # never leave sync_state stuck on "running" no matter what breaks.
        log.exception("sync aborted unexpectedly")
        results.append({"plex_title": "(sync aborted)", "mal_title": None,
                        "note": f"sync aborted: {e}", "css": "err"})
    finally:
        sort_results(results)
        log.info("sync finished: %d results", len(results))
        sync_state["results"] = results
        sync_state["running"] = False


@app.route("/resolve", methods=["POST"])
def resolve():
    cfg = load_config()
    plex_title = request.form["plex_title"]
    mal_ref = request.form.get("mal_ref", "").strip()
    if not mal_ref:
        return redirect("/")  # e.g. the optional "+ part" form submitted empty - no-op
    m = re.search(r"\d+", mal_ref)
    if not m:
        return render_page(error=f"Could not find a MAL id in {mal_ref!r} — paste the numeric id or the anime's MAL URL")
    mal_id = int(m.group())

    show = next((s for s in plex_watched_shows(cfg) if s["title"] == plex_title), None)
    if not show:
        return render_page(error=f"{plex_title!r} is no longer in the selected Plex libraries")
    status, episodes = compute_status(show["leaf_count"], show["viewed_leaf_count"])
    if status is None:
        return render_page(error=f"{plex_title!r} has no watched episodes in Plex")

    # If parts are already tracked for this title (e.g. a first cour already matched), this is
    # a sequel/second part - its episode count starts where the prior parts' totals end.
    existing_parts = match_parts(cfg, plex_title)
    offset = next_match_offset(cfg, plex_title) if existing_parts else 0
    local_episodes = max(0, episodes - offset)
    if local_episodes == 0:
        return render_page(error=f"Plex hasn't reached that far into {plex_title!r} yet "
                                  f"(only {episodes} of {offset} episodes covered by earlier parts)")

    info = mal_single_status(cfg, mal_id)
    total = info[2] if info else 0
    send_episodes = clamp_episodes(local_episodes, total)
    send_status = "completed" if total and send_episodes >= total else status

    try:
        mal_update(cfg, mal_id, send_status, send_episodes)
    except requests.RequestException as e:
        return render_page(error=f"MAL update failed: {e}")

    add_match_part(cfg, plex_title, mal_id, offset)
    save_config(cfg)
    log.info("manually resolved %r part (offset %d) -> MAL id %s: %s (%d ep)",
              plex_title, offset, mal_id, send_status, send_episodes)

    if sync_state["results"]:
        for r in sync_state["results"]:
            if r["plex_title"] == plex_title:
                r["mal_title"] = f"MAL id {mal_id} (manual)"
                r["note"] = f"set {send_status} ({send_episodes} ep)"
                r["css"] = "ok"
        sort_results(sync_state["results"])
    return redirect("/")


@app.route("/ignore", methods=["POST"])
def ignore():
    cfg = load_config()
    plex_title = request.form["plex_title"]
    ignored = cfg.setdefault("mal_ignored", [])
    if plex_title not in ignored:
        ignored.append(plex_title)
    save_config(cfg)
    log.info("ignored %r", plex_title)

    if sync_state["results"]:
        sync_state["results"] = [r for r in sync_state["results"] if r["plex_title"] != plex_title]
    return redirect("/")


@app.route("/sync", methods=["POST"])
def sync():
    if not sync_state["running"]:
        sync_state["running"] = True
        sync_state["results"] = None
        threading.Thread(target=run_sync, args=(load_config(),), daemon=True).start()
    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
