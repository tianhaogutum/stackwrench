import sys

from stackwrench.cli import main

raise SystemExit(main(["compose-merge", *sys.argv[1:]]))
