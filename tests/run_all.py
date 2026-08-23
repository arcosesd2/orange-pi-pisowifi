"""Run every test in this folder and summarise.

These are standalone scripts, not pytest modules -- each calls sys.exit() at
module level, which makes pytest abort collection with an INTERNALERROR and run
nothing at all. That is why a plain `pytest tests` reports success while
testing nothing. Run this instead:

    python tests/run_all.py

Each test is executed in its own interpreter so that one module's global state
(the shared dev.db, the coin slot's background thread) cannot leak into the
next and produce results that depend on ordering.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Ordering matters only in that the slow ones go last, so a quick mistake
# surfaces quickly.
TESTS = [
    "test_db.py",
    "test_security.py",
    "test_api_limits.py",
    "test_wallet_flow.py",
    "test_smoke.py",
    "test_reachability.py",
    "test_functional.py",
]


def main():
    results, width = [], max(len(t) for t in TESTS)
    for name in TESTS:
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            print("  %-*s  SKIP (missing)" % (width, name))
            continue
        t0 = time.time()
        p = subprocess.run([sys.executable, path], capture_output=True, text=True)
        dt = time.time() - t0
        passed = p.returncode == 0
        results.append((name, passed, p))
        print("  %-*s  %-4s  %5.1fs" % (width, name, "PASS" if passed else "FAIL", dt))

    bad = [(n, p) for n, ok_, p in results if not ok_]
    print("\n%d/%d suites passed" % (len(results) - len(bad), len(results)))

    # Warnings are not failures, but they are the things a human should read.
    for name, _ok, p in results:
        for line in (p.stdout or "").splitlines():
            if line.strip().startswith("~ "):
                print("  WARN %s: %s" % (name, line.strip()[2:]))

    for name, p in bad:
        print("\n" + "=" * 70)
        print("FAILED: %s" % name)
        print("=" * 70)
        tail = (p.stdout or "").strip().splitlines()[-40:]
        print("\n".join(tail))
        if p.stderr.strip():
            print("--- stderr ---")
            print("\n".join(p.stderr.strip().splitlines()[-20:]))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
