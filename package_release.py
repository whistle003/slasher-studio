import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile

from studio_core import EXTRA_TABLES, TABLE, VERSION


HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
RELEASE=HERE/'release'
ENGINE_REV='375cbfb81ab2ff0db474d18184e29dc50b02d6e5'
METHOD_REV='208c7d641e665475bc385e8446e0189d7f407bc5'


def zip_paths(out, items, level=5):
    with ZipFile(out,'w',compression=ZIP_DEFLATED,compresslevel=level) as z:
        for source, name in items:
            z.write(source,name)


def data():
    repo=ROOT/'cod-name-db'/'csv'
    capture=ROOT/'mw7-snapshots'/'fresh-retail-with-aliases'
    files=((capture/n,f'MW7-Input-Pack/snapshots/{n}') for n in ('modwar7.ids','modwar7.pools.txt','sound_alias.raw64.txt.gz'))
    tables=((repo/(s+'.csv'),f'MW7-Input-Pack/cod-name-db/csv/{s}.csv') for s in sorted(set(TABLE.values())|EXTRA_TABLES))
    p=RELEASE/'MW7-Input-Pack.zip'
    zip_paths(p,[*files,*tables],4)
    print(p,p.stat().st_size,flush=True)


def app():
    dist=HERE/'.build/dist/SlasherStudio'
    if not (dist/'SlasherStudio.exe').is_file():
        raise FileNotFoundError('Build the PyInstaller app first')
    shutil.copy2(HERE/'README.md',dist/'README.zh-CN.md')
    shutil.copy2(HERE/'studio_vendor/LICENSE-MIT.txt',dist/'COD-Name-Finder-LICENSE.txt')
    engine_src=dist/'Hash-Slinging-Slasher-source.zip'
    subprocess.run(['git','-C',str(ROOT/'hash-slinging-slasher'),'archive','--format=zip',
                    '--prefix=hash-slinging-slasher/',f'--output={engine_src}',ENGINE_REV,
                    'LICENSE','Cargo.toml','Cargo.lock','src','data','.github/workflows'],check=True)
    methods_src=dist/'Hash-Slinging-Slasher-methods.zip'
    subprocess.run(['git','-C',str(ROOT/'hash-slinging-slasher'),'archive','--format=zip',
                    '--prefix=hash-slinging-slasher/',f'--output={methods_src}',METHOD_REV,
                    'LICENSE','scripts'],check=True)
    nameindex_src=dist/'COD-Name-DB-source.zip'
    repo=ROOT/'cod-name-db'
    zip_paths(nameindex_src,[(f,'cod-name-db/'+f.relative_to(repo).as_posix()) for f in
              [repo/'Cargo.toml',repo/'Cargo.lock',repo/'README.md',*sorted((repo/'src').glob('*.rs'))]])
    shutil.copy2(HERE/'METHOD_SOURCES.md',dist/'METHOD_SOURCES.md')
    shutil.copy2(HERE/'studio_peel.rs',dist/'studio_peel.rs')
    shutil.copy2(HERE/'build_peel.ps1',dist/'build_peel.ps1')
    if (HERE/'PERFORMANCE.md').is_file():shutil.copy2(HERE/'PERFORMANCE.md',dist/'PERFORMANCE.md')
    package_name=f'Slasher-Studio-{VERSION}'
    p=RELEASE/(package_name+'-win-x64.zip')
    zip_paths(p,[(f,package_name+'/'+f.relative_to(dist).as_posix()) for f in sorted(dist.rglob('*')) if f.is_file()])
    source=RELEASE/(package_name+'-source.zip')
    files=[x for x in HERE.iterdir() if x.is_file() and x.suffix in ('.py','.md','.ps1','.rs')
           and not x.name.startswith('benchmark_') and x.name!='verify_resume_112.py']
    files.extend(HERE/name for name in ('requirements.txt','requirements-build.txt'))
    files.extend(x for area in ['studio_vendor','assets'] for x in (HERE/area).rglob('*') if x.is_file() and '__pycache__' not in x.parts)
    zip_paths(source,[(x,'Slasher-Studio-source/'+x.relative_to(HERE).as_posix()) for x in files]+
              [(x,'Slasher-Studio-source/'+x.name) for x in (engine_src,methods_src,nameindex_src)])
    print(p,p.stat().st_size,source,source.stat().st_size,flush=True)


def checksums():
    rows=[]
    for file in sorted(RELEASE.glob('*.zip')):
        with file.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
        rows.append(f'{digest}  {file.name}')
    (RELEASE/'SHA256SUMS.txt').write_text('\n'.join(rows)+'\n',encoding='ascii')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('part',choices=['data','app','checksum'])
    RELEASE.mkdir(exist_ok=True)
    {'data':data,'app':app,'checksum':checksums}[parser.parse_args().part]()
