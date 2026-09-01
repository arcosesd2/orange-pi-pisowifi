"""Storage health: can this machine still record the money it takes?

The worst failure this project has seen was a rootfs that went read-only
mid-install on a no-name SD card. Nothing detected it. In a shop that failure
is silent and expensive: the portal keeps serving, the coin slot keeps
counting, customers keep getting online -- and not one sale is written down.
The machine takes cash and produces no record of it.

So the question this module answers is not "is the disk healthy" in the
abstract. It is: **can we still write a sale?** Everything else is diagnosis.

`check()` is cheap enough to run on every reconcile pass: one small write, one
read of /proc/mounts, and a bounded scan of the kernel ring buffer.
"""
import os
import subprocess
import time

_MMC_PAT = ("mmc", "mmcblk", "EXT4-fs error", "I/O error", "Buffer I/O error",
            "remounting filesystem read-only", "critical medium error")


def _rootfs_readonly():
    """True if / is mounted read-only, straight from the kernel's own view."""
    try:
        with open("/proc/mounts") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == "/":
                    return "ro" in parts[3].split(",")
    except OSError:
        return False
    return False


def _can_write(path):
    """Actually write, fsync and delete a file where the database lives.

    Not os.access(): that asks about permission bits and cheerfully returns
    True on a filesystem that has gone read-only underneath. The only honest
    test of "can I write" is to write.
    """
    probe = os.path.join(path, ".write-probe")
    try:
        with open(probe, "w") as fh:
            fh.write(str(time.time()))
            fh.flush()
            os.fsync(fh.fileno())
        os.unlink(probe)
        return True, None
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _kernel_storage_errors(limit=5):
    """Recent kernel complaints about the card. Diagnosis, not the verdict."""
    try:
        r = subprocess.run(["dmesg", "--level=err,crit,alert,emerg", "--ctime"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return []
    except (OSError, subprocess.SubprocessError):
        return []
    hits = [ln.strip() for ln in r.stdout.splitlines()
            if any(p.lower() in ln.lower() for p in _MMC_PAT)]
    return hits[-limit:]


def check(db_dir):
    """Returns a dict; `ok` False means STOP TAKING MONEY.

    Keys: ok, writable, readonly, reason, kernel_errors, checked_at.
    """
    readonly = _rootfs_readonly()
    writable, why = _can_write(db_dir)
    errors = _kernel_storage_errors() if (readonly or not writable) else []

    reason = None
    if readonly:
        reason = ("The SD card has gone read-only. Sales cannot be recorded, "
                  "so the machine must not take any more coins until it is "
                  "fixed. This usually means a failing card or an unstable "
                  "power supply.")
    elif not writable:
        reason = ("Cannot write where the database lives (%s). Sales cannot be "
                  "recorded, so the machine must not take any more coins." % why)

    return {
        "ok": not readonly and writable,
        "writable": writable,
        "readonly": readonly,
        "reason": reason,
        "kernel_errors": errors,
        "checked_at": time.time(),
    }
