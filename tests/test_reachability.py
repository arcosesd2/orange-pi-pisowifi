"""Firewall reachability matrix — what can each kind of client actually reach?

This exists because of a bug that only appeared *after* a customer paid: the
prerouting chain accepts @allowed clients before the port-80 redirect, nothing
listens on :80, and the input chain answered with a TCP reset. Browsing worked;
going back to the portal said "refused to connect". Nothing in the code was
wrong -- the rule *order* was.

Rule ordering bugs cannot be caught by reading a diff, so this walks the real
network/nftables.conf and evaluates synthetic packets against it, then asserts
the matrix of "who can reach what" that the product actually requires.

Run directly:  python tests/test_reachability.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, os.pardir, "network", "nftables.conf")

LAN, WAN, GW, NET, PORTAL = "enxLAN", "end0", "10.0.0.1", "10.0.0.0/24", 8080

# What the box actually has bound. NOTHING LISTENS ON PORT 80 -- the captive
# portal only ever worked there because prerouting DNATs 80 to the portal port.
# Letting a packet through to :80 is therefore still a failure: the kernel
# answers with a reset and the browser says "refused to connect". Modelling
# this is the difference between "the firewall permitted it" and "the customer
# saw a page", and it is where the shipped bug hid.
LISTENERS = {53, PORTAL}          # dnsmasq, and the Flask/waitress portal
SSH_PORT = 22                     # sshd is bound too, but must not be reachable


# ---------------------------------------------------------------- rendering

def render():
    """Apply the same substitutions network/detect.sh performs at boot."""
    s = open(CONF, encoding="utf-8").read()
    s = s.replace("eth0", WAN).replace("wlan0", LAN).replace("10.0.0.0/24", NET)
    s = re.sub(r"^(define GW_IP\s*=\s*).*$", r"\g<1>" + GW, s, count=1, flags=re.M)
    for token in ("#@ANTI_TETHER@", "#@FLOWTABLE@", "#@OFFLOAD@", "#@WAN_MGMT@"):
        s = s.replace(token, "")
    return s


def chain(text, name):
    """Return the executable rule lines of one chain, in order."""
    body = text.split("chain %s {" % name, 1)[1]
    depth, out = 1, []
    for line in body.splitlines():
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("type "):
            out.append(line)
    return out


# ---------------------------------------------------------------- evaluator

class Packet(dict):
    """iif, oif, saddr, daddr, proto, dport, mac_in (sets the MAC belongs to),
    ctstate."""


def matches(rule, p):
    """True if this rule's match clauses all apply to the packet."""
    if "iifname $LAN_IF" in rule and p["iif"] != LAN:
        return False
    if 'iifname "lo"' in rule and p["iif"] != "lo":
        return False
    if "iifname $WAN_IF" in rule and p["iif"] != WAN:
        return False
    if "oifname $LAN_IF" in rule and p.get("oif") != LAN:
        return False
    if "oifname $WAN_IF" in rule and p.get("oif") != WAN:
        return False

    m = re.search(r"ether saddr @(\w+)", rule)
    if m and m.group(1) not in p["mac_in"]:
        return False

    if "ip daddr @walled" in rule and not p.get("walled"):
        return False
    if "ip daddr $GW_IP" in rule and p["daddr"] != GW:
        return False
    if "ip saddr != $CLIENT_NET" in rule and p["saddr"].startswith("10.0.0."):
        return False

    if "ct state established,related" in rule and p.get("ctstate") != "established":
        return False
    # Rate-limit rules only fire on floods; a single packet never matches.
    if "limit rate over" in rule:
        return False
    if "ct count over" in rule:
        return False
    if "ct state new" in rule and p.get("ctstate") == "established":
        return False

    if "meta l4proto tcp" in rule and p["proto"] != "tcp":
        return False
    if "icmp type echo-request" in rule and p["proto"] != "icmp":
        return False

    m = re.search(r"(tcp|udp) dport \{([^}]*)\}", rule)
    if m:
        if p["proto"] != m.group(1):
            return False
        ports = {PORTAL if "PORTAL_PORT" in x else int(x.strip())
                 for x in m.group(2).split(",")}
        if p["dport"] not in ports:
            return False
    else:
        m = re.search(r"(tcp|udp) dport (\S+)", rule)
        if m:
            if p["proto"] != m.group(1):
                return False
            port = PORTAL if "PORTAL_PORT" in m.group(2) else int(m.group(2))
            if p["dport"] != port:
                return False
    return True


def verdict(rules, p):
    """Walk a chain; return (action, rule) for the first matching rule."""
    for rule in rules:
        if not matches(rule, p):
            continue
        for action in ("drop", "reject", "accept", "masquerade"):
            if re.search(r"\b%s\b" % action, rule):
                return action, rule
        m = re.search(r"redirect to :(\S+)", rule)
        if m:
            port = PORTAL if "PORTAL_PORT" in m.group(1) else int(m.group(1))
            return ("redirect", port), rule
    return "policy accept", None


