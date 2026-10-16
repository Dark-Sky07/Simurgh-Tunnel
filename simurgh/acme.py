"""Let's Encrypt without the ritual.

The tunnel already knows how to *use* a certificate (``cert_file`` / ``key_file``
in the role's config, per listener if you want) and it now tells the operator
when one is about to expire.  What was missing is obtaining one: this module
drives whichever ACME client is already installed on the machine — ``acme.sh``
first, then ``certbot`` — wires the files it produced into the config, and
installs a reload hook so a renewal needs no human.

Nothing here speaks ACME itself on purpose: the two clients above are what
Iranian operators actually have (``acme.sh`` is a single shell script, which
matters on a server where pip and snap are filtered), they keep working when
this project is upgraded, and they already know how to renew and reload.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .certs import expiry_days, fingerprint_of
from .util import get_logger

log = get_logger("simurgh.acme")

#: where the two clients usually live (a login shell's PATH is not ours)
ACME_SH_PATHS = ("~/.acme.sh/acme.sh", "/root/.acme.sh/acme.sh")
#: certbot's default store
LETSENCRYPT_DIR = "/etc/letsencrypt"
TIMEOUT = 600.0


class AcmeError(RuntimeError):
    """Raised with a message an operator can act on."""


@dataclass
class Result:
    """What `simurgh cert issue` did, step by step (printed by the CLI)."""

    client: str = ""
    binary: str = ""
    steps: list[str] = field(default_factory=list)
    cert_file: str = ""
    key_file: str = ""
    wired: bool = False
    staging: bool = False

    def as_dict(self) -> dict:
        return {"client": self.client, "binary": self.binary, "steps": list(self.steps),
                "cert_file": self.cert_file, "key_file": self.key_file,
                "wired": self.wired, "staging": self.staging}


# --------------------------------------------------------------------- clients
def _first_executable(candidates) -> str:
    for item in candidates:
        path = Path(str(item)).expanduser()
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:                       # not ours to look at (e.g. /root)
            continue
    return ""


def acme_sh() -> str:
    found = shutil.which("acme.sh") or ""
    return found or _first_executable(ACME_SH_PATHS)


def certbot() -> str:
    return shutil.which("certbot") or ""


def client(prefer: str = "") -> tuple[str, str]:
    """Find an ACME client: ``("acme.sh", /path)``, ``("certbot", /path)`` or ``("", "")``."""
    sh, cb = acme_sh(), certbot()
    if prefer == "acme.sh":
        return ("acme.sh", sh) if sh else ("", "")
    if prefer == "certbot":
        return ("certbot", cb) if cb else ("", "")
    if sh:
        return "acme.sh", sh
    if cb:
        return "certbot", cb
    return "", ""


def describe_clients() -> str:
    sh, cb = acme_sh(), certbot()
    bits = []
    if sh:
        bits.append(f"acme.sh ({sh})")
    if cb:
        bits.append(f"certbot ({cb})")
    return " + ".join(bits) if bits else ""


def install_hint() -> str:
    return ("no ACME client found.  Install one first, for example:\n"
            "  curl https://get.acme.sh | sh -s email=you@example.com\n"
            "  apt install certbot        # or: python3 -m pip install certbot\n"
            "then run this command again.")


# -------------------------------------------------------------------- run it
def run(argv: list[str], *, timeout: float = TIMEOUT) -> tuple[int, str]:
    """Run a client and return (code, combined output).  Never raises for rc≠0."""
    log.debug("running %s", " ".join(argv))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise AcmeError(f"cannot run {argv[0]}: {exc}") from None
    except subprocess.TimeoutExpired as exc:
        raise AcmeError(f"{argv[0]} did not finish in {timeout:.0f} s") from None
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _tail(output: str, lines: int = 12) -> str:
    parts = [line for line in output.splitlines() if line.strip()]
    return "\n".join(parts[-lines:])


# ------------------------------------------------------------------- issue it
def issue(domain: str, *, email: str = "", webroot: str = "", standalone: bool = False,
          staging: bool = False, prefer: str = "", reload_cmd: str = "",
          config_dir: str = "", binary: str = "") -> Result:
    """Obtain a certificate for *domain* and install a renewal hook.

    Either ``webroot`` (a directory a webserver serves) or ``standalone`` (the
    client binds port 80 itself for a moment) has to be given.  The files land
    in the client's own store; :func:`certificate_paths` knows where.
    """
    domain = (domain or "").strip()
    if not domain:
        raise AcmeError("a domain is required")
    if not webroot and not standalone:
        raise AcmeError("choose how to prove ownership: --webroot DIR (nginx/caddy "
                        "already serves it) or --standalone (port 80 must be free)")

    name, path = client(prefer)
    if binary:
        name, path = (name or "acme.sh"), binary
    if not name or not path:
        raise AcmeError(install_hint())

    result = Result(client=name, binary=path, staging=staging)
    if name == "acme.sh":
        argv = [path, "--issue", "-d", domain]
        if standalone:
            argv.append("--standalone")
        else:
            argv += ["--webroot", webroot]
        if staging:
            argv += ["--server", "letsencrypt_test"]
        if email:
            argv += ["--accountemail", email]
        code, out = run(argv)
        result.steps.append(f"{' '.join(argv)} -> exit {code}")
        if code != 0:
            raise AcmeError(f"acme.sh could not issue a certificate for {domain}:\n"
                            f"{_tail(out)}")
    else:
        argv = [path, "certonly", "-d", domain, "--non-interactive",
                "--agree-tos", "-m", email or f"admin@{domain}"]
        if config_dir:
            argv += ["--config-dir", config_dir, "--work-dir",
                     str(Path(config_dir) / "work"), "--logs-dir",
                     str(Path(config_dir) / "logs")]
        if standalone:
            argv.append("--standalone")
        else:
            argv += ["--webroot", "-w", webroot]
        if staging:
            argv.append("--staging")
        code, out = run(argv)
        result.steps.append(f"{' '.join(argv)} -> exit {code}")
        if code != 0:
            raise AcmeError(f"certbot could not issue a certificate for {domain}:\n"
                            f"{_tail(out)}")

    cert_file, key_file = certificate_paths(domain, name, config_dir=config_dir,
                                            binary=path)
    if not (Path(cert_file).is_file() and Path(key_file).is_file()):
        raise AcmeError(f"{name} reported success but {cert_file} / {key_file} "
                        "are not there; check the client's output")
    result.cert_file, result.key_file = cert_file, key_file
    result.steps.append(f"certificate: {cert_file} (valid {_days(cert_file)})")

    if reload_cmd:
        result.steps.append(install_renewal(domain, name, reload_cmd,
                                            config_dir=config_dir, binary=path))
    return result


def certificate_paths(domain: str, name: str, *, config_dir: str = "",
                      binary: str = "") -> tuple[str, str]:
    """Where this client keeps the certificate it just made for *domain*."""
    if name == "acme.sh":
        store = Path(binary or acme_sh() or "acme.sh").parent
        for suffix in ("_ecc", ""):
            base = store / f"{domain}{suffix}"
            cert, key = base / "fullchain.cer", base / f"{domain}.key"
            if cert.is_file() and key.is_file():
                return str(cert), str(key)
        # not issued yet: the ECC directory is where acme.sh puts it by default
        base = store / f"{domain}_ecc"
        return str(base / "fullchain.cer"), str(base / f"{domain}.key")
    root = Path(config_dir) if config_dir else Path(LETSENCRYPT_DIR)
    live = root / "live" / domain
    return str(live / "fullchain.pem"), str(live / "privkey.pem")


def install_renewal(domain: str, name: str, reload_cmd: str, *,
                    config_dir: str = "", binary: str = "") -> str:
    """Tell the client what to run after it renews (so the service reloads)."""
    if name == "acme.sh":
        path = binary or acme_sh()
        cert_file, key_file = certificate_paths(domain, "acme.sh", binary=path)
        argv = [path, "--install-cert", "-d", domain, "--key-file", key_file,
                "--fullchain-file", cert_file, "--reloadcmd", reload_cmd]
        code, out = run(argv)
        if code != 0:
            raise AcmeError(f"acme.sh --install-cert failed:\n{_tail(out)}")
        return f"renewal hook installed: {reload_cmd}"
    path = binary or certbot()
    argv = [path, "renew", "--cert-name", domain, "--deploy-hook", reload_cmd,
            "--dry-run"]
    code, out = run(argv)
    if code != 0:
        # the hook is stored by certbot when it is passed; a dry run failing
        # (e.g. rate limits or no config) must not throw away a good certificate
        return f"renewal hook: add `--deploy-hook \"{reload_cmd}\"` to your certbot renewal"
    return f"renewal hook installed: {reload_cmd}"


def renew(*, prefer: str = "", config_dir: str = "") -> tuple[str, bool, str]:
    """Run the client's own renewal (the same thing its cron/timer runs)."""
    name, path = client(prefer)
    if not name:
        raise AcmeError(install_hint())
    if name == "acme.sh":
        argv = [path, "--cron"]
    else:
        argv = [path, "renew", "--non-interactive"]
        if config_dir:
            argv += ["--config-dir", config_dir]
    code, out = run(argv)
    return name, code == 0, _tail(out, 20)


