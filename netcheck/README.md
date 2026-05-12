# netcheck

Network health monitor for `thefootonline.local`, deployed as a Docker container
on **smarthome01** (`/usr/local/apps/docker-compose/netcheck/`).

A hand-rolled Flask app (`netcheck.py`) that probes the home network and exposes
results via HTTP API on port 8090. Results are persisted to MongoDB
(`network_monitor` database, auth-required as of 2026-05-06).

## Build

```
docker compose build
docker compose up -d
```

The container is built locally from the `Dockerfile` — no registry image.

## Container privileges

Runs with `network_mode: host` and capabilities `NET_ADMIN` + `NET_RAW` because
it needs raw socket access for ping/traceroute and host-level DNS visibility.

## Bind mounts

| Host path                  | Container path             | Purpose                          |
|----------------------------|----------------------------|----------------------------------|
| `./netcheck.py`            | `/app/netcheck.py:ro`      | Live-reloadable script           |
| `/var/run/docker.sock`     | `/var/run/docker.sock:ro`  | Inspect running containers       |
| `/mnt/data/dnsmasq`        | `/host/dnsmasq:ro`         | Read dnsmasq leases/config       |
| `/proc/sys/net/ipv4`       | `/host/proc/sys/net/ipv4:ro` | Read kernel net tunables       |
| `/home/thefoot/dave`       | `/home/thefoot/dave:ro`    | Read Dave repo context           |

## Environment

A `.env` file is required at runtime and is **gitignored**. Required keys:

```
MONGO_URI=mongodb://<user>:<password>@127.0.0.1:27017/network_monitor?authSource=network_monitor
```

The `MONGO_URI` should move to the HQ SOPS+age vault as part of the broader
secrets-migration effort.

## API

Once running, hit:

```
http://smarthome01.thefootonline.local:8090/health
http://smarthome01.thefootonline.local:8090/quick
http://smarthome01.thefootonline.local:8090/full
```

## Maintenance

`netcheck.py` is the single source of truth for the probe logic. The
auth-required MongoDB transition (2026-05-06) is reflected in the `MONGO_URI`
shape — older un-authenticated URIs no longer work.
