from plex_mal_sync import compute_status, best_match, sort_results, clamp_episodes, plan_update

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
send, already = plan_update(None, current, 111, "completed", 26)
assert send == 11 and already is True  # matches MAL's own clamped value -> no write needed

send, already = plan_update(None, current, 222, "completed", 12)
assert send == 12 and already is False  # status differs -> needs a write

print("ok")
