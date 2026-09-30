# Steam Download

A live Steam download indicator for the [Omarchy](https://omarchy.org/) shell
bar. While something is downloading, a green arrow with the percentage sits in
your bar. Click it for per-game detail — bytes downloaded, current throughput,
and time remaining — plus a button to bring the Steam window forward.

The pill **disappears entirely** when nothing is downloading, so it costs
nothing when you are not using Steam.

## Where the numbers come from

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

### Matching Steam's percentage

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

A useful sanity check: because the phases run together, the staging tree
routinely grows *past* the entire compressed total before the download
finishes. If the on-disk figure exceeds `BytesToDownload`, the compressed
number is not a usable denominator.

Two details make the size measurement reliable:

- **Allocated blocks, not apparent size.** Steam preallocates staging space, so
  apparent size runs ahead of what is really on disk. The walk sums
  `st_blocks`, falling back to apparent size only for sparse files that have
  not been written yet.
- **File mtimes, not directory mtimes.** The directory's own mtime only moves
  when entries are added or removed, and Steam streams most bytes into files
  that already exist. Staleness is judged from the newest file inside the tree.

Because Steam fills the staging area as data lands, the percentage tracks the
"Installing files" bar and moves in step with the download. The byte counts
are uncompressed on-disk sizes, matching what Steam prints beside that bar.

## Install

```bash
omarchy plugin add https://github.com/davidmessenger123/omarchy-steam-download --enable --yes
```

Or install by hand:

```bash
mkdir -p ~/.config/omarchy/plugins/davidjm.steam-download
cp manifest.json BarWidget.qml Model.js steam-downloads.py ~/.config/omarchy/plugins/davidjm.steam-download/
omarchy-shell shell rescanPlugins
omarchy plugin enable davidjm.steam-download
```

Bar plugins land in the section from their manifest (`right`) and can be moved
with `omarchy bar move`.

Requires Steam (the `steam` package) and Python 3. No other dependencies — the
helper uses only the standard library, so there is no `curl`/`jq`/`du` shell-out
and nothing to install.

## Remove

```bash
omarchy plugin remove davidjm.steam-download
```

## Configuration

Settings live on the bar layout entry in `~/.config/omarchy/shell.json`:

```jsonc
{
  "id": "davidjm.steam-download",
  "interval": 3,      // seconds between measurements (1-60, default 3)
  "showQueued": true  // list downloads Steam has queued but not started
}
```

`interval` is the knob that matters. Throughput is derived from the growth of
the staging tree between two polls, so a shorter interval makes the speed and
ETA steadier at the cost of more disk reads. Each measurement is bounded to
1.5 seconds of walking and skips directories it runs out of time on, so a
large library cannot stall the bar.

`showQueued` only affects the popup. Queued downloads are never counted as
active and never put a pill in the bar.

## Interaction

| Action | Result |
| --- | --- |
| Left-click the pill | Open or close the download list |
| Right-click the pill | Re-measure immediately |
| Middle-click the pill | Focus or launch Steam |
| `omarchy-shell davidjm.steam-download refresh` | Re-measure from a keybind |

## Development

```bash
python3 -m py_compile steam-downloads.py   # helper syntax
python3 -m unittest discover -s tests      # helper tests
node --check Model.js                      # model syntax
node tests/test_model.js                   # model tests
omarchy plugin validate .                  # manifest schema
```

The tests pin the two-phase accounting rule specifically, because getting it
wrong reports a download as roughly half finished when Steam says it is nearly
done — a bug that is invisible until you compare the two side by side.

## Layout

| File | Role |
| --- | --- |
| `manifest.json` | plugin id, entry point, and the bar layout settings |
| `BarWidget.qml` | the bar pill and the popup |
| `Model.js` | pure parse/format/derive helpers, no QML dependencies |
| `steam-downloads.py` | reads the Steam library and reports each download |
| `tests/` | `test_model.js` for the model, `test_steam_downloads.py` for the helper |

## Refresh

Saving any file in this folder reloads the plugin. To force it:

```bash
omarchy-shell shell rescanPlugins
```
