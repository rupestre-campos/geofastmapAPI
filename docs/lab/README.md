# Advanced setup (lab)

**Work in progress.** Docs here describe **multi-machine** deployment on **owned hardware** (no cloud bill for the app stack). The normal path is still **single-host** [`docker compose up`](../DEPLOYMENT.md).

- **[geofast-distributed-experiment.md](geofast-distributed-experiment.md)** — Advanced setup guide: primary vs optional small worker hosts, Raspberry Pi tunnel edge, phased checklist, experiment log.
- **[nfs-host-ubuntu.md](nfs-host-ubuntu.md)** — **Recommended:** share tiles/bulk/rasters with workers using **nfs-kernel-server** on the primary (outside Docker), with copy-paste commands.

**In the app:** Project documentation → **Advanced setup (multi-machine, WIP)** — `/project-docs/advanced-setup?f=html`.
