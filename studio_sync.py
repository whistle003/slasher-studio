"""Read-only public GitHub refresh, independent of API tokens and API quotas."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from html.parser import HTMLParser
import gzip,hashlib,json,re,tempfile
import socket,ssl,time,http.client
from pathlib import Path
from urllib import request,parse,error
from zipfile import ZipFile,ZIP_DEFLATED

import studio_community as community
from studio_sources import atomic_bytes,read_survey,save_survey

REPO='KingslayerKyle/hash-slinging-slasher'
WEB='https://github.com/'+REPO
SHA=re.compile(r'[0-9a-f]{40}\Z')
TIMEOUTS=(20,30,45)


class SyncNetworkError(RuntimeError):pass


def request_label(url):
    parsed=parse.urlsplit(url)
    if parsed.hostname=='api.github.com':return '主分支版本（GitHub API）'
    if '/info/refs' in parsed.path:return '主分支版本（Git 备用接口）'
    if parsed.path.endswith('/pulls'):
        return '开放 PR 列表，第 '+parse.parse_qs(parsed.query).get('page',['1'])[0]+' 页'
    match=re.search(r'/pull/(\d+)\.diff$',parsed.path)
    if match:return f'PR #{match[1]} 的名称提交'
    return '固定版本的上游名称归档'


def tls_interrupted(reason):
    return (isinstance(reason,(ssl.SSLEOFError,ssl.SSLZeroReturnError))
            or (isinstance(reason,ssl.SSLError)
                and getattr(reason,'reason',None)=='UNEXPECTED_EOF_WHILE_READING'))


def retryable(exc):
    if isinstance(exc,error.HTTPError):return exc.code in (408,429,500,502,503,504)
    reason=exc.reason if isinstance(exc,error.URLError) else exc
    return tls_interrupted(reason) or isinstance(reason,(TimeoutError,ConnectionError,socket.gaierror,
                                                        http.client.IncompleteRead,http.client.RemoteDisconnected))


def reason_text(exc):
    if isinstance(exc,error.HTTPError):return f'HTTP {exc.code}'
    reason=exc.reason if isinstance(exc,error.URLError) else exc
    if tls_interrupted(reason):return 'GitHub 的 TLS 连接提前关闭'
    if isinstance(reason,TimeoutError):return '网络读取超时'
    if isinstance(reason,socket.gaierror):return '域名解析失败'
    if isinstance(reason,(ConnectionError,http.client.RemoteDisconnected)):return '连接中断'
    if isinstance(reason,http.client.IncompleteRead):return '下载未完成'
    return str(reason)


def backoff(seconds,check):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        check();time.sleep(min(.1,max(0,end-time.monotonic())))
    check()


class PublicRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        url=parse.urlsplit(newurl)
        if url.scheme!='https' or url.hostname not in {'github.com','api.github.com','codeload.github.com','patch-diff.githubusercontent.com'}:
            raise ValueError('上游同步跳转到不受支持的地址。')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def fetch(url,limit=64*1024*1024,*,log=lambda _:None,check=lambda:None):
    label=request_label(url)
    for attempt,timeout in enumerate(TIMEOUTS,1):
        check();log(f'正在读取 {label}（第 {attempt}/{len(TIMEOUTS)} 次，读取超时 {timeout} 秒）…')
        try:
            with request.build_opener(PublicRedirect()).open(request.Request(url,headers={
                'User-Agent':'Slasher-Studio-public-sync','Accept-Encoding':'identity','Cache-Control':'no-cache'}),timeout=timeout) as response:
                chunks=[];size=0
                read=getattr(response,'read1',response.read)
                while True:
                    check();block=read(min(64*1024,limit+1-size))
                    if not block:break
                    chunks.append(block);size+=len(block)
                    if size>limit:raise ValueError('上游同步内容过大，未使用不完整数据。')
                expected=response.headers.get('Content-Length')
                if expected is not None and int(expected)!=size:raise http.client.IncompleteRead(b'',int(expected)-size)
                check()
                return b''.join(chunks)
        except InterruptedError:raise
        except (OSError,error.URLError,http.client.HTTPException) as exc:
            if attempt==len(TIMEOUTS) or not retryable(exc):
                raise SyncNetworkError(f'{label}失败：{reason_text(exc)}。请检查 GitHub 连接后点“继续搜索”，无需新建任务。') from exc
            delay=(1,3)[attempt-1]
            log(f'{label}：{reason_text(exc)}，{delay} 秒后自动重试。')
            backoff(delay,check)


class Embedded(HTMLParser):
    def __init__(self):super().__init__();self.active=False;self.data=[]
    def handle_starttag(self,tag,attrs):
        if tag=='script':self.active=dict(attrs).get('data-target')=='react-app.embeddedData'
    def handle_endtag(self,tag):
        if tag=='script':self.active=False
    def handle_data(self,data):
        if self.active:self.data.append(data)


def parse_page(data):
    html=Embedded();html.feed(data.decode('utf-8'))
    for block in html.data:
        value=json.loads(block).get('payload',{}).get('repoPullsDashboardContentRoute')
        if value is not None:return value
    raise ValueError('GitHub PR 列表格式已变，无法确认完整名单；未开始搜索。')


def heads(get=fetch):
    result={};page=1;total=None
    while True:
        data=parse_page(get(WEB+'/pulls?q=is%3Apr%20is%3Aopen&page='+str(page)))
        count=data.get('totalCount');pages=data.get('totalPages')
        if (not isinstance(count,int) or count!=data.get('openCount') or
            data.get('currentPage')!=page or not isinstance(pages,int) or pages>100 or
            (total is not None and count!=total)):
            raise ValueError('PR 分页不完整或同步期间有变化，请再次运行。')
        total=count
        for row in data['results']:
            number=row['number'];sha=row['headSha']
            if (row.get('state')!='REPO_PULL_REQUEST_STATE_OPEN' or row.get('repoNameWithOwner')!=REPO or
                not isinstance(number,int) or not SHA.fullmatch(sha) or number in result):
                raise ValueError('PR 列表存在重复、关闭或无效记录。')
            result[number]=sha
        if page>=max(1,pages):break
        page+=1
    if len(result)!=total:raise ValueError('开放 PR 数量与完整名单不符。')
    return result


def main_head(get=fetch,log=lambda _:None):
    try:
        ref=json.loads(get('https://api.github.com/repos/'+REPO+'/git/ref/heads/main'))
        sha=ref['object']['sha']
        if ref.get('ref')!='refs/heads/main' or ref['object'].get('type')!='commit' or not SHA.fullmatch(sha):
            raise ValueError('main 分支返回值无效')
        return sha
    except (SyncNetworkError,OSError,ValueError,KeyError,TypeError) as exc:
        if isinstance(exc,InterruptedError):raise
        log('GitHub API 暂不可用，自动改用 Git 备用接口核对主分支。')
    data=get(WEB+'.git/info/refs?service=git-upload-pack')
    matches=re.findall(rb'([0-9a-f]{40}) refs/heads/main(?:\x00|\n)',data)
    if len(matches)!=1:raise ValueError('无法确认上游 main 分支版本。')
    return matches[0].decode()


def added_rows(diff):
    """Read added lines only after validating every text hunk's line counts."""
    path=None;old=new=0;active=False;result={}
    for line in diff.decode('utf-8-sig').splitlines():
        if active and (old or new):
            if line.startswith('\\ No newline'):continue
            if line.startswith('+'):new-=1;result.setdefault(path,[]).append(line[1:]) if path else None
            elif line.startswith('-'):old-=1
            elif line.startswith(' '):old-=1;new-=1
            else:raise ValueError('PR diff 被截断或格式不完整。')
            if old<0 or new<0:raise ValueError('PR diff 行数不正确。')
            continue
        active=False
        if line.startswith('diff --git '):path=None
        elif line.startswith('+++ b/'):
            candidate=line[6:]
            path=candidate if community.PR_PATH.fullmatch(candidate) else None
        elif line.startswith('@@ '):
            m=re.match(r'@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@',line)
            if not m:raise ValueError('PR diff 段头无效。')
            old=int(m[1] or 1);new=int(m[2] or 1);active=True
    if old or new:raise ValueError('PR diff 未完整下载。')
    return result


