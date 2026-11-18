"""Discover prior Studio exports and combine them with the user's original Saluki library."""
import csv,gzip,hashlib,json,os,sys,tempfile,shutil
from pathlib import Path

import studio_core as core
from studio_pipeline import export_names,convert
from studio_sources import atomic_bytes,capture_identity

SKIP={'.git','.tools','.build','release','benchmarks','engine','methods','sources','public-sync','known-cdb','live.previous','__pycache__'}


def roots_for(ctx,profile,task):
    roots={ctx.out,task.parent}
    registered=profile/'history-roots.json'
    if registered.exists():
        for p in json.loads(registered.read_text(encoding='utf-8')):
            if Path(p).is_dir():roots.add(Path(p))
    settings=profile/'settings.json'
    if settings.exists():
        data=json.loads(settings.read_text(encoding='utf-8'))
        if data.get('output'):roots.add(Path(data['output']))
    here=Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent
    for parent in [here,*here.parents]:
        if (parent/'slasher-studio').is_dir() and (parent/'cod-name-db').is_dir():
            roots.update((parent/'slasher-studio'/'runs',parent/'slasher-studio'/'recovery'))
            roots.update(parent.glob('*-名称库'));roots.update(parent.glob('*-查名结果'))
            # Older exports also lived directly under the Studio project.
            roots.update((parent/'slasher-studio').glob('task-*'))
            break
    return {p.resolve() for p in roots if p.is_dir()}


def discover(ctx,profile,task,latest=None,check=lambda:None):
    roots=roots_for(ctx,profile,task)
    cdbs=set();models=set()
    names={stem+'.cdb' for kind,stem in core.TABLE.items() if kind in ctx.pools}
    if 'xmodel' in ctx.pools:names.update(('fnv1a_xmodels.cdb','fnv1a_xmodels_v2.cdb'))
    for folder in (ctx.saluki_dir,latest):
        if folder:
            cdbs.update(p.resolve() for name in names if (p:=folder/name).is_file())
    current=task.resolve()
    for origin in sorted(roots):
        for folder,dirs,files in os.walk(origin,followlinks=False):
            check();p=Path(folder)
            dirs[:]=[name for name in dirs if name not in SKIP and not name.startswith('.') and
                     'verification' not in name and 'packaged-check' not in name and
                     not (p/name).is_symlink() and not (p/name).is_relative_to(current)]
            if p.is_relative_to(current):dirs[:]=[];continue
            if len(p.relative_to(origin).parts)>9:dirs[:]=[];continue
            cdbs.update((p/name).resolve() for name in files if name in names)
            if 'xmodel' in ctx.pools and 'new_names.csv' in files:models.add((p/'new_names.csv').resolve())
    return roots,sorted(cdbs),sorted(models)


