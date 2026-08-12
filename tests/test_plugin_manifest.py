import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins/zlib-skill/scripts"))
import validate_plugin_manifest  # noqa: E402


def test_plugin_manifest_validates():
    assert validate_plugin_manifest.main(str(Path(__file__).parent.parent)) == 0
