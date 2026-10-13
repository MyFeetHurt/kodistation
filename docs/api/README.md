# EPGStation API notes

`openapi.json` is the API contract captured from EPGStation 2.10.0 (`GET /api/docs`). Where the real
2.10.0 server answers differently, the addon accepts both shapes:

| What | openapi.json | Real 2.10.0 (`GET /api/config`, prod and dev) | Used by |
|---|---|---|---|
| Live streaming switch | `isEnableLiveStream` | `isEnableTSLiveStream` | `live.live_enabled` |
| Live m2ts modes | `streamConfig.live.m2ts` | `streamConfig.live.ts.m2ts` (plus `m2tsll`, `webm`, `mp4`, `hls`) | `live.m2ts_modes` |

The m2ts modes are `[{name, isUnconverted}]`; the `mode` query parameter of
`/streams/live/{channelId}/m2ts` is the index in that list.
