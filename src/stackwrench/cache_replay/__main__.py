import sys

from stackwrench.cli import main

raise SystemExit(main(["cache-replay", *sys.argv[1:]]))
