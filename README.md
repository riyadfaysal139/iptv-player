# IPTV Player

A cross-platform desktop IPTV client with **embedded VLC** playback, VLC's full
control bar, an organised **TV / Movies / Series** catalog, resumable downloads,
and **VLSub subtitles with no API key**.

Built for large catalogs: a busy provider ships **tens of thousands of items**,
which the app keeps in SQLite and renders through virtualized views so memory
stays flat (~200 MB) no matter how big the list is.

---

## VLC is required

The app plays video with **VLC's engine (libVLC)**, which it does *not* bundle —
it uses your VLC install. Get it from <https://www.videolan.org/vlc/>.

> **Windows:** you must install the **64-bit** build ("Windows 64bit" on the
> download page). A 32-bit VLC cannot be loaded by the 64-bit app, and that
> mismatch is the single most common cause of "it installed but nothing plays".
> Both the installer and the app detect this and say so explicitly.

---

## Installing

### macOS

1. Open `IPTV-Player-1.2.0-macOS-<arch>.dmg`
2. Drag **IPTV Player** onto **Applications**
3. **First launch only:** right-click (or Control-click) the app in Applications
   and choose **Open**, then confirm. The build is unsigned, so a normal
   double-click shows "cannot be opened because the developer cannot be
   verified".

### Windows

1. Run `IPTV-Player-1.2.0-Windows-x64-Setup.exe`
2. SmartScreen will warn about an unrecognised app — click **More info → Run
   anyway** (again, unsigned).
3. The installer checks for VLC and points you at the download if it is missing
   or is the wrong architecture.

Installs per-user by default (no admin needed); choose a system-wide install in
the elevation prompt if you prefer. Uninstall from **Settings → Apps**; it asks
whether to keep your playlists and cached catalog, and never deletes downloaded
videos.

### Linux

```sh
chmod +x IPTV_Player-1.2.0-x86_64.AppImage
./IPTV_Player-1.2.0-x86_64.AppImage
```

Install VLC first — `sudo apt install vlc`, `sudo dnf install vlc`, or your
distribution's equivalent.

> **"dlopen(): error loading libfuse.so.2"** — AppImages need FUSE 2, which
> Ubuntu 22.04+ and Fedora no longer install by default. Either
> `sudo apt install libfuse2`, or use the `.tar.gz` instead: extract it and run
> `IPTVPlayer/IPTVPlayer`, which needs nothing extra.

Built on Ubuntu 22.04 (glibc 2.35), so it runs on Ubuntu 22.04+, Debian 12+,
Mint 21+ and equivalents. To get a desktop menu entry, use any AppImage
integrator (Gear Lever, AppImageLauncher) or copy
`packaging/iptvplayer.desktop` into `~/.local/share/applications/`.

---

## Building the installers yourself

> **PyInstaller cannot cross-compile.** Each installer must be built on its own
> platform: a Windows `.exe` cannot be produced from macOS or Linux, and vice
> versa. Use the CI workflow below if you only have one machine.

