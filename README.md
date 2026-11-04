# Game Downloads

A live download indicator for the [Omarchy](https://omarchy.org/) shell bar.
While a game is downloading, a green arrow with the percentage sits in your
bar. Click it for per-game detail — source, bytes downloaded, current
throughput, and time remaining — plus a button to bring the relevant launcher
forward.

The pill **disappears entirely** when nothing is downloading, so it costs
nothing when you are not downloading.

Formerly Steam Download. The plugin id stays `davidjm.steam-download` so
existing bar entries and settings keep working; the display name is now Game
Downloads.

## What it watches

| Launcher | Local source | Notes |
| --- | --- | --- |
| Steam | `steamapps/appmanifest_<appid>.acf`, `steamapps/downloading/<appid>/` | Reads every library from `libraryfolders.vdf`. |
| Heroic | `store/download-manager.json`, `Heroic/logs/heroic.log` | Covers Epic (legendary), GOG, Amazon (nile), Zoom and sideloaded games. |
| Epic Games Launcher (native EGLv2) | `com.epicgames.eglv2/queue.json`, `<install>/.egstore/chunk_cache` | Preview/native Linux launcher; uses the operation journal. |

All three are read-only. The helper never edits a launcher's state and never
calls a privileged API.

## Where the numbers come from

### Steam

Steam exposes no supported download-progress API on Linux, so this reads the
files the client already maintains:

| Source | Used for |
| --- | --- |
| `steamapps/libraryfolders.vdf` | every installed library, not just the default one |
| `steamapps/appmanifest_<appid>.acf` | game name, and the total to download |
| `steamapps/downloading/<appid>/` | how many bytes have actually landed |

The obvious field to read, `BytesDownloaded` in the appmanifest, is **not**
used: Steam only rewrites it when a download starts or stops, so it reads
stale for the entire download. The staging tree, by contrast, grows as bytes
arrive, which is what the widget measures.

#### Matching Steam's percentage

Steam runs two phases **concurrently** and shows each as its own bar:

```
Downloading data    56.5 GB / 59.7 GB   <- compressed, over the wire
Installing files               94%       <- uncompressed, on disk
```

The manifest records both sizes, and they are easy to mistake for two halves
of one total. They are not. `BytesToDownload` is the compressed transfer size
and `BytesToStage` is the uncompressed size — **the same bytes at two
different compressions.** Adding them gives a denominator roughly twice the
real one, which reports a download as roughly half finished when Steam says
it is nearly done. So this widget divides the staging tree by `BytesToStage`
alone, reproducing Steam's "Installing files" bar.

Two details make the size measurement reliable:

- **Allocated blocks, not apparent size.** Steam preallocates staging space, so
  apparent size runs ahead of what is really on disk. The walk sums
  `st_blocks`, falling back to apparent size only for sparse files that have
  not been written yet.
- **File mtimes, not directory mtimes.** The directory's own mtime only moves
  when entries are added or removed, and Steam streams most bytes into files
  that already exist. Staleness is judged from the newest file inside the tree.

### Heroic

Heroic keeps its queue in `~/.config/heroic/store/download-manager.json`.
While a download runs it writes progress lines to
`~/.local/state/Heroic/logs/heroic.log`:

```
(12:34:56) [INFO]: [Legendary]: Progress for Game: 42.3%/1.2GB/00:05:00 Down: 12.3MB/s / Disk: 45.6MB/s
```

The widget takes the latest line for the active title, so percentage,
throughput and ETA come from Heroic itself rather than a guessed filesystem
rate. If logging is disabled or a line has not appeared yet, the row still
shows the queue state and an indeterminate progress bar.

### Epic Games Launcher (native Linux)

The native launcher’s EGLv2 backend records its operation journal in
`~/.local/share/com.epicgames.eglv2/queue.json`. Progress is measured from
the operation’s `.egstore/chunk_cache` against the action sizes stored in the
journal. The launcher does not expose a stable public progress API, so this is
deliberately best-effort:

- Active, queued, paused, verifying and repairing states are recognised.
- If the launcher is closed, queued operations are shown as queued rather than
  pretending they are still running.
- If the journal has no action sizes yet, the row is indeterminate until the
  first bytes land.
- A title is resolved from the official launcher’s catalog cache, Heroic’s
  Epic library cache, the install folder name, then the AppName. The operation
  journal itself does not store the display name.

## Notifications and idle

Each source is compared separately between polls. When a source’s last active
row disappears, the widget reports one notification for that source:

- **finished** when the measured bytes are effectively complete;
- **stopped** when the source is still running but the row vanished;
- **interrupted** when the launcher itself closed.

Clicking the notification opens Steam’s downloads page, Heroic, or the Epic
Games Launcher as appropriate. Heroic already sends its own completion
notifications, so disable this plugin’s notifications if you prefer its
native ones.

While any monitored download is active, the plugin asks the Media Idle
Inhibit service for a single `game-downloads` lease and renews it every five
minutes. The lease is released as soon as the last download stops.

## Install

```bash
omarchy plugin add https://github.com/davidmessenger123/omarchy-steam-download --enable --yes
```

Or install by hand:

```bash
mkdir -p ~/.config/omarchy/plugins/davidjm.steam-download
cp manifest.json BarWidget.qml Model.js game-downloads.py ~/.config/omarchy/plugins/davidjm.steam-download/
omarchy-shell shell rescanPlugins
omarchy plugin enable davidjm.steam-download
```

Bar plugins land in the section from their manifest (`right`) and can be moved
with `omarchy bar move`.

Requires Python 3. No other dependencies — the helper uses only the standard
library, so there is no `curl`/`jq`/`du` shell-out and nothing to install.

## Remove

```bash
omarchy plugin remove davidjm.steam-download
```

## Configuration

Settings live on the bar layout entry in `~/.config/omarchy/shell.json`:

```jsonc
{
  "id": "davidjm.steam-download",
  "interval": 3,          // seconds between measurements (1-60, default 3)
  "showQueued": true,     // list downloads a launcher has queued
  "notifyDownloads": true,
  "inhibitIdle": true
}
```

`interval` is the knob that matters. Steam and Epic throughput is derived from
growth between two polls, so a shorter interval makes speed and ETA steadier
at the cost of more disk reads. Each Steam scan is bounded to 1.5 seconds and
skips directories it runs out of time on, so a large library cannot stall the
bar.

`showQueued` only affects the popup. Queued downloads are never counted as
active and never put a pill in the bar.

## Interaction

| Action | Result |
| --- | --- |
| Left-click the pill | Open or close the download list |
| Right-click the pill | Re-measure immediately |
| Middle-click the pill | Focus or launch the active download’s launcher |
| `omarchy-shell davidjm.steam-download refresh` | Re-measure from a keybind |

## Development

```bash
python3 -m py_compile game-downloads.py   # helper syntax
python3 -m unittest discover -s tests      # helper tests
node --check Model.js                      # model syntax
node tests/test_model.js                   # model tests
omarchy plugin validate .                  # manifest schema
```

The Steam tests pin the two-phase accounting rule specifically, because getting
it wrong reports a download as roughly half finished when Steam says it is
nearly done — a bug that is invisible until you compare the two side by side.
The Heroic and Epic tests use their real local formats, not invented ones.

## Layout

| File | Role |
| --- | --- |
| `manifest.json` | plugin id, entry point, and the bar layout settings |
| `BarWidget.qml` | the bar pill and the popup |
| `Model.js` | pure parse/format/derive helpers, no QML dependencies |
| `game-downloads.py` | reads all three launchers and reports normalised rows |
| `tests/` | `test_model.js` for the model, `test_game_downloads.py` for the helper |

## Refresh

Saving any file in this folder reloads the plugin. To force it:

```bash
omarchy-shell shell rescanPlugins
```
