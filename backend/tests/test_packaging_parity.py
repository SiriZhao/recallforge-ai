"""The provenance checker must distinguish path changes from real stale code."""
import runpy
from pathlib import Path


def test_archive_code_signature_ignores_filenames_but_detects_semantic_changes():
    signature=runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts'/'verify-packaged-engine.py'))['signature']
    source='def answer(value):\n    return value + 1\n'
    assert signature(compile(source,'build/path.py','exec'))==signature(compile(source,'local/path.py','exec'))
    assert signature(compile(source,'same.py','exec'))!=signature(compile(source.replace('+ 1','+ 2'),'same.py','exec'))
