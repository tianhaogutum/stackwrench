import sys

from stackwrench.cli import main

raise SystemExit(main(["podman-audit", *sys.argv[1:]]))
