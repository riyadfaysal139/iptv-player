"""Offline tests.

The classifier numbers below were measured against a real 92k-item catalog and
act as regression anchors: if a rule changes and items start falling into the
wrong group, the totals stop matching.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import m3u
from core.classify import (
    UNCATEGORIZED, classify, classify_live, classify_movie, classify_series,
    era_sort_key,
)
from core import vlsub
from core.db import Database, fold
from core.downloads import destination_for, safe_name
from core.playlists import PlaylistStore, TYPE_XTREAM
from core.subtitles import LANGUAGES, SubtitleError
from core.api import parse_pasted, normalise_server


class TestClassifier(unittest.TestCase):
    def test_movie_language_and_era(self):
        self.assertEqual(classify_movie("VOD | English 2026"), ("English", "English 2026"))
        self.assertEqual(classify_movie("VOD | Hindi 90s"), ("Hindi", "Hindi 90s"))
        self.assertEqual(classify_movie("4K VOD | English"), ("4K", "English"))

    def test_movie_hollywood_is_english(self):
        self.assertEqual(classify_movie("VOD | Hollywood Modern")[0], "English")

    def test_movie_genre_and_collection(self):
        self.assertEqual(classify_movie("VOD | Horror"), ("Genres", "Horror"))
        self.assertEqual(classify_movie("VOD | 007 James Bond")[0], "Collections")
        self.assertEqual(classify_movie("VOD | Kids"), ("Kids", "Kids"))

    def test_live_folds_sports_prefixes(self):
        # The provider splits these across two prefixes; they belong together.
        self.assertEqual(classify_live("LIVE | EPL Hub")[0], "Sports & Events")
        self.assertEqual(classify_live("SPORTS | ESPN+")[0], "Sports & Events")
        self.assertEqual(classify_live("SPORTS | ESPN+")[1], "ESPN+")

    def test_live_country_codes(self):
        self.assertEqual(classify_live("US | News")[0], "United States")
        self.assertEqual(classify_live("INR | Movies")[0], "India")
        self.assertEqual(classify_live("Indonesian News")[0], "Other Regions")

    def test_series_groups(self):
        self.assertEqual(classify_series("Netflix Series - Hindi")[0], "Streaming Services")
        self.assertEqual(classify_series("Cartoon Network")[0], "Kids & Animation")
        self.assertEqual(classify_series("Comedy")[0], "Genres")
        self.assertEqual(classify_series("Turkish TV Series")[0], "Regional / Language")
        self.assertEqual(classify_series("USA Network")[0], "TV Networks")

    def test_never_raises_on_junk(self):
        for value in ("", "|", "   ", "???", "VOD |"):
            group, sub = classify("movie", value)
            self.assertTrue(group)
        self.assertEqual(classify("nonsense-kind", "x")[0], UNCATEGORIZED)

    def test_era_sort_is_newest_first(self):
        names = ["English 2000-2009", "English 2026", "English 90s",
                 "English Trending", "English 80s", "English 2025"]
        ordered = sorted(names, key=era_sort_key)
        self.assertEqual(ordered[0], "English 2026")
        self.assertEqual(ordered[1], "English 2025")
        self.assertLess(ordered.index("English 90s"), ordered.index("English 80s"))
        self.assertEqual(ordered[-1], "English Trending")


class TestUrlParsing(unittest.TestCase):
    def test_parse_get_php_link(self):
        parsed = parse_pasted(
            "http://um.example.com:8080/get.php?username=bob&password=secret&type=m3u_plus"
        )
        self.assertEqual(parsed["server"], "http://um.example.com:8080")
        self.assertEqual(parsed["username"], "bob")
        self.assertEqual(parsed["password"], "secret")

    def test_normalise_adds_scheme(self):
        self.assertEqual(normalise_server("example.com:8080"), "http://example.com:8080")
        self.assertEqual(normalise_server("http://example.com/"), "http://example.com")

    def test_normalise_rejects_empty(self):
        with self.assertRaises(ValueError):
            normalise_server("")


class TestM3U(unittest.TestCase):
    SAMPLE = [
        "#EXTM3U",
        '#EXTINF:-1 tvg-id="a.uk" tvg-name="Chan" tvg-logo="http://l/o.png" '
        'group-title="UK | News",Chan One',
        "http://host/user/pass/123.ts",
        '#EXTINF:-1 group-title="VOD | English",Some Movie - 2020',
        "http://host/movie/user/pass/456.mp4",
        '#EXTINF:-1 group-title="Netflix",Show S01E02",',
        "http://host/series/user/pass/789.mkv",
    ]

    def test_parses_kinds_and_ids(self):
        entries = list(m3u.parse(iter(self.SAMPLE)))
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["kind"], "live")
        self.assertEqual(entries[0]["stream_id"], "123")
        self.assertEqual(entries[0]["group"], "UK | News")
        self.assertEqual(entries[1]["kind"], "movie")
        self.assertEqual(entries[1]["container_extension"], "mp4")
        self.assertEqual(entries[2]["kind"], "series")

    def test_episode_pattern(self):
        self.assertEqual(m3u.split_episode("Some.Show.S01E02"), ("Some Show", 1, 2))
        self.assertIsNone(m3u.split_episode("Just A Movie 2020"))

    def test_ignores_stray_directives(self):
        lines = ["#EXTM3U", "#EXTVLCOPT:x=1", '#EXTINF:-1,Name', "http://h/1.ts"]
        self.assertEqual(len(list(m3u.parse(iter(lines)))), 1)


class TestPaths(unittest.TestCase):
    def test_safe_name_strips_separators(self):
        self.assertNotIn("/", safe_name("a/b:c"))
        self.assertTrue(safe_name(""))

    def test_series_destination_layout(self):
        path = destination_for(Path("/root"), "series", "Pilot", "My Show", 1, 3, "mkv")
        self.assertEqual(path.parent.name, "Season 01")
        self.assertEqual(path.name, "S01E03 - Pilot.mkv")

    def test_movie_destination(self):
        path = destination_for(Path("/root"), "movie", "Film - 2020", ext="mp4")
        self.assertEqual(path.parent.name, "Movies")


class TestDatabase(unittest.TestCase):
    def setUp(self):
        import tempfile

        from core import playlists as pl_mod

        # Never touch the real OS keychain from tests. Credential storage is
        # namespaced per database, but a fake backend makes that impossible to
        # get wrong by accident.
        self._fake_store = {}
        self._real_keyring = pl_mod.keyring
        self._real_ok = pl_mod._KEYRING_OK

        class _FakeKeyring:
            @staticmethod
            def set_password(service, key, value):
                self._fake_store[(service, key)] = value

            @staticmethod
            def get_password(service, key):
                return self._fake_store.get((service, key))

            @staticmethod
            def delete_password(service, key):
                self._fake_store.pop((service, key), None)

        pl_mod.keyring = _FakeKeyring
        pl_mod._KEYRING_OK = True

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        from core import playlists as pl_mod

        pl_mod.keyring = self._real_keyring
        pl_mod._KEYRING_OK = self._real_ok
        self.db.close()
        self.tmp.cleanup()

    def test_credentials_are_namespaced_per_database(self):
        """Two databases must not share a keychain slot for playlist 1.

        Without namespacing, a second profile (or a test fixture) creating its
        own playlist 1 silently overwrites the first one's stored password.
        """
        import tempfile

        store_a = PlaylistStore(self.db)
        first = store_a.add("A", TYPE_XTREAM, "http://a", "user", "secret-A")

        with tempfile.TemporaryDirectory() as other_dir:
            other_db = Database(Path(other_dir) / "other.sqlite")
            store_b = PlaylistStore(other_db)
            second = store_b.add("B", TYPE_XTREAM, "http://b", "user", "secret-B")
            self.assertEqual(first.id, second.id, "both are playlist 1")
            self.assertEqual(store_b.get(second.id).password, "secret-B")
            other_db.close()

        self.assertEqual(
            store_a.get(first.id).password, "secret-A",
            "the other database must not have clobbered this credential",
        )

    def test_password_round_trip_is_not_truncated(self):
        store = PlaylistStore(self.db)
        # Shape matters here, not the value: mixed case and digits with no
        # separators is what once came back truncated and broke playback.
        secret = "Xk7QpR2wZ"
        playlist = store.add("A", TYPE_XTREAM, "http://a", "user", secret)
        self.assertEqual(store.get(playlist.id).password, secret)
        client = store.get(playlist.id).client()
        self.assertIn(f"/{secret}/", client.movie_url("123", "mp4"))

    def test_fold_strips_accents(self):
        self.assertEqual(fold("Ünïcodé"), "unicode")
        self.assertEqual(fold(""), "")

    def test_cascade_isolates_playlists(self):
        store = PlaylistStore(self.db)
        first = store.add("A", TYPE_XTREAM, "http://a", "u", "p")
        second = store.add("B", TYPE_XTREAM, "http://b", "u", "p")
        for playlist in (first, second):
            self.db.execute(
                "INSERT INTO streams(playlist_id, kind, stream_id, name, name_folded)"
                " VALUES(?,?,?,?,?)",
                (playlist.id, "live", "1", "X", "x"),
            )
        store.delete(first.id)
        remaining = self.db.scalar("SELECT COUNT(*) FROM streams")
        self.assertEqual(remaining, 1, "deleting a playlist must not touch the other")
        owner = self.db.scalar("SELECT playlist_id FROM streams")
        self.assertEqual(owner, second.id)

    def test_due_for_sync(self):
        import time

        store = PlaylistStore(self.db)
        playlist = store.add("A", TYPE_XTREAM, "http://a", "u", "p")
        self.assertTrue(store.due_for_sync(playlist), "never synced is always due")
        store.update(playlist.id, last_sync_at=int(time.time()))
        self.assertFalse(store.due_for_sync(store.get(playlist.id)))
        store.update(playlist.id, last_sync_at=int(time.time()) - 25 * 3600)
        self.assertTrue(store.due_for_sync(store.get(playlist.id)))
        store.update(playlist.id, auto_sync=0)
        self.assertFalse(store.due_for_sync(store.get(playlist.id)),
                         "auto_sync off must suppress the daily refresh")

    def test_subtitle_login_stays_out_of_the_database(self):
        """The OpenSubtitles password must live in the keychain, like the rest.

        Same trap as the playlist credentials: namespaced, so a second profile
        or a test fixture cannot overwrite the real one.
        """
        from core.playlists import get_secret, set_secret

        namespace = self.db.instance_id
        set_secret("opensubtitles-password", "sub-secret", namespace)
        self.assertEqual(get_secret("opensubtitles-password", namespace), "sub-secret")
        self.assertEqual(get_secret("opensubtitles-password", "other-namespace"), "",
                         "another database must not see this credential")

        self.db.set_setting("subtitle_backend", "vlsub")
        self.db.close()
        blob = (Path(self.tmp.name) / "t.sqlite").read_bytes()
        self.assertNotIn(b"sub-secret", blob,
                         "the subtitle password leaked into the database file")


class TestVLSub(unittest.TestCase):
    """The keyless OpenSubtitles path, tested without touching the network."""

    def test_status_messages(self):
        self.assertEqual(vlsub.status_message("200 OK"), "")
        self.assertEqual(vlsub.status_message(""), "")
        self.assertIn("daily download limit", vlsub.status_message("407 Download limit reached"))
        self.assertIn("session expired", vlsub.status_message("406 No session").lower())
        # An unknown code must still surface something readable, not a KeyError.
        self.assertIn("999", vlsub.status_message("999 Something new"))

    def test_request_is_valid_xmlrpc(self):
        """The body we post must be a well-formed call the server can parse."""
        import xmlrpc.client

        body = xmlrpc.client.dumps(
            ("", "", "en", vlsub.USER_AGENT), "LogIn", allow_none=True,
        ).encode("utf-8")
        params, method = xmlrpc.client.loads(body)
        self.assertEqual(method, "LogIn")
        self.assertEqual(params, ("", "", "en", "VLSub 0.10.2"))

    def test_response_maps_onto_a_result(self):
        row = {
            "SubDownloadLink": "https://dl.opensubtitles.org/x.gz",
            "SubFileName": "Movie.2023.srt",
            "SubLanguageID": "ben",
            "MovieReleaseName": "Movie.2023.1080p.BluRay",
            "SubDownloadsCnt": "4211",
            "SubRating": "8.5",
            "MatchedBy": "moviehash",
            "SubEncoding": "CP1256",
            "SubFormat": "SRT",
            "SubHearingImpaired": "1",
        }
        result = vlsub._to_result(row)
        self.assertTrue(result.from_hash)
        self.assertEqual(result.language, "BEN")
        self.assertEqual(result.downloads, 4211)
        self.assertEqual(result.fmt, "srt")
        self.assertEqual(result.encoding, "CP1256")
        self.assertTrue(result.hearing_impaired)
        self.assertIn("★", result.label)

        # XML-RPC sends boolean false for absent fields; that must not crash.
        sparse = vlsub._to_result({"SubFileName": False, "SubDownloadsCnt": None})
        self.assertEqual(sparse.downloads, 0)
        self.assertEqual(sparse.fmt, "srt")

    def test_download_is_gunzipped_and_transcoded(self):
        """Uploads are in the submitter's charset; VLC needs UTF-8."""
        import gzip

        arabic = "1\n00:00:01,000 --> 00:00:02,000\nمرحبا\n"
        payload = gzip.compress(arabic.encode("cp1256"))
        self.assertEqual(vlsub.decode_payload(payload, "CP1256"), arabic)

        # A bogus charset must fall back rather than raise.
        self.assertTrue(vlsub.decode_payload(gzip.compress(b"hello"), "not-a-charset"))
        # A UTF-8 BOM must not survive into the file.
        self.assertEqual(vlsub.decode_payload(gzip.compress(b"\xef\xbb\xbf1\n"), "utf-8"), "1\n")

    def test_quota_page_is_reported_not_written(self):
        """Over quota the server sends HTML with HTTP 200, not an error."""
        with self.assertRaises(SubtitleError) as caught:
            vlsub.decode_payload(b"<html>download limit reached</html>", "utf-8")
        self.assertIn("limit", str(caught.exception).lower())

    def test_language_ids_are_iso_639_2(self):
        """This endpoint wants bibliographic codes: French is 'fre', not 'fra'."""
        self.assertEqual(vlsub._languages(["eng", "ben"]), "eng,ben")
        self.assertEqual(vlsub._languages([]), "eng", "a search always needs a language")
        self.assertEqual(vlsub._languages(["eng", "eng"]), "eng", "duplicates collapse")
        codes = {iso3 for _label, iso3, _iso2 in LANGUAGES}
        self.assertIn("fre", codes)
        self.assertIn("ger", codes)
        self.assertNotIn("fra", codes)

    def test_search_relogs_in_once_on_a_stale_token(self):
        """An idle session answers 401; that deserves one retry, not an error."""
        client = vlsub.VLSubClient()
        calls = []

        def fake_checked(method, *args):
            calls.append(method)
            if method == "LogIn":
                return {"status": "200 OK", "token": f"token-{calls.count('LogIn')}"}
            if calls.count("SearchSubtitles") == 1:
                raise SubtitleError("The OpenSubtitles session expired.", "401")
            return {"status": "200 OK", "data": [{"SubFileName": "ok.srt"}]}

        client._checked = fake_checked
        results = client.search_name("Dune", languages=["eng"])
        self.assertEqual(len(results), 1)
        self.assertEqual(calls, ["LogIn", "SearchSubtitles", "LogIn", "SearchSubtitles"])

    def test_search_does_not_retry_a_spent_quota(self):
        client = vlsub.VLSubClient()
        calls = []

        def fake_checked(method, *args):
            calls.append(method)
            if method == "LogIn":
                return {"status": "200 OK", "token": "t"}
            raise SubtitleError("limit", "407")

        client._checked = fake_checked
        with self.assertRaises(SubtitleError):
            client.search_name("Dune", languages=["eng"])
        self.assertEqual(calls.count("SearchSubtitles"), 1, "a quota failure is not retryable")

    def test_hash_search_sends_the_size_as_a_string(self):
        client = vlsub.VLSubClient()
        captured = {}

        def fake_checked(method, *args):
            if method == "LogIn":
                return {"status": "200 OK", "token": "t"}
            captured.update(args[1][0])
            return {"status": "200 OK", "data": []}

        client._checked = fake_checked
        client.search_hash("8e245d9679d31e12", 12909756, ["eng"])
        self.assertEqual(captured["moviehash"], "8e245d9679d31e12")
        self.assertEqual(captured["moviebytesize"], "12909756")
        self.assertIsInstance(captured["moviebytesize"], str)

    def test_no_api_key_is_needed(self):
        self.assertTrue(vlsub.VLSubClient().configured,
                        "the keyless backend must always report itself usable")


