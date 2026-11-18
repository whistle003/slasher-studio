from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
from itertools import chain
import json
from pathlib import Path
import re
import time
from zipfile import ZipFile

from studio_core import ASSETS, GAMES, HEX, MASK, TABLE, canonical, matches, name_key, parse_finding_rows, snapshot_data
from studio_vendor.github_api import COMMUNITY, UPSTREAM, GitHubApi, GitHubError, _blob_sha


BASE = f'/repos/{UPSTREAM}'
PR_PATH = re.compile(r'^submissions/[^/]+_('+'|'.join(sorted(GAMES))+r')_[^/]+/(xmodel|xanim|image|material|sound_asset|sndasset|sound_alias|animpkg)_\d{8}-\d{6}\.txt$')
PR_URL = re.compile(r'https://github\.com/KingslayerKyle/hash-slinging-slasher/pull/[0-9]+\Z')


def report_zip():
    return ZipFile(ASSETS/'community.zip')


def local_preview(ctx, rows, folder, claims):
    """A local draft only. Online prepare still gates official publication."""
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    target=folder/'local-pr'/f'LocalReview_{ctx.game}_{stamp}'
    accepted={kind:{key:name for key,name in found.items()
                    if (kind,key) not in claims and matches(ctx,kind,key,name)}
              for kind,found in rows.items() if kind in TABLE}
    count=sum(map(len,accepted.values()))
    target.mkdir(parents=True)
    files=[]
    for kind,found in accepted.items():
        if not found:continue
        path=target/f'{kind}_{stamp}.txt'
        path.write_text(''.join(f'{key:x},{name}\n' for key,name in sorted(found.items())),encoding='utf-8')
        files.append(path.relative_to(folder).as_posix())
    body=(f'# [{ctx.game}] 本地名称投稿草稿：{count:,} 条\n\n'
          '已按当前本地快照、名称表、所选 CDB 和社区缓存排重，并通过 CPU 回算。\n'
          '尚未在线核对上游快照与最新开放 PR，不是可直接公开提交的最终预览。\n'
          '不会自动公开提交。以后需要投稿时，再生成在线 PR 预览。\n\n'+
          ('方法与命中统计见 method_results.json。\n\n' if (folder/'method_results.json').exists() else '')+
          '\n'.join('- '+p for p in files)+'\n')
    (folder/'pr-preview.txt').write_text(body,encoding='utf-8')
    (target/f'about_{stamp}.md').write_text(
        'Local draft only. Verified against a local capture. Online upstream capture and novelty checks are required before publication.\n',
        encoding='utf-8')


def row_keys(ctx, path, text, allowed=None):
    match = PR_PATH.fullmatch(path)
    if not match:
        return set()
    kind = canonical(match[2])
    present = ctx.pools.get(kind, ())
    if not present:
        return set()
    return {(kind,key) for key, name in parse_finding_rows(text)
            if key & MASK in present and (allowed is None or (kind,key) in allowed)
            and matches(ctx, kind, key, name)}


def safe_ids(ctx, rows):
    covered = {kind: set() for kind in ctx.pools}
    known_alias = set()
    for kind, key in rows:
        covered[kind].add(key & MASK)
        if kind == 'sound_alias':
            known_alias.add(key)
    if 'sound_alias' in covered:
        # The engine takes 63-bit exclusion keys; both aliases sharing a key
        # must be known before it can suppress that key.
        covered['sound_alias'].difference_update(key & MASK for key in ctx.raw if key not in known_alias)
    total = set().union(*covered.values())
    ambiguous = set().union(*(total & (ctx.pools[k] - ids) for k, ids in covered.items()))
    return total - ambiguous


