"""Persistent live PR survey and official Saluki package cache."""

from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib import parse, request
from zipfile import ZipFile

import studio_core as core


RELEASE_PAGE = 'https://github.com/echo000/cod-name-db/releases/latest'
LIMIT = 512 * 1024**2


def atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        f.write(data)
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def capture_identity(ctx):
    h = hashlib.sha256()
    for p in (ctx.path, ctx.path.with_suffix('.pools.txt')):
        with p.open('rb') as f:
            h.update(hashlib.file_digest(f, 'sha256').digest())
    for key in sorted(ctx.raw):
        h.update(key.to_bytes(8,'little'))
    return h.hexdigest()


def survey_path(root, ctx, identity=None):
    identity = identity or capture_identity(ctx)
    kinds = '-'.join(sorted(ctx.pools))
    return root/'surveys'/f'{identity}-{kinds}.json.gz'


def save_survey(root, ctx, rows, stamp):
    atomic_bytes(survey_path(root,ctx),gzip.compress(json.dumps({'stamp':stamp,'rows':sorted(rows)},separators=(',',':')).encode(),compresslevel=3))


def read_survey(root, ctx, identity=None):
    path = survey_path(root,ctx,identity)
    if not path.is_file():
        return None
    with gzip.open(path,'rt',encoding='utf-8') as stream:
        data=json.load(stream)
    return {(k,int(key)) for k,key in data['rows'] if k in ctx.pools},data['stamp']


def latest_cdb(root):
    p = root/'cdb'/'latest.json'
    if not p.is_file():
        return None, ''
    item=json.loads(p.read_text(encoding='utf-8'))
    digest=item['sha256']
    if not re.fullmatch(r'[0-9a-f]{64}',digest):
        raise ValueError('CDB 同步缓存索引不正确。请重新同步。')
    folder = p.parent/digest
    if not all((folder/(core.TABLE[k]+'.cdb')).is_file() for k in core.DEFAULT_KINDS):
        raise ValueError('缓存的 CDB 不完整。请重新同步。')
    return folder,item['tag']


class OfficialRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = parse.urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.hostname not in {'github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}:
            raise ValueError('官方 CDB 下载跳转到不受支持的来源。')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch_latest_cdb(root, log, opener=None):
    opener = opener or request.build_opener(OfficialRedirect())
    headers={'User-Agent':'Slasher-Studio','Accept-Encoding':'identity'}
    with opener.open(request.Request(RELEASE_PAGE,headers=headers),timeout=45) as response:
        url=response.geturl()
    match=re.fullmatch(r'https://github\.com/echo000/cod-name-db/releases/tag/([A-Za-z0-9._-]{1,80})',url)
    if not match:
        raise ValueError('无法确认官方 CDB 发布版本。')
    tag=match[1]
    try: existing,old=latest_cdb(root)
    except (ValueError,KeyError,OSError): existing,old=None,''
    if existing and old==tag and (existing/(core.TABLE['animpkg']+'.cdb')).is_file():
        log(f'已是官方 CDB {tag}。')
        return tag
    cache=root/'cdb'
    cache.mkdir(parents=True,exist_ok=True)
    address=f'https://github.com/echo000/cod-name-db/releases/download/{tag}/hash_pkg.zip'
    log(f'正在下载官方 CDB {tag}…')
    with tempfile.TemporaryDirectory(dir=cache,prefix='download-') as directory:
        temp=Path(directory)
        archive=temp/'hash_pkg.zip'
        h=hashlib.sha256(); size=0
        with opener.open(request.Request(address,headers=headers),timeout=60) as response, archive.open('xb') as file:
            while data:=response.read(1024*1024):
                size+=len(data)
                if size>LIMIT:raise ValueError('官方 CDB 下载超过大小限制。')
                file.write(data);h.update(data)
        unpack=temp/'data';unpack.mkdir()
        required={stem+'.cdb' for stem in core.TABLE.values()}
        available=set()
        with ZipFile(archive) as z:
            for entry in z.infolist():
                if entry.is_dir():continue
                name=entry.filename.replace('\\','/').rsplit('/',1)[-1]
                if name not in required:continue
                if name in available or entry.file_size>LIMIT:raise ValueError('CDB 包内含重复文件或异常大小。')
                with z.open(entry) as inp, (unpack/name).open('xb') as out:
                    shutil.copyfileobj(inp,out,length=1024**2)
                if not core.read_cdb(unpack/name):raise ValueError(f'发布库为空：{name}')
                available.add(name)
                log(f'已校验 {name}。')
        if available != required:raise ValueError('发布包缺少 Slasher 需要的现代 CDB。')
        digest=h.hexdigest()
        installed=cache/digest
        if not installed.exists():
            unpack.rename(installed)
        else:
            # Earlier Studio versions cached only five files from this same ZIP.
            for name in available:
                os.replace(unpack/name,installed/name)
        atomic_bytes(cache/'latest.json',json.dumps({'tag':tag,'sha256':digest,'checked_at':datetime.now(timezone.utc).isoformat()}).encode())
    return tag


def cdb_claims(ctx, latest, log):
    out=set()
    dirs=tuple(dict.fromkeys(p for p in (latest,ctx.saluki_dir) if p))
    for k, ids in ctx.pools.items():
        if k not in core.TABLE:
            continue
        n=0
        for folder in dirs:
            p=folder/(core.TABLE[k]+'.cdb')
            if not p.is_file():continue
            for key in core.read_cdb(p):
                if key & core.MASK in ids and (k!='sound_alias' or key in ctx.raw):
                    old=len(out)
                    out.add((k,key))
                    n+=len(out)-old
        if n:log(f'{core.LABEL[k]}：所选 CDB 中有 {n:,} 个当前快照的已知 ID。')
    return out
