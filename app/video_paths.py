"""Read physical names from locators without changing source/parser evidence."""
from pathlib import PurePosixPath

from .models import Video, UnresolvedExternalSubtitle


def current_physical_filename(asset: Video | UnresolvedExternalSubtitle) -> str:
    """Source filename stays historical during V6; relative_path locates the file."""
    return PurePosixPath(asset.relative_path).name
