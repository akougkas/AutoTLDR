"""AutoTLDR: point it at anything, get back what it means.

Nothing heavy is imported here. The package root is on the cold-start path of
every invocation, including ``autotldr --help``, and the startup contract in
tests/test_startup.py enforces that.
"""

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]
