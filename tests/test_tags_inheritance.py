import os
import sys
import types
from pathlib import Path

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Create minimal dummy modules so exif_extractor_gui can be imported without heavy dependencies.
class _DummyType(type):
    pass

class _QtModule(types.ModuleType):
    def __getattr__(self, name):
        if name == "pyqtSignal":
            def _sig(*a, **k):
                return lambda *a, **k: None
            return _sig
        if name == "pyqtSlot":
            def _slot(*a, **k):
                def decorator(fn):
                    return fn
                return decorator
            return _slot
        return _DummyType(name, (), {})

def _setup_dummy_modules():
    pyqt6 = types.ModuleType("PyQt6")
    for sub in ["QtWidgets", "QtCore", "QtGui", "QtWebEngineWidgets", "QtWebChannel"]:
        mod = _QtModule(f"PyQt6.{sub}")
        setattr(pyqt6, sub, mod)
        sys.modules[f"PyQt6.{sub}"] = mod
    sys.modules.setdefault("PyQt6", pyqt6)

    class _DummyModule(types.ModuleType):
        def __getattr__(self, name):
            return _DummyType(name, (), {})

    for mod in [
        "PIL", "PIL.Image", "PIL.ExifTags", "geopy", "geopy.distance",
        "plotly", "plotly.graph_objects", "plotly.subplots", "folium"
    ]:
        sys.modules.setdefault(mod, _DummyModule(mod))

    workers_dummy = types.ModuleType("workers")
    class _Worker:
        pass
    workers_dummy.ExifExtractorWorker = _Worker
    workers_dummy.BatchProcessWorker = _Worker
    workers_dummy.RadiusSearchWorker = _Worker
    sys.modules.setdefault("workers", workers_dummy)

_setup_dummy_modules()

import exif_extractor_gui as gui


def test_find_applicable_tags_inheritance(tmp_path):
    """tags.config files should merge from root to child directories."""
    root = tmp_path
    (root / "tags.config").write_text("#root: R\n#shared: R\n")
    (root / "image_root.jpg").write_text("")

    sub1 = root / "sub1"
    sub1.mkdir()
    (sub1 / "tags.config").write_text("#a1: A1\n#shared: A\n")
    (sub1 / "image_sub1.jpg").write_text("")

    sub2 = sub1 / "sub2"
    sub2.mkdir()
    (sub2 / "tags.config").write_text("#shared: B\n#b1: B1\n")
    (sub2 / "image_sub2.jpg").write_text("")

    sub3 = sub1 / "sub3"
    sub3.mkdir()
    (sub3 / "image_sub3.jpg").write_text("")

    tags_root = gui.find_applicable_tags(str(root / "image_root.jpg"))
    tags_sub1 = gui.find_applicable_tags(str(sub1 / "image_sub1.jpg"))
    tags_sub2 = gui.find_applicable_tags(str(sub2 / "image_sub2.jpg"))
    tags_sub3 = gui.find_applicable_tags(str(sub3 / "image_sub3.jpg"))

    assert tags_root == {"Tag_root": "R", "Tag_shared": "R"}
    assert tags_sub1 == {"Tag_root": "R", "Tag_shared": "A", "Tag_a1": "A1"}
    assert tags_sub2 == {
        "Tag_root": "R",
        "Tag_a1": "A1",
        "Tag_shared": "B",
        "Tag_b1": "B1",
    }
    assert tags_sub3 == {"Tag_root": "R", "Tag_shared": "A", "Tag_a1": "A1"}
