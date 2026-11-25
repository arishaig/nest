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
| No GPU constructs (version/capability probes, subtitle extraction) | Run the real ffmpeg locally, unchanged |
| Trickplay thumbnail extraction (`-f image2 …/%08d.jpg`) | Omega only, see [Trickplay](#trickplay); never on CPU |
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
- **Encoding settings.** Tracked in git, see the next section.
- **Trickplay.** See the next section.

## Encoding settings

Jellyfin's transcode settings (Dashboard > Playback) live in
`/config/config/encoding.xml`. The offload only works while
`HardwareAccelerationType` is `nvenc`: with `none` Jellyfin emits plain CPU
command lines (`libx265`, no `-hwaccel`), the wrapper sees no GPU constructs and
runs them locally on alpha, and omega sits idle. That is how all playback
transcodes ended up on alpha's CPU, with only trickplay reaching omega.

The file is therefore tracked at `k8s/apps/media/jellyfin-config/encoding.xml`
and published as the `jellyfin-encoding-config` ConfigMap. An `encoding-xml`
initContainer on the Jellyfin pod copies it over the PVC copy on **every start**,
so the settings follow the pod to whichever node it lands on and survive a blank
or restored config volume. The value does not depend on the node: no node Jellyfin
may use has a GPU (it is excluded from omega), and the wrapper already handles
omega being down.

- **Git wins.** Changes made in Dashboard > Playback last until the next pod
  restart. To keep one, edit `encoding.xml` in git.
- **Applying a change.** The ConfigMap has no name hash, so restart Jellyfin after
  editing it (`kubectl -n media rollout restart deploy/jellyfin`).
- **Re-capturing from the UI.** Change the setting in the dashboard, copy the file
  out (`kubectl -n media exec deploy/jellyfin -- cat /config/config/encoding.xml`),
  and diff it against the tracked one before committing. Jellyfin may add keys on
  upgrade; a new key missing from the tracked file just takes its default.

## Trickplay

Trickplay (the scrub-bar thumbnails) runs **only on omega**. Decoding a 2160p
HEVC episode in software takes 1 h or more on alpha, uses ~3.6 cores, and
Jellyfin holds one global trickplay lock while the library scan waits on it
(`Process.WaitForExitAsync` with no timeout). That is what made "Scan Media
Library" look frozen at a fixed percentage for weeks (diagnosed 2026-10-24 from a
live heap dump). The wrapper therefore:

- recognises a trickplay call by shape (last argument `<dir>/%08d.jpg`, `-f image2`),
  independent of Jellyfin's hardware-acceleration setting;
- adds `-hwaccel cuda` before `-i` so omega decodes with NVDEC (ffmpeg falls back
  to software for codecs the GPU cannot decode, still on omega);
- keeps decoded frames **on the GPU until after Jellyfin's `fps` filter**
  (`-hwaccel_output_format cuda`, with `hwdownload,format=p010le` inserted right
  after `fps=`; `hwdownload` needs the stream's exact pixel format, so the wrapper
  ffprobes the source and starts with `nv12` (8-bit) or `p010le` (10-bit); an
  unrecognised format or failed probe starts at `p010le` and relies on the retries below), so only the one frame in ~240 that survives `fps` is
  downloaded for the software scale/tonemap filters. Measured on omega with a 4K
  HDR/DV episode: 13x realtime, versus 3x when ffmpeg downloads every frame and
  versus 30-45 s to the *first* thumbnail with `-skip_frame nokey` (these files
  have sparse key frames, so key-frame-only is slower, not faster). If that
  pipeline fails before producing any thumbnail (wrong pixel format, or a codec
  NVDEC cannot decode) the wrapper retries on omega: `p010le`, then `nv12`, then
  plain NVDEC (`TRICKPLAY_MODE=plain` forces the last);
- points the output at a scratch dir on the shared volume
  (`/config/cache/transcodes/trickplay-<pid>-<time>/`), because omega cannot see
  Jellyfin's pod-local `/tmp/jellyfin/<id>/`, and **moves each JPEG into the
  original directory as it appears**. This matters: Jellyfin kills a trickplay
  job ("Trickplay process unresponsive") when no new file shows up in its own
  output dir for ~20 s, so moving everything at the end got every job killed.
  Scratch dirs left behind by killed jobs are swept after 10 minutes;
- **when omega is unavailable** (unreachable, busy, wrong token, kill switch) it
  exits **98** immediately with an `[offload] trickplay runs only on omega…`
  line. Nothing is generated and nothing runs on alpha.

### If someone asks "why is trickplay not generating?"

Omega is the only place it runs. Check, in order:

1. Is omega up (it is routinely rebooted into Windows)? `kubectl -n media get pods
   -l app.kubernetes.io/name=jellyfin-ffmpeg-runner`.
2. Look for `[offload] trickplay runs only on omega and omega is unavailable (…)`
   in Jellyfin's log (`/config/log/`, or the pod log): the reason is in the
   parentheses (unreachable, `declined (ERR busy)` when 3 jobs are already
   running, `ERR auth`, or `offload disabled`).
3. Nothing needs fixing when omega is simply off: the nightly "Generate Trickplay
   Images" task (03:00 UTC) and any later library scan retry the items that are
   still missing thumbnails.

To allow CPU trickplay again, change the wrapper (the trickplay branch calls
`fallback()`, which exits 98 for trickplay) and set Jellyfin's trickplay option
"Only generate images from key frames" first, or 4K episodes will block the
scan again.

## Rollout order

1. Token + runner + shared volume + wired spike (no change to live Jellyfin).
2. Test end to end on the spike: omega up, omega scaled to 0, omega killed
   mid-stream, playback stopped by the client.
3. Wire the live Jellyfin (`JELLYFIN_FFMPEG`, mounts, token, NVENC in the
   encoding settings via the tracked `encoding.xml`). This restarts Jellyfin, so
   pick a window with no active streams. Seed the shared volume **before** the
   change deploys, or the first plays of each file re-extract subtitles (a full
   read of the file over NFS). The runner mounts the shared subPaths already, so
   copy through it (additive, safe while Jellyfin runs):

   ```bash
   kubectl -n media exec deploy/jellyfin -- tar -C /config/data -cf - subtitles attachments \
     | kubectl -n media exec -i deploy/jellyfin-ffmpeg-runner -- tar -C /config/data -xf -
   kubectl -n media exec deploy/jellyfin-ffmpeg-runner -- sh -c \
     'ls /config/data/subtitles | wc -l; ls /config/data/attachments | wc -l'
   ```

   Then confirm a playback transcode logs `[offload] running on omega` in
   `/config/log/FFmpeg.Transcode-*.log`.
4. Remove `jellyfin-spike.yaml`.