def client_reaches_portal(text, mac_in, dport):
    """Simulate a LAN client opening http://<gw>:<dport>/ .

    prerouting (nat) may DNAT it, then the input chain decides.
    """
    p = Packet(iif=LAN, saddr="10.0.0.50", daddr=GW, proto="tcp",
               dport=dport, mac_in=mac_in, ctstate="new")
    act, rule = verdict(chain(text, "prerouting"), p)
    if act == "drop":
        return "DROPPED in prerouting", rule
    if isinstance(act, tuple) and act[0] == "redirect":
        p["dport"] = act[1]                      # DNAT rewrites the port
    act2, rule2 = verdict(chain(text, "input"), p)
    if act2 not in ("accept", "policy accept"):
        return "%s (browser: refused/timeout)" % act2.upper(), rule2
    if p["dport"] not in LISTENERS:
        # Permitted by the firewall, but nothing is bound: the kernel resets it
        # and the customer sees exactly the same "refused to connect".
        return "NO LISTENER on :%d (browser: refused)" % p["dport"], rule2
    return "reaches portal on :%d" % p["dport"], rule2


def client_reaches_internet(text, mac_in):
    p = Packet(iif=LAN, oif=WAN, saddr="10.0.0.50", daddr="1.1.1.1",
               proto="tcp", dport=443, mac_in=mac_in, ctstate="new")
    act, rule = verdict(chain(text, "forward"), p)
    return ("allowed" if act in ("accept", "policy accept") else act.upper()), rule


# ------------------------------------------------------------------- checks

def main():
    text = render()
    fails = []

    def check(desc, got, want):
        ok = want in got
        print("  %-58s %-34s %s" % (desc, got, "ok" if ok else "FAIL"))
        if not ok:
            fails.append("%s -> %s (wanted %r)" % (desc, got, want))

    # Every customer state the machine can put a device in.
    STATES = {
        "unpaid":      set(),
        "paid":        {"allowed"},
        "whitelisted": {"whitelist"},
        "blocked":     {"blocked"},
    }

    print("\n=== The portal must be reachable in EVERY non-blocked state ===")
    print("    (this is the bug that shipped: it worked unpaid, broke once paid)")
    for state, sets in STATES.items():
        for port, label in ((80, "http://10.0.0.1/"), (PORTAL, "http://10.0.0.1:8080/")):
            got, _ = client_reaches_portal(text, sets, port)
            want = "DROPPED" if state == "blocked" else "reaches portal"
            check("%-11s %s" % (state, label), got, want)

    print("\n=== Internet access must follow payment state ===")
    for state, sets in STATES.items():
        got, _ = client_reaches_internet(text, sets)
        want = "allowed" if state in ("paid", "whitelisted") else \
               ("DROP" if state == "blocked" else "REJECT")
        check("%-11s tcp/443 out" % state, got, want)

    print("\n=== DNS must work for everyone (captive detection needs it) ===")
    for state, sets in STATES.items():
        p = Packet(iif=LAN, saddr="10.0.0.50", daddr=GW, proto="udp",
                   dport=53, mac_in=sets, ctstate="new")
        act, _ = verdict(chain(text, "prerouting"), p)
        act2, _ = verdict(chain(text, "input"), p)
        got = "accepted" if act2 in ("accept", "policy accept") else str(act2).upper()
        check("%-11s udp/53 to gateway" % state, got,
              "DROP" if state == "blocked" else "accepted")

    print("\n=== DHCP must work before anything else does ===")
    p = Packet(iif=LAN, saddr="0.0.0.0", daddr="255.255.255.255", proto="udp",
               dport=67, mac_in=set(), ctstate="new")
    act, _ = verdict(chain(text, "input"), p)
    check("unpaid      udp/67 DHCP request", str(act), "accept")

    print("\n=== The box must not expose SSH to customers ===")
    for state, sets in STATES.items():
        p = Packet(iif=LAN, saddr="10.0.0.50", daddr=GW, proto="tcp",
                   dport=22, mac_in=sets, ctstate="new")
        act, _ = verdict(chain(text, "input"), p)
        got = str(act).upper()
        # whitelisted devices are documented as having full access to the box
        check("%-11s tcp/22 to the box" % state, got,
              "ACCEPT" if state == "whitelisted" else ("DROP" if state == "blocked" else "REJECT"))

    print("\n=== The uplink must be closed by default ===")
    p = Packet(iif=WAN, saddr="192.168.1.5", daddr="192.168.1.9", proto="tcp",
               dport=22, mac_in=set(), ctstate="new")
    act, _ = verdict(chain(text, "input"), p)
    check("wan         tcp/22 inbound (wan_management off)", str(act), "drop")

    print("\n=== Established traffic must never be broken mid-session ===")
    p = Packet(iif=LAN, saddr="10.0.0.50", daddr=GW, proto="tcp",
               dport=PORTAL, mac_in=set(), ctstate="established")
    act, _ = verdict(chain(text, "input"), p)
    check("unpaid      established to portal", str(act), "accept")

    print()
    if fails:
        print("%d REACHABILITY FAILURE(S):" % len(fails))
        for f in fails:
            print("  *", f)
        return 1
    print("all reachability expectations hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
