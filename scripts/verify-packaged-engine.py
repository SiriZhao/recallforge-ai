"""Read-only verification of compiled engine/source and frontend archive parity.

Does not launch the desktop executable or touch user application data. Ignores
bytecode filename/line-table differences; compares code, constants and bindings.
This is build provenance verification, not Windows runtime/installer acceptance.
"""
import argparse
import json
import types
from pathlib import Path


def signature(value):
    if isinstance(value,types.CodeType):
        fields=('co_code','co_names','co_varnames','co_freevars','co_cellvars',
            'co_argcount','co_posonlyargcount','co_kwonlyargcount','co_flags','co_exceptiontable')
        return tuple(getattr(value,name) for name in fields),tuple(signature(item) for item in value.co_consts)
    if isinstance(value,tuple):return tuple(signature(item) for item in value)
    return value


def verify(executable,root):
    from PyInstaller.archive.readers import CArchiveReader
    archive=CArchiveReader(str(executable))
    pyz=archive.open_embedded_archive(next(name for name in archive.toc if name.endswith('.pyz')))
    compared=[];mismatches=[];data_only=[]
    app=root/'backend'/'app'
    for source in sorted(app.rglob('*.py')):
        parts=list(source.relative_to(app).with_suffix('').parts)
        if parts[-1]=='__init__':parts.pop()
        name='.'.join(['app',*parts])
        if name not in pyz.toc:
            data_only.append(name);continue
        actual=pyz.extract(name)
        expected=compile(source.read_bytes(),str(source),'exec',dont_inherit=True,optimize=0)
        compared.append(name)
        if signature(actual)!=signature(expected):mismatches.append(name)
    entries={name.replace('\\','/'):name for name in archive.toc}
    frontend_bad=[];frontend_count=0
    for source in sorted((root/'frontend'/'dist').rglob('*')):
        if not source.is_file():continue
        frontend_count+=1
        name='frontend/dist/'+source.relative_to(root/'frontend'/'dist').as_posix()
        if name not in entries or archive.extract(entries[name])!=source.read_bytes():frontend_bad.append(name)
    sensitive=[name for name in entries if name.lower().endswith(('.env','.sqlite3','workspace.db'))]
    required={'app.main','app.routers.learning','app.services.generation_pipeline','app.services.artifact_validation',
        'app.services.artifact_repair','app.services.course_budget','app.services.learning_assessment'}
    missing=sorted(required-set(compared))
    result={'compiled_modules_checked':len(compared),'compiled_mismatches':mismatches,'required_modules_missing':missing,
        'frontend_assets_checked':frontend_count,'frontend_mismatches':frontend_bad,'sensitive_file_entries':sensitive,
        'data_only_modules':data_only,'exe_bytes':executable.stat().st_size,
        'runtime_acceptance':'NOT_TESTED','installer_acceptance':'NOT_TESTED'}
    result['archive_parity_verified']=not(mismatches or missing or frontend_bad or sensitive) and frontend_count>0
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('executable',type=Path)
    args=parser.parse_args()
    report=verify(args.executable.resolve(),Path(__file__).resolve().parents[1])
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if report['archive_parity_verified'] else 1)