### macOS → `.dmg`

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pyinstaller Pillow
./packaging/build_macos.sh
```

Output: `dist/IPTV-Player-<version>-macOS-<arch>.dmg` (~31 MB).

To ship a build that opens without the right-click dance, set your signing
identity first (requires a paid Apple Developer account):

```sh
export SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export NOTARY_PROFILE="my-profile"   # xcrun notarytool store-credentials
./packaging/build_macos.sh
```

### Windows → `Setup.exe`

Needs **64-bit Python 3.10+** and **[Inno Setup 6.3+](https://jrsoftware.org/isdl.php)**.

```bat
packaging\build_windows.bat
```

Output: `dist\IPTV-Player-<version>-Windows-x64-Setup.exe`. The script creates
the venv, installs dependencies, builds the `.exe`, self-tests it, then compiles
the installer. If Inno Setup is missing it stops after producing the runnable
`dist\IPTVPlayer\` folder.

### Linux → `.AppImage`

Needs **Python 3.10+** and
[`appimagetool`](https://github.com/AppImage/AppImageKit/releases).

```sh
sudo apt install libxcb-cursor0 libxcb-util1 libfuse2 desktop-file-utils
export APPIMAGETOOL=/path/to/appimagetool-x86_64.AppImage
./packaging/build_linux.sh
```

Outputs `dist/IPTV_Player-<version>-x86_64.AppImage` and a
`dist/IPTV-Player-<version>-linux-<arch>.tar.gz` fallback.

> **Build on the oldest distribution you intend to support.** glibc is forward-
> compatible, not backward: an AppImage built on Ubuntu 24.04 will not start on
> Ubuntu 22.04, and nothing about the build warns you. The CI job pins
> `ubuntu-22.04` for exactly this reason.

The script copies `libxcb-cursor.so.0` into the AppDir because Qt 6's `xcb`
platform plugin links it and PySide6 does not ship it — without it the app exits
with "could not load the Qt platform plugin xcb" on any machine that lacks it.

### All three, via GitHub Actions

`.github/workflows/build-installers.yml` builds macOS (Apple Silicon + Intel),
Windows and Linux on their native runners, and self-tests each artifact before
uploading it. Run it from the Actions tab and download the artifacts, or push a
`v*` tag to attach them all to a Release.

### Verifying a build

Every frozen build supports a self-test — useful because a GUI app that fails to
start otherwise tells you nothing:

```sh
"dist/IPTV Player.app/Contents/MacOS/IPTVPlayer" --selftest   # macOS
dist\IPTVPlayer\IPTVPlayer.exe --selftest                     # Windows
```

It reports module imports, the keyring backend, bundled data files and libVLC
detection, and writes the same report to the app's config folder.

---

## Running from source

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh                    # Windows: .venv\Scripts\python main.py
```

On first launch the app asks you to add a playlist. Paste your provider's portal
address **or** a full `get.php?username=…&password=…` M3U link — the fields fill
in automatically and the app probes for an Xtream API.

---

## Features

### The homepage

The house icon beside the magnifier — and where the app opens — is the one place
channels, films and shows appear together. Everywhere else shows a single kind,
because the catalog list pins its query to whichever tab is open.

Rows of posters that scroll sideways: **Continue Watching** and **Favourites**
first, both mixing all three kinds, then what is new in each, the biggest live
group, and the provider's own biggest genres. The genre rows are read from the
catalog rather than hard-coded, so a provider that ships no genre taxonomy gets
none instead of a screen of empty headings, and a row with fewer than six titles
in it is dropped — that is a gap with a heading on it, not a row.

Double-click plays, and **leaves you on the homepage** with the video docked
beside it; only a show takes you away, to its own page. **See all** on a row
hands over to the sidebar node it corresponds to.

**The arrows move across the wall.** `←` `→` run along a row, `↑` `↓` step
between rows keeping your place across, `Home`/`End` jump to the ends of a row
and `PgUp`/`PgDn` a few rows at a time. The poster under the cursor is drawn
larger, framed in yellow; the row scrolls sideways and the page scrolls down to
keep it in view. `Enter` plays it — or opens it, for a show. Every cell reserves
the space that growth needs, so nothing shifts as the cursor moves. Playing
something hands the keyboard to the video, as everywhere else in the app; one
click anywhere on the wall takes it back, with the cursor landing where you
clicked.

The homepage does not belong to a tab, so while it is up the house icon is the
only thing lit, TV/MOVIES/SERIES go grey, and the category sidebar — which lists
one tab's catalog — steps aside and gives the wall the full width. Clicking any
tab brings all three back, **including the tab already selected**: it is where
you would land, so it has to work.

**Pin your own.** Any sidebar group or category can be added to the homepage:
hover the row and click the pin, or right-click it and choose *Add to
homepage*. `BANGLA` under Movies — 385 films across five categories, nowhere
near the top five genres — becomes a row of its own, sitting after your history
and before anything the app guessed. Pinned rows are never trimmed by the cap
and never dropped for being small: a row you asked for is not a gap. The pin
stays lit in the sidebar so what is on the homepage is visible at a glance, and
**✕ Remove** on the row itself takes it off again. Pins are stored per playlist,
and pinning a genre suppresses the app's own version of it rather than showing
the heading twice.

The personal rows are the reason the queries are shaped the way they are. Joined
the obvious way — 93,000 `streams` rows against a grouped `history` subquery —
SQLite drives from `streams` and the row costs **57 ms warm, a full second
cold**. Selecting the handful of watched ids first and seeking `streams` by
primary key measures **0.0 ms**. The `CROSS JOIN` that does it is not
decoration: written as a plain `JOIN` the planner reorders it straight back to
the slow plan. The whole page builds in **57 ms**.

