"""Tailscale control, for administering a sealed board from anywhere.

This exists because of a gap the rest of the design creates. After `seal.sh`
runs, SSH is closed on both interfaces and `admin_lan_access` keeps customers
away from `/admin` -- which is correct, and leaves the owner with the uplink
port as the only way in. Serving admin there means exposing the login page to
whatever network the ISP router hands out. Tailscale replaces that with a path
authenticated by the owner's own account, encrypted end to end, that traverses
CGNAT without a forwarded port. With it up, `wan_admin` can go back to false
and admin is reachable from no physical network at all.

`main._admin_lan_denied()` already treats any non-customer interface as the
owner, so a tailscale0 request reaches admin with no special case; the input
chain names the interface explicitly so tightening the policy later cannot
strand a sealed board.

**This module never installs anything.** Bringing in a third-party apt repo is
the operator's decision at install time (`install.sh --tailscale`), not
something a web request should do. Here we only enrol and control what is
already present, and say so plainly when it is not.

**The auth key is never stored.** It is used once to enrol and then discarded;
tailscaled keeps its own node state in /var/lib/tailscale. A key sitting in the
settings table would be a fleet-wide secret in every backup, which is the same
mistake as the cloned root password.
"""
import json
import shutil
import subprocess

BIN = "tailscale"
STATE_DIR = "/var/lib/tailscale"


def _run(*args, timeout=30):
    try:
        return subprocess.run((BIN,) + args, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "tailscale not installed")
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def installed():
    return shutil.which(BIN) is not None


def status():
    """What state the tunnel is in, safe to call when nothing is installed.

    Keys: installed, running, state, ip4, hostname, tailnet, peers, error.
    """
    out = {"installed": installed(), "running": False, "state": "NotInstalled",
           "ip4": None, "hostname": None, "tailnet": None, "peers": 0,
           "error": None}
    if not out["installed"]:
        out["error"] = ("Tailscale is not installed on this machine. "
                        "Re-run the installer with --tailscale, or install it "
                        "by hand, then come back to this page.")
        return out

    r = _run("status", "--json", timeout=10)
    if r.returncode != 0:
        # A stopped daemon is a normal state, not a failure to report loudly.
        out["state"] = "Stopped"
        out["error"] = (r.stderr or r.stdout or "").strip() or None
        return out
    try:
        d = json.loads(r.stdout)
    except ValueError:
        out["error"] = "could not parse tailscale status output"
        return out

    out["state"] = d.get("BackendState") or "Unknown"
    out["running"] = out["state"] == "Running"
    me = d.get("Self") or {}
    ips = me.get("TailscaleIPs") or []
    out["ip4"] = next((i for i in ips if ":" not in i), None)
    out["hostname"] = me.get("HostName")
    out["tailnet"] = d.get("CurrentTailnet", {}).get("Name") or d.get("MagicDNSSuffix")
    out["peers"] = len(d.get("Peer") or {})
    return out


def up(authkey, hostname=None):
    """Enrol this machine. Returns (ok, message).

    --accept-dns=false is not optional here: the box runs dnsmasq for the
    captive portal, and letting Tailscale rewrite /etc/resolv.conf would point
    the portal's own resolver at MagicDNS and break DNS for every customer.

    --accept-routes=false likewise -- a subnet route learned from the tailnet
    could collide with 10.0.0.0/24 and blackhole the customer network.
    """
    if not installed():
        return False, "Tailscale is not installed on this machine."
    authkey = (authkey or "").strip()
    if not authkey:
        return False, "An auth key is required."
    if not authkey.startswith("tskey-"):
        return False, ("That does not look like a Tailscale auth key -- they "
                       "begin with 'tskey-'.")

    args = ["up", "--authkey=" + authkey,
            "--accept-dns=false", "--accept-routes=false"]
    if hostname:
        args.append("--hostname=" + hostname)
    r = _run(*args, timeout=90)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "tailscale up failed").strip()
    s = status()
    if s["ip4"]:
        return True, "Connected. This machine is reachable at %s" % s["ip4"]
    return True, "Connected."


def down():
    if not installed():
        return False, "Tailscale is not installed on this machine."
    r = _run("down", timeout=30)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "tailscale down failed").strip()
    return True, "Disconnected. This machine is no longer reachable over Tailscale."


def logout():
    """Forget the node identity entirely.

    Distinct from down(): down() keeps the enrolment and can be reversed from
    this page, logout() cannot. seal.sh removes the state directory outright,
    because a master image carrying one node identity would clone it onto every
    card and the copies would fight over it.
    """
    if not installed():
        return False, "Tailscale is not installed on this machine."
    r = _run("logout", timeout=30)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "tailscale logout failed").strip()
    return True, "Logged out. Re-enrolling needs a new auth key."
