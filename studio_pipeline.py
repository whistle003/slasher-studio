"""Slasher findings TXT -> merged CSV -> original cod-name-db NameIndex."""
import csv
import json
from pathlib import Path
import shutil
import subprocess

import studio_core as core


def export_names(out, kind, rows):
    folder = Path(out)/'txt'
    folder.mkdir(parents=True, exist_ok=True)
    file = folder/(kind+'.txt')
    file.write_text(''.join(f'{key:016x},{name}\n' for key,name in sorted(rows.items())), encoding='utf-8')
    return file


def convert(txt, csv_path, destination, baseline, log):
    """Consume the persisted TXT and verify the full converted database."""
    names = dict(baseline)
    for key,name in core.parse_finding_rows(txt.read_text(encoding='utf-8')):
        names[key] = name
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open('w', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerows((f'{key:016x}',name) for key,name in sorted(names.items()))
    converter = core.ASSETS/'converter'/'NameIndex.exe'
    if not converter.is_file():
        raise FileNotFoundError('缺少 cod-name-db 转换器 NameIndex.exe。TXT 已保留，请使用完整的软件包。')
    log(f'cod-name-db：{txt.name} → {csv_path.name} → {destination.name}')
    result = subprocess.run([str(converter),str(csv_path.resolve())], capture_output=True,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    generated = csv_path.with_suffix('.cdb')
    # Upstream prints errors but can exit zero; never trust its return code alone.
    if result.returncode or result.stderr.strip() or not generated.is_file():
        raise RuntimeError('cod-name-db 转换失败：'+result.stderr.decode('utf-8',errors='replace')[:1200])
    if core.read_cdb(generated) != names:
        raise RuntimeError('cod-name-db 输出未通过完整回读，TXT 和 CSV 已保留。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(generated),str(destination))
    record = destination.parents[2]/'pipeline.json'
    data = json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
    data[txt.stem] = dict(txt=str(txt.relative_to(record.parent)),
                         csv=str(csv_path.relative_to(record.parent)),
                         cdb=str(destination.relative_to(record.parent)),
                         converter='cod-name-db/NameIndex', merged=len(names), verified=True)
    record.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