### Playlists
Add, edit, delete, reorder and switch between multiple sources. Three types are
supported: **Xtream Codes** (preferred), **M3U URL**, and **local M3U file**.
Each playlist is isolated by a foreign key with `ON DELETE CASCADE`, so
catalogs, favourites and downloads never mix. Passwords are stored in the OS
keychain, never in the database.

### Organised categories
The provider's flat list becomes a two-level tree. Typically a couple of hundred
live categories collapse into around twenty groups, and the VOD and series
categories into a handful each — every `LIVE |` and `SPORTS |` category, for
instance, merges into one **Sports & Events** group. `View ▸ Flat category list`
restores the provider's original ordering.

Nothing is ever unreachable: items whose category is missing from the provider's
own category list land in an **Uncategorized** bucket.

**CONTINUE WATCHING** lists one row per title, most recent first. Watch history
is stored per *episode*, so a show has as many history rows as episodes you have
seen — the list groups them, and the sidebar count is taken the same way the
list is built, including dropping titles the provider has since removed from the
catalog. Otherwise the badge says a number you cannot see on screen.

A position is filed against whatever is **playing**, not against the tab that
happens to be open or the show you have since browsed to — change tab mid-
episode, or open a second show while a first one plays, and the resume record
still belongs where it started.

### Browse first

The window opens on the homepage alone — no idle black rectangle taking a third
of the width before you have played anything. Double-click a channel, a film or
an episode and the video pane comes in beside the list; press ⏹, or let a film
run out, and it folds away again so browsing gets the width back. It keeps
whatever width you dragged it to, and `View ▸ Video pane` (`Ctrl+Shift+V`)
opens or closes it by hand without stopping the stream.

**Once it is on the right, it stays there.** Nothing the app's own navigation
does takes the video away: the series page and the search results render into
the items pane beside it, so playing an episode leaves you in the show, and
`Ctrl+F` mid-episode does not make the picture disappear. Only ⏹ closes it.

### Master search

The magnifier in the top bar — or `Ctrl+F` / `Cmd+F` from anywhere, including
fullscreen and Picture-in-Picture — searches **TV, Movies and Series at once**,
so you do not have to guess which tab a title was filed under. The results fill
the items pane, so whatever is playing keeps playing beside them. Results appear
grouped by type in the layout each one normally uses: channel rows with logos
for TV, poster cells for films and shows. **See all in MOVIES** hands the term
to that tab's own filter when a section has more than it shows.

Each section is capped, and the cap is not cosmetic. `name_folded` has no index
a leading-wildcard `LIKE` can use, so a search is a scan of ~93,000 rows; the
per-section limit lets SQLite stop early, which is the difference between
**0.2 ms and 190 ms** on a single common letter. Terms shorter than two
characters are refused for the same reason.

Search terms are folded, not merely lowercased — `cafe` finds `Café`, because
that is how the stored `name_folded` column is built.

### Series

Opening a show gives it the items pane: its backdrop behind, the poster, and
the provider's metadata — director, release date, genre, cast, rating and plot —
then a row of season buttons and a wrapping grid of episode cards. The category
sidebar stays where it is, and **playing an episode keeps you on the page** —
the video docks to the right of it instead of replacing it, so picking the next
episode is one click, not a trip back through the grid of every show you own.

**Continue watching, per show.** The page remembers the last episode you played
and opens on its season with a **Continue S01E04 · 12 min in** button that
resumes where you stopped. Finish an episode and the button moves on to the next
one, across season boundaries. Part-watched episodes carry a progress bar on
their card.

**One episode leads to the next.** When an episode ends, a card appears in the
video area with the next one's still and a ten-second countdown, then plays it.
`Space`, the play button or a click on the card starts it straight away;
**Cancel** stops the clock and leaves the offer standing. `Settings ▸ Autoplay
next episode` turns the countdown off — the card still appears, it just waits.
At the end of the show the card says so and offers the way back instead.

The card only appears because the episode has *ended*: the stream is stopped
first, which frees the single connection the account allows and leaves the video
area free to draw in. Qt cannot paint over libVLC's native view, which is why
nothing tries to overlay a running picture.