class TestSeekClamp(unittest.TestCase):
    """Where the arrow keys land.

    Kept as a pure function in ui/player_widget.py so it can be tested without
    Qt or libVLC. Importing the module is safe: it only pulls in PySide6 types
    at class-definition time, not a running application.
    """

    def setUp(self):
        from ui.player_widget import SEEK_TAIL_MS, clamp_seek

        self.clamp = clamp_seek
        self.tail = SEEK_TAIL_MS

    def test_ordinary_jump_is_exact(self):
        self.assertEqual(self.clamp(60_000, 10, 600_000), 70_000)
        self.assertEqual(self.clamp(60_000, -10, 600_000), 50_000)

    def test_backwards_past_the_start_stops_at_zero(self):
        self.assertEqual(self.clamp(3_000, -10, 600_000), 0)
        self.assertEqual(self.clamp(0, -10, 600_000), 0)

    def test_forwards_past_the_end_keeps_headroom(self):
        """Landing on the duration ends the media, so the arrow would look
        like it skipped to the next item instead of seeking."""
        duration = 600_000
        landed = self.clamp(duration - 2_000, 10, duration)
        self.assertLess(landed, duration)
        self.assertEqual(landed, duration - self.tail)
        # Repeated presses at the end stay put rather than running away.
        self.assertEqual(self.clamp(landed, 10, duration), duration - self.tail)

    def test_unknown_duration_is_not_clamped_but_never_negative(self):
        """Live streams report length 0; the value must stay sane anyway."""
        self.assertEqual(self.clamp(30_000, 10, 0), 40_000)
        self.assertEqual(self.clamp(5_000, -10, 0), 0)
        self.assertEqual(self.clamp(0, -10, -1), 0)

    def test_short_media_cannot_produce_a_negative_target(self):
        self.assertEqual(self.clamp(200, 10, 500), 0)


