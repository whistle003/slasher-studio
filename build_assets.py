from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from studio_community import PR_PATH

here=Path(__file__).resolve().parent
root=here.parent
repo=root/'hash-slinging-slasher'
sys.path.insert(0,str(root))
from build_mw7_pr_exclusions import candidates_from_diff


def git(*words):
    return subprocess.check_output(['git','-C',str(repo),*words])


def blob(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def build():
    path=here/'assets/community.zip'
    index={}
    diffs=sorted((root/'mw7-pr-dedupe/source/diffs').glob('*.diff'),key=lambda p:int(p.stem))
    if len(diffs)!=21:
        raise RuntimeError('expected the pinned 21-diff survey')
    with ZipFile(path,'w',compression=ZIP_DEFLATED,compresslevel=3) as z:
        for entity in git('ls-tree','-rz','HEAD','--','submissions').decode().split('\0'):
            if not entity: continue
            meta,name=entity.split('\t',1)
            if not PR_PATH.fullmatch(name):continue
            mode,typ,sha=meta.split()
            data=(repo/name).read_bytes()
            if blob(data)!=sha:
                data=data.replace(b'\r\n',b'\n')
            if blob(data)!=sha:
                raise ValueError(f'changed source file: {name}')
            index[name]=sha
            z.writestr('merged/'+name,data)
        for p in diffs:
            candidates_from_diff(p)  # checks hunk boundaries before archiving
            current=None
            added={}
            for line in p.read_text(encoding='utf-8').splitlines():
                if line.startswith('diff --git '):
                    current=None
                elif line.startswith('+++ b/'):
                    name=line[6:]
                    current=name if PR_PATH.fullmatch(name) else None
                elif current and re.match(r'^\+[0-9a-fA-F]{1,16},',line):
                    added.setdefault(current,[]).append(line[1:])
            for name,lines in added.items():
                z.writestr(f'open/{p.stem}/{name}',('\n'.join(lines)+'\n').encode())
        summary={'commit':git('rev-parse','HEAD').decode().strip(),'date':'2026-11-17 19:27（UTC+8）',
                 'open':len(diffs),'merged_blobs':index,'model_submissions':True}
        z.writestr('survey.json',json.dumps(summary))
    print(path,len(index), path.stat().st_size)


if __name__=='__main__':build()
