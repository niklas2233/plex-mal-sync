from plex_mal_sync import compute_status, best_match, sort_results, clamp_episodes, plan_update, match_parts, add_match_part, episode_count_compatible

assert compute_status(0, 0) == (None, 0)
assert compute_status(12, 0) == (None, 0)
assert compute_status(12, 12) == ("completed", 12)
assert compute_status(12, 5) == ("watching", 5)
assert compute_status(12, 13) == ("completed", 12)  # over-reported viewedLeafCount, clamp to total

cands = [{"id": 1, "title": "Attack on Titan"}, {"id": 2, "title": "Attack on Titan: Final Season"}]
match, ratio = best_match("Attack on Titan", cands)
assert match["id"] == 1 and ratio > 0.9

match, ratio = best_match("Some Show Not On MAL At All", cands)
assert ratio < 0.5

# MAL's primary title is often Japanese romaji; match must fall back to alternative_titles.en
candidates_with_alts = [{
    "id": 12345,
    "title": "Isekai de Nazeka Ore ga Ichiban Tsuyoi",
    "alternative_titles": {
        "en": "Somehow I Became the Strongest in Another World",
        "ja": "異世界でなぜか俺が一番強い",
        "synonyms": [],
    },
}]
match, ratio = best_match("Somehow I Became the Strongest in Another World", candidates_with_alts)
assert match["id"] == 12345 and ratio > 0.9

results = [{"plex_title": "A", "css": "ok"}, {"plex_title": "B", "css": "skip"},
           {"plex_title": "C", "css": "err"}, {"plex_title": "D", "css": "skip"}]
sort_results(results)
assert [r["plex_title"] for r in results] == ["B", "D", "C", "A"]

# Plex sometimes merges a multi-cour show into one entry with more episodes than any single
# MAL entry has (MAL splits cours into separate entries) - never claim more than it actually has.
assert clamp_episodes(26, 11) == 11
assert clamp_episodes(24, 24) == 24
assert clamp_episodes(5, 0) == 5  # total unknown (e.g. still airing) - don't clamp

current = {111: ("completed", 11, 11), 222: ("watching", 3, 12)}
send_status, send_episodes, already = plan_update(None, current, 111, "completed", 26)
assert send_status == "completed" and send_episodes == 11 and already is True  # already matches -> no write

send_status, send_episodes, already = plan_update(None, current, 222, "completed", 12)
assert send_status == "completed" and send_episodes == 12 and already is False  # status differs -> needs a write

# Plex's merged view says "watching" (20/49 across both cours of a split show), but the matched
# entry only has 20 episodes total - fully watching THIS entry means "completed" on MAL, not
# "watching", regardless of what the wider Plex-side total implies.
current = {333: ("watching", 20, 20)}
send_status, send_episodes, already = plan_update(None, current, 333, "watching", 20)
assert send_status == "completed" and send_episodes == 20 and already is False

# match_parts must transparently upgrade the old bare-int format (single MAL id, offset 0)
cfg = {"mal_matches": {"Old Format Show": 42}}
assert match_parts(cfg, "Old Format Show") == [{"id": 42, "offset": 0}]
assert match_parts(cfg, "Never Matched") == []

# Sequel parts (e.g. season 2 as a separate MAL entry Plex merges into one show) get appended,
# each starting where the prior part's episode count ends.
add_match_part(cfg, "Split Show", 100, 0)
add_match_part(cfg, "Split Show", 200, 20)
assert match_parts(cfg, "Split Show") == [{"id": 100, "offset": 0}, {"id": 200, "offset": 20}]

# episode_count_compatible: exact match, valid divisor of a Plex-merged total, or unknown side
assert episode_count_compatible(13, 26) is True   # 13-episode season divides a 26-ep merged show
assert episode_count_compatible(12, 26) is False  # 12 doesn't evenly divide 26 - wrong season
assert episode_count_compatible(13, 13) is True   # exact single-season match
assert episode_count_compatible(0, 26) is True    # candidate's own count unknown - can't rule out
assert episode_count_compatible(13, 0) is True    # Plex total unknown - can't rule out
assert episode_count_compatible(20, 13) is False  # candidate has MORE episodes than Plex reports
assert episode_count_compatible(1, 1201) is False  # a movie's "1 episode" isn't a season/cour signal
assert episode_count_compatible(1, 1) is True      # exact single-episode match still valid

# Genuine title tie (identical alt-title string, differ only in id/episode count) - without a
# Plex total, whichever candidate comes first wins, since there's nothing else to break the tie.
tied_seasons = [
    {"id": 902, "title": "Season 2 Romaji Title", "num_episodes": 12,
     "alternative_titles": {"en": "Same Show Name", "synonyms": []}},
    {"id": 901, "title": "Season 1 Romaji Title", "num_episodes": 13,
     "alternative_titles": {"en": "Same Show Name", "synonyms": []}},
]
match, ratio = best_match("Same Show Name", tied_seasons)
assert match["id"] == 902  # no episode info to disambiguate -> first-listed tie wins

# With the Plex total known (26, matching season 1's 13-episode divisor), the SAME tie now
# resolves to the actually-correct season instead of whichever happened to come first.
match, ratio = best_match("Same Show Name", tied_seasons, total_episodes=26)
assert match["id"] == 901

# Real-world regression: a long-running, ongoing series (e.g. 1000+ episodes, no seasons at
# all) can still tie 1.0-to-1.0 against its own movie spinoff's alternate title (movies are
# often (re)titled identically to the main show in their native-language title) - the movie's
# 1 episode must NOT be treated as a valid "divides evenly" match against the huge episode
# count, or it wrongly wins the tie by list order alone.
ongoing_show_candidates = [
    {"id": 501, "title": "Ongoing Show: The Movie", "num_episodes": 1,
     "alternative_titles": {"en": "Ongoing Show: The Movie", "ja": "ONGOING SHOW", "synonyms": []}},
    {"id": 500, "title": "Ongoing Show", "num_episodes": 0,
     "alternative_titles": {"en": "Ongoing Show", "ja": "ONGOING SHOW", "synonyms": []}},
]
match, ratio = best_match("Ongoing Show", ongoing_show_candidates, total_episodes=1201)
assert match["id"] == 500

print("ok")
