import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "catalyst"))

from _version import _describe_to_version


def test_describe_exact_release_tag():
    assert _describe_to_version("v1.2.16") == "1.2.16"


def test_describe_commits_after_release_tag():
    assert _describe_to_version("v1.2.16-1-gd514e75") == "1.2.16+1.gd514e75"


def test_describe_dirty_source_checkout():
    assert (
        _describe_to_version("v1.2.16-1-gd514e75-dirty")
        == "1.2.16+1.gd514e75.dirty"
    )
