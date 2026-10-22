# Tunarr transcode offload to omega (design)

**Status: design only.** Nothing here is deployed. What exists is the pure
argument-rewrite function and its tests
(`k8s/apps/media/tunarr-offload/nvenc-rewrite.pl`, `test/test-rewrite.pl`). Every
claim below is marked *verified* or *to measure*; the second kind needs omega.

Tunarr transcodes in software inside its own pod. This moves that work to
omega's RTX 3080 when omega is up, the way [jellyfin-offload.md](jellyfin-offload.md)
does for Jellyfin, and does nothing different when it is down.

## Why

Observed 2026-10-22, while repairing the channels after a Jellyfin re-ID:

- Tunarr was **OOMKilled** (`kube_pod_container_status_last_terminated_reason`).
  Its last log line before dying was a software transcode of a 4K HEVC HDR file:
  `hardwareAccelerationMode: none`, decode and `scale` and `libx264` all on the
  CPU, in a pod limited to 2Gi. My own API traffic ran at the same time, so the
  transcode is the likely cause, not a proven one.
- Jellyfin's Live TV tuner gives up on a channel after about 15 s
  (`Zero bytes copied from stream SharedHttpStream`, then
  `Unable to find host to play channel`). Tunarr will not answer until it has two
  HLS segments, and the software path did not get there in time on 4K sources.
- Tunarr's `tonemapHdr` is `false`, so HDR sources are squeezed to SDR without
  tone mapping. A GPU path is a place to fix that (separate step, see Risks).
- omega's GPU is idle most of the time.

## What Tunarr actually runs

Captured from Tunarr 1.3.15's logs (`Starting ffmpeg with args`); *verified*.

| Command | Input | Output | Offload? |
|---|---|---|---|
| Transcode a program | `http://jellyfin:8096/Videos/<id>/stream?static=true` (the whole original file over HTTP, with `-ss` to seek) | HLS segments in `/config/tunarr/streams/stream_<channel>/` | **yes** |
| Error / flex screen | `http://localhost:8000/images/...png` (Tunarr itself) | same HLS directory | no (localhost) |
| Concat | `http://localhost:8000/stream/channels/<id>.m3u8` | `pipe:1` (mpegts to the viewer) | no (localhost, stream copy) |
| `-version`, `ffprobe` | n/a | n/a | no |

Details that shape the design:

- Input is paced: `-readrate 1 -readrate_initial_burst 60`. ffmpeg reads at real
  time after a 60 s burst, so **a GPU cannot make Tunarr faster than 1x**. What it
  buys is CPU and memory off the pod, and a fast first two segments (the burst
  is decoded quickly), which is what the 15 s limit needs.
- Decode is forced to CPU: `-c:v hevc` (or `mpeg4`, `h264`, ...) before `-i`.
- Encode is `-c:v libx264 -preset:v veryfast -sc_threshold 1000000000`, 4000k,
  1080p, with `-force_key_frames expr:gte(t,n_forced*4)` for 4 s segments.
- Two shapes: `scale`+`pad` to 1080p (source not 1080p), or no video filter
  (source already 1080p; video goes straight to the encoder).
- Local inputs (`localhost:8000`) do not resolve on omega, so those commands must
  stay in the Tunarr pod.

## Options

**A. Keep Tunarr in CPU mode; the wrapper upgrades to NVENC when omega answers
(recommended).** Tunarr is configured exactly as today. The wrapper rewrites the
one offloadable command to use NVDEC and NVENC and sends it to a runner. If the
runner is down, busy, or fails before output, the wrapper runs the **original
arguments locally, which is today's behaviour**. No CPU-fallback rewriter is
needed, so there is no second translation to get wrong.

**B. Mirror Jellyfin: set Tunarr to `cuda`, and rewrite to CPU when omega is
down.** Tunarr's own cuda pipeline emits different arguments (`hwupload`,
`scale_cuda`, ...) that I have not captured, and the fallback must handle every
shape it can produce. More surface, and the failure path (omega off, the common
case) is the one that runs through the rewriter.