class SearchExclusions:
    """Only recheck the affected masked IDs when a batch discovers names."""

    def __init__(self, ctx, baseline, hits):
        self.ctx = ctx
        self.baseline = baseline
        self.hits = set(hits)
        self.safe = safe_ids(ctx, chain(baseline, self.hits))

    def add(self, rows):
        affected = set()
        for kind,key in rows:
            self.hits.add((kind,key))
            affected.add(key&MASK)
        added = set()
        for low in affected-self.safe:
            # A masked engine key can cover multiple pools or two full alias keys.
            spellings = (low, low | (MASK+1))
            for kind,ids in self.ctx.pools.items():
                if low not in ids:
                    continue
                required = [k for k in spellings if k in self.ctx.raw] if kind=='sound_alias' else []
                known = [k for k in spellings if (kind,k) in self.baseline or (kind,k) in self.hits]
                if not known or any(k not in known for k in required):
                    break
            else:
                self.safe.add(low)
                added.add(low)
        return added


def offline_claims(ctx, progress=lambda _: None):
    progress('正在读取随包的已合并提交和开放 PR 快照。')
    claims = set()
    with report_zip() as z:
        stamp=json.loads(z.read('survey.json'))
        for name in z.namelist():
            if name.startswith('merged/'):
                claims.update(row_keys(ctx,name[len('merged/'):],z.read(name).decode('utf-8-sig')))
            elif name.startswith('open/') and name.endswith('.txt'):
                claims.update(row_keys(ctx,name.split('/',2)[2],z.read(name).decode('utf-8-sig')))
    progress(f'随包排除数据：{stamp["date"]}，{len(claims):,} 条与当前快照相符。')
    return claims, stamp


def open_state(api):
    return api.paginate(f'{BASE}/pulls?state=open')


def signature(pulls):
    return sorted((int(p['number']), p['head']['sha']) for p in pulls)


def entry_map(tree):
    return {e['path']:e for e in tree['entries'] if e['type']=='blob'}


def decode(bytes_):
    return bytes_.decode('utf-8-sig')


def remote_claims(ctx, api, tree, pulls, progress, allowed=None):
    rows = set()
    index = entry_map(tree)
    with report_zip() as z:
        manifest = json.loads(z.read('survey.json'))['merged_blobs']
        selected = [e for path,e in index.items()
                    if (m:=PR_PATH.fullmatch(path)) and canonical(m[2]) in ctx.pools]
        changed = []
        for e in selected:
            path = e['path']
            if manifest.get(path) == e['sha']:
                rows.update(row_keys(ctx,path,decode(z.read('merged/'+path)),allowed))
            else:
                changed.append(e)
        progress(f'主分支：{len(selected):,} 份名称文件，需刷新 {len(changed):,} 份。')
        blobs = api.blobs(UPSTREAM,changed)
        for e in changed:
            rows.update(row_keys(ctx,e['path'],decode(blobs[e['sha']]),allowed))
    for n, head in signature(pulls):
        detail = api.request('GET', f'{BASE}/pulls/{n}')
        if detail['head']['sha'] != head or detail['state'] != 'open' or detail['changed_files'] > 3000:
            raise ValueError(f'开放 PR #{n} 有更新或范围超限，请重新核对。')
        files = api.paginate(f'{BASE}/pulls/{n}/files')
        if len(files) != detail['changed_files']:
            raise ValueError(f'PR #{n} 的文件清单未完整返回，已停止。')
        entries = [dict(f,path=f['filename']) for f in files if f['status']!='removed'
                   and (m:=PR_PATH.fullmatch(f['filename'])) and canonical(m[2]) in ctx.pools]
        values = api.blobs(UPSTREAM,entries)
        for e in entries:
            rows.update(row_keys(ctx,e['path'],decode(values[e['sha']]),allowed))
        if entries:
            progress(f'已核对 PR #{n} 的 {len(entries)} 份名称文件。')
    return rows


def live_claims(ctx, api, progress):
    tree = api.tree(UPSTREAM)
    pulls = open_state(api)
    rows = remote_claims(ctx,api,tree,pulls,progress)
    if api.request('GET',f'{BASE}/commits/HEAD')['sha'] != tree['commit'] or signature(open_state(api)) != signature(pulls):
        raise ValueError('核对期间主分支或开放 PR 有变化，请重新刷新。')
    return rows, {'commit':tree['commit'],'open':len(pulls),'date':datetime.now(timezone.utc).isoformat()}