class TestContinueWatching(unittest.TestCase):
    """One row per show, not one per watched episode.

    history is keyed per episode, so a plain join multiplied a series by the
    number of episodes watched - eight copies of the same show in the list, and
    a sidebar count that disagreed with what was on screen.
    """

    CONTINUE_JOIN = (
        "JOIN (SELECT playlist_id, kind, stream_id,"
        " MAX(watched_at) AS watched_at FROM history"
        " GROUP BY playlist_id, kind, stream_id) h"
        " ON h.playlist_id=s.playlist_id AND h.kind=s.kind"
        " AND h.stream_id=s.stream_id"
    )

    def setUp(self):
        import tempfile

        self.dir = Path(tempfile.mkdtemp())
        self.db = Database(self.dir / "t.sqlite")
        self.db.execute(
            "INSERT INTO playlists(id, name, type, position) VALUES(1,'p','xtream',0)")
        for stream_id, name in (("100", "Trailer Park Boys"), ("200", "Other Show")):
            self.db.execute(
                "INSERT INTO streams(playlist_id, kind, stream_id, name, name_folded,"
                " available) VALUES(1,'series',?,?,?,1)",
                (stream_id, name, name.lower()),
            )
        # Eight episodes of one show, one of the other.
        for index in range(8):
            self.db.execute(
                "INSERT INTO history(playlist_id, kind, stream_id, episode_id,"
                " position_secs, duration_secs, watched_at) VALUES(1,'series','100',?,"
                " 60, 1200, ?)", (f"e{index}", 1000 + index),
            )
        self.db.execute(
            "INSERT INTO history(playlist_id, kind, stream_id, episode_id,"
            " position_secs, duration_secs, watched_at)"
            " VALUES(1,'series','200','x', 60, 1200, 900)")

    def tearDown(self):
        import shutil

        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_show_appears_once_however_many_episodes_were_watched(self):
        rows = self.db.query(
            "SELECT s.stream_id, s.name FROM streams s " + self.CONTINUE_JOIN +
            " WHERE s.playlist_id=1 AND s.kind='series' ORDER BY h.watched_at DESC")
        self.assertEqual([r["name"] for r in rows],
                         ["Trailer Park Boys", "Other Show"])

    def test_the_old_unaggregated_join_is_what_produced_the_duplicates(self):
        """Guards the fix: the naive join returns nine rows for two shows."""
        rows = self.db.query(
            "SELECT s.stream_id FROM streams s "
            "JOIN history h ON h.playlist_id=s.playlist_id AND h.kind=s.kind "
            "AND h.stream_id=s.stream_id WHERE s.playlist_id=1 AND s.kind='series'")
        self.assertEqual(len(rows), 9)

    CONTINUE_COUNT = (
        "SELECT COUNT(*) FROM (SELECT DISTINCT h.stream_id FROM history h "
        "JOIN streams s ON s.playlist_id=h.playlist_id AND s.kind=h.kind "
        "AND s.stream_id=h.stream_id AND s.available=1 AND s.name <> '' "
        "WHERE h.playlist_id=1 AND h.kind='series')"
    )

    def test_the_count_matches_the_number_of_rows_shown(self):
        self.assertEqual(self.db.scalar(self.CONTINUE_COUNT, (), 0), 2)

    def test_a_show_dropped_from_the_catalog_is_not_counted(self):
        """History outlives the catalog: a title the provider removed cannot be
        listed, so counting it made the badge disagree with the list."""
        self.db.execute(
            "INSERT INTO history(playlist_id, kind, stream_id, episode_id,"
            " position_secs, duration_secs, watched_at)"
            " VALUES(1,'series','999','g', 60, 1200, 500)")
        self.assertEqual(self.db.scalar(self.CONTINUE_COUNT, (), 0), 2)
        rows = self.db.query(
            "SELECT s.stream_id FROM streams s " + self.CONTINUE_JOIN +
            " WHERE s.playlist_id=1 AND s.kind='series'")
        self.assertEqual(len(rows), 2)

    def test_an_unavailable_show_is_neither_listed_nor_counted(self):
        self.db.execute("UPDATE streams SET available=0 WHERE stream_id='200'")
        self.assertEqual(self.db.scalar(self.CONTINUE_COUNT, (), 0), 1)

    def test_most_recently_watched_show_sorts_first(self):
        rows = self.db.query(
            "SELECT s.name FROM streams s " + self.CONTINUE_JOIN +
            " WHERE s.playlist_id=1 AND s.kind='series' ORDER BY h.watched_at DESC")
        self.assertEqual(rows[0]["name"], "Trailer Park Boys")


class TestResumePick(unittest.TestCase):
    """Which episode the Continue button offers — the Netflix rule."""

    def setUp(self):
        from ui.series_page import WATCHED_FRACTION, describe_resume, pick_resume

        self.pick = pick_resume
        self.describe = describe_resume
        self.watched = WATCHED_FRACTION
        self.episodes = [
            {"episode_id": f"s{season}e{num}", "season": season, "episode": num}
            for season in (1, 2) for num in (1, 2, 3)
        ]

    def test_nothing_watched_offers_the_first_episode(self):
        episode, resume = self.pick(self.episodes, {})
        self.assertEqual(episode["episode_id"], "s1e1")
        self.assertEqual(resume, 0)

    def test_part_way_through_resumes_that_episode(self):
        episode, resume = self.pick(self.episodes, {"s1e2": (400, 1200, 50)})
        self.assertEqual(episode["episode_id"], "s1e2")
        self.assertEqual(resume, 400)

    def test_a_finished_episode_moves_on_to_the_next(self):
        history = {"s1e2": (int(1200 * self.watched) + 1, 1200, 50)}
        episode, resume = self.pick(self.episodes, history)
        self.assertEqual(episode["episode_id"], "s1e3")
        self.assertEqual(resume, 0)

    def test_finishing_a_season_crosses_into_the_next(self):
        history = {"s1e3": (1200, 1200, 50)}
        episode, _ = self.pick(self.episodes, history)
        self.assertEqual(episode["episode_id"], "s2e1")

    def test_the_most_recent_episode_wins_not_the_furthest(self):
        """Re-watching an early episode is what you want to continue from."""
        history = {"s2e3": (300, 1200, 10), "s1e1": (200, 1200, 99)}
        episode, resume = self.pick(self.episodes, history)
        self.assertEqual(episode["episode_id"], "s1e1")
        self.assertEqual(resume, 200)

    def test_finishing_the_last_episode_offers_it_again(self):
        history = {"s2e3": (1200, 1200, 50)}
        episode, resume = self.pick(self.episodes, history)
        self.assertEqual(episode["episode_id"], "s2e3")
        self.assertEqual(resume, 0)

    def test_history_for_an_unknown_episode_is_ignored(self):
        """Episodes disappear when a provider renumbers a series."""
        episode, _ = self.pick(self.episodes, {"gone": (400, 1200, 999)})
        self.assertEqual(episode["episode_id"], "s1e1")

    def test_no_episodes_offers_nothing(self):
        self.assertEqual(self.pick([], {"s1e1": (10, 20, 1)}), (None, 0))

    def test_zero_duration_does_not_divide_by_zero(self):
        episode, resume = self.pick(self.episodes, {"s1e2": (400, 0, 50)})
        self.assertEqual(episode["episode_id"], "s1e2")
        self.assertEqual(resume, 400)

    def test_the_button_says_what_it_will_do(self):
        self.assertEqual(self.describe(self.episodes[1], 0), "Play S01E02")
        self.assertEqual(self.describe(self.episodes[1], 400),
                         "Continue S01E02 · 6 min in")
        # A few seconds in is a restart, not a resume worth announcing.
        self.assertEqual(self.describe(self.episodes[0], 5), "Play S01E01")


