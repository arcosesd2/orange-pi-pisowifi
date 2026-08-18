"""nftables session enforcement.

Access = membership in the `allowed` set of table inet pisowifi
(see network/nftables.conf). Elements carry a timeout, so the kernel
expires paid time on its own — this module only adds/removes elements.
"""
import ipaddress
import re
import subprocess

_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


def _nft(*args, check=False):
    return subprocess.run(
        ["nft", *args], capture_output=True, text=True, check=check
    )


def _validate(mac):
    mac = mac.lower().strip()
    if not _MAC_RE.match(mac):
        raise ValueError(f"bad mac: {mac!r}")
    return mac


def allow(mac, seconds):
    """Grant `mac` internet access for `seconds` (replaces any previous grant)."""
    mac = _validate(mac)
    seconds = max(1, int(seconds))
    revoke(mac)
    _nft(
        "add", "element", "inet", "pisowifi", "allowed",
        f"{{ {mac} timeout {seconds}s }}", check=True,
    )


def revoke(mac):
    mac = _validate(mac)
    _nft("delete", "element", "inet", "pisowifi", "allowed", f"{{ {mac} }}")


def is_allowed(mac):
    mac = _validate(mac)
    r = _nft("get", "element", "inet", "pisowifi", "allowed", f"{{ {mac} }}")
    return r.returncode == 0


# ---- device sets: whitelist (always-on free) / blocked (banned) ----

def _elem(op, set_name, elem):
    _nft(op, "element", "inet", "pisowifi", set_name, f"{{ {elem} }}")


def whitelist_add(mac):
    _elem("add", "whitelist", _validate(mac))


def whitelist_remove(mac):
    _elem("delete", "whitelist", _validate(mac))


def block_add(mac):
    mac = _validate(mac)
    revoke(mac)                       # drop any paid grant when banning
    _elem("add", "blocked", mac)


def block_remove(mac):
    _elem("delete", "blocked", _validate(mac))


def load_device_sets(devices):
    """Sync the whitelist/blocked nft sets from the DB device list (boot/reload).
    `devices` = iterable of rows with .kind ('white'|'black') and .mac."""
    for s in ("whitelist", "blocked"):
        _nft("flush", "set", "inet", "pisowifi", s)
    for d in devices:
        mac = (d["mac"] if isinstance(d, dict) else d.get("mac", "")).lower()
        if not _MAC_RE.match(mac):
            continue
        kind = d["kind"] if isinstance(d, dict) else d.get("kind")
        _elem("add", "whitelist" if kind == "white" else "blocked", mac)


# ---- walled garden (destinations unpaid clients may reach) ----

def set_walled(hosts):
    """Replace the walled-garden set with `hosts` (IPs or CIDR strings)."""
    _nft("flush", "set", "inet", "pisowifi", "walled")
    for h in hosts:
        try:
            net = ipaddress.ip_network(str(h).strip(), strict=False)
        except ValueError:
            continue
        if net.version != 4:
            continue
        _elem("add", "walled", str(net))
