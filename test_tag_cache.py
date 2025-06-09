import os
import sys
import types
from pathlib import Path
from typing import List

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(__file__))

# Create minimal dummy modules so exif_extractor_gui can be imported without its
# heavy GUI dependencies.
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
    class _Worker:  # minimal stand-in for PyQt-based workers
        pass
    workers_dummy.ExifExtractorWorker = _Worker
    workers_dummy.BatchProcessWorker = _Worker
    workers_dummy.RadiusSearchWorker = _Worker
    sys.modules.setdefault("workers", workers_dummy)

_setup_dummy_modules()

import exif_extractor_gui as gui


def test_process_tags_for_batch_uses_cache(tmp_path, monkeypatch):
    """parse_tags_config should only be called once per unique path."""
    folder1 = tmp_path / "folder1"
    folder1.mkdir()
    (folder1 / "tags.config").write_text("#tag1: v1\n")
    img1 = folder1 / "img1.jpg"
    img2 = folder1 / "img2.jpg"
    img1.write_text("")
    img2.write_text("")

    folder2 = tmp_path / "folder2"
    folder2.mkdir()
    (folder2 / "tags.config").write_text("#tag2: v2\n")
    img3 = folder2 / "img3.jpg"
    img4 = folder2 / "img4.jpg"
    img3.write_text("")
    img4.write_text("")

    call_paths: List[str] = []
    original_parse = gui.parse_tags_config

    def counting_parse(path: str):
        call_paths.append(path)
        return original_parse(path)

    monkeypatch.setattr(gui, "parse_tags_config", counting_parse)

    gui.process_tags_for_batch([
        str(img1), str(img2), str(img3), str(img4)
    ], str(tmp_path / "dummy.db"))

    assert call_paths.count(str(folder1 / "tags.config")) == 1
    assert call_paths.count(str(folder2 / "tags.config")) == 1