def prepare(ctx,profile,task,latest,log,check=lambda:None):
    roots,files,model_files=discover(ctx,profile,task,latest,check)
    tables=[ctx.table_dir/(stem+'.csv') for kind,stem in core.TABLE.items() if kind in ctx.pools]
    if 'xmodel' in ctx.pools:tables.append(ctx.table_dir/'fnv1a_xmodels.csv')
    if 'xmodel' in ctx.pools:tables.append(ctx.table_dir/(core.TABLE['material']+'.csv'))
    entries=[]
    for p in sorted(set(files+model_files+tables)):
        if p.is_file():
            stat=p.stat();entries.append([str(p),stat.st_size,stat.st_mtime_ns])
    signature=hashlib.sha256(json.dumps([capture_identity(ctx),sorted(ctx.pools),entries],ensure_ascii=False).encode()).hexdigest()
    base=profile/'known-cdb';base.mkdir(parents=True,exist_ok=True)
    dest=base/signature
    if dest.exists() and (not (dest/'models.json.gz').is_file() or any(
            not (dest/'saluki-ready/hash_pkg'/(core.TABLE[k]+'.cdb')).is_file() for k in ctx.pools if k in core.TABLE)):
        raise ValueError(f'自动合库缓存不完整：{dest}；请保留原库并移走这个缓存目录后重试。')
    if not (dest/'complete.json').exists():
        log(f'运行前自动合库：发现 {len(files)} 份原库/历史 CDB，{len(model_files)} 份模型结果。')
        with tempfile.TemporaryDirectory(dir=base,prefix='.merge-') as temp:
            stage=Path(temp)/'result';stage.mkdir()
            for kind,stem in core.TABLE.items():
                if kind not in ctx.pools:continue
                check()
                values=dict(core.names_csv(ctx.table_dir/(stem+'.csv')))
                chosen=[p for p in files if p.name==stem+'.cdb']
                # Original Saluki spelling wins on collisions; every stored key survives.
                chosen.sort(key=lambda p:(p.parent==ctx.saluki_dir,p.stat().st_mtime_ns))
                seen_bytes=set()
                for p in chosen:
                    check()
                    with p.open('rb') as f:digest=hashlib.file_digest(f,'sha256').digest()
                    if digest in seen_bytes:continue
                    seen_bytes.add(digest);values.update(core.read_cdb(p))
                txt=export_names(stage,kind,{})
                convert(txt,stage/'merged-csv'/(stem+'.csv'),stage/'saluki-ready/hash_pkg'/(stem+'.cdb'),values,log)
                log(f'自动名称底库 / {core.LABEL[kind]}：{len(values):,} 条，保留原库及历史结果。')
            models={}
            if 'xmodel' in ctx.pools:
                class Check:
                    def check(self):check()
                for key,name in core.seed_rows(ctx,'xmodel',Check()):
                    if core.matches(ctx,'xmodel',key,name):models[key]=name
                # Legacy model tables use a different hash. Rehash names using
                # the current game's policy, then require model-pool membership.
                for p in files:
                    if p.name not in ('fnv1a_xmodels.cdb','fnv1a_xmodels_v2.cdb'):continue
                    for i,name in enumerate(core.read_cdb(p).values()):
                        if i%8192==0:check()
                        key=core.name_key(name,'xmodel')
                        if core.matches(ctx,'xmodel',key,name):models[key]=name
                for p in model_files:
                    with p.open(encoding='utf-8-sig',newline='') as f:
                        for i,row in enumerate(csv.DictReader(f)):
                            if i%8192==0:check()
                            if row.get('type')!='xmodel':continue
                            try:key=int(row['hash'],16);name=row['name']
                            except (KeyError,ValueError):continue
                            if core.matches(ctx,'xmodel',key,name):models[key]=name
            atomic_bytes(stage/'models.json.gz',gzip.compress(json.dumps(sorted(models)).encode()))
            check()
            atomic_bytes(stage/'complete.json',json.dumps({'sources':entries,'models':len(models)},ensure_ascii=False,indent=2).encode())
            stage.rename(dest)
    info=json.loads((dest/'complete.json').read_text(encoding='utf-8'))
    with gzip.open(dest/'models.json.gz','rt') as f:models=json.load(f)
    atomic_bytes(profile/'history-roots.json',json.dumps(sorted(map(str,roots)),ensure_ascii=False,indent=2).encode())
    log(f'自动合库就绪：{len(files)} 份原库/历史 CDB，已知模型 {info["models"]:,} 个；无需切换 Saluki 目录。')
    return dest/'saluki-ready/hash_pkg',{('xmodel',int(k)) for k in models},info


def complete_export(ctx,out,latest):
    """Keep the full merged library usable even when a type has no new hits."""
    if not latest or not (latest.parents[1]/'complete.json').is_file():return
    base=latest.parents[1]
    record=out/'pipeline.json'
    manifest=json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
    for kind,stem in core.TABLE.items():
        if kind not in ctx.pools:continue
        target=out/'saluki-ready/hash_pkg'/(stem+'.cdb')
        if target.exists():continue
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(latest/(stem+'.cdb'),target)
        csv_path=out/'merged-csv'/(stem+'.csv');csv_path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(base/'merged-csv'/(stem+'.csv'),csv_path)
        txt=export_names(out,kind,{})
        manifest[kind]=dict(txt=str(txt.relative_to(out)),csv=str(csv_path.relative_to(out)),
            cdb=str(target.relative_to(out)),converter='cod-name-db/NameIndex',verified=True,source='automatic merged library')
    record.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
