import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "packages", "py-core"))

from apps.cli.runtime.paths import PathComposer
from apps.cli.runtime.autonomy import ExplorationMemory


def test_path_normalize_double_slash():
    p = PathComposer.compose(".", "app/api/issues//route.ts")
    assert "//" not in p
    assert "route.ts" in p


def test_exploration_memory_list_dir_patterns():
    em = ExplorationMemory()
    assert em.get_list_dir_path("Get-ChildItem -Path app\\api\\issues") is not None
    assert em.get_list_dir_path("dir app\\api\\issues /s /b") is not None
    assert em.get_list_dir_path("ls app/api/issues") == "app/api/issues"
    assert em.get_list_dir_path("npm run build") is None


if __name__ == "__main__":
    test_path_normalize_double_slash()
    test_exploration_memory_list_dir_patterns()
    print("OK: path + exploration memory tests passed")
