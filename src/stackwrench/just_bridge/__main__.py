import sys

from stackwrench.cli import main

raise SystemExit(main(["just", *sys.argv[1:]]))
