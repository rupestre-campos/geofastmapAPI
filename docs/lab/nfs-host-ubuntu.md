# NFS on the primary host (Ubuntu, nfs-kernel-server)

This guide is the **recommended** way to share **tiles**, **bulk uploads**, and **rasters** with worker machines on your LAN. It uses the **Linux kernel NFS server** on the **primary** (outside Docker), with clear `/etc/exports` and normal **CIDR** client lists.

**Why not Docker NFS?** The optional [`docker-compose.nfs.yml`](../../deploy/compose/docker-compose.nfs.yml) helper can hit image quirks (`PERMITTED` / `sed`), and a **single** export over **bind-mounted** subdirectories may show **empty directories on clients** unless `crossmnt` is configured. Exporting **three separate paths** on the host avoids that class of issues.

**Default GeoFast deploy:** still [`docker compose up`](../../DEPLOYMENT.md) on the primary; this doc only applies when you add **remote workers** that need the same disk paths as the API.

---

## Goal

- **Primary:** runs Postgres, Redis, API, Titiler, volumes (unchanged).
- **Workers:** mount NFS at **`/data/tiles`**, **`/data/bulk-uploads`**, **`/data/rasters`** to match `TILES_STORAGE_PATH`, `BULK_STORAGE_PATH`, and Titiler’s raster path.

---

## Prerequisites

- Ubuntu Server (or similar) on the **primary** with GeoFast data in Docker **named volumes** (default root [`docker-compose.yml`](../../docker-compose.yml)).
- Workers on the **same LAN** (adjust IPs below).

Resolve volume directory names (prefix may match your compose project directory):

```bash
docker volume ls | grep geofast
docker volume inspect geofast_api_geofastmap_tiles --format '{{ .Mountpoint }}'
docker volume inspect geofast_api_geofastmap_bulk_uploads --format '{{ .Mountpoint }}'
docker volume inspect geofast_api_geofastmap_rasters --format '{{ .Mountpoint }}'
```

Typical paths:

```text
/var/lib/docker/volumes/geofast_api_geofastmap_tiles/_data
/var/lib/docker/volumes/geofast_api_geofastmap_bulk_uploads/_data
/var/lib/docker/volumes/geofast_api_geofastmap_rasters/_data
```

---

## Step A — Stable paths with bind mounts on the primary

Create mount points and **bind** each volume’s `_data` into **`/srv/geofast/`** (names match app paths: `tiles`, `bulk-uploads`, `rasters`):

```bash
sudo mkdir -p /srv/geofast/tiles /srv/geofast/bulk-uploads /srv/geofast/rasters

sudo mount --bind /var/lib/docker/volumes/geofast_api_geofastmap_tiles/_data /srv/geofast/tiles
sudo mount --bind /var/lib/docker/volumes/geofast_api_geofastmap_bulk_uploads/_data /srv/geofast/bulk-uploads
sudo mount --bind /var/lib/docker/volumes/geofast_api_geofastmap_rasters/_data /srv/geofast/rasters
```

Verify:

```bash
ls /srv/geofast/tiles
```

### Persist binds across reboots (`/etc/fstab`)

Use the real `_data` paths from `docker volume inspect`. Example lines (adjust volume names if yours differ):

```fstab
/var/lib/docker/volumes/geofast_api_geofastmap_tiles/_data /srv/geofast/tiles none bind 0 0
/var/lib/docker/volumes/geofast_api_geofastmap_bulk_uploads/_data /srv/geofast/bulk-uploads none bind 0 0
/var/lib/docker/volumes/geofast_api_geofastmap_rasters/_data /srv/geofast/rasters none bind 0 0
```

Then:

```bash
sudo mount -a
```

---

## Step B — Install the NFS server

On the **primary**:

```bash
sudo apt update
sudo apt install -y nfs-kernel-server
```

---

## Step C — `/etc/exports`

Export **each** of the three directories **on its own line**. That way clients see file listings inside `tiles/` (and the others) reliably; one parent export over bind children can confuse NFS without `crossmnt`.

Edit **`/etc/exports`**:

```bash
sudo nano /etc/exports
```

Example (replace **`192.168.8.0/24`** with your LAN; use a single host like `192.168.8.106/32` if you prefer):

```exports
/srv/geofast/tiles        192.168.8.0/24(rw,sync,no_subtree_check,no_root_squash)
/srv/geofast/bulk-uploads 192.168.8.0/24(rw,sync,no_subtree_check,no_root_squash)
/srv/geofast/rasters      192.168.8.0/24(rw,sync,no_subtree_check,no_root_squash)
```

