# Kustomize reorg + Helm migration

Runbook for reorganizing `k8s/` so the directory tree mirrors the mental model,
and replacing hand-vendored raw manifests with upstream Helm charts via Flux
`HelmRelease`.

## North star

The success test is **not** "fewer lines of YAML." It is: *understand this
cluster as intuitively as a single LXC running one `docker-compose.yml`.*

That analogy drives every choice here:

- **One app = one file.** A `HelmRelease` using the `app-template` chart is the
  closest k8s equivalent to one `docker-compose` service block — image, env,
  ports, volumes, all in one readable values block. After this migration, "what
  is sonarr?" is answered by exactly one file, not 3–5 scattered ones.
- **The tree is the map.** Functional group directories (`acquisition/`,
  `streaming/`, …) are the equivalent of splitting a giant compose file into
  logical sections. The filesystem layout *is* the diagram.
- **Consume, don't vendor.** Cluster primitives (traefik, cert-manager, …) move
  to official charts where you maintain *values*, not 500-line CRD files you
  never wanted to read — like depending on an image instead of forking it.

If at any phase the cluster gets *harder* to hold in your head, stop and
reconsider that step. Legibility is the goal; DRY and tidiness are only means.

## The reframe: workload vs. cluster-primitive

The useful split is not custom-vs-upstream. It is:

- **Workloads** — a Deployment/CronJob/DaemonSet plus its Service/PVC/Ingress.
  Includes your own `ghcr.io/arishaig/*` images. Almost none have a good
  dedicated chart and none need one: the **bjw-s `app-template`** chart renders
  all of it from a values block. ~30 apps live here.
- **Cluster primitives** — software that ships its own CRDs/controllers/RBAC
  (traefik, cert-manager, metallb, …). These have real **official charts**;
  converting them also fixes a latent bug (below).
- **Raw Kustomize stays** for genuine primitives: namespaces, the cert
  ClusterIssuer, MetalLB pools, StorageClasses, `mcp-rbac`, cross-cutting
  IngressRoutes, the media NFS PV/PVC, `rpi5-net-tuning`.

## The latent bug this fixes

Renovate's `kubernetes` manager bumps the **image tag only** on vendored infra.
So a component's image can advance while its bundled RBAC/CRDs/config stay
frozen — silent drift. A Flux `HelmRelease` upgrades the chart **as a unit**,
and Renovate's `flux`/`helm` managers track the chart version.

(cert-manager is the exception: it is already pulled as a *pinned full manifest*
in `cert-manager/kustomization.yaml`, so it is coherent today — low priority.)

## Chart choices

`app-template` (the workload chart):

```text
oci://ghcr.io/bjw-s-labs/helm/app-template   # v4.6.0 as of Feb 2026
```

First-class Flux support (ships HelmRelease JSON schemas). One shared
`HelmRepository` of `type: oci` serves every app HelmRelease.

### Dependency trust (why app-template is acceptable)

`app-template` is bjw-s's personal project — the de-facto standard in the
k8s-at-home / home-operations community (successor to k8s-at-home's `common`
library). Provenance is "community project," **not** foundation/vendor-backed,
so it gets adopted deliberately, not by default. What makes it acceptable:

- It is a **templating library** — ships **no images, no runtime code**. At
  reconcile it only renders plain k8s manifests. The images that run are the
  ones *we* name; app-template injects none.
- Blast radius is "what YAML does it emit," and that is **pinned** (chart
  version), **delayed** (`minimumReleaseAge: 5 days`), and **inspectable**
  (`flux diff` / `helm template` before apply). A rogue release shows up in the
  diff.

The infra charts (traefik, cert-manager, metallb, kube-state-metrics, alloy,
authelia) are CNCF/vendor-backed and clear the trust bar without debate — and
they carry the real correctness win. app-template is the apps-legibility layer;
the first-party fallback if trust ever sours is Kustomize bases.

Cluster-primitive charts:

| Component | Chart repo |
|---|---|
| traefik | `https://traefik.github.io/charts` (`traefik/traefik`) |
| cert-manager | `https://charts.jetstack.io` (`jetstack/cert-manager`) — low priority |
| metallb | `https://metallb.github.io/metallb` |
| kube-state-metrics | `https://prometheus-community.github.io/helm-charts` |
| authelia (+redis) | `https://charts.authelia.com` |
| alloy | `https://grafana.github.io/helm-charts` |
| nfs-subdir-external-provisioner | `kubernetes-sigs/nfs-subdir-external-provisioner` |
| local-path-provisioner | rancher chart — marginal, optional |

## Inventory & classification

| Group | Apps | Target |
|---|---|---|
| **acquisition** | sonarr, radarr, prowlarr, bazarr, lidarr(prarr), sabnzbd, recyclarr, flaresolverr, subgen | app-template |
| **streaming** | jellyfin, tunarr, tdarr (server+node) | app-template |
| **requests** | seerr, recommendarr | app-template |
| **tracking** | watcharr, watchback | app-template |
| **tools** | metube, medialyze, storyteller, copyparty | app-template |
| **dashboard** | homepage | app-template |
| **recipes** | mealie, postgres, postgres-exporter | app-template |
| **your own** | anagnorisis, lidarr-ui, nest-mcp | app-template |
| **infrastructure** | traefik, cert-manager, metallb, kube-state-metrics, authelia(+redis), alloy, nfs-subdir, local-path | official charts |
| **stays raw** | namespaces, cluster-issuer, metallb pools, storageclasses, mcp-rbac, ingress-routes, media NFS PV/PVC, rpi5-net-tuning | Kustomize |
| **delete** | nginx-test | — |

