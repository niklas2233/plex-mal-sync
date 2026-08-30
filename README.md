# plex-mal-sync

A small self-hosted web app that syncs watched anime from a Plex server to MyAnimeList.

- Log in with your Plex server URL and X-Plex-Token
- Connect your MyAnimeList account via OAuth2 (PKCE)
- Pick which Plex libraries to sync (defaults to every TV-type library)
- Runs in the background, matches titles against MAL (including alternative/Japanese titles),
  and only writes to MAL entries that actually changed
- Unmatched shows can be resolved manually (paste a MAL id or link) or ignored permanently

## Setup

1. Register an API app at [myanimelist.net/apiconfig](https://myanimelist.net/apiconfig).
   App type can be "other". You'll fill in the redirect URL after the app is running (step 3).
2. Run it — with Docker Compose:
   ```
   docker compose up -d
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
