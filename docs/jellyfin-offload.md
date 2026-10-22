# Jellyfin transcode offload to omega

Jellyfin runs on alpha (a VM with no GPU) and transcodes in software. omega's
RTX 3080 is idle most of the time but is routinely offline (the owner boots
Windows). This offloads GPU transcodes to omega when it is up and falls back to
CPU on alpha when it is not, without Jellyfin noticing.

Jellyfin must never run on omega (see `docs/gaming-gpu-talos.md`), so only
ffmpeg moves.

## How it works

```
Jellyfin (alpha)                          omega
  JELLYFIN_FFMPEG=/offload/ffmpeg
  ffmpeg-wrapper.pl ──── TCP :9876 ────►  ffmpeg-runner.pl ──► jellyfin-ffmpeg + NVENC
        │  (token)                              │
        └─ falls back to local CPU              └─ writes HLS to the shared volume
           ffmpeg via ffmpeg-sw-fallback.pl
```

Jellyfin is configured for **NVENC** and does not care that alpha has no GPU:
it builds `-hwaccel cuda … h264_nvenc` command lines either way. Every ffmpeg
call goes through the wrapper, which decides per call:

| Command | What happens |
|---|---|
| No GPU constructs (version/capability probes, trickplay, subtitle extraction) | Run the real ffmpeg locally, unchanged |
| GPU constructs, runner reachable | Run on omega with the original arguments |
| GPU constructs, runner unreachable / busy / token rejected | Rewrite for CPU and run locally |
| Runner fails *before* producing any output (NVENC session limit, out of VRAM) | Rewrite for CPU and run locally |
| Runner fails *after* producing output, or omega drops mid-stream | The stream ends; the client's retry takes the local path |

The last row is inherent: a transcode already running on omega when it
reboots into Windows cannot be moved.

Decisions are logged on stderr with an `[offload]` prefix, so they appear in
Jellyfin's per-transcode ffmpeg log (`/config/log/FFmpeg.Transcode-*.log`).

## Pieces (`k8s/apps/media/`)

- `jellyfin-offload/ffmpeg-wrapper.pl` – installed as `ffmpeg` in the
  `jellyfin-offload-scripts` ConfigMap, next to an `ffprobe` shim (Jellyfin
  finds ffprobe beside the configured ffmpeg).
- `jellyfin-offload/ffmpeg-runner.pl` – the server, run by the
  `jellyfin-ffmpeg-runner` HelmRelease pinned to omega like tdarr-node.
- `jellyfin-offload/ffmpeg-sw-fallback.pl` – rewrites NVENC/CUDA arguments for
  CPU (`h264_nvenc`→`libx264`, `scale_cuda`→`scale`, `tonemap_cuda`→`tonemapx`,
  `overlay_cuda`→`overlay`, drops the `-hwaccel`/`hwupload` plumbing). Anything
  GPU-flavoured it does not recognise exits 97 instead of being guessed at.
- `jellyfin-scratch-nfs.yaml` – a static RWX NFS volume (PV+PVC, `lookupcache=positive`) mounted at the same paths in both
  pods: `/config/cache/transcodes`, `/config/data/subtitles`,
  `/config/data/attachments`. Jellyfin writes absolute `/config/...` paths into
  ffmpeg command lines, so both sides must see them at identical paths.
- Secret `jellyfin-ffmpeg-runner` (key `token`), from `jellyfin_ffmpeg_runner_token`
  in `inventory/group_vars/all.sops.yaml`. Flannel enforces no NetworkPolicy, so
  the token is what stops other pods running ffmpeg on the GPU.

The stock image has perl, bash and curl but no python, ssh or nc, which is why
this is a small custom protocol instead of rffmpeg (needs python + ssh and
therefore a custom Jellyfin image).

## Protocol

Client → runner: `FFRUN1 <token>\n<argc>\n` then each argument as
`<len>\n<bytes>\n`, then raw bytes forwarded to ffmpeg's stdin (Jellyfin stops a
transcode by writing `q`). Runner → client: `OK\n` or `ERR <reason>\n`, then
frames `<type:1><len:4 BE><payload>`: `E` stderr, `H` heartbeat (every 2 s),
`X` exit code. The wrapper treats 10 s of silence as a dead runner; the runner
kills ffmpeg when the client disconnects (TCP keepalive covers a vanished peer).

## Measured behaviour

Measured on the spike (Jellyfin 12.1.0, jellyfin-ffmpeg 8.1.2), 4K HEVC remux →
1080p, CPU fallback on alpha capped at 4 cores:

| Case | fps | speed |
|---|---|---|
| HDR, tone-mapped | 20 | 0.83x |
| HDR + external SRT burned in | 16 | 0.67x |
| HDR + PGS burned in | 21 | 0.88x |
| SDR scale | 22 | 0.92x |

So the **fallback does not keep up with 4K at 4 cores**. It is no worse than
today (Jellyfin already software-transcodes on alpha), and decode dominates
(tone-mapping costs about 10%). Offload only helps while omega is up.

## Operating notes

- **GPU slot.** The runner holds one `nvidia.com/gpu` time-slice. With
  subgen, ollama, tdarr-node and audiomuse-worker that made five residents,
  which used up the spare slot (#673) that ad-hoc jobs such as `shrink-remux`
  rely on; `replicas` in
  `k8s/infrastructure/nvidia-gpu-operator/helmrelease.yaml` is now 6 to keep one
  free. Add a 7th, not the spare, for the next resident.
- **Script changes need a rollout.** The ConfigMap has no name hash, so restart
  the runner and Jellyfin after changing a script.
- **Upgrades.** After a Jellyfin bump, check the generated command lines still
  rewrite cleanly: run `test/test-sw-fallback.sh` with `FFMPEG`, `MEDIA_FILE`
  and `SUB_FILE` set (see its header), and `test/test-offload.sh`.
- **Kill switch.** Set `OFFLOAD_DISABLED=1` on the Jellyfin container to force
  CPU without removing the wrapper.
- **Trickplay.** Hardware acceleration is off for trickplay, so it never carries
  GPU arguments and always runs locally.

## Rollout order

1. Token + runner + shared volume + wired spike (no change to live Jellyfin).
2. Test end to end on the spike: omega up, omega scaled to 0, omega killed
   mid-stream, playback stopped by the client.
3. Wire the live Jellyfin (`JELLYFIN_FFMPEG`, mounts, token, NVENC in the
   encoding settings). This restarts Jellyfin, so pick a window with no active
   streams. Copy the existing `/config/data/subtitles` and `attachments`
   contents into the shared volume first, or the first plays of each file
   re-extract subtitles (a full read of the file over NFS).
4. Remove `jellyfin-spike.yaml`.
