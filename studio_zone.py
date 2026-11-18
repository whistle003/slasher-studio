"""Read game strings from already unpacked zones using a ZoneKit cipher profile."""
from pathlib import Path
import re

import numpy as np


BLOCK = 16 * 1024**2
PRINTABLE = np.arange(256)
PRINTABLE = (PRINTABLE >= 32) & (PRINTABLE < 127)
TUPLE = re.compile(r'(?:[0-9a-fA-F]{3}){2,4096}\Z')


def profile(path):
    select = {}
    transforms = {}
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(),1):
        fields = line.partition('#')[0].split()
        if not fields:
            continue
        try:
            if len(fields) == 3 and fields[0] == 'cipher' and TUPLE.fullmatch(fields[2]):
                ident = int(fields[1])
                if ident < 0 or ident in transforms:
                    raise ValueError()
                values = np.tile(np.arange(256,dtype=np.uint16),(len(fields[2])//3,1))
                for i, pos in enumerate(range(0,len(fields[2]),3)):
                    r, k = int(fields[2][pos],16)&7, int(fields[2][pos+1:pos+3],16)
                    row = values[i]
                    row ^= k
                    row[k] = k
                    row[:] = ((row << r) | (row >> (8-r))) & 255
                transforms[ident] = values.astype(np.uint8)
            elif 3 <= len(fields) <= 258 and fields[0] == 'select':
                code = int(fields[1],16)
                if not 0x80 <= code <= 0xbf or code in select:
                    raise ValueError()
                select[code] = [int(value) for value in fields[2:]]
            else:
                raise ValueError()
        except ValueError:
            raise ValueError(f'字符串解密表第 {number} 行无效。') from None
    if not select or not transforms or any(value not in transforms and value != -1
                                          for values in select.values() for value in values):
        raise ValueError('字符串解密表缺少选择器或引用了不存在的变换。')
    ordered = sorted(transforms)
    indices = {code:i for i,code in enumerate(ordered)}
    table = np.full((256,256),-1,dtype=np.int32)
    for marker, values in select.items():
        table[marker,:len(values)] = [indices.get(value,-1) for value in values]
    maps = [transforms[i] for i in ordered]
    return table, maps, PRINTABLE[np.array([m[:2] for m in maps])]


def strings(path, parsed, check):
    table, maps, starts = parsed
    with path.open('rb') as src:
        while True:
            check()
            before = src.tell()
            raw = src.read(BLOCK + 257)
            data = np.frombuffer(raw,np.uint8)
            stop = min(BLOCK,len(data)-5)
            if stop <= 0:
                break
            offsets = np.flatnonzero((data[:stop] & 0xc0) == 0x80)
            codes = table[data[offsets],data[offsets+1]]
            offsets, codes = offsets[codes >= 0], codes[codes >= 0]
            size = data[offsets+2].astype(np.int64)
            end = offsets+size+2
            valid = (size >= 4) & (end < len(data))
            offsets, codes, end = offsets[valid], codes[valid], end[valid]
            valid = (data[end] == 0) & starts[codes,0,data[offsets+3]] & starts[codes,1,data[offsets+4]]
            for i,(start,code,end) in enumerate(zip(offsets[valid],codes[valid],end[valid])):
                if i % 1024 == 0:
                    check()
                cipher = data[start+3:end]
                if len(cipher) > len(maps[code]) or not cipher.all():
                    continue
                clear = maps[code][np.arange(len(cipher)),cipher]
                if PRINTABLE[clear].all():
                    yield clear.tobytes().decode('ascii')
            if len(raw) <= BLOCK:
                break
            src.seek(before+BLOCK)


def names(value):
    for part in value.lower().split('::'):
        part = part.strip().replace('\\','/')
        base = part.rsplit('/',1)[-1]
        yield part
        if base != part:
            yield base
        if '.' in base:
            yield part.rsplit('.',1)[0]
            yield base.rsplit('.',1)[0]
        if '/' in part:
            yield part.replace('/','.')


def candidates(root, check):
    root = Path(root).resolve()
    parsed = profile(root/'strings.cipher.txt')
    directory = (root/'zones').resolve()
    paths = sorted(p for p in directory.rglob('*') if p.suffix.lower() in ('.zone','.bin')
                   and p.is_file() and p.resolve().is_relative_to(root))
    if not paths:
        raise ValueError('strings.cipher.txt 旁需有 zones 目录，内含已解压的 .zone 或 .bin。')
    seen = set()
    for path in paths:
        for word in strings(path,parsed,check):
            for name in names(word):
                if name not in seen:
                    seen.add(name)
                    yield name