def community_matched(ctx, api, tree, eligible, progress):
    entries = entry_map(tree)
    used = {kind for kind,key in eligible}
    names = {k: set() for k in used}
    for k,key in eligible:
        names[k].add(key)
    hits = set()
    for kind in sorted(used):
        rel = f'csv/{TABLE[kind]}.csv'
        e = entries.get(rel)
        if not e:
            raise ValueError(f'上游名称表缺少 {rel}。')
        local = (ctx.table_dir/Path(rel).name).read_bytes()
        if _blob_sha(local) != e['sha']:
            normalized = local.replace(b'\r\n',b'\n')
            local = normalized if _blob_sha(normalized)==e['sha'] else api.blobs(COMMUNITY,[e])[e['sha']]
        for row in io.BytesIO(local):
            key, sep, _ = row.partition(b',')
            if sep and re.fullmatch(rb'[0-9a-fA-F]{1,16}',key):
                val=int(key,16)
                if val in names[kind]:
                    hits.add((kind,val))
        progress(f'已核对 {TABLE[kind]} 的当前发布键。')
    return hits


@dataclass
class Preview:
    upstream_commit: str
    upstream_tree: str
    database_commit: str
    open_heads: list
    login: str
    branch: str
    title: str
    body: str
    files: dict
    count: int
    excluded: dict
    commit: str = ''


def ensure_current(api, upstream, database, heads):
    if (api.request('GET',f'{BASE}/commits/HEAD')['sha']!=upstream
            or api.request('GET',f'/repos/{COMMUNITY}/commits/HEAD')['sha']!=database
            or signature(open_state(api))!=heads):
        raise ValueError('核对期间上游内容已变更，请重新生成 PR 预览。')


def prepare(ctx, accepted, api, out, progress):
    login=api.request('GET','/user')['login']
    tree=api.tree(UPSTREAM)
    index=entry_map(tree)
    files=[f'snapshots/{ctx.game.lower()}.{ext}' for ext in ('ids','pools.txt')]
    if any(p not in index for p in files):
        raise ValueError('上游没有这款游戏的可核验快照。')
    fetched=api.blobs(UPSTREAM,[index[p] for p in files])
    tag, official, _=snapshot_data(fetched[index[files[0]]['sha']],decode(fetched[index[files[1]]['sha']]))
    if tag != ctx.game:
        raise ValueError('上游快照标签与当前运行不符。')
    eligible = {}
    excluded = Counter()
    for k, rows in accepted.items():
        if k not in TABLE:
            excluded['模型结果仅本地使用']+=len(rows)
            continue
        for key,name in rows.items():
            if not matches(ctx,k,key,name) or not name.isascii() or not name.isprintable() or len(name)>250:
                excluded['不可投稿的名称']+=1
            elif key&MASK not in official[k]:
                excluded['上游快照未收录']+=1
            else:
                eligible[k,key]=name
    pulls=open_state(api)
    if eligible:
        remote=remote_claims(ctx,api,tree,pulls,progress,set(eligible))
        excluded['已合并或开放 PR 已有']=len(remote)
        for row in remote: eligible.pop(row,None)
    db=api.tree(COMMUNITY)
    if eligible:
        dupe=community_matched(ctx,api,db,eligible,progress)
        excluded['社区名称表已有']=len(dupe)
        for row in dupe:eligible.pop(row,None)
    if len(eligible)>10000:
        raise ValueError('本次可投稿记录超过 10,000 条。请缩小搜索范围再生成 PR。')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    digest=hashlib.sha256(json.dumps(sorted((k,key,n) for (k,key),n in eligible.items())).encode()).hexdigest()
    title=f'[{ctx.game}] findings from {login}, {stamp} ({len(eligible)} names)'
    branch=f'studio/findings-{digest[:16]}-{tree["commit"][:7]}'
    prefix=f'submissions/{login}-{digest[:8]}_{ctx.game}_{stamp}/'
    payload={}
    for k in sorted({kind for kind,key in eligible}):
        payload[prefix+f'{k}_{stamp}.txt']=''.join(f'{key:x},{n}\n' for (kind,key),n in sorted(eligible.items()) if kind==k)
    evidence = getattr(getattr(ctx,'source',None),'evidence','')
    method = ('Recovered with Slasher Studio using supplied name evidence, such as animation slots, sound banks, or unpacked zone strings. '
              if evidence else 'Recovered through the Slasher candidate search. ')
    body=method+'Names were rehashed and checked against the upstream capture. Community tables, merged findings and open PR files were checked.'
    if payload:
        payload[prefix+f'about_{stamp}.md']=f'# {title}\n\n{body}\n\n- fingerprint: {digest}\n'
    result=Preview(tree['commit'],tree['tree_sha'],db['commit'],signature(pulls),login,branch,title,body,payload,len(eligible),dict(excluded))
    ensure_current(api,result.upstream_commit,result.database_commit,result.open_heads)
    review=out/'pr-preview.txt'
    review.write_text(title+'\n\n'+body+'\n\n'+json.dumps(dict(excluded),ensure_ascii=False,indent=2)+'\n\n'+'\n'.join(f'=== {p} ===\n{t}' for p,t in payload.items()),encoding='utf-8')
    return result


