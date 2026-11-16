"""Offline, conservative just → Task translation. No recipe execution."""

from stackwrench import __version__

from .core import Analysis, analyze, render_dot, render_taskfile

__all__ = ["Analysis", "analyze", "render_dot", "render_taskfile", "__version__"]
