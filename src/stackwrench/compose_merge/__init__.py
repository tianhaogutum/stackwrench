"""Independent, bounded Compose merger. No runtime or environment resolution."""
from stackwrench import __version__

from .core import MergeError, Project, merge_files
from .compare import compare_files

__all__ = ["MergeError", "Project", "merge_files", "compare_files", "__version__"]
