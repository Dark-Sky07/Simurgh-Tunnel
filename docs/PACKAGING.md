# Packages and containers

**English** · [فارسی](PACKAGING.fa.md)

`install.sh` is still the shortest path (it works on any systemd distribution
and builds the compiled engine for you). The packages exist for the machines
where a package manager, an image or an unpack-and-run tarball is the right
answer — and for the people who cannot run a script from the internet at all.

| | Command | Covers |
|---|---|---|
| **one command** | `curl … \| sudo bash` | everything, builds the Go engine |
| **`.deb`** | `sudo dpkg -i simurgh_2.9.1_amd64.deb` | Debian, Ubuntu, Proxmox |
| **`.rpm`** | `sudo dnf install ./simurgh-2.9.1-1.x86_64.rpm` | RHEL, Fedora, Alma, Rocky, openSUSE |
| **tarball** | `tar -xzf simurgh-2.9.1-noarch.tar.gz -C /` | anything with Python 3.9+ |
| **container** | `docker compose up -d` | both roles, one machine, or one role per host |

## What is inside

A package never touches your configuration: it creates `/etc/simurgh` with mode
`0700` and stops there. Everything else is the same files `install.sh` would
leave behind:

| Path | What |
|---|---|
| `/usr/bin/simurgh` | the CLI, menu, panel and both engines' supervisor |
| `/usr/lib/simurgh/simurgh/` | the Python engine (no dependencies: the TOML reader is bundled) |
| `/usr/bin/simurgh-go` | the compiled engine, in builds that include it (both `.deb`/`.rpm` variants and the tarball for your architecture) |
| `/usr/lib/systemd/system/simurgh-{relay,exit}.service` | one unit per role; the package enables a role only if **its** config already exists |
| `/usr/share/doc/simurgh/` | this file, the README (both languages) and the configuration reference |

The two variants are deliberate: `simurgh_2.9.1_all.deb` /
`simurgh-2.9.1-1.noarch.rpm` are pure Python and run anywhere, while
`simurgh_2.9.1_amd64.deb` / `…-1.x86_64.rpm` also carry the compiled engine —
which the CLI picks up from `/usr/bin/simurgh-go` by itself, so
`engine = "go"` works with no path to configure.

## Build them

```bash
bash packaging/build-packages.sh                              # .deb + .tar.gz here
bash packaging/build-packages.sh --out dist                   # into dist/
bash packaging/build-packages.sh --out dist \
     --go-binary dist/simurgh-go-linux-amd64                  # include the engine
bash packaging/build-packages.sh --out dist --prefix /opt/simurgh
```

No network, no Python packaging tools: the tree is copied into a staging root,
the launcher is pointed at the right library directory, and `dpkg-deb` /
`rpmbuild` (whichever is installed) turns that root into a package. `--no-deb`,
`--no-rpm` and `--no-tar` skip the ones you do not need, and the build works on
a machine that has neither tool — then you get the tarball.

CI builds all four artefacts on every push (`.github/workflows/ci.yml`, job
`packages`) and the release workflow attaches them to the GitHub release next to
the six engine binaries.

## Install

### Debian, Ubuntu, Proxmox

```bash
sudo dpkg -i simurgh_2.9.1_amd64.deb      # apt -f install, if you skipped the deps
sudo simurgh install --role exit --listen 443      # or --role relay --exit-host <ip>
sudo systemctl enable --now simurgh-exit           # the unit for the role you configured
```

`remove` keeps `/etc/simurgh` (your tokens and ledger survive a reinstall);
`purge` takes it away as well. The panel, metrics, alerts, quotas and the
`simurgh update` command all work exactly as they do after `install.sh`.

### RHEL, Fedora, Alma, Rocky, openSUSE

```bash
sudo dnf install ./simurgh-2.9.1-1.x86_64.rpm
sudo simurgh install --role relay --exit-host 203.0.113.9 --exit-port 443 --token XXX
sudo systemctl enable --now simurgh-relay
```

### Anywhere else: the tarball

It is rooted at `/`, so it unpacks over a system that has Python 3.9+:

```bash
sudo tar -xzf simurgh-2.9.1-noarch.tar.gz -C /
sudo simurgh install --role exit --listen 443
sudo simurgh exit          # no systemd? run the role by hand
```

A prefix other than `/usr` is one flag at build time (`--prefix /opt/simurgh`),
and the launcher then looks in `/opt/simurgh/lib/simurgh` (`$SIMURGH_LIB`
overrides that at runtime, which is what makes a tree unpacked somewhere else
runnable).

## Containers

```bash
docker build -t simurgh .
docker run --rm simurgh --version
docker run --rm -v simurgh-exit:/etc/simurgh simurgh init --role exit --listen 443
docker run -d --name simurgh-exit -v simurgh-exit:/etc/simurgh \
       -p 443:443 -p 19443:19443 simurgh exit
```

The image is two stages: the compiled engine is built from this checkout with
`GOPROXY=off` (every Go dependency is vendored), and the runtime is
`python:3.12-slim` with `ca-certificates` and `curl` — the Python side has
nothing to install. It carries no configuration; `/etc/simurgh` is a volume and
only `simurgh` writes it, exactly like on a server.

`docker-compose.yml` runs a relay and an exit side by side, which is the fastest
way to see the whole thing work:

```bash
docker compose build
TOKEN=$(openssl rand -hex 24)
docker compose run --rm exit  install --role exit  --listen 19443 --token "$TOKEN" --force
docker compose run --rm relay install --role relay --exit-host exit --exit-port 19443 \
    --carrier tls --insecure --mapping 8443:9000 --token "$TOKEN" --force
docker compose up -d
docker compose run --rm relay status        # and the panel on :8787
```

`--insecure` is only for that local pair (the exit serves a self-signed
certificate); on two real servers pin the certificate with
`--fingerprint AB:CD:…` or obtain one with `simurgh cert issue`.

On real servers, use the host network so the tunnel sees the machine's real
interfaces and ports (the compose file has the two-service example at the
bottom):

```yaml
services:
  relay:
    image: ghcr.io/dark-sky07/simurgh-tunnel:latest
    network_mode: host
    command: ["relay"]
    volumes: ["/etc/simurgh:/etc/simurgh"]
    restart: unless-stopped
```

The entrypoint refuses to start a role that has no config yet, and explains what
to run instead — the container is the same CLI, so `docker compose exec relay
simurgh menu` or `… simurgh web` work as usual.

## Notes

* **Updating.** `simurgh update` understands all of these: a git checkout moves
  to the newest tag, a package install downloads the matching release asset
  (verifying its SHA-256) and a pip install upgrades through pip. `apt upgrade`
  and `dnf upgrade` work for the packages too.
* **`simurgh doctor`** checks the same things after any install method, and
  `simurgh status` is a fine container health check (`simurgh status --json`
  exits 0 when the config is readable and non-zero when it is not).
* **The Go engine in a container** is at `/usr/bin/simurgh-go` and is used
  automatically; `engine = "go"` in the config is enough.
* **No configuration is ever generated by a package**: two servers are paired by
  one token that you pass to both (`--token`), or by a setup link
  (`simurgh link --show` → `simurgh join '<link>'`).
