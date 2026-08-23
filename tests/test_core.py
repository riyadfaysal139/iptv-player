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


if __name__ == "__main__":
    unittest.main(verbosity=2)