⏮ and ⏭ follow what is playing rather than what is on screen: while an episode
is playing they step through the show, across season boundaries, even after you
have walked back to the catalog or opened a different show. Unlike the up-next
card they wrap round at the end, because pressing a button is deliberate in a
way a countdown is not.

Only what the provider sends can be shown, and providers are inconsistent, so
each field degrades quietly: the backdrop falls back to the cover, an episode
with no still of its own borrows the series cover, and empty fields are simply
blank. Metadata arrives with the episode list, on demand — a catalog of ten
thousand shows is far too many to prefetch — and a show opened before this
existed fills in its metadata the next time you open it.

Because the account allows a single connection, refreshing a series while
something is playing is refused by the provider. When that happens the page
shows the episodes already saved rather than an error.

### Playback
libVLC renders directly into a native widget. Live TV, movies and series
episodes all play in-window, with resume-from-position.

The control bar is VLC's, in this app's colours — three rows, the same controls
in the same order:

```
 00:42:17  ═══════════●───────────────────────────  −01:05:33
 ● 📷 ▣ A↔B                        1.00x  Auto  AUDIO  SUBS
 ▶ ⏮ ⏭ ■ │ ⛶ ⧉ ⚙ ☰ │ ⟲ ⇄                      🔊 ▬▬▬●──
```

Play/pause, previous/next through the current list, stop, click-anywhere seek
with a hover preview of the target time, elapsed and remaining (click to switch
to total), fullscreen, Picture-in-Picture, **Adjustments and Effects**,
show/hide the browser, three-state loop, random, mute and volume. The middle row is VLC's *advanced
controls* — record, snapshot, frame-step, A↔B loop, playback speed, aspect
ratio/crop/deinterlace, and the audio and subtitle track menus — and hides from
**View ▸ Advanced controls**, as in VLC.

Controls a live stream cannot honour (seek, frame-step, A↔B, speed) grey out on
live channels.

### Keyboard

| Key | Action |
|---|---|
| `Space` | Play / pause · plays the next episode when the up-next card is up |
| `←` `→` | Seek −10 s / +10 s · move along a row on the homepage |
| `↑` `↓` | Volume −5 / +5 · move between rows on the homepage |
| `Enter` | Play what the homepage cursor is on |
| `M` | Mute |
| `[` `]` | Speed down / up · `=` normal speed |
| `Ctrl+F` | Search everything · `Esc` leaves it |
| `F` | Fullscreen · `Esc` leaves it |
| `P` | Picture-in-Picture · `Esc` leaves it |
| `Ctrl+L` | Show or hide the browser |
| `Ctrl+Shift+V` | Show or hide the video pane |

`Space`, `M` and the speed keys work wherever you are in the app — they are
filtered before the channel list can swallow them, which is what makes them
reliable.

**The arrow keys are shared**, because they are also how you move through the
channel list. Whoever has focus gets them:

- **The video has focus** — arrows seek and change volume. Playing something
  hands focus to the video, so they work straight after you press play, and
  clicking the video gets them back.
- **The list has focus** — arrows move through channels, as before. Click the
  list to browse. Closing a page (search, a series) with no video
  pane on screen hands the keyboard back to the list rather than to the hidden
  video, which would leave the arrows doing nothing at all. The same applies
  when the video pane folds away by itself once playback stops: the keyboard
  goes back to whatever is actually on screen — the list, or the homepage wall.
- **The homepage has focus** — arrows move the poster cursor, and `Enter` plays.
  `Home`, `End`, `PgUp` and `PgDn` work there too.
- **Fullscreen or Picture-in-Picture** — always the player, since there is no
  list to compete.
- **Typing in a search box** — always the box. `Space` inserts a space and the
  arrows move the caret; the player does not interfere.

The status bar says which happened (`Seek +10s`, `Volume 80%`), so there is no
guessing about where focus is. In fullscreen the status bar is hidden, so a
keypress pops the floating control bar up instead — it shows the new position
and volume, then fades out again.

**Adjustments and Effects** (the ⚙ button) is VLC's panel: a 10-band equaliser
with all 18 of VLC's presets and a preamp, and video adjustment for contrast,
brightness, saturation, gamma and hue. Settings persist and are re-applied to
each new stream — libVLC drops them whenever the engine is rebuilt.

In fullscreen the bar floats over the video as its own window, VLC-style, and
fades out after a few idle seconds. If a window manager will not keep it above
the video, set `fullscreen_floating_bar` to `0` in settings and it stays docked.

