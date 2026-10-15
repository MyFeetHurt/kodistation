# KODIstation

English | [日本語](README.ja.md)

![KODIstation demo: a series in TV shows, the full EPG details, playback, live TV and recording a program from a channel's schedule in Kodi](media/demo.gif)

Full video (1:46: first start, TV shows, details, genres, search, live TV, record from the guide, transcoded playback): [kodistation-demo.mp4](https://cdn.jsdelivr.net/gh/MyFeetHurt/kodistation@main/media/kodistation-demo.mp4) · Screenshots: [recordings](media/screenshot-recordings.png) · [details](media/screenshot-details.png) · [guide](media/screenshot-guide.png) · [live TV](media/screenshot-live.png)

KODIstation is a Kodi video add-on (`plugin.video.epgstation`) for [EPGStation](https://github.com/l3tnun/EPGStation)
2.x. It browses and plays your EPGStation recordings with all the EPG information the server has, watches live TV,
shows the program guide, and manages reservations.

## Features

- **Recordings**: recent, recording now, by channel, by genre, by rule, search; real file length, partial recordings
  flagged, choose which file to play (TS or encoded), full EPG text while playing.
- **Movies and TV shows**: recordings grouped into shows and episodes from the EPG text, newest first, plus browsing
  by genre. Genre names in Japanese or English.
- **Live TV**: terrestrial, BS and CS channels with what is on now and next.
- **Program guide**: per channel and day, with a genre filter and an on-air marker. Record a program from the guide
  with a chosen encode setting.
- **Scheduled recordings**: upcoming, conflicts, duplicates and skipped programs; cancel, skip or un-skip a
  reservation, stop a running recording.
- **Recordings management**: protect, unprotect and delete recordings (or a single file of one).
- **Artwork**: recording thumbnails as posters and fanart, with Kodi icons as a fallback.
- English and Japanese user interface.

> [!WARNING]
> **KODIstation writes to the EPGStation server it is configured for.** From Kodi you can create and cancel
> reservations, skip airings, stop recordings in progress, and protect or **delete recordings and their files**.
> Deleting asks for confirmation, but it cannot be undone. Point it only at a server where that is what you want.

## Install

Kodi installs KODIstation from the KODIstation Repository and then updates it by itself on every release (Kodi checks
repositories about once a day).

1. *Settings → System → Add-ons*: turn on *Unknown sources*.
2. *Settings → File manager → Add source*: `https://myfeethurt.github.io/kodistation/`, name it e.g. `KODIstation`.
3. *Add-ons → Install from zip file → KODIstation → repository.kodistation-x.y.z.zip*.
4. *Add-ons → Install from repository → KODIstation Repository → Video add-ons → KODIstation → Install*.

A box that already has KODIstation from a zip file: open KODIstation in the repository (step 4) and press *Install*
(or *Update*) once. Kodi asks to replace the "Manually installed" copy: *Yes*; then "remove related data?": **No**,
which keeps your settings. From then on the repository is its origin and it updates automatically.

## Requirements and settings

- Kodi 20 (Nexus) or newer, any platform.
- EPGStation 2.x (tested with 2.10.0), reachable from the Kodi box over HTTP.

Settings (*Add-ons → My add-ons → Video add-ons → KODIstation → Configure*):

- **Server**: EPGStation URL, e.g. `http://epgstation.local:8888`; optional Basic authentication user name and
  password (when EPGStation sits behind a reverse proxy that asks for one).
- **Display**: page size, genre language (Japanese or English), real file length on lists, which video file to
  play by default.
- **Live TV**: live stream mode (unconverted, or one of the server's transcoded live modes); show terrestrial
  sub-channels (off: only each broadcaster's main channel).
- **Recording**: encode after recording, for recordings started from the guide.

## Not affiliated

KODIstation is an independent project. It is not affiliated with or endorsed by EPGStation or its author (l3tnun),
nor by Kodi or Team Kodi / the XBMC Foundation. Kodi® is a registered trademark of the XBMC Foundation.

## Development

```
uv sync --frozen
make ci          # lint, tests (a fake EPGStation serves captured free-channel fixtures), kodi-addon-checker, public audit
make fake        # serve the fixtures as a fake EPGStation on :8889
make zip         # reproducible zips of the add-on and the repository add-on in dist/
```

Machine-specific settings (your Kodi box, your EPGStation) go into an untracked file; see
[local.env.example](local.env.example). Test fixtures come from free-to-air channels only.

## License

MIT ([LICENSE](LICENSE)). Changes: [changelog.txt](plugin.video.epgstation/changelog.txt).