def reload_now(command: str) -> tuple[bool, str]:
    """Run the reload command once, now (what the installed hook runs later).

    Best effort by design: the command comes from the operator or from a
    ``systemctl`` that may not exist in a container, so the caller reports the
    failure instead of raising it.
    """
    argv = shlex.split(command)
    if not argv:
        return True, ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return False, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]} did not finish in {TIMEOUT:.0f} s"
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


# ------------------------------------------------------------------ wire it in
def wire(cfg, role: str, cert_file: str, key_file: str, port: int | None = None) -> list[str]:
    """Point a loaded config at the files ACME produced.

    Without ``port`` the certificate becomes the role's default (the exit's
    ``cert_file``/``key_file``, the relay's ``[tunnel]``); with ``port`` only
    that ``[[listen]]`` block is changed, which is how one box serves several
    domains.  ``cert_auto`` is turned off so the engine never replaces a real
    certificate with a self-signed one.
    """
    changed: list[str] = []
    if role == "exit":
        target = None
        if port:
            target = next((ls for ls in cfg.listen if ls.port == int(port)), None)
            if target is None:
                raise AcmeError(f"no [[listen]] block on port {port}")
            target.cert_file, target.key_file = cert_file, key_file
            changed.append(f"[[listen]] {target.carrier}:{target.port}")
        else:
            cfg.cert_file, cfg.key_file = cert_file, key_file
            cfg.cert_auto = False
            changed.append("cert_file/key_file")
            for ls in cfg.listen:
                if ls.cert_file:
                    # a listener with its own certificate keeps it: that is a
                    # deliberate setup, not something to overwrite silently
                    changed.append(f"note: [[listen]] {ls.carrier}:{ls.port} keeps "
                                   "its own certificate")
    else:
        cfg.tunnel.cert_file, cfg.tunnel.key_file = cert_file, key_file
        cfg.tunnel.cert_auto = False
        changed.append("[tunnel] cert_file/key_file")
    return changed