**C. `mpegts` stream mode with output over the socket.** Removes the shared
volume but changes the runner protocol that Jellyfin also uses (a stdout frame).
Revisit only if the shared volume is a problem.

## Design (option A)

```
Tunarr pod (alpha)                                   omega
  ffmpegExecutablePath=/offload/ffmpeg
  ffmpeg-wrapper.pl ── TCP :9876 (FFRUN1) ──►  jellyfin-ffmpeg-runner (existing)
     │ should_offload? rewrite()                    │ jellyfin-ffmpeg + NVDEC/NVENC
     └─ else / on failure: real ffmpeg, original args   └─ writes HLS to the shared volume
```

- **Wrapper** (`tunarr-offload/ffmpeg-wrapper.pl`, not written yet): the Jellyfin
  wrapper with two changes. `should_offload`/`rewrite` come from
  `nvenc-rewrite.pl`, and the fallback runs the original arguments instead of a
  rewritten set. Same protocol, token, silence watchdog and `[offload]` logging.
  *Verified*: the Tunarr image has perl, bash, curl and nc, and ffmpeg 7.1.1.
- **Runner: reuse `jellyfin-ffmpeg-runner`.** *Verified*: the time-slicing config
  advertises 5 `nvidia.com/gpu` replicas and 5 residents already hold them
  (audiomuse, jellyfin runner, ollama, subgen, tdarr-node). A second runner pod
  would not schedule without a sixth replica, which restarts the device plugin
  (the chart has wedged Flux before, see the gpu-operator v26.7.0 note). Sharing
  one runner costs coupling: `RUNNER_MAX` (3) is shared with Jellyfin, and a
  runner restart interrupts both (both fall back).
- **Shared output volume.** Tunarr's ffmpeg writes absolute paths
  (`/config/tunarr/streams/...`) and polls for segments, so the runner must see
  the same directory at the same path. Tunarr has a `transcodeDirectory` setting;
  point it at a new RWX static NFS volume (`tunarr-scratch`, same pattern as
  `jellyfin-scratch-nfs.yaml`) mounted in the Tunarr pod and in the runner. It
  needs `lookupcache=positive` and `actimeo=2`: Jellyfin's offload measured 9.6 s
  to see a new file and about 40 s to the first segment on default NFS options,
  which would blow the 15 s budget by itself. `tunarr-config` is `ReadWriteOnce`
  and holds SQLite, so it is not reused.
- **Tunarr settings** (`ffmpegExecutablePath`, `transcodeDirectory`) live in
  Tunarr's database, not in git. Set them with `tunarr_set_settings` and record
  the values in a runbook; this is a GitOps gap, like Dispatcharr's UI config.

### What gets rewritten (`nvenc-rewrite.pl`, *tested*)

Offloaded only if the input is a non-localhost URL, the output is `-f hls`, and
the encoder is `libx264`. Then:

| Original | Rewritten |
|---|---|
| `-c:v hevc` before `-i` (any of h264, hevc, mpeg1/2/4, vc1, vp8, vp9, av1, mjpeg) | `-hwaccel cuda` (frames return to system memory, so Tunarr's CPU `scale`/`pad` still work) |
| decoder NVDEC cannot do | left alone (encode still moves) |
| `-c:v libx264` | `-c:v h264_nvenc` |
| `-preset:v veryfast` | `-preset:v p3` (x264 to NVENC preset map) |
| `-sc_threshold N` | dropped (x264 only) |
| `-force_key_frames expr:...` | same, plus `-forced-idr 1` so segment starts are IDR frames |

`test/test-rewrite.pl` runs the real captured command lines through it
(`perl test-rewrite.pl`, no GPU) and checks the result is the original with exactly
these substitutions. *Verified* separately: ffmpeg (local 9.0.2) accepts every
emitted option; with a bogus option it fails with `Unrecognized option`, with these
it fails only at `Cannot load libcuda.so.1`. That is not a test on jellyfin-ffmpeg
8.1 or on a GPU.

### Behaviour

| Case | What happens |
|---|---|
| Not offloadable (error screen, concat, probes) | real ffmpeg locally, unchanged |
| Offloadable, runner up | rewritten command on omega |
| Runner unreachable, busy, token rejected, or fails before any output | original command locally (= today) |
| Runner fails after output, or omega drops mid-stream | the stream ends; Tunarr shows its error screen and the next tune restarts it (to confirm) |
| omega off (the normal state when gaming) | identical to today |

Kill switch: `OFFLOAD_DISABLED=1` on the Tunarr container.

## The 15-second budget (*to measure*)

Jellyfin tuner (about 15 s) -> Tunarr waits for playlist plus 2 segments (8 s of
media) -> wrapper connects (2 s timeout) -> runner starts ffmpeg -> ffmpeg opens
the Jellyfin URL and seeks (`-ss` over HTTP into a large remux) -> decodes the
burst -> NFS makes the first segments visible. Rough expectation: NVDEC and NVENC
produce 200 frames in about 1 s, so the dominant costs are the HTTP seek and NFS
visibility. Today the software path misses 15 s on 4K. **The spike has to report
time to two segments**, and a failure-before-output must also fit the budget, or
the fallback arrives too late to help.

## Risks and open questions

1. **NVDEC coverage.** 10-bit H.264 and some old codecs fail on NVDEC. That is
   "fail before output", so it falls back, but it burns time against the 15 s
   budget. *To measure*: how many library files this hits.
2. **10-bit HEVC to 8-bit H.264.** Expected to convert automatically; confirm no
   banding and that the encoder accepts the frames.
3. **HDR.** `tonemapHdr` stays off in this design, so HDR output looks as it does
   today. Fixing it means GPU tone mapping (`tonemap_cuda` exists in
   jellyfin-ffmpeg) plus changing Tunarr's option; a separate step after the base
   offload works.
4. **Capacity.** Five channels can each hold a session; `RUNNER_MAX` is 3 and shared
   with Jellyfin, so a fourth concurrent transcode is declined and runs on CPU.
   Consumer NVENC also has a session cap, and VRAM (10 GB) is shared with ollama
   and subgen.
5. **The Jellyfin token is in the arguments** (`-headers X-Emby-Token`) and
   crosses the cluster network to the runner. The runner logs argument counts, not
   values, and there is no NetworkPolicy; same exposure the Jellyfin offload
   already accepts. Worth stating, not new.
6. **Tunarr upgrades** can change argument shapes. The captured fixtures in the
   test are the regression check; re-capture after a bump (Renovate bumps the
   image).
7. **Coupling to the Jellyfin runner.** A change to the runner affects both apps.
   If that proves painful, a dedicated runner needs the sixth GPU replica.

## Spike (needs a decision before it runs)

0. *Done in this PR:* captured the commands, wrote and tested the rewrite.
1. **Validate on real hardware.** Run the two rewritten transcode commands on omega
   against a real Jellyfin item (one 4K HDR HEVC, one 1080p H.264) into an
   `emptyDir`; record time to two segments, speed, VRAM, and look at the output.
   This needs a GPU time-slice and GitOps (no hand-run `kubectl`), so a one-shot
   Job like `shrink-remux.yaml`. All five slots are taken, so either a sixth
   replica (device-plugin restart) or temporarily scaling a resident down. **Your
   call.**
2. **Wire a spike Tunarr** (like `jellyfin-spike.yaml`) with the wrapper, the shared
   volume and the runner, and test: omega up, omega scaled to 0, omega killed
   mid-stream, viewer stops.
3. **Wire live Tunarr** in a window with no viewers (it restarts Tunarr), set the
   two settings, and keep the kill switch in the notes.
