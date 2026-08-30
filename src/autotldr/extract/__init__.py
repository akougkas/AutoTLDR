"""Format extractors.

Each module exposes ``extract(path: Path) -> Extraction`` and imports its own
heavy dependencies at module scope. That is safe because the router imports the
module only when a file of that format is actually present.
"""
