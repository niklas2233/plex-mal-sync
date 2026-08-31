# plex-mal-sync

A small self-hosted web app that syncs watched anime from a Plex server to MyAnimeList.

- Log in with your Plex server URL and X-Plex-Token
- Connect your MyAnimeList account via OAuth2 (PKCE)
- Pick which Plex libraries to sync (defaults to every TV-type library)
- Runs in the background, only writing to MAL entries that actually changed
- Three-tier matching for a Plex show that isn't linked to a MAL entry yet:
  1. **[Fribb/anime-lists](https://github.com/Fribb/anime-lists)** — cross-references Plex's own
     TheTVDB id against a community-maintained TVDB↔MAL mapping, auto-building a full multi-season
     match (e.g. a show Plex merges into one continuous run that MAL splits into separate season
     entries) without ever guessing from titles. Downloaded once and cached for a week.
  2. **Title search** — falls back to MAL's own search API (matching alternative/Japanese titles
     too) when the show isn't in that dataset.
  3. **Manual** — if neither finds a confident match, resolve it yourself in the web UI by pasting
     a MAL id or link (or permanently ignore the show).

## Setup

1. Register an API app at [myanimelist.net/apiconfig](https://myanimelist.net/apiconfig).
   App type can be "other". You'll fill in the redirect URL after the app is running (step 3).
2. Run it — save this as `docker-compose.yml` (also included in this repo) and run
   `docker compose up -d`:
   ```yaml
   services:
     plex-mal-sync:
       image: ghcr.io/niklas2233/plex-mal-sync:latest
       container_name: plex-mal-sync
       ports:
         - "5057:5057"
       volumes:
         - ./plex_mal_sync_data:/data
       restart: unless-stopped
   ```
   or with plain `docker run`:
   ```
   docker run -d --name plex-mal-sync \
     -p 5057:5057 \
     -v "$(pwd)/plex_mal_sync_data:/data" \
     --restart unless-stopped \
     ghcr.io/niklas2233/plex-mal-sync:latest
   ```
   Both pull the prebuilt image from GHCR (published automatically by this repo's CI on every
   push to `main`) — no local build needed. The app listens on port 5057. Config (API tokens,
   cached matches) is stored in `./plex_mal_sync_data/config.json` on the host, bind-mounted
   into the container.
3. Open `http://<your-host>:5057`, fill in your Plex server URL/token and your MAL Client ID
   (+ secret if your app has one). The page shows the exact redirect URL to paste back into
   your MAL app config — it must match exactly.
4. Click "Connect MyAnimeList", authorize, then run a sync.

## Notes

- MAL's search API rejects queries over 64 characters and is often slow — the app truncates
  search queries and retries transient failures automatically.
- MAL splits some multi-cour shows into separate entries; if Plex reports more watched episodes
  than a matched MAL entry actually has, the app clamps what it sends to that entry's real total.
- NSFW-flagged MAL entries are included via `nsfw=true` on the list-fetch call, since MAL
  otherwise silently omits them from the bulk listing.