def sync(ctx,root,log,check=lambda:None,get=None):
    if get is None:
        def get(url,limit=64*1024*1024):return fetch(url,limit,log=log,check=check)
    log('运行前自动同步：正在核对最新主分支和开放 PR…')
    check();commit=main_head(get,log);pulls=heads(get)
    signature=[[n,h] for n,h in sorted(pulls.items())]
    previous=read_survey(root,ctx)
    stamp={'version':2,'commit':commit,'heads':signature,'open':len(pulls),'date':datetime.now(timezone.utc).isoformat(),'transport':'public-read-only'}
    if previous and previous[1].get('version')==2 and previous[1].get('commit')==commit and previous[1].get('heads')==signature:
        if main_head(get,log)!=commit or heads(get)!=pulls:raise ValueError('核对期间上游有变化，请再次运行。')
        save_survey(root,ctx,previous[0],stamp)
        log(f'已在线确认上游未变：{len(pulls)} 个开放 PR，复用已校验的名称缓存。')
        return previous[0],stamp
    cache=root/'public-sync';cache.mkdir(parents=True,exist_ok=True)
    claims=set()
    with community.report_zip() as bundled:
        manifest=json.loads(bundled.read('survey.json'))
        archive=None
        if manifest['commit']==commit and ('xmodel' not in ctx.pools or manifest.get('model_submissions')):
            archive=bundled;prefix='merged/'
        else:
            target=cache/(commit+'.zip')
            if not target.exists():
                log('主分支有更新，正在下载固定版本的名称提交。')
                data=get('https://codeload.github.com/'+REPO+'/zip/'+commit,512*1024*1024)
                import io
                with ZipFile(io.BytesIO(data)) as upstream:
                    output=io.BytesIO()
                    with ZipFile(output,'w',ZIP_DEFLATED) as compact:
                        for entry in upstream.infolist():
                            check();path=entry.filename.partition('/')[2]
                            if community.PR_PATH.fullmatch(path):compact.writestr(path,upstream.read(entry))
                    atomic_bytes(target,output.getvalue())
            archive=ZipFile(target);prefix=''
        try:
            for path in archive.namelist():
                check()
                if prefix and not path.startswith(prefix):continue
                name=path[len(prefix):]
                if community.PR_PATH.fullmatch(name):
                    claims.update(community.row_keys(ctx,name,archive.read(path).decode('utf-8-sig')))
        finally:
            if archive is not bundled:archive.close()
    def read_pull(item):
        number,head=item;check()
        path=cache/f'{number}-{head}-{commit}.diff'
        data=path.read_bytes() if path.exists() else get(WEB+f'/pull/{number}.diff')
        parsed=added_rows(data)
        return parsed,path,data
    downloaded=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for parsed,path,data in pool.map(read_pull,sorted(pulls.items())):
            downloaded.append((path,data))
            check()
            for path,rows in parsed.items():claims.update(community.row_keys(ctx,path,'\n'.join(rows)))
    check()
    if main_head(get,log)!=commit or heads(get)!=pulls:
        raise ValueError('同步期间上游发生变化，未采用部分结果，请再次运行。')
    for path,data in downloaded:
        if not path.exists():atomic_bytes(path,data)
    save_survey(root,ctx,claims,stamp)
    log(f'最新 PR 同步完成：{len(pulls)} 个开放 PR，{len(claims):,} 条排除记录。')
    return claims,stamp
