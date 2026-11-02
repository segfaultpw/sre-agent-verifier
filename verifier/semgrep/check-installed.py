#!/usr/bin/env python3
"""Compares the Python distributions installed in a site directory with a hash-locked
requirements file: the install is exactly the lock, nothing missing and nothing extra.

    check-installed.py LOCK SITEDIR [--ignore NAME]...

Standard library only. install-semgrep.sh runs it with the standalone Python it just installed
semgrep into, after pip and before pip is removed (hence --ignore pip). Exit 0 when the two
agree; otherwise 1, naming every difference on stderr.
"""

import importlib.metadata
import re
import sys


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def locked(path):
    pins = {}
    with open(path) as handle:
        for line in handle:
            match = re.match(r"([A-Za-z0-9._-]+)==(\S+)", line)
            if match:
                pins[normalize(match.group(1))] = match.group(2)
    return pins


def installed(site):
    found = {}
    for dist in importlib.metadata.distributions(path=[site]):
        name = normalize(dist.metadata["Name"])
        if name in found:
            found[name] += "," + dist.version
        else:
            found[name] = dist.version
    return found


def main(argv):
    if len(argv) < 2:
        print("usage: check-installed.py LOCK SITEDIR [--ignore NAME]...", file=sys.stderr)
        return 2
    lock, site = argv[0], argv[1]
    ignore = set()
    rest = argv[2:]
    while rest:
        if rest[0] != "--ignore" or len(rest) < 2:
            print("usage: check-installed.py LOCK SITEDIR [--ignore NAME]...", file=sys.stderr)
            return 2
        ignore.add(normalize(rest[1]))
        rest = rest[2:]

    want = locked(lock)
    if not want:
        print("%s pins nothing" % lock, file=sys.stderr)
        return 1
    have = {name: version for name, version in installed(site).items() if name not in ignore}

    problems = []
    for name in sorted(want.keys() - have.keys()):
        problems.append("missing: %s==%s" % (name, want[name]))
    for name in sorted(have.keys() - want.keys()):
        problems.append("not in the lock: %s==%s" % (name, have[name]))
    for name in sorted(want.keys() & have.keys()):
        if want[name] != have[name]:
            problems.append("%s is %s, the lock pins %s" % (name, have[name], want[name]))
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