### Picture-in-Picture

The ⧉ button (or `P`) shrinks the window to a small player that stays above
other applications, so a match keeps running while you work. Move and resize it
by its frame; the size is remembered. The controls appear while the pointer is
over it and get out of the way when it is not — the pointer moving elsewhere on
the screen deliberately does *not* raise them. `P`, `Esc`, double-clicking the
video or the button again gives the full window back, restoring the panes,
splitter sizes and geometry exactly as they were.

The video surface is never reparented for this — the window itself shrinks and
its chrome is hidden, which is fullscreen's trick in reverse. libVLC is bound to
the surface's native handle and moving it mid-playback would lose the drawable;
this way playback simply continues, with no restart or black frame.

The icons are drawn rather than typed: several transport glyphs live in
Unicode's emoji block, so macOS and Windows render them in colour at their own
size and ignore the stylesheet entirely.

### Downloads
Movies, single episodes, or a whole season. Transfers are **resumable** — quit
mid-download and it continues from the byte offset on next launch. Completed
files play from local disk and appear under **DOWNLOADS** in the sidebar. Live
channels support timed **recording** instead, since a live stream has no end.

### Subtitles — VLSub, and no API key

The **Subtitles…** button opens VLSub's window, rebuilt natively: title, season
and episode, two languages, and VLSub's own two buttons.

**Search by hash** fingerprints the file — size plus its first and last 64 KiB —
and asks for subtitles matching that exact copy, so the timing is right without
guessing. **Search by name** is the fallback.

