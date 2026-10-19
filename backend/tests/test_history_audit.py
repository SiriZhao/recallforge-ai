import runpy
import subprocess
from pathlib import Path


def test_history_audit_checks_staged_aliases_not_only_reachable_blob_paths(tmp_path,monkeypatch):
    audit=runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts'/'audit-publish-history.py'))['audit']
    monkeypatch.setattr('dotenv.dotenv_values',lambda _: {})
    monkeypatch.chdir(tmp_path)
    def git(*args):return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL)
    git('init')
    uploads=tmp_path/'backend'/'uploads';uploads.mkdir(parents=True)
    (uploads/'.gitkeep').write_bytes(b'\n');git('add','backend/uploads/.gitkeep')
    git('-c','user.name=Audit Test','-c','user.email=audit@example.invalid','commit','-m','empty scaffold')
    assert audit()['findings']==[]
    # Same blank blob, different prohibited filename: object dedup must not hide it.
    (tmp_path/'.env').write_bytes(b'\n');git('add','.env')
    assert any(item['path']=='.env' for item in audit()['findings'])


def test_history_audit_does_not_whitelist_nonempty_upload_placeholders(tmp_path,monkeypatch):
    audit=runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts'/'audit-publish-history.py'))['audit']
    monkeypatch.setattr('dotenv.dotenv_values',lambda _: {})
    monkeypatch.chdir(tmp_path)
    def git(*args):return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL)
    git('init');uploads=tmp_path/'backend'/'uploads';uploads.mkdir(parents=True)
    (uploads/'.gitkeep').write_text('Private course content',encoding='utf-8');git('add','.')
    git('-c','user.name=Audit Test','-c','user.email=audit@example.invalid','commit','-m','fixture')
    assert any(item['path']=='backend/uploads/.gitkeep' for item in audit()['findings'])
