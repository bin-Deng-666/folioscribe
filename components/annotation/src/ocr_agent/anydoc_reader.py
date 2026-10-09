from __future__ import annotations

import os
import sys
from pathlib import Path


def read_anydoc(path: str | Path, package_dir: Path | None = None) -> str:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    configured = package_dir or os.environ.get("ANYDOC_PACKAGE_DIR")
    if configured:
        package = str(Path(configured).expanduser())
        if package not in sys.path:
            sys.path.insert(0, package)
    import anydoc

    markdown = anydoc.to_markdown(str(source))
    if not isinstance(markdown, str):
        markdown = str(markdown)
    return markdown
