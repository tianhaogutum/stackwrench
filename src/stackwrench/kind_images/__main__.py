import sys

from stackwrench.cli import main

raise SystemExit(main(["kind-images", *sys.argv[1:]]))
