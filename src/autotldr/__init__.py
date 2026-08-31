"""AutoTLDR: point it at anything, get back what it means.

Nothing heavy is imported here. The package root is on the cold-start path of
every invocation, including ``autotldr --help``, and the startup contract in
tests/test_startup.py enforces that.
"""

from ._version import __version__


def acquire(*args, **kwargs):
    """Lazily acquire/fuse sources through :mod:`autotldr.api`."""

    from .api import acquire as _acquire

    return _acquire(*args, **kwargs)


def summarize(*args, **kwargs):
    """Lazily run AutoTLDR's composable public pipeline."""

    from .api import summarize as _summarize

    return _summarize(*args, **kwargs)


def summarize_product(*args, **kwargs):
    """Lazily run the configured first-user product pipeline."""

    from .api import summarize_product as _summarize_product

    return _summarize_product(*args, **kwargs)


__all__ = ["__version__", "acquire", "summarize", "summarize_product"]