class TestNextEpisode(unittest.TestCase):
    """What the up-next card offers when an episode ends.

    Separate from the ⏭ button on purpose: this one stops at the end of the
    show, because that is the case the "you've finished" card exists for.
    """

    def setUp(self):
        from ui.series_page import next_episode

        self.next = next_episode
        self.episodes = [
            {"episode_id": f"s{season}e{num}", "season": season, "episode": num}
            for season in (1, 2) for num in (1, 2, 3)
        ]

    def test_the_next_one_in_the_season(self):
        self.assertEqual(self.next(self.episodes, "s1e1")["episode_id"], "s1e2")

    def test_the_last_of_a_season_crosses_into_the_next(self):
        self.assertEqual(self.next(self.episodes, "s1e3")["episode_id"], "s2e1")

    def test_the_last_episode_of_the_show_offers_nothing(self):
        self.assertIsNone(self.next(self.episodes, "s2e3"))

    def test_it_never_wraps_back_to_the_beginning(self):
        """The difference from ⏭, and the reason the finished card exists."""
        self.assertIsNone(self.next(self.episodes, "s2e3"))

    def test_an_episode_from_another_show_offers_nothing(self):
        """Better nothing than autoplaying into a show you are not watching."""
        self.assertIsNone(self.next(self.episodes, "gone"))

    def test_no_episodes_offers_nothing(self):
        self.assertIsNone(self.next([], "s1e1"))
        self.assertIsNone(self.next(None, "s1e1"))

    def test_ids_are_compared_as_strings(self):
        """Providers send episode ids as both ints and strings."""
        episodes = [{"episode_id": 11}, {"episode_id": 12}]
        self.assertEqual(self.next(episodes, "11")["episode_id"], 12)
        self.assertEqual(self.next(episodes, 11)["episode_id"], 12)

    def test_a_single_episode_show_offers_nothing(self):
        self.assertIsNone(self.next([{"episode_id": "only"}], "only"))


class TestEpisodeCaption(unittest.TestCase):
    """Naming an episode on the up-next card.

    Provider titles are a mess: some carry a real episode name, many just echo
    the show and the SxxEyy label, which reads as "S01E02 · Show - S01E02" if
    you paste them together without looking.
    """

    def setUp(self):
        from ui.series_page import episode_caption

        self.caption = episode_caption

    def episode(self, title, season=1, num=2):
        return {"season": season, "episode": num, "title": title}

    def test_a_real_name_is_worth_showing(self):
        title, subtitle = self.caption(self.episode("Mrs. Peterson"), "Trailer Park Boys")
        self.assertEqual(title, "S01E02 · Mrs. Peterson")
        self.assertEqual(subtitle, "Trailer Park Boys")

    def test_a_title_that_only_repeats_the_show_is_dropped(self):
        """The real case on this catalog: 'Trailer Park Boys - S01E02'."""
        title, subtitle = self.caption(
            self.episode("Trailer Park Boys - S01E02"), "Trailer Park Boys")
        self.assertEqual(title, "S01E02")
        self.assertEqual(subtitle, "Trailer Park Boys")

    def test_a_title_that_is_only_the_label_is_dropped(self):
        self.assertEqual(self.caption(self.episode("S01E02"), "Show")[0], "S01E02")

    def test_an_empty_title_still_names_the_episode(self):
        self.assertEqual(self.caption(self.episode(""), "Show")[0], "S01E02")
        self.assertEqual(self.caption(self.episode(None), "Show")[0], "S01E02")

    def test_no_show_name_is_not_an_error(self):
        title, subtitle = self.caption(self.episode("Mrs. Peterson"))
        self.assertEqual(title, "S01E02 · Mrs. Peterson")
        self.assertEqual(subtitle, "")

    def test_dangling_punctuation_is_cleaned_up(self):
        for raw in ("Show — S01E02", "Show: S01E02", "Show | S01E02", "S01E02 - Show"):
            self.assertEqual(self.caption(self.episode(raw), "Show")[0], "S01E02", raw)

    def test_the_label_is_zero_padded_two_digits(self):
        title, _ = self.caption(self.episode("Name", season=12, num=7), "")
        self.assertTrue(title.startswith("S12E07"), title)


