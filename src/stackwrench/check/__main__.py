import sys

from stackwrench.cli import main

raise SystemExit(main(["check", *sys.argv[1:]]))
