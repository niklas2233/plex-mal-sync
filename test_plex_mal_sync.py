import plex_mal_sync as m
from plex_mal_sync import compute_status, best_match, sort_results, clamp_episodes, plan_update, match_parts, add_match_part, episode_count_compatible, non_special_episode_counts, build_tvdb_chain, fribb_entry_included

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

# The reverse case: Plex says "completed" because the user watched everything Plex currently
# HAS (e.g. 9/9, a still-airing show or one not fully added to the library yet), but this MAL
# entry's own real total is higher (26) - must never claim "completed" just because Plex's own,
# smaller, total was reached.
current = {700: (None, 0, 26)}
send_status, send_episodes, already = plan_update(None, current, 700, "completed", 9)
assert send_status == "watching" and send_episodes == 9 and already is False

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

# non_special_episode_counts: Plex's show-level aggregate includes Season 0 (Specials), which
# has no consistent place in MAL's own numbering and shouldn't count toward a show's real total.
seasons_with_specials = [
    {"index": 0, "leafCount": 28, "viewedLeafCount": 28},  # Specials - excluded
    {"index": 1, "leafCount": 25, "viewedLeafCount": 25},
    {"index": 2, "leafCount": 24, "viewedLeafCount": 24},
]
leaf, viewed = non_special_episode_counts(seasons_with_specials, fallback_leaf=77, fallback_viewed=77)
assert (leaf, viewed) == (49, 49)  # 25+24, specials' 28 excluded

# A show with no season breakdown at all (or every season oddly tagged 0) falls back to the
# show-level aggregate, rather than silently reporting zero episodes.
leaf, viewed = non_special_episode_counts([], fallback_leaf=12, fallback_viewed=12)
assert (leaf, viewed) == (12, 12)

# build_tvdb_chain: cumulative offsets come from each entry's own real MAL episode count, in
# (season, within-season offset) order; entries with no real episode count yet (unreleased)
# are skipped entirely rather than breaking the offset math for what comes after.
fake_fribb_index = {
    12345: [
        {"mal_id": 111, "season": 1, "offset": 0},
        {"mal_id": 222, "season": 2, "offset": 0},
        {"mal_id": 333, "season": 3, "offset": 0},  # unreleased - no real episode count yet
    ],
}
fake_info = {111: (None, 0, 25), 222: (None, 0, 24), 333: (None, 0, 0)}
m.mal_single_status = lambda cfg, mal_id: fake_info.get(mal_id)
chain = build_tvdb_chain(None, fake_fribb_index, 12345)
assert chain == [{"id": 111, "offset": 0}, {"id": 222, "offset": 25}]

assert build_tvdb_chain(None, fake_fribb_index, 99999) == []  # tvdb_id not in the dataset at all

# A transient fetch failure (mal_single_status returns None) must abort the whole chain rather
# than being silently treated like "unreleased" - skipping season 2 here would otherwise leave
# season 3 with season 1's offset instead of season 1+2's, corrupting every entry after it.
fake_info_with_failure = {111: (None, 0, 25), 222: None, 333: (None, 0, 12)}
m.mal_single_status = lambda cfg, mal_id: fake_info_with_failure.get(mal_id)
assert build_tvdb_chain(None, fake_fribb_index, 12345) == []

# fribb_entry_included: a TV entry is always kept. A SPECIAL is only kept when it has BOTH a
# real season (>=1) AND a nonzero within-season offset (e.g. "Final Chapters" continuing after
# a season's main episodes, as with Attack on Titan) - a SPECIAL at offset 0 (the vast majority)
# is standalone bonus content.
assert fribb_entry_included("TV", 1, 0) is True
assert fribb_entry_included("TV", 4, 16) is True
assert fribb_entry_included("SPECIAL", 4, 28) is True   # e.g. AoT's "Final Chapters"
assert fribb_entry_included("SPECIAL", 1, 0) is False   # a standalone special, not a continuation
# A season-0 special can still carry its own nonzero offset (its position among OTHER season-0
# extras, e.g. SAO's "Extra Edition" recap) - that's not real episode numbering, so season >= 1
# is required too, not just a nonzero offset.
assert fribb_entry_included("SPECIAL", 0, 9) is False
assert fribb_entry_included("OVA", 1, 5) is False
assert fribb_entry_included("MOVIE", 0, 0) is False

print("ok")
