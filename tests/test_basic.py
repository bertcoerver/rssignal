"""Basic import and version tests."""

import rssignal


def test_version():
    """Test that version is defined."""
    assert hasattr(rssignal, "__version__")
    assert isinstance(rssignal.__version__, str)