class TestMasterSearch(unittest.TestCase):
    """Searching the whole catalog at once, not just the open tab."""

    def setUp(self):
        import tempfile

        from ui.search_page import search_catalog

        self.search = search_catalog
        self.dir = Path(tempfile.mkdtemp())
        self.db = Database(self.dir / "t.sqlite")
        self.db.execute(
            "INSERT INTO playlists(id, name, type, position) VALUES(1,'p','xtream',0)")
        # The same title filed under all three kinds.
        for kind, stream_id in (("live", "1"), ("movie", "2"), ("series", "3")):
            self.add(kind, stream_id, "Trailer Park Boys")

    def add(self, kind, stream_id, name, available=1, dup_rank=1):
        self.db.execute(
            "INSERT INTO streams(playlist_id, kind, stream_id, name, name_folded,"
            " available, dup_rank) VALUES(1,?,?,?,?,?,?)",
            (kind, stream_id, name, fold(name), available, dup_rank),
        )

    def tearDown(self):
        import shutil

        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def names(self, results, kind):
        return [row[1] for row in results[kind][0]]

    def test_one_call_reaches_every_kind(self):
        results = self.search(self.db, 1, "trailer")
        for kind in ("live", "movie", "series"):
            self.assertEqual(self.names(results, kind), ["Trailer Park Boys"], kind)

    def test_accents_are_folded_not_just_lowercased(self):
        """lower('café') matches nothing; the folded column needs a folded term."""
        self.add("movie", "9", "Café Society")
        self.assertEqual(self.names(self.search(self.db, 1, "cafe"), "movie"),
                         ["Café Society"])
        self.assertEqual(self.names(self.search(self.db, 1, "café"), "movie"),
                         ["Café Society"])

    def test_results_are_capped_and_report_truncation(self):
        for index in range(30):
            self.add("movie", f"m{index}", f"Park Number {index:02d}")
        rows, truncated = self.search(self.db, 1, "park")["movie"]
        self.assertEqual(len(rows), 18)
        self.assertTrue(truncated)

    def test_a_short_result_set_is_not_marked_truncated(self):
        rows, truncated = self.search(self.db, 1, "trailer")["movie"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(truncated)

    def test_a_custom_cap_is_honoured(self):
        for index in range(10):
            self.add("live", f"c{index}", f"Park Channel {index}")
        rows, truncated = self.search(self.db, 1, "park", caps={"live": 3})["live"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(truncated)

    def test_withdrawn_nameless_and_duplicate_rows_stay_out(self):
        self.add("movie", "d1", "Park Gone", available=0)
        self.add("movie", "d2", "", available=1)
        # dup_rank>1 is the sync-time marker for the same title in another
        # category; without excluding it one film lists several times.
        self.add("movie", "d3", "Park Duplicate", dup_rank=2)
        names = self.names(self.search(self.db, 1, "park"), "movie")
        self.assertNotIn("Park Gone", names)
        self.assertNotIn("Park Duplicate", names)
        self.assertNotIn("", names)

    def test_a_term_too_short_to_be_worth_scanning_returns_nothing(self):
        for term in ("", " ", "t", "  a "):
            results = self.search(self.db, 1, term)
            self.assertEqual(sum(len(r) for r, _ in results.values()), 0, repr(term))

    def test_no_match_gives_empty_sections_rather_than_an_error(self):
        results = self.search(self.db, 1, "nothinglikethis")
        self.assertEqual(set(results), {"live", "movie", "series"})
        self.assertEqual(sum(len(r) for r, _ in results.values()), 0)

    def test_another_playlists_rows_are_not_returned(self):
        self.db.execute(
            "INSERT INTO playlists(id, name, type, position) VALUES(2,'q','xtream',1)")
        self.db.execute(
            "INSERT INTO streams(playlist_id, kind, stream_id, name, name_folded,"
            " available, dup_rank) VALUES(2,'movie','x','Trailer Other','trailer other',1,1)")
        self.assertEqual(self.names(self.search(self.db, 1, "trailer"), "movie"),
                         ["Trailer Park Boys"])

    def test_rows_match_the_shape_the_catalog_model_expects(self):
        from ui.models import CatalogModel

        rows = self.search(self.db, 1, "trailer")["movie"][0]
        self.assertEqual(len(rows[0]), len(CatalogModel.FIELDS))
        self.assertEqual(rows[0][1], "Trailer Park Boys")

    def test_live_results_keep_the_providers_running_order(self):
        self.db.execute("UPDATE streams SET num=7 WHERE kind='live'")
        self.add("live", "20", "Park Extra")
        self.db.execute("UPDATE streams SET num=2 WHERE stream_id='20'")
        self.assertEqual(self.names(self.search(self.db, 1, "park"), "live"),
                         ["Park Extra", "Trailer Park Boys"])


class TestFlowLayout(unittest.TestCase):
    """Episode cards wrap instead of scrolling sideways.

    Driven with stub items rather than real widgets so it needs no QApplication
    and no display — the layout only ever asks an item for its size hint and
    hands it a rectangle.
    """

    class Stub:
        def __init__(self, width, height):
            from PySide6.QtCore import QSize

            self._size = QSize(width, height)
            self.rect = None

        def sizeHint(self):
            return self._size

        def minimumSize(self):
            return self._size

        def setGeometry(self, rect):
            self.rect = rect

    def _layout(self, count=6, spacing=10, size=(268, 190)):
        from ui.series_page import FlowLayout

        layout = FlowLayout(None, margin=0, spacing=spacing)
        items = [self.Stub(*size) for _ in range(count)]
        for item in items:
            layout.addItem(item)
        return layout, items

    def test_a_narrower_view_needs_more_height(self):
        layout, _ = self._layout()
        wide = layout.heightForWidth(1200)
        medium = layout.heightForWidth(600)
        narrow = layout.heightForWidth(280)
        self.assertLess(wide, medium)
        self.assertLess(medium, narrow)

    def test_items_land_on_wrapped_rows(self):
        from PySide6.QtCore import QRect

        layout, items = self._layout()
        layout.setGeometry(QRect(0, 0, 600, 2000))
        rows = sorted({item.rect.y() for item in items})
        columns = sorted({item.rect.x() for item in items})
        self.assertEqual(len(columns), 2)      # two fit across 600px
        self.assertEqual(len(rows), 3)         # so six need three rows
        self.assertTrue(all(item.rect.x() >= 0 for item in items))

    def test_one_column_when_nothing_else_fits(self):
        from PySide6.QtCore import QRect

        layout, items = self._layout(count=4)
        layout.setGeometry(QRect(0, 0, 300, 2000))
        self.assertEqual(len({item.rect.y() for item in items}), 4)

    def test_an_empty_layout_is_harmless(self):
        layout, _ = self._layout(count=0)
        self.assertEqual(layout.count(), 0)
        self.assertGreaterEqual(layout.heightForWidth(800), 0)


class TestSeriesInfoParsing(unittest.TestCase):
    """The provider's info object, which varies more than its schema admits."""

    def setUp(self):
        from core.sync import episode_image, first_backdrop

        self.image = episode_image
        self.backdrop = first_backdrop

    def test_backdrop_is_a_list_in_the_schema(self):
        self.assertEqual(self.backdrop(["http://a/1.jpg", "http://a/2.jpg"]),
                         "http://a/1.jpg")

    def test_backdrop_is_sometimes_a_bare_string(self):
        self.assertEqual(self.backdrop("http://a/1.jpg"), "http://a/1.jpg")

    def test_backdrop_missing_or_empty(self):
        for value in (None, [], [""], "", "   ", 17):
            self.assertEqual(self.backdrop(value), "", value)

    def test_episode_image_prefers_the_still(self):
        self.assertEqual(
            self.image({"movie_image": "http://a/s.jpg", "cover": "http://a/c.jpg"}),
            "http://a/s.jpg")

    def test_episode_image_falls_through_the_alternatives(self):
        self.assertEqual(self.image({"cover_big": "http://a/b.jpg"}), "http://a/b.jpg")

    def test_the_literal_string_null_is_not_a_url(self):
        """Providers send "null" as text often enough to matter."""
        self.assertEqual(self.image({"movie_image": "null", "cover": " "}), "")
        self.assertEqual(self.image({}), "")
        self.assertEqual(self.image(None), "")


class TestPipRect(unittest.TestCase):
    """Where the Picture-in-Picture window lands.

    Pure arithmetic in ui/main_window.py so the corner cases can be checked
    without a display — the same reason clamp_seek lives at module level.
    """

    def setUp(self):
        from ui.main_window import PIP_WIDTH, pip_rect

        self.rect = pip_rect
        self.default_width = PIP_WIDTH
        self.screen = (0, 0, 1920, 1080)

    def test_sits_in_the_bottom_right_with_its_margin(self):
        x, y, w, h = self.rect(self.screen, width=560, margin=24)
        self.assertEqual((w, h), (560, 315))                 # 16:9
        self.assertEqual(x, 1920 - 560 - 24)
        self.assertEqual(y, 1080 - 315 - 24)

    def test_screen_origin_is_respected(self):
        """A second monitor's area does not start at (0, 0)."""
        x, y, w, h = self.rect((1920, -200, 1280, 800), width=560, margin=24)
        self.assertEqual(x, 1920 + 1280 - 560 - 24)
        self.assertEqual(y, -200 + 800 - 315 - 24)

    def test_width_wider_than_the_screen_is_clamped(self):
        x, y, w, h = self.rect((0, 0, 480, 320), width=1200, margin=24)
        self.assertLessEqual(w, 480)
        self.assertLessEqual(h, 320)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def test_never_starts_off_the_screen(self):
        """The margin gives way rather than pushing the window out of view."""
        for screen in ((0, 0, 1920, 1080), (0, 0, 600, 340), (0, 0, 560, 315),
                       (100, 50, 640, 400)):
            for width in (320, 560, 900, 4000):
                x, y, w, h = self.rect(screen, width=width, margin=24)
                sx, sy, sw, sh = screen
                self.assertGreaterEqual(x, sx, (screen, width))
                self.assertGreaterEqual(y, sy, (screen, width))
                self.assertLessEqual(x + w, sx + sw, (screen, width))
                self.assertLessEqual(y + h, sy + sh, (screen, width))

    def test_a_short_screen_gives_up_the_aspect_before_going_off_screen(self):
        x, y, w, h = self.rect((0, 0, 1920, 200), width=560, margin=0)
        self.assertLessEqual(h, 200)
        self.assertLessEqual(y + h, 200)

    def test_default_width_is_wide_enough_for_the_bar(self):
        """The bar's main row needs roughly 490px; the default must clear it."""
        self.assertGreaterEqual(self.default_width, 500)


class TestRevealSizes(unittest.TestCase):
    """Giving the video pane its width back.

    Hiding a splitter child is not symmetric: the width goes to the items pane
    and never returns on its own, so the reveal has to say where it comes from.
    Pure arithmetic in ui/main_window.py, for the same reason pip_rect is.
    """

    def setUp(self):
        from ui.main_window import (
            MIN_LEFT_WIDTH, MIN_MIDDLE_WIDTH, PLAYER_PANE_WIDTH, reveal_sizes,
        )

        self.sizes = reveal_sizes
        self.default = PLAYER_PANE_WIDTH
        self.min_middle = MIN_MIDDLE_WIDTH
        self.min_left = MIN_LEFT_WIDTH

    def test_a_normal_window_gets_the_starting_layout_back(self):
        """1500px wide, collapsed: items holds the video's width and hands it back."""
        self.assertEqual(self.sizes([330, 1170, 0], want=510), [330, 660, 510])

    def test_a_dragged_width_is_honoured(self):
        """Drag the pane wider, stop, play again — it returns as you left it."""
        self.assertEqual(self.sizes([330, 1170, 0], want=700), [330, 470, 700])

    def test_items_keeps_its_minimum_and_categories_pays_the_rest(self):
        left, middle, right = self.sizes([330, 400, 0], want=510)
        self.assertEqual(middle, self.min_middle)
        self.assertEqual(left, self.min_left)
        # Both panes gave all they could spare, which is still short of 510.
        self.assertEqual(right, (400 - self.min_middle) + (330 - self.min_left))
        self.assertLess(right, 510)

    def test_a_window_with_nothing_to_spare_still_returns_sane_widths(self):
        for current in ([330, 1170, 0], [180, 260, 0], [100, 200, 0], [0, 0, 0],
                        [180, 259, 0], [50, 900, 0]):
            for want in (0, 120, 510, 900, 4000):
                got = self.sizes(current, want=want)
                self.assertEqual(len(got), 3, (current, want))
                self.assertTrue(all(v >= 0 for v in got), (current, want, got))
                # The total is all the width there is; it must not grow.
                self.assertEqual(sum(got), sum(current[:2]), (current, want, got))

    def test_it_never_takes_more_than_it_wants(self):
        self.assertEqual(self.sizes([330, 1170, 0], want=200)[2], 200)

    def test_the_default_width_clears_the_docked_bar(self):
        """The bar lives in this pane and its minimum is a measured 522px.

        Asking for less is not an error, it is just a lie: Qt widens the pane to
        the minimum anyway, and the remembered width then creeps to 522.
        """
        self.assertGreaterEqual(self.default, 522)


class TestMachOArches(unittest.TestCase):
    """Reading a Mach-O header to explain an architecture mismatch.

    An Apple Silicon app cannot load an Intel libVLC, and dyld only says
    "incompatible architecture". Naming the two builds is only helpful if the
    parse is right — a wrong answer ("your VLC is Intel" when it is not) is
    worse than the raw error, so the header is built here from known bytes
    rather than read off whichever machine happens to run the tests.
    """

    ARM64 = 0x0100000C
    X86_64 = 0x01000007

    def setUp(self):
        import struct
        import tempfile

        from core import vlc_setup

        self.struct = struct
        self.arches = vlc_setup.macho_arches
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, data: bytes) -> Path:
        path = self.dir / "libvlc.dylib"
        path.write_bytes(data)
        return path

    def _thin(self, cpu: int, little: bool = True) -> bytes:
        magic = b"\xcf\xfa\xed\xfe" if little else b"\xfe\xed\xfa\xcf"
        order = "<" if little else ">"
        # magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, pad
        return magic + self.struct.pack(order + "7I", cpu, 0, 6, 0, 0, 0, 0) + b"\0" * 32

    def _fat(self, cpus) -> bytes:
        out = b"\xca\xfe\xba\xbe" + self.struct.pack(">I", len(cpus))
        for cpu in cpus:
            # cputype, cpusubtype, offset, size, align
            out += self.struct.pack(">5I", cpu, 0, 4096, 100, 12)
        return out + b"\0" * 64

    def test_thin_apple_silicon(self):
        self.assertEqual(self.arches(self._write(self._thin(self.ARM64))), {"arm64"})

    def test_thin_intel(self):
        self.assertEqual(self.arches(self._write(self._thin(self.X86_64))), {"x86_64"})

    def test_universal_binary_reports_every_slice(self):
        path = self._write(self._fat([self.X86_64, self.ARM64]))
        self.assertEqual(self.arches(path), {"x86_64", "arm64"})

    def test_byte_swapped_header_is_still_read(self):
        self.assertEqual(self.arches(self._write(self._thin(self.ARM64, little=False))), {"arm64"})

    def test_the_mismatch_this_exists_to_catch(self):
        """An Intel-only VLC on an Apple Silicon app: the set must not contain
        the running architecture, which is what triggers the hint."""
        have = self.arches(self._write(self._thin(self.X86_64)))
        self.assertTrue(have)
        self.assertNotIn("arm64", have)

    def test_unknown_cpu_type_is_not_guessed_at(self):
        self.assertEqual(self.arches(self._write(self._thin(0x0BADF00D))), set())

    def test_garbage_and_truncation_stay_silent(self):
        """Every one of these must give an empty set, never an exception: the
        caller treats empty as 'say nothing' and falls back to dyld's message."""
        self.assertEqual(self.arches(self._write(b"not a mach-o file at all")), set())
        self.assertEqual(self.arches(self._write(b"")), set())
        self.assertEqual(self.arches(self._write(b"\xcf\xfa")), set())
        # A fat header whose table is cut short.
        self.assertEqual(self.arches(self._write(b"\xca\xfe\xba\xbe" + self.struct.pack(">I", 3))), set())
        # An absurd slice count, which would otherwise mean a huge read.
        self.assertEqual(self.arches(self._write(b"\xca\xfe\xba\xbe" + self.struct.pack(">I", 2**31))), set())
        self.assertEqual(self.arches(self._write(b"\xca\xfe\xba\xbe" + self.struct.pack(">I", 0))), set())

    def test_a_missing_or_unreadable_path_is_not_an_error(self):
        self.assertEqual(self.arches(self.dir / "nothing-here.dylib"), set())
        self.assertEqual(self.arches(self.dir), set())  # a directory


class TestSubtitleHash(unittest.TestCase):
    """The remote hash is the whole basis of VLSub's exact match.

    A wrong hash fails silently — the server simply returns nothing — so this
    pins `hash_url` (two ranged GETs against a redirecting CDN) to `hash_file`,
    which reads the same bytes locally.
    """

    def test_local_files_are_hashed_directly(self):
        """A downloaded title plays from file://, which requests cannot fetch.

        That is also the case where an exact match actually succeeds, so
        routing it to hash_file rather than hash_url is the whole point.
        """
        import tempfile

        from core.subtitles import CHUNK, hash_any, hash_file, local_path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "A Film (1964).mp4"
            path.write_bytes(b"\x00" * (CHUNK * 2 + 16))
            self.assertEqual(local_path(path.absolute().as_uri()), path,
                             "a file:// URI with escaped spaces must resolve")
            self.assertEqual(local_path(str(path)), path)
            self.assertEqual(hash_any(path.absolute().as_uri()), hash_file(path))

        self.assertIsNone(local_path("http://portal/movie/u/p/1.mp4"))
        self.assertIsNone(local_path("file:///nowhere/missing.mp4"))
        self.assertIsNone(local_path(""))

    def test_hash_url_matches_hash_file(self):
        import http.server
        import random
        import re
        import tempfile
        import threading

        from core.subtitles import CHUNK, hash_file, hash_url

        payload = random.Random(7).randbytes(CHUNK * 3 + 1234)

        class RangeHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                match = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "") or "")
                if match:
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else len(payload) - 1
                else:
                    start, end = 0, len(payload) - 1
                body = payload[start:end + 1]
                self.send_response(206 if match else 200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class QuietServer(http.server.ThreadingHTTPServer):
            # hash_url closes the size probe without reading its body, which
            # resets the connection; that is correct client behaviour, so the
            # server should not print a traceback about it.
            def handle_error(self, request, client_address):
                pass

        server = QuietServer(("127.0.0.1", 0), RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "movie.mp4"
                path.write_bytes(payload)
                local_hash, local_size = hash_file(path)
                remote_hash, remote_size = hash_url(
                    f"http://127.0.0.1:{server.server_address[1]}/movie.mp4")
            self.assertEqual(remote_size, local_size)
            self.assertEqual(remote_hash, local_hash)
            self.assertEqual(len(remote_hash), 16, "OpenSubtitles wants 16 hex digits")
        finally:
            server.shutdown()
            server.server_close()


class TestHomeSections(unittest.TestCase):
    """The homepage wall: the one place all three kinds appear together."""

    def setUp(self):
        import tempfile
        import time

        from ui.home_page import MIN_RAIL, home_sections

        self.build = home_sections
        self.min_rail = MIN_RAIL
        self.dir = Path(tempfile.mkdtemp())
        self.db = Database(self.dir / "t.sqlite")
        self.db.execute(
            "INSERT INTO playlists(id, name, type, position) VALUES(1,'p','xtream',0)")
        self.now = int(time.time())
        # Enough of each kind that the "new" rails clear MIN_RAIL.
        for kind in ("live", "movie", "series"):
            for n in range(8):
                self.add(kind, f"{kind}{n}", f"{kind.title()} {n}")

    def tearDown(self):
        import shutil

        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, kind, stream_id, name, available=1, dup_rank=1, category=None,
            added=None):
        self.db.execute(
            "INSERT INTO streams(playlist_id, kind, stream_id, name, name_folded,"
            " available, dup_rank, category_id, added) VALUES(1,?,?,?,?,?,?,?,?)",
            (kind, stream_id, name, fold(name), available, dup_rank, category,
             self.now if added is None else added),
        )

    def category(self, kind, category_id, sub_name, group="Genres", count=100):
        self.db.execute(
            "INSERT INTO categories(playlist_id, kind, category_id, name,"
            " group_name, sub_name, item_count) VALUES(1,?,?,?,?,?,?)",
            (kind, category_id, sub_name, group, sub_name, count),
        )

    def watch(self, kind, stream_id, when, episode=""):
        self.db.execute(
            "INSERT INTO history(playlist_id, kind, stream_id, episode_id,"
            " position_secs, duration_secs, watched_at) VALUES(1,?,?,?,60,600,?)",
            (kind, stream_id, episode, when),
        )

    def favourite(self, kind, stream_id, when):
        self.db.execute(
            "INSERT INTO favourites(playlist_id, kind, stream_id, added_at)"
            " VALUES(1,?,?,?)", (kind, stream_id, when))

    def sections(self, **kw):
        return {key: (rows, kinds, target)
                for key, _title, rows, kinds, target in self.build(self.db, 1, **kw)}

    # -- the personal rails -------------------------------------------------

    def test_continue_watching_mixes_kinds_in_one_rail(self):
        """The whole point: films and shows side by side, which no other view does."""
        self.watch("movie", "movie1", 100)
        self.watch("series", "series2", 200)
        self.watch("live", "live3", 300)
        rows, kinds, _ = self.sections()["continue"]
        self.assertEqual(kinds, ["live", "series", "movie"])   # most recent first
        self.assertEqual([r[0] for r in rows], ["live3", "series2", "movie1"])

    def test_a_show_watched_episode_by_episode_appears_once(self):
        """History is keyed per episode; ungrouped, one show fills the rail."""
        for number in range(6):
            self.watch("series", "series1", 100 + number, episode=f"e{number}")
        rows, _, _ = self.sections()["continue"]
        self.assertEqual([r[0] for r in rows], ["series1"])

    def test_the_personal_rails_are_dropped_when_empty(self):
        """A new install has neither, and two empty headings is not a homepage."""
        sections = self.sections()
        self.assertNotIn("continue", sections)
        self.assertNotIn("favourites", sections)

    def test_favourites_are_newest_first_and_mix_kinds(self):
        self.favourite("movie", "movie1", 100)
        self.favourite("live", "live2", 200)
        rows, kinds, _ = self.sections()["favourites"]
        self.assertEqual(kinds, ["live", "movie"])
        self.assertEqual([r[0] for r in rows], ["live2", "movie1"])

    def test_a_catalog_with_nothing_in_it_yields_no_rails_at_all(self):
        """What the page's empty state is driven by — not ten blank headings."""
        self.db.execute("DELETE FROM streams WHERE playlist_id=1")
        self.assertEqual(self.build(self.db, 1), [])

    def test_a_watched_title_the_provider_dropped_is_not_offered(self):
        """available=0 rows would render as a dead cell you cannot play."""
        self.add("movie", "gone", "Gone", available=0)
        self.watch("movie", "gone", 100)
        self.assertNotIn("continue", self.sections())

    def test_history_for_a_title_not_in_the_catalog_is_ignored(self):
        self.watch("movie", "nosuch", 100)
        self.assertNotIn("continue", self.sections())

    # -- the generated rails ------------------------------------------------

    def test_every_kind_gets_a_new_rail(self):
        sections = self.sections()
        for key in ("new_movie", "new_series", "new_live"):
            self.assertIn(key, sections)

    def test_duplicates_and_nameless_rows_never_appear(self):
        self.add("movie", "dup", "Dup", dup_rank=2)
        self.add("movie", "blank", "")
        ids = [r[0] for r in self.sections()["new_movie"][0]]
        self.assertNotIn("dup", ids)
        self.assertNotIn("blank", ids)

    def test_rails_are_capped(self):
        for n in range(40):
            self.add("movie", f"extra{n}", f"Extra {n}")
        self.assertEqual(len(self.sections(cap=7)["new_movie"][0]), 7)

    def test_kinds_line_up_with_rows_one_for_one(self):
        self.watch("movie", "movie1", 100)
        self.favourite("series", "series1", 100)
        for _key, (rows, kinds, _target) in self.sections().items():
            self.assertEqual(len(rows), len(kinds))

    # -- genres -------------------------------------------------------------

    def test_genre_rails_come_from_the_providers_taxonomy(self):
        self.category("movie", "c1", "Horror", count=500)
        for n in range(8):
            self.add("movie", f"h{n}", f"Horror {n}", category="c1")
        sections = self.sections()
        self.assertIn("genre_movie_c1", sections)
        rows, kinds, target = sections["genre_movie_c1"]
        self.assertEqual(kinds, ["movie"] * len(rows))
        self.assertEqual(target, ("movie", "category", "c1"))

    def test_no_genre_taxonomy_means_no_genre_rails(self):
        """Not five empty headings — providers differ, and many ship none."""
        keys = self.sections()
        self.assertFalse([k for k in keys if k.startswith("genre_")])

    def test_a_genre_too_small_to_fill_a_rail_is_skipped(self):
        self.category("movie", "c2", "Tiny", count=500)
        self.add("movie", "t1", "Tiny One", category="c2")
        self.assertNotIn("genre_movie_c2", self.sections())

    def test_show_genres_are_labelled_so_headings_do_not_collide(self):
        """Both taxonomies have a Documentary; two identical headings confuse."""
        self.category("movie", "m1", "Documentary", count=900)
        self.category("series", "s1", "Documentary", count=800)
        for n in range(8):
            self.add("movie", f"md{n}", f"Doc {n}", category="m1")
            self.add("series", f"sd{n}", f"Show Doc {n}", category="s1")
        titles = [title for _k, title, _r, _ki, _t in self.build(self.db, 1)]
        self.assertIn("DOCUMENTARY", titles)
        self.assertIn("SHOWS · DOCUMENTARY", titles)
        self.assertEqual(len(titles), len(set(titles)))

    def test_the_wall_is_capped_and_keeps_the_personal_rails(self):
        from ui.home_page import MAX_RAILS

        self.watch("movie", "movie1", 100)
        self.favourite("movie", "movie2", 100)
        for n in range(20):
            self.category("movie", f"g{n}", f"Genre {n}", count=1000 - n)
            for m in range(8):
                self.add("movie", f"g{n}i{m}", f"G{n} {m}", category=f"g{n}")
        sections = self.build(self.db, 1)
        keys = [key for key, _t, _r, _k, _ta in sections]
        self.assertLessEqual(len(sections), MAX_RAILS + 2)
        self.assertEqual(keys[0], "continue")
        self.assertEqual(keys[1], "favourites")


class TestPinnedRails(unittest.TestCase):
    """Categories the user pins to the homepage.

    The motivating case is a *group*: Bangla is five categories and 385 films,
    nowhere near the top five genres, so the wall would never mention it.
    """

    def setUp(self):
        import tempfile
        import time

        from ui.home_page import (
            home_sections, is_pinned, pin_keys, pin_rail, pinned_rails, unpin_rail,
        )

        self.build = home_sections
        self.pin = pin_rail
        self.unpin = unpin_rail
        self.pinned = pinned_rails
        self.keys = pin_keys
        self.is_pinned = is_pinned
        self.dir = Path(tempfile.mkdtemp())
        self.db = Database(self.dir / "t.sqlite")
        for playlist in (1, 2):
            self.db.execute(
                "INSERT INTO playlists(id, name, type, position) VALUES(?,?,'xtream',0)",
                (playlist, f"p{playlist}"))
        self.now = int(time.time())

    def tearDown(self):
        import shutil

        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add(self, kind, stream_id, name, category, playlist=1):
        self.db.execute(
            "INSERT INTO streams(playlist_id, kind, stream_id, name, name_folded,"
            " available, dup_rank, category_id, added) VALUES(?,?,?,?,?,1,1,?,?)",
            (playlist, kind, stream_id, name, fold(name), category, self.now),
        )

    def category(self, kind, category_id, sub_name, group, count=100, playlist=1):
        self.db.execute(
            "INSERT INTO categories(playlist_id, kind, category_id, name,"
            " group_name, sub_name, item_count) VALUES(?,?,?,?,?,?,?)",
            (playlist, kind, category_id, sub_name, group, sub_name, count),
        )

    def bangla(self, playlist=1):
        """The user's own example: a group of several categories."""
        for index, (category_id, name) in enumerate(
                (("c1", "Bangla Modern"), ("c2", "Bangla Trending"))):
            self.category("movie", category_id, name, "Bangla", playlist=playlist)
            for n in range(4):
                self.add("movie", f"{category_id}-{n}", f"{name} {n}", category_id,
                         playlist=playlist)

    def sections(self, playlist=1):
        return {key: (title, rows, kinds, target)
                for key, title, rows, kinds, target in self.build(self.db, playlist)}

    # -- the table ----------------------------------------------------------

    def test_pin_and_unpin_round_trip(self):
        self.pin(self.db, 1, "movie", "group", "Bangla", "Bangla")
        self.assertTrue(self.is_pinned(self.db, 1, "movie", "group", "Bangla"))
        self.assertEqual(self.pinned(self.db, 1),
                         [("movie", "group", "Bangla", "Bangla")])
        self.unpin(self.db, 1, "movie", "group", "Bangla")
        self.assertFalse(self.is_pinned(self.db, 1, "movie", "group", "Bangla"))
        self.assertEqual(self.pinned(self.db, 1), [])

    def test_pinning_twice_does_not_duplicate(self):
        self.pin(self.db, 1, "movie", "group", "Bangla", "Bangla")
        self.pin(self.db, 1, "movie", "group", "Bangla", "Bangla renamed")
        rows = self.pinned(self.db, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "Bangla renamed")

    def test_the_same_name_under_two_kinds_is_two_pins(self):
        self.pin(self.db, 1, "movie", "group", "Sports", "Sports")
        self.pin(self.db, 1, "live", "group", "Sports", "Sports")
        self.assertEqual(len(self.pinned(self.db, 1)), 2)

    def test_pins_are_per_playlist(self):
        self.pin(self.db, 1, "movie", "category", "c1", "One")
        self.assertEqual(self.pinned(self.db, 2), [])
        self.assertFalse(self.is_pinned(self.db, 2, "movie", "category", "c1"))

    def test_keys_are_what_the_sidebar_lights(self):
        self.pin(self.db, 1, "movie", "group", "Bangla", "Bangla")
        self.assertEqual(self.keys(self.db, 1), {("movie", "group", "Bangla")})

    # -- the rails ----------------------------------------------------------

    def test_a_pinned_group_becomes_a_rail(self):
        self.bangla()
        self.pin(self.db, 1, "movie", "group", "Bangla", "Bangla")
        title, rows, kinds, target = self.sections()["pin_movie_group_Bangla"]
        self.assertEqual(title, "BANGLA")
        self.assertEqual(len(rows), 8)          # both categories in the group
        self.assertEqual(set(kinds), {"movie"})
        self.assertEqual(target, ("movie", "group", "Bangla"))

    def test_a_pinned_category_becomes_a_rail(self):
        self.bangla()
        self.pin(self.db, 1, "movie", "category", "c1", "Bangla Modern")
        title, rows, _kinds, target = self.sections()["pin_movie_category_c1"]
        self.assertEqual(title, "BANGLA MODERN")
        self.assertEqual(len(rows), 4)
        self.assertEqual(target, ("movie", "category", "c1"))

    def test_a_small_pinned_rail_is_kept(self):
        """MIN_RAIL drops generated gaps; a rail you asked for is not a gap."""
        self.category("movie", "tiny", "Tiny", "Odds")
        self.add("movie", "t1", "Only One", "tiny")
        self.pin(self.db, 1, "movie", "category", "tiny", "Tiny")
        self.assertIn("pin_movie_category_tiny", self.sections())

    def test_pinned_rails_sit_after_your_history_and_before_the_guesses(self):
        self.bangla()
        self.db.execute(
            "INSERT INTO history(playlist_id, kind, stream_id, episode_id,"
            " position_secs, duration_secs, watched_at) VALUES(1,'movie','c1-0','',6,60,?)",
            (self.now,))
        self.pin(self.db, 1, "movie", "group", "Bangla", "Bangla")
        keys = [key for key, _t, _r, _k, _ta in self.build(self.db, 1)]
        self.assertEqual(keys[0], "continue")
        self.assertEqual(keys[1], "pin_movie_group_Bangla")
        self.assertTrue(keys[2].startswith("new_"), keys[2])

    def test_a_pinned_rail_survives_the_cap(self):
        from ui.home_page import MAX_RAILS

        self.bangla()
        self.pin(self.db, 1, "movie", "group", "Bangla", "Bangla")
        for n in range(MAX_RAILS + 6):
            self.category("movie", f"g{n}", f"Genre {n}", "Genres", count=1000 - n)
            for m in range(8):
                self.add("movie", f"g{n}i{m}", f"G{n} {m}", f"g{n}")
        keys = [key for key, _t, _r, _k, _ta in self.build(self.db, 1)]
        self.assertIn("pin_movie_group_Bangla", keys)

    def test_a_pinned_genre_does_not_appear_twice(self):
        self.category("movie", "h1", "Horror", "Genres", count=900)
        for n in range(8):
            self.add("movie", f"h{n}", f"Horror {n}", "h1")
        self.pin(self.db, 1, "movie", "category", "h1", "Horror")
        titles = [title for _k, title, _r, _ki, _t in self.build(self.db, 1)]
        self.assertEqual(titles.count("HORROR"), 1)

    def test_a_pin_the_provider_dropped_yields_no_rail_but_keeps_the_pin(self):
        self.pin(self.db, 1, "movie", "category", "gone", "Gone")
        self.assertFalse([k for k in self.sections() if k.startswith("pin_")])
        self.assertTrue(self.is_pinned(self.db, 1, "movie", "category", "gone"))


class TestCatalogModelKinds(unittest.TestCase):
    """Per-item kinds, which the mixed homepage rails need."""

    def setUp(self):
        # No QApplication: a QAbstractListModel is a plain QObject, and the
        # suite is meant to run headless.
        from ui.models import ROLE_KIND, CatalogModel

        self.role = ROLE_KIND
        self.model = CatalogModel()
        self.rows = [("1", "A"), ("2", "B")]

    def kinds(self):
        return [self.model.index(i, 0).data(self.role)
                for i in range(self.model.rowCount())]

    def test_without_a_kinds_list_every_row_reports_the_models_kind(self):
        """Every existing caller passes no kinds and must be unaffected."""
        self.model.set_rows(self.rows, "movie")
        self.assertEqual(self.kinds(), ["movie", "movie"])

    def test_a_kinds_list_gives_each_row_its_own(self):
        self.model.set_rows(self.rows, "movie", None, ["live", "series"])
        self.assertEqual(self.kinds(), ["live", "series"])

    def test_a_short_kinds_list_falls_back_rather_than_raising(self):
        self.model.set_rows(self.rows, "movie", None, ["live"])
        self.assertEqual(self.kinds(), ["live", "movie"])

    def test_setting_rows_again_without_kinds_clears_the_old_ones(self):
        self.model.set_rows(self.rows, "movie", None, ["live", "series"])
        self.model.set_rows(self.rows, "movie")
        self.assertEqual(self.kinds(), ["movie", "movie"])


class TestHomeCursor(unittest.TestCase):
    """Where the arrow keys land on the homepage's wall of rails."""

    def setUp(self):
        from ui.home_page import FAR_END, PAGE_RAILS, move_cursor

        self.move = move_cursor
        self.page = PAGE_RAILS
        self.far = FAR_END
        # three rails: 4 posters, 2, 6
        self.wall = [4, 2, 6]

    # -- along a rail ------------------------------------------------------

    def test_right_and_left_step_one(self):
        self.assertEqual(self.move(self.wall, (0, 1), 0, 1), (0, 2))
        self.assertEqual(self.move(self.wall, (0, 1), 0, -1), (0, 0))

    def test_the_ends_of_a_rail_clamp_rather_than_wrap(self):
        """Running off a rail should stop, not take you somewhere else."""
        self.assertEqual(self.move(self.wall, (0, 3), 0, 1), (0, 3))
        self.assertEqual(self.move(self.wall, (0, 0), 0, -1), (0, 0))

    def test_home_and_end(self):
        self.assertEqual(self.move(self.wall, (2, 3), 0, -self.far), (2, 0))
        self.assertEqual(self.move(self.wall, (2, 3), 0, self.far), (2, 5))

    # -- between rails -----------------------------------------------------

    def test_down_and_up_keep_the_column(self):
        self.assertEqual(self.move(self.wall, (0, 3), 1, 0), (1, 1))
        self.assertEqual(self.move(self.wall, (2, 1), -1, 0), (1, 1))

    def test_a_shorter_rail_clamps_the_column(self):
        """Column 3 has nowhere to land on a rail of two."""
        self.assertEqual(self.move(self.wall, (0, 3), 1, 0), (1, 1))

    def test_the_top_and_bottom_of_the_wall_clamp(self):
        self.assertEqual(self.move(self.wall, (0, 2), -1, 0), (0, 2))
        self.assertEqual(self.move(self.wall, (2, 2), 1, 0), (2, 2))

    def test_page_keys_jump_several_rails_and_still_clamp(self):
        wall = [3] * 8
        self.assertEqual(self.move(wall, (0, 1), self.page, 0), (self.page, 1))
        self.assertEqual(self.move(wall, (7, 1), self.page, 0), (7, 1))
        self.assertEqual(self.move(wall, (1, 1), -self.page, 0), (0, 1))

    # -- rails that are not there ------------------------------------------

    def test_an_empty_rail_is_skipped_not_landed_on(self):
        self.assertEqual(self.move([4, 0, 6], (0, 1), 1, 0), (2, 1))

    def test_a_cursor_on_a_vanished_rail_snaps_to_the_nearest_live_one(self):
        """An unpinned category takes its rail with it mid-session."""
        self.assertEqual(self.move([4, 0, 6], (1, 2), 0, 0), (0, 2))

    def test_a_cursor_past_the_end_of_a_shrunken_wall_comes_back(self):
        self.assertEqual(self.move([4], (5, 9), 0, 0), (0, 3))

    def test_no_cursor_starts_at_the_first_poster(self):
        self.assertEqual(self.move(self.wall, None), (0, 0))
        self.assertEqual(self.move([0, 0, 3], None), (2, 0))

    def test_an_empty_wall_takes_every_key_without_raising(self):
        for d_rail, d_column in ((0, 1), (0, -1), (1, 0), (-1, 0),
                                 (0, self.far), (self.page, 0)):
            self.assertIsNone(self.move([], (0, 0), d_rail, d_column))
            self.assertIsNone(self.move([0, 0], None, d_rail, d_column))


if __name__ == "__main__":
    unittest.main(verbosity=2)
