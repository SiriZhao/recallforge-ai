"""Read-only history audit; never prints matched secret values or private text."""
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def audit(ref='HEAD',private_root=None):
    from dotenv import dotenv_values
    values=dotenv_values(Path(__file__).resolve().parents[1]/'backend'/'.env')
    keys=[value.encode() for name,value in values.items() if value and ('API_KEY' in name or name.endswith('_TOKEN'))]
    private_hashes={hashlib.sha256(path.read_bytes()).digest() for path in private_root.rglob('*') if path.is_file()} if private_root else set()
    entries={}
    for line in subprocess.check_output(['git','rev-list','--objects',ref],text=True,encoding='utf-8').splitlines():
        identity,_,path=line.partition(' ');entries[identity]=path
    historic_paths={path.strip('\n') for path in subprocess.check_output(['git','log',ref,'--format=','--name-only','-z'],text=True,encoding='utf-8').split('\0') if path.strip('\n')}
    for entry in subprocess.check_output(['git','ls-files','--stage','-z'],text=True,encoding='utf-8').split('\0'):
        if not entry:continue
        metadata,_,path=entry.partition('\t');identity=metadata.split()[1]
        entries[identity]=path;historic_paths.add(path)
    packed=subprocess.check_output(['git','cat-file','--batch'],input=('\n'.join(entries)+'\n').encode())
    patterns=[rb'sk-[A-Za-z0-9_-]{24,}',rb'gh[pousr]_[A-Za-z0-9]{30,}',rb'github_pat_[A-Za-z0-9_]{40,}',rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----']
    position=0;blobs=0;findings=[]
    def sensitive(path):
        return any(part in {'.env','.venv','node_modules','uploads','runtime','workspace.db','dist','build'} for part in path.lower().split('/')) or path.lower().endswith(('.sqlite3','.db','.exe'))
    # Path history is checked independently of blob IDs: two filenames may share
    # one blob, and rev-list --objects need not emit every historical alias.
    allowed_scaffolds=[]
    for path in sorted(historic_paths):
        if not sensitive(path):continue
        if path=='backend/uploads/.gitkeep':
            raw_changes=subprocess.check_output(['git','log',ref,'--format=','--raw','--no-abbrev','--',path],text=True).splitlines()
            versions={value for line in raw_changes if line.startswith(':') for value in line.split() [2:4] if set(value)!={'0'}}
            versions.update(identity for identity,name in entries.items() if name==path)
            contents=[subprocess.check_output(['git','cat-file','blob',identity]) for identity in versions]
            if contents and all(len(content)<=16 and not content.strip() for content in contents):
                allowed_scaffolds.append(path);continue
        findings.append({'path':path,'reason':'sensitive_or_generated_path'})
    while position<len(packed):
        end=packed.index(b'\n',position);identity,kind,size=packed[position:end].decode().split();size=int(size)
        content=packed[end+1:end+size+1];position=end+size+2;path=entries[identity]
        if kind not in {'blob','commit'}:continue
        if any(key in content for key in keys) or any(re.search(pattern,content) for pattern in patterns):
            findings.append({'object':identity,'path':path,'reason':'credential_content_pattern'})
        if kind!='blob':continue
        blobs+=1
        if hashlib.sha256(content).digest() in private_hashes:
            findings.append({'object':identity,'path':path,'reason':'private_file_content'})
    return {'ref':ref,'reachable_objects':len(entries),'historical_blobs':blobs,'configured_secrets_checked':len(keys),
        'private_file_hashes_checked':len(private_hashes),'findings':findings,'verified_empty_scaffolds':allowed_scaffolds,
        'limitation':'Exact-secret, credential-pattern, path and file-hash checks; not a proof that every possible secret or transformed private excerpt is absent.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--ref',default='HEAD');parser.add_argument('--private-root',type=Path)
    args=parser.parse_args()
    if args.private_root and not args.private_root.is_dir():raise SystemExit('Private input directory is not accessible; audit stopped')
    result=audit(args.ref,args.private_root)
    print(json.dumps(result,ensure_ascii=True,indent=2));raise SystemExit(bool(result['findings']))