If `exportfs` warns about multiple directories on one filesystem, add a **unique `fsid=`** per line (pick unused numbers), e.g.:

```exports
/srv/geofast/tiles        192.168.8.0/24(rw,sync,no_subtree_check,no_root_squash,fsid=101)
/srv/geofast/bulk-uploads 192.168.8.0/24(rw,sync,no_subtree_check,no_root_squash,fsid=102)
/srv/geofast/rasters      192.168.8.0/24(rw,sync,no_subtree_check,no_root_squash,fsid=103)
```

**Security:** only list **trusted LAN** subnets. Do **not** export to `*` on machines reachable from the internet.

---

## Step D — Apply and enable

```bash
sudo exportfs -rav
sudo systemctl enable --now nfs-server
sudo exportfs -v
```

On the primary, check locally:

```bash
showmount -e localhost
```

---

## Firewall (primary)

If **`ufw`** is enabled, allow NFS and RPC from your LAN only, for example:

```bash
sudo ufw allow from 192.168.8.0/24 to any port nfs
sudo ufw allow from 192.168.8.0/24 to any port 111 proto tcp
sudo ufw allow from 192.168.8.0/24 to any port 111 proto udp
```

You may need additional rules for `mountd` (often dynamic). If clients still cannot connect, temporarily test with `ufw disable` on a lab-only machine, or see Ubuntu’s NFS and UFW documentation. Prefer **LAN-only** exposure in all cases.

---

## Worker host (Ubuntu): install client and mount

On each **worker** (replace **`PRIMARY_IP`**):

```bash
sudo apt install -y nfs-common
sudo mkdir -p /data/tiles /data/bulk-uploads /data/rasters

sudo mount -t nfs -o vers=4,tcp PRIMARY_IP:/srv/geofast/tiles /data/tiles
sudo mount -t nfs -o vers=4,tcp PRIMARY_IP:/srv/geofast/bulk-uploads /data/bulk-uploads
sudo mount -t nfs -o vers=4,tcp PRIMARY_IP:/srv/geofast/rasters /data/rasters
```

Verify:

```bash
findmnt /data/tiles
ls /data/tiles | head
```

Set on the worker ([`deploy/env/.env.workers`](../../deploy/env/workers.sample)):

```env
TILES_STORAGE_PATH=/data/tiles
BULK_STORAGE_PATH=/data/bulk-uploads
```

Titiler on the worker should mount or bind **`/data/rasters`** the same way.

### Persist mounts (`/etc/fstab` on the worker)

After manual mounts work:

```fstab
PRIMARY_IP:/srv/geofast/tiles /data/tiles nfs vers=4,tcp,_netdev,nofail 0 0
PRIMARY_IP:/srv/geofast/bulk-uploads /data/bulk-uploads nfs vers=4,tcp,_netdev,nofail 0 0
PRIMARY_IP:/srv/geofast/rasters /data/rasters nfs vers=4,tcp,_netdev,nofail 0 0
```

Test:

```bash
sudo mount -a
```

---

## Troubleshooting

| Symptom | What to check |
|--------|----------------|
| `showmount` / `rpcinfo` errors from worker | Firewall on primary; `systemctl status nfs-server`; `exportfs -v` on primary. |
| Mount OK but **`ls` empty** inside `tiles` | Prefer **three separate export lines** as above (not one parent export over bind mounts without `crossmnt`). |
| `nfsvers=3` not supported | Server may offer **NFSv4 only**; use `vers=4` as in this doc. |
| Stale / wrong data | Confirm **`/srv/geofast/...`** on primary still has the bind mounts (`findmnt /srv/geofast/tiles`). |

---

## Optional: Docker-based NFS helper

The repo still includes [`deploy/compose/docker-compose.nfs.yml`](../../deploy/compose/docker-compose.nfs.yml) for experiments. Limitations include **`PERMITTED`** wildcard form (no `192.168.0.0/24` in some images), and **NFSv4 + bind submounts** may need **`crossmnt`** or separate exports—**host `nfs-kernel-server`** as in this document is simpler for production-style labs.

---

## References

- Advanced lab overview: [`geofast-distributed-experiment.md`](geofast-distributed-experiment.md)
- Split compose: [`../../deploy/compose/README.md`](../../deploy/compose/README.md)
- Default deploy: [`../DEPLOYMENT.md`](../DEPLOYMENT.md)