def _days(cert_file: str) -> str:
    days = expiry_days(cert_file)
    return "unknown" if days is None else f"{days:.0f} more day(s)"


def status(cfg, role: str) -> list[dict]:
    """One row per certificate this role serves (see `simurgh cert`)."""
    rows: list[dict] = []
    if role == "exit":
        for ls in cfg.listen:
            if ls.carrier not in ("tls", "wss") or not ls.enabled:
                continue
            cert = ls.cert_file or cfg.cert_file
            rows.append(_row(f"{ls.carrier}:{ls.port}", cert,
                             ls.key_file or cfg.key_file,
                             "generated" if (cfg.cert_auto and not ls.cert_file) else ""))
    else:
        spec = cfg.tunnel
        if spec.enabled and spec.carrier in ("tls", "wss"):
            rows.append(_row(f"tunnel {spec.carrier}:{spec.port}", spec.cert_file,
                             spec.key_file, "generated" if spec.cert_auto else ""))
    return rows


def _row(label: str, cert_file, key_file, note: str = "") -> dict:
    cert = str(cert_file or "")
    days = expiry_days(cert) if cert else None
    return {
        "endpoint": label,
        "cert_file": cert,
        "key_file": str(key_file or ""),
        "exists": bool(cert) and Path(cert).is_file(),
        "days": None if days is None else round(days, 1),
        "fingerprint": fingerprint_of(cert) if cert and Path(cert).is_file() else "",
        "note": note,
    }
