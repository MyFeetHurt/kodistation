# EPGStation API notes

`openapi.json` is the API contract captured from EPGStation 2.10.0 (`GET /api/docs`). Where the real
2.10.0 server answers differently, the addon accepts both shapes:

| What | openapi.json | Real 2.10.0 (`GET /api/config`, prod and dev) | Used by |
|---|---|---|---|
| Live streaming switch | `isEnableLiveStream` | `isEnableTSLiveStream` | `live.live_enabled` |
| Live m2ts modes | `streamConfig.live.m2ts` | `streamConfig.live.ts.m2ts` (plus `m2tsll`, `webm`, `mp4`, `hls`) | `live.m2ts_modes` |

The m2ts modes are `[{name, isUnconverted}]`; the `mode` query parameter of
`/streams/live/{channelId}/m2ts` is the index in that list.

## Recorded HLS (transcoded playback, backlog 011)

Checked against the 2.10.0 source and measured on the dev server (details: `docs/backlog/011-hls-transcoded-stream.md`).

- Modes: `Config.streamConfig.recorded.{ts|encoded}.hls` (names; `mode` is the index), gated by
  `isEnableTSRecordedStream` / `isEnableEncodedRecordedStream`. This part of `/config` matches the spec. Used by
  `transcode.hls_modes` / `transcode.recorded_enabled`.
- Start: `GET /streams/recorded/{videoFileId}/hls?ss=<sec>&mode=<idx>` starts ffmpeg and answers
  `{"streamId": n}`. Errors are HTTP 500 with `errors`: `OutOfRange`, `ConfigIsUndefined`, `VideoFileIsNotFound`,
  `VideoIsNull`, `CreateStreamProcessError` / `EncodeProcessManageModelCreateError` (no free encoder slot).
- Playlist: not under `/api`. EPGStation serves `data/streamfiles` statically: `/streamfiles/stream<n>.m3u8`,
  as `Content-Type: video/MP2T` (segments `video/mpeg`), so Kodi needs the mimetype set and no content lookup. The
  static server ignores query strings; stream ids are the lowest free integer and are **reused**, so the addon adds
  its own `?k=<nonce>`.
- Ready: `GET /streams?isHalfWidth=true` (`isHalfWidth` required) shows `isEnable: true` once the playlist and two
  segments exist (~4-6 s for a TS at 480p on the dev VM). The items carry **`viodeFileId`** (typo in 2.10.0; the
  spec says `videoFileId`): match by `streamId` only.
- The playlist has no `#EXT-X-PLAYLIST-TYPE` and no `#EXT-X-ENDLIST` while encoding (a live playlist). The HLS
  timeline starts at 0 at `ss`: absolute position = ss + player time.
- Keep / stop: the server stops a stream 15 s after its start or its last `PUT /streams/{id}/keep` (200
  `{"code":200}`; a gone id: 500 `StreamIsUndefined`). `DELETE /streams/{id}` stops it at once and deletes its files
  (200, also for an unknown id). Segments of the rest of the recording pile up in `data/streamfiles` until then.
- 2.10.0 bug (backlog 011 F1): when the 15 s timer stops a stream, ffmpeg and the files go, but a double unlink
  (ENOENT, `unhandledRejection` in system.log) leaves the stream listed in `GET /streams` (isEnable true) until someone
  DELETEs it; its playlist then answers 404 (HEAD on `/streamfiles/...` is a static file, no side effect). The addon
  records its streams (`addon_data/.../stream_active/`, one file each) and at its next start DELETEs only such dead
  listings: listed, enabled, same file and mode, playlist 404. A live stream always has its playlist.
