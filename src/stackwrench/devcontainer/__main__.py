import sys

from stackwrench.cli import main

raise SystemExit(main(["devcontainer", *sys.argv[1:]]))