There is **no API key and no account**. This uses the same keyless XML-RPC
endpoint the real VLSub logs into anonymously, which is exactly what VLSub does
on Windows. A free [opensubtitles.org](https://www.opensubtitles.org/en/newuser)
login can be added under *Config* — it only raises the daily download cap, which
is counted per internet connection — and it is stored in the OS keychain, never
in the database.

Downloads are gunzipped and transcoded to UTF-8 using the charset the uploader
declared, so Bengali and Arabic subtitles render instead of turning to mojibake.

> **Which button to use:** hash matching only works when your exact copy is in
> OpenSubtitles' index. That is usually true for **downloaded** files — a
> download of *A Fistful of Dollars* returned three exact matches — and usually
> false for a **live stream**, because providers serve their own re-encodes.
> Search by name covers those.

Also available: **Load local file…** (`.srt`, `.ass`, `.sub`, `.vtt`) with a
delay control, the SUBS menu on the control bar for tracks the stream already
carries, and **Open in VLC**, which hands the stream to the full VLC
application.

> **On the REST API:** OpenSubtitles' newer REST API, which does require a key,
> is kept as an alternative under *Config* in case the legacy endpoint is ever
> retired. Nobody needs to touch it.

### Daily updates
The catalog refreshes automatically once a day — on launch if it is stale, and
on a timer at a configurable hour (default 04:00). It runs in a background
thread and writes to staging tables, swapping atomically, so **an interrupted
refresh never damages the catalog**.

Items the provider drops are marked *unavailable* rather than deleted, so a
favourite does not vanish because the provider reshuffled its catalog — it
greys out, and lights back up if the item returns.

---

## A note on your connection limit

Xtream accounts have a `max_connections` value. If yours is **1**:

- Downloads **pause automatically while something is playing** and resume when
  playback stops (*Settings ▸ Allow downloads while playing* overrides this).
- Only one transfer runs at a time.
- Channel changes are debounced (~300 ms) so holding an arrow key cannot open a
  socket per keypress.
- Catalog updates are unaffected — API calls do not consume a streaming slot
  (verified against a live portal).

Completed downloads play from disk and use **no** connection, so you can watch
one offline while a live stream occupies the slot.

---

## Why the JSON API rather than the M3U

For Xtream providers the app uses `player_api.php`, not `get.php`. Measured
against a live portal:

| | Xtream JSON API | M3U |
|---|---|---|
| Size / time | ~40 MB · ~9 s | ~160 MB · 42 s |
| Entries | tens of thousands | six times as many |
| Series | shows → seasons → episodes | flat episode rows |
| Metadata | TMDB id, rating, poster, duration | name + logo + group only |

The M3U has no concept of a series — it flattens every episode to top level, so
a single "Netflix Series" category becomes tens of thousands of rows instead of
the shows you drill into. M3U parsing is kept for providers that offer no API,
and is streamed line-by-line so a 160 MB file is never held in memory.

---

## Layout

```
main.py                 entry point
core/
  playlists.py          sources, keychain, probing
  api.py                Xtream client, stream URLs
  m3u.py                streaming M3U parser
  db.py                 SQLite schema
  sync.py               catalog refresh (staging + atomic swap)
  classify.py           category -> (group, subcategory) rules
  downloads.py          resumable single-slot queue
  subtitles.py          hashing, shared result type, REST fallback
  vlsub.py              keyless OpenSubtitles (XML-RPC), VLSub's protocol
  vlc_setup.py          libVLC discovery and loading
ui/
  main_window.py        pane layout, tabs, the video pane's reveal
  category_tree.py      grouped sidebar, homepage pins
  models.py             virtualized model, poster cache, delegates
  player_widget.py      libVLC surface, operations, the up-next card
  home_page.py          the homepage wall, rails across every kind
  search_page.py        master search across every kind
  series_page.py        series view, resume, wrapping layout
  transport_bar.py      VLC's control bar
  effects_dialog.py     equalizer and video adjustments
  icons.py              painted transport icons
  playlist_dialog.py    playlist manager and wizard
  subtitle_dialog.py    the VLSub window
  downloads_panel.py    transfer list
  theme.qss             dark theme
tests/test_core.py      offline unit tests
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Covers the classifier rules, URL parsing, M3U parsing, download path layout,
and playlist isolation. They run offline and need no credentials.

## Performance notes

Measured against a live portal, on a catalog of roughly ninety thousand items:

| | Before | After |
|---|---|---|
| Click channel → picture | ~2.0 s | **~1.0 s** |
| `play_item()` blocks the UI for | 579 ms | **6–40 ms** |
| First click on a big category | 1414 ms | **62 ms** |
| Tab switch | 835 ms | **195–443 ms** |
| Catalog update | 15–546 s | **9.9 s** |
| Cold import | ~600 ms | **160 ms** |

What made the difference:

- **The EPG fetch used to block playback.** `player.play()` only arms a timer,
  so a synchronous EPG request on the GUI thread delayed the stream by its own
  duration. It now runs on a worker and the guide fills in behind the video.
- **The portal's 302 redirect is resolved ahead of the click.** Selecting an
  item resolves it in the background, so libVLC is handed the CDN URL directly:
  1.68 s → 0.96 s to first frame, measured A/B. Falls back to the original URL
  on any error, and verified not to consume a connection.
- **SQLite was running with a 2 MB page cache.** It is now 64 MB with mmap.
- **Duplicate ranking is precomputed at sync time** instead of a `ROW_NUMBER()`
  window over 55k rows on every visit.
- **A staging-table index in sync.** The "retire vanished items" step ran a
  correlated scan of ~93k staged rows for each of ~93k existing rows; indexing
  the staging table took that step from 256 s to 0.04 s. This only appeared once
  the catalog was populated, which is why a first-run sync always looked fast.

Lowering VLC's `--network-caching` was tested (1500/800/400) and made no
difference — the latency is the network handshake, not the buffer — so it is
left alone.

## Troubleshooting

**"VLC was not found"** — install VLC, or point the app at a custom location:
`IPTVPLAYER_VLC_DIR=/path/to/vlc-dir`.

**Video stutters or freezes** — turn off *Settings ▸ Hardware decoding*.
Hardware decode paths vary by GPU and driver and are the usual cause.

**Playback fails while a download runs** — your account allows one connection;
pause the download or leave the default gating on.

**"Search by hash" finds nothing** — expected on a streamed title. Providers
re-encode, so the file's fingerprint is not in OpenSubtitles' index. Use
**Search by name**; hash matching pays off on downloaded files.

**"The daily download limit is used up"** — OpenSubtitles counts anonymous
downloads per internet connection. Add a free opensubtitles.org login under
*Config* to raise it, or wait until tomorrow.

**The fullscreen controls sit behind the video** — some Linux window managers
will not honour a stay-on-top overlay. Set `fullscreen_floating_bar` to `0` and
the bar stays docked beneath the video instead.

## Licence note

Video playback is powered by VLC/libVLC from the VideoLAN project, which the
user installs separately; no VLC binaries are redistributed here.
