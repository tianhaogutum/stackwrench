import sys

from stackwrench.cli import main

raise SystemExit(main(["task-audit", *sys.argv[1:]]))