## Target tree (media, after reorg + convert)

The reorg and the Helm conversion are the **same operation** — do not build
per-app Kustomize directories first and then redo them as HelmReleases.

```text
k8s/apps/media/
  kustomization.yaml            # lists the group dirs
  namespace.yaml  media-nfs-pv.yaml  media-nfs-pvc.yaml  ingress-routes.yaml   # raw, shared
  acquisition/
    kustomization.yaml
    sonarr-config-pvc.yaml      # raw PVC kept (see Data safety)
    sonarr.yaml                 # HelmRelease -> app-template, existingClaim: sonarr-config
    radarr.yaml  prowlarr.yaml  ...
  streaming/  requests/  tracking/  tools/  dashboard/  recipes/
```

The group names are exactly the comment headers already in today's
`media/kustomization.yaml` — promoted from comments to directories.

## Flux + Renovate wiring (one-time plumbing)

- **One** `HelmRepository` (`type: oci`) -> `oci://ghcr.io/bjw-s-labs/helm`,
  shared by every app HelmRelease (`chart: app-template`, version pinned). Plus
  one `HelmRepository` per infra chart repo.
- **Flux ordering — deliberately deferred.** App HelmReleases resolve their
  chart by *eventual consistency* (retry until the source exists), so the pilot
  needs no hard ordering. A blanket `apps -> dependsOn: infrastructure` couples
  *all* app reconciliation to infra's `wait: true`; if the new `type: oci`
  HelmRepository ever fails to report `Ready`, every app would freeze. Introduce
  `dependsOn` only in the infra-charts phase, where metallb/cert-manager must
  land before things that need LB IPs / certs — and verify the OCI source
  reports `Ready` first.
- **renovate.json:** add the `flux` manager (chart version + HelmRepository) and
  a `customManager` for container images inside HelmRelease values (the flux
  manager does not track in-values images). Keep the `kubernetes` manager for
  the remaining raw manifests. The custom regex is unverified until the first
  image bump — a miss only means a skipped update, never a deploy risk.

## Data safety (the one thing that can hurt you)

The `*-config` PVCs hold real state (*arr databases, jellyfin config). Rule for
every conversion:

> **Leave the PVC as a raw manifest and bind to it** via
> `persistence.config.existingClaim: <name>` in app-template values. Never let
> app-template mint a new PVC; never let Flux `prune` delete the old one.

Converting a workload is then just: old Deployment pruned -> new one created
against the *same* PVC. Seconds of downtime, zero data risk.

### Selector handoff (every conversion)

app-template labels pods with `app.kubernetes.io/*`; the old raw manifests use
`app: <name>`. A Deployment's `.spec.selector` is **immutable**, so this is not
an in-place patch — the old Deployment (kustomize-controller owned) must be
**pruned** and the new one (helm-controller owned) **created** fresh. Because
the removed raw manifest and the new HelmRelease ride the *same* `apps`
Kustomization apply, kustomize prunes the old object before helm-controller
installs, so it converges on its own. If a conversion ever wedges on
`field is immutable` or Helm `invalid ownership metadata`, the fix is to let the
old Deployment finish pruning (or delete it once) and reconcile again. Watch
this on the stateful apps; for stateless ones it is a non-event.

## Phased sequence

Each phase is independently shippable.

0. **Plumbing** — add HelmRepository sources + Renovate managers. No behavior
   change. Confirm Flux reconciles the sources.
1. **Pilot one stateless app** end-to-end (flaresolverr or metube). Validates
   values shape, service/metallb annotations, ingress, and that Renovate sees
   the chart — *before* touching anything stateful.
2. **Lay the group skeleton** in `media/` and move the pilot in.
3. **Convert workloads group-by-group**, starting with **acquisition** (the
   *arr stack — most repetitive, so the shared shape incl. the `exportarr`
   sidecar + metallb shared-IP service gets codified once). Each app: new
   HelmRelease with `existingClaim`, delete old raw deploy/svc, keep PVC, verify.
4. **Infra -> official charts**, one at a time with `dependsOn`. Do metallb and
   traefik when you can watch (load-bearing for LB IPs and ingress). cert-manager
   last/optional.
5. **Cleanup** — delete nginx-test, prune dead kustomization entries, update
   README + MEMORY.

## Decisions (settled)

- **DRY ceiling:** keep one HelmRelease file per app (~25 lines of values). Do
  **not** build a local umbrella/wrapper chart — that re-introduces the
  rendered-≠-file opacity this migration exists to remove.
- **postgres:** keep simple as an app-template workload (not CloudNativePG) for
  now; revisit only if Postgres becomes load-bearing for more than mealie.
- **anagnorisis:** no longer deferred — it is up and hashing; convert in the
  `your own` pass.
- **local-path-provisioner / cert-manager:** marginal / already coherent —
  optional, lowest priority.
