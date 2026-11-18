"""Bounded file parsing cache and checksummed, non-executable JSON corpus cache."""
from collections import OrderedDict
import gzip,hashlib,json,threading
from pathlib import Path

LIMIT=192*1024*1024
_files=OrderedDict()
_bytes=0
_lock=threading.RLock()


def stamp(path):
    path=Path(path).resolve();s=path.stat()
    return str(path),s.st_size,s.st_mtime_ns,s.st_ctime_ns


def file_value(path,kind,loader):
    global _bytes
    key=(kind,*stamp(path))
    with _lock:
        if key in _files:
            value,size=_files.pop(key);_files[key]=(value,size)
            return value
    value=loader()
    if (kind,*stamp(path))!=key:raise ValueError(f'读取期间名称库发生变化，请重试：{path}')
    items=value.items() if isinstance(value,dict) else value
    size=sum(128+len(name)*2 for _,name in items)
    with _lock:
        if key in _files:return _files[key][0]
        if size<=LIMIT:
            # Remove stale versions of this path as well as ordinary LRU entries.
            for old in list(_files):
                if old[:2]==key[:2]:_bytes-=_files.pop(old)[1]
            while _files and _bytes+size>LIMIT:_bytes-=_files.popitem(last=False)[1][1]
            _files[key]=(value,size);_bytes+=size
    return value


def clear():
    global _bytes
    with _lock:_files.clear();_bytes=0


def read_json(path,key):
    try:
        with gzip.open(path,'rt',encoding='utf-8') as stream:envelope=json.load(stream)
        payload=envelope['payload']
        if envelope['key']!=key or hashlib.sha256(payload.encode()).hexdigest()!=envelope['sha256']:return None
        return json.loads(payload)
    except (OSError,ValueError,KeyError,EOFError,TypeError):return None


def write_json(path,key,value):
    from studio_sources import atomic_bytes
    payload=json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True)
    envelope=dict(key=key,sha256=hashlib.sha256(payload.encode()).hexdigest(),payload=payload)
    atomic_bytes(path,gzip.compress(json.dumps(envelope,ensure_ascii=False,separators=(',',':')).encode(),compresslevel=1))
