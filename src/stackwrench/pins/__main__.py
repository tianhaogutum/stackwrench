import sys

from stackwrench.cli import main

raise SystemExit(main(["pins", *sys.argv[1:]]))
