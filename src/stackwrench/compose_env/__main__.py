import sys

from stackwrench.cli import main

raise SystemExit(main(["compose-env", *sys.argv[1:]]))