def existing_pr(api, preview):
    pulls=api.paginate(f'{BASE}/pulls?state=all&head={preview.login}:{preview.branch}')
    for p in pulls:
        if p.get('head',{}).get('ref')==preview.branch and PR_URL.fullmatch(p.get('html_url','')):
            return p['html_url']
    return None


def publish(preview, api, progress):
    if not preview.count:
        raise ValueError('没有可投稿的名称。')
    login=api.request('GET','/user')['login']
    if login!=preview.login:
        raise ValueError('GitHub 登录账号已变更，请重新生成预览。')
    if found:=existing_pr(api,preview):
        return found
    ensure_current(api,preview.upstream_commit,preview.database_commit,preview.open_heads)
    fork=f'{login}/hash-slinging-slasher'
    if fork.casefold()==UPSTREAM.casefold():
        raise ValueError('工作台只在贡献者的 Fork 上创建分支。')
    try:
        info=api.request('GET',f'/repos/{fork}')
    except GitHubError as e:
        if e.status_code!=404:
            raise
        api.request('POST',f'{BASE}/forks',{'default_branch_only':True})
        for _ in range(12):
            try:
                info=api.request('GET',f'/repos/{fork}')
                break
            except GitHubError as pending:
                if pending.status_code!=404: raise
                time.sleep(1)
        else:
            raise ValueError('Fork 尚未就绪，请稍后重试。')
    if not info.get('fork') or info.get('parent',{}).get('full_name','').casefold()!=UPSTREAM.casefold():
        raise ValueError('账号里的同名仓库不是上游的 Fork。')
    base=api.request('GET',BASE)['default_branch']
    progress(f'向 {fork} 写入预览中的 {len(preview.files)} 个文本文件。')
    try:
        ref=api.request('GET',f'/repos/{fork}/git/ref/heads/{preview.branch}')
    except GitHubError as e:
        if e.status_code!=404: raise
        ref=None
    if ref:
        if not preview.commit or ref['object']['sha']!=preview.commit:
            raise ValueError('已有同名远程分支，无法确认来自这次预览；没有覆盖。')
    else:
        t=api.request('POST',f'/repos/{fork}/git/trees',{'base_tree':preview.upstream_tree,'tree':[
            {'path':p,'mode':'100644','type':'blob','content':content} for p,content in preview.files.items()]})
        c=api.request('POST',f'/repos/{fork}/git/commits',{'message':preview.title,'tree':t['sha'],'parents':[preview.upstream_commit]})
        preview.commit=c['sha']
        api.request('POST',f'/repos/{fork}/git/refs',{'ref':'refs/heads/'+preview.branch,'sha':preview.commit})
    ensure_current(api,preview.upstream_commit,preview.database_commit,preview.open_heads)
    try:
        pr=api.request('POST',f'{BASE}/pulls',{'head':f'{login}:{preview.branch}','base':base,'title':preview.title,'body':preview.body})
    except GitHubError:
        if found:=existing_pr(api,preview):return found
        raise
    if not PR_URL.fullmatch(pr.get('html_url','')):
        raise ValueError('GitHub 返回了无法确认的 PR 链接。')
    return pr['html_url']
