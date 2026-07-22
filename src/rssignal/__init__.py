"""rssignal package."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("rssignal")
except PackageNotFoundError:
    __version__ = "unknown"
