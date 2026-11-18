from __future__ import annotations

import multiprocessing
if __name__=='__main__':
    multiprocessing.freeze_support()

from dataclasses import asdict
from pathlib import Path
import csv
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser

import studio_animpkg as animpkg
import studio_core as core
import studio_community as community
import studio_sources as sources
import studio_tasks as tasks


INK='#132821'; MUTED='#53665c'; LINE='#d2ddd4'; PAGE='#eef3ee'; PAPER='#fbfdf9'
GREEN='#176847'; DEEP='#192f28'; LOG='#e1f5e5'
CONTROL='#eff4ef'; HOVER='#e7efe8'; PRESSED='#dbe9de'; SELECTED='#e3f0e6'
FOCUS='#387858'; CHECK_EDGE='#788f81'; ACCENT_INK='#174f38'
FONT='Microsoft YaHei UI'
ENGINES={'自动（按任务选择）':'auto','CPU':'cpu','GPU':'gpu'}
SNAPSHOT_MODE='资产快照'
EXPORT_MODE='Saluki 导出目录'
CONFIRM_WORK='验证并合并名称'


def app_data():
    return Path(os.environ.get('SLASHER_STUDIO_SETTINGS', str(Path(os.getenv('LOCALAPPDATA',Path.home()))/'SlasherStudio')))


class Studio:
    def __init__(self, root):
        self.root=root
        self.settings=app_data()
        self.root.title(f'Slasher Studio {core.VERSION} · 资产名称工作台')
        from ctypes import wintypes
        desktop=wintypes.RECT()
        usable=ctypes.windll.user32.SystemParametersInfoW(48,0,ctypes.byref(desktop),0)
        h=min(775,max(590,desktop.bottom-desktop.top-50)) if usable else 740
        self.root.geometry(f'1140x{h}')
        self.root.minsize(940,590)
        try: self.root.iconbitmap(core.ASSETS/'studio.ico')
        except tk.TclError: pass
        self.root.configure(bg=PAGE)
        self.events=queue.Queue()
        self.job=None
        self.busy=None
        self.ctx=None
        self.run_dir=None
        self.rows={}
        self.preview=None
        self.pr_url=''
        self.task_dir=None
        self.export_cancel=threading.Event()
        self.close_requested=False
        self.task_text=tk.StringVar(value='开始后自动查找、保存 TXT 并转换 CDB。')
        self.advanced=tk.BooleanVar(value=False)
        self.token=tk.StringVar()
        self.status=tk.StringVar(value='就绪')
        self.run_text=tk.StringVar(value='尚未运行')
        self.pr_status=tk.StringVar(value='搜索完成后，可以生成公开投稿预览。')
        self.count_text=tk.StringVar(value='暂无结果')
        defaults={'snapshot':'','tables':'','saluki':'','output':str(Path.home()/'Documents'/'Slasher Studio'),
                  'candidates':'','evidence':'','alias_keys':'','keyword':'','millions':'10'}
        try:
            saved=json.loads((self.settings/'settings.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            saved={}
        self.fields={k:tk.StringVar(value=saved.get(k,v)) for k,v in defaults.items()}
        found=saved.get('export_inputs',saved.get('package_fields',{}))
        if not self.fields['saluki'].get() and found.get('cdb'):
            self.fields['saluki'].set(found['cdb'])
        self.export_fields={k:tk.StringVar(value=found.get(k,'')) for k in ('source','clips')}
        mode=saved.get('source_mode')
        self.source_mode=tk.StringVar(value=mode if mode in (SNAPSHOT_MODE,EXPORT_MODE) else SNAPSHOT_MODE)
        self.kinds={k:tk.BooleanVar(value=k in saved.get('kinds',core.DEFAULT_KINDS)) for k in core.SUPPORTED_KINDS}
        if not saved.get('priority_version') and any(s in saved.get('snapshot','').lower() for s in ('mw7','modwar7')):
            self.kinds['xmodel'].set(True)
        self.continuous=tk.BooleanVar(value=saved.get('continuous',True))
        self.adaptive=tk.BooleanVar(value=saved.get('adaptive',True))
        self.engine=tk.StringVar(value=next((label for label, key in ENGINES.items()
                                             if key==saved.get('engine')),next(iter(ENGINES))))
        self.log_len=0
        self.style()
        self.layout()
        if saved.get('active_task'):
            folder=Path(saved['active_task'])
            try:
                if (folder/tasks.QUEUE).is_file():
                    _,done,maximum,_=tasks.overview(folder)
                    self.task_dir=folder
                    self.update_checkpoint(done,maximum)
                    self.status.set('已载入上次任务，可继续搜索。')
            except Exception as e:self.log_line(f'暂时无法恢复上次任务：{e}')
        self.refresh_buttons()
        self.root.after(70,self.tick)
        self.root.protocol('WM_DELETE_WINDOW',self.close)

    def style(self):
        s=ttk.Style(); s.theme_use('clam')
        s.configure('.',font=(FONT,10),foreground=INK,background=PAPER,focuscolor=FOCUS)
        s.configure('TButton',padding=(12,8),background=CONTROL,bordercolor=LINE,relief='flat')
        s.map('TButton',background=[('disabled','#f2f5f1'),('pressed',PRESSED),('active',HOVER)],
              foreground=[('disabled','#65736a')])
        s.configure('Primary.TButton',background=SELECTED,foreground=ACCENT_INK)
        s.map('Primary.TButton',background=[('disabled','#f0f4ef'),('pressed','#cfe4d6'),('active','#d7e9dd')])
        s.configure('TEntry',fieldbackground='#ffffff',bordercolor='#afc0b6',insertcolor=INK,padding=6,
                    selectbackground='#d5e9da',selectforeground=INK)
        s.configure('Compact.TEntry',padding=(6,3))
        s.configure('Compact.TButton',padding=(10,4))
        s.configure('TSpinbox',fieldbackground='#ffffff',bordercolor='#afc0b6',padding=5,
                    selectbackground='#d5e9da',selectforeground=INK)
        for field in ('TEntry','TSpinbox'):
            s.map(field,bordercolor=[('focus',FOCUS)])
        s.configure('TCheckbutton',background=PAPER,foreground=INK,padding=(0,4))
        s.map('TCheckbutton',foreground=[('disabled','#65736a')])
        s.configure('TNotebook',background=PAGE,tabmargins=(0,0,0,0),
                    borderwidth=1,bordercolor=LINE,lightcolor=LINE,darkcolor=LINE)
        s.configure('TNotebook.Tab',padding=(19,10),foreground=MUTED)
        s.map('TNotebook.Tab',padding=[],foreground=[('selected',ACCENT_INK)])
        self.state_indicators(s)
        s.configure('Treeview',background=PAPER,fieldbackground=PAPER,foreground=INK,rowheight=32,bordercolor=LINE)
        s.configure('Treeview.Heading',background='#e4eee6',foreground=INK,padding=7)
        s.map('Treeview',background=[('selected',SELECTED)],foreground=[('selected',INK)],
              bordercolor=[('focus',FOCUS)])
        s.configure('Horizontal.TProgressbar',background=GREEN,troughcolor='#dce8de',borderwidth=0)

    def state_indicators(self, style):
        # Keep ttk's native focus, labels and keyboard bindings; replace only the
        # two drawn elements whose geometry or mark changed with the old theme.
        self._theme_images=[]

        def image(w,h):
            photo=tk.PhotoImage(master=self.root,width=w,height=h)
            self._theme_images.append(photo)
            return photo

        def tab(fill, current=False):
            photo=image(19,6)
            photo.put(fill,to=(0,0,19,6))
            if current: photo.put(GREEN,to=(7,3,12,5))
            return photo

        style.element_create('Studio.Notebook.tab','image',tab(CONTROL),
                             ('selected',tab(SELECTED,True)),('active',tab(HOVER)),
                             border=(7,1,7,3),sticky='nswe')
        style.layout('TNotebook.Tab',[('Studio.Notebook.tab',style.layout('TNotebook.Tab')[0][1])])

        def box(fill, edge, mark=None):
            photo=image(20,16)
            photo.put(edge,to=(0,2,15,14))
            photo.put(edge,to=(1,1,14,15))
            photo.put(fill,to=(1,2,14,14))
            if mark:
                for y,x0,x1 in ((4,10,12),(5,9,12),(6,8,11),(7,3,5),(7,7,10),
                                (8,3,9),(9,4,8),(10,5,7)):
                    photo.put(mark,to=(x0,y,x1,y+1))
            return photo

        style.element_create('Studio.Checkbutton.indicator','image',box('#ffffff',CHECK_EDGE),
                             ('disabled','selected',box(CONTROL,'#adbeb2','#65736a')),
                             ('disabled',box(CONTROL,'#adbeb2')),
                             ('selected','active',box('#d7e9dd',FOCUS,ACCENT_INK)),
                             ('selected',box(SELECTED,FOCUS,ACCENT_INK)),
                             ('active',box(CONTROL,FOCUS)))
        layout=style.layout('TCheckbutton')
        children=layout[0][1]['children']
        children[0]=('Studio.Checkbutton.indicator',children[0][1])
        style.layout('TCheckbutton',layout)

    @staticmethod
    def frame(parent,bg=PAPER,**kw):
        return tk.Frame(parent,bg=bg,**kw)

    @staticmethod
    def label(parent,text='',size=10,color=INK,bg=PAPER,weight='normal',**kw):
        return tk.Label(parent,text=text,bg=bg,fg=color,font=(FONT,size,weight),anchor='w',justify='left',**kw)

    def layout(self):
        shell=self.frame(self.root,PAGE)
        shell.pack(fill='both',expand=True,padx=22,pady=(17,13))
        mast=self.frame(shell,PAGE); mast.pack(fill='x',pady=(0,12))
        self.label(mast,'Slasher Studio',25,bg=PAGE,weight='bold').pack(anchor='w')
        self.label(mast,'查找名称 → 分类 TXT → Saluki CDB',10,MUTED,PAGE).pack(anchor='w',pady=(2,0))
        footer=self.frame(shell,PAGE); footer.pack(side='bottom',fill='x',pady=(9,0))
        self.label(footer,bg=PAGE,color=MUTED,textvariable=self.status).pack(side='left',fill='x',expand=True)
        self.progress=ttk.Progressbar(footer,length=150,mode='indeterminate'); self.progress.pack(side='right')
        self.tabs=ttk.Notebook(shell); self.tabs.pack(fill='both',expand=True)
        self.search=self.frame(self.tabs); self.result=self.frame(self.tabs); self.contribution=self.frame(self.tabs)
        for pane,title in [(self.search,'搜索'),(self.result,'结果与 CDB'),
                           (self.contribution,'更新与 PR 预览')]: self.tabs.add(pane,text=title)
        self.search_layout(); self.result_layout(); self.pr_layout()
        self.show_search_mode()

    def pathrow(self,parent,row,key,label,pick='dir',second=False,fields=None,compact=False):
        fields=self.fields if fields is None else fields
        caption=self.label(parent,label,9,MUTED)
        caption.grid(row=row*2,column=0,columnspan=3,sticky='w',pady=(2 if compact else 5,1))
        ttk.Entry(parent,textvariable=fields[key],width=24,style='Compact.TEntry' if compact else 'TEntry').grid(
            row=row*2+1,column=0,sticky='ew',padx=(0,6))
        ttk.Button(parent,text='文件夹' if pick=='dir' else '选择文件',command=lambda:self.pick(key,pick,fields),
                   style='Compact.TButton' if compact else 'TButton').grid(row=row*2+1,column=1,sticky='ew')
        if second:
            ttk.Button(parent,text='文件夹',command=lambda:self.pick(key,'dir')).grid(row=row*2+1,column=2,sticky='ew',padx=(5,0))
        return caption

    def search_layout(self):
        pane=self.search
        pane.grid_columnconfigure(0,weight=6,minsize=500); pane.grid_columnconfigure(1,weight=4,minsize=350); pane.grid_rowconfigure(0,weight=1)
        left_area=self.frame(pane); left_area.grid(row=0,column=0,sticky='nsew',padx=(19,14),pady=(14,11))
        top=self.frame(left_area); top.pack(fill='x')
        self.label(top,'搜索资料',16,weight='bold').pack(side='left')
        self.detect_btn=ttk.Button(top,text='从资料目录识别',command=self.detect)
        self.task_load=ttk.Button(top,text='载入任务',command=self.load_task)
        mode=self.frame(left_area);mode.pack(fill='x',pady=(7,2))
        self.label(mode,'目标来源',9,MUTED).pack(side='left',padx=(0,8))
        self.mode_picker=ttk.Combobox(mode,textvariable=self.source_mode,values=(SNAPSHOT_MODE,EXPORT_MODE),
                                    width=20,state='readonly')
        self.mode_picker.pack(side='left')
        self.mode_picker.bind('<<ComboboxSelected>>',lambda _:self.change_search_mode())
        bottom=self.frame(left_area); bottom.pack(side='bottom',fill='x',pady=(7,0))
        task_caption=self.frame(bottom)
        task_caption.pack(fill='x',pady=(0,5))
        self.advanced_panel=self.frame(left_area)
        engine_row=self.frame(self.advanced_panel);engine_row.pack(fill='x')
        self.engine_picker=ttk.Combobox(engine_row,textvariable=self.engine,values=tuple(ENGINES),
                                        width=14,state='readonly')
        self.engine_picker.pack(side='right',padx=(5,0))
        self.label(engine_row,'引擎',9,MUTED).pack(side='right',padx=(5,0))
        self.label(task_caption,color=MUTED,textvariable=self.task_text).pack(side='left',fill='x',expand=True)
        bar=self.frame(bottom);bar.pack(fill='x')
        scroll_area=self.frame(left_area); scroll_area.pack(fill='both',expand=True,pady=(4,0))
        canvas=tk.Canvas(scroll_area,bg=PAPER,highlightthickness=0,width=1,height=1);self.search_canvas=canvas
        scroller=ttk.Scrollbar(scroll_area,command=canvas.yview); canvas.configure(yscrollcommand=scroller.set)
        scroller.pack(side='right',fill='y');canvas.pack(side='left',expand=True,fill='both')
        left=self.frame(canvas); window=canvas.create_window(0,0,anchor='nw',window=left)
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(window,width=e.width))
        left.bind('<Configure>',lambda e:canvas.configure(scrollregion=canvas.bbox('all')))
        def wheel(event):
            if self.tabs.select()!=str(self.search): return
            target=self.root.winfo_containing(event.x_root,event.y_root)
            if target is not None and str(target).startswith(str(scroll_area)):
                canvas.yview_scroll(-int(event.delta/120),'units')
        self.root.bind('<MouseWheel>',wheel,add='+')
        self.snapshot_files=self.frame(left);self.snapshot_files.grid_columnconfigure(0,weight=1)
        self.pathrow(self.snapshot_files,0,'snapshot','资产快照（.ids 与 .pools.txt）','snapshot',True)
        self.pathrow(self.snapshot_files,1,'tables','社区名称表（cod-name-db 或 csv 文件夹）')
        advanced_files=self.frame(self.advanced_panel);advanced_files.pack(fill='x');advanced_files.grid_columnconfigure(0,weight=1)
        self.pathrow(advanced_files,0,'candidates','额外 TXT 名单（选填）','candidates',True,compact=True)
        self.pathrow(advanced_files,1,'evidence','额外名称线索目录（选填）',compact=True)
        self.export_files=self.frame(left);self.export_files.grid_columnconfigure(0,weight=1)
        self.pathrow(self.export_files,0,'source','MW7 导出目录（animpkgs 或 MW7 根目录）',fields=self.export_fields,compact=True)
        self.pathrow(self.export_files,1,'clips','CAST 线索目录（选填，默认同级 animations）',fields=self.export_fields,compact=True)
        files=self.frame(left); files.pack(fill='x',pady=(0,5)); files.grid_columnconfigure(0,weight=1)
        self.shared_files=files
        self.saluki_label=tk.StringVar()
        caption=self.pathrow(files,0,'saluki','',compact=True)
        caption.configure(textvariable=self.saluki_label)
        self.pathrow(files,1,'output','结果目录',compact=True)
        opts=self.frame(bottom); opts.pack(fill='x',before=task_caption)
        self.label(opts,'搜索范围',9,MUTED).pack(side='left',padx=(0,8))
        for key in ('xmodel','xanim','sound_asset','sound_alias','material','image'):
            ttk.Checkbutton(opts,text=core.LABEL[key],variable=self.kinds[key]).pack(side='left',padx=(0,9))
        extras=self.frame(self.advanced_panel); extras.pack(fill='x')
        self.label(extras,'额外池',9,MUTED).pack(side='left',padx=(0,8))
        ttk.Checkbutton(extras,text='动画包（animpkg）',variable=self.kinds['animpkg']).pack(side='left',padx=(0,9))
        self.label(extras,'模型仅导出本地 CSV；运行前自动同步 PR、读取原库与历史 CDB。',9,MUTED).pack(side='left')
        tune=self.frame(self.advanced_panel); tune.pack(fill='x',pady=(2,6))
        self.continuous_btn=ttk.Checkbutton(tune,text='持续搜索',variable=self.continuous,command=self.refresh_search_mode)
        self.continuous_btn.pack(side='left',padx=(0,10))
        self.adaptive_btn=ttk.Checkbutton(tune,text='自动换方法',variable=self.adaptive)
        self.adaptive_btn.pack(side='left',padx=(0,8))
        self.limit_label=self.label(tune,'限额（百万）',9,MUTED)
        self.limit_label.pack(side='left')
        self.limit_spin=ttk.Spinbox(tune,from_=1,to=500,width=5,textvariable=self.fields['millions'])
        self.limit_spin.pack(side='left',padx=(4,10))
        self.label(tune,'名称关键词',9,MUTED).pack(side='left')
        ttk.Entry(tune,width=11,textvariable=self.fields['keyword']).pack(side='left',expand=True,fill='x',padx=(5,0))
        advanced_toggle=ttk.Checkbutton(bottom,text='高级选项',variable=self.advanced,command=self.toggle_advanced)
        self.snapshot_options=(opts,advanced_toggle,task_caption)
        self.search_bottom=bottom;self.search_bar=bar
        self.start_btn=ttk.Button(bar,command=self.start_selected);self.start_btn.pack(side='left')
        self.resume_btn=ttk.Button(bar,text='继续搜索',style='Primary.TButton',command=lambda:self.start(True))
        self.stop_btn=ttk.Button(bar,command=self.stop,state='disabled')
        self.alias_btn=ttk.Button(bar,text='指定别名键',command=lambda:self.pick('alias_keys','aliases'))
        right=self.frame(pane,DEEP); right.grid(row=0,column=1,sticky='nsew',padx=(0,14),pady=(14,15))
        logbar=self.frame(right,DEEP);logbar.pack(fill='x',padx=13,pady=(13,9))
        self.label(logbar,'运行记录',12,'#f0fbf1',DEEP,weight='bold').pack(side='left')
        self.label(logbar,color='#a8c6b3',bg=DEEP,textvariable=self.run_text).pack(side='right')
        holder=self.frame(right,DEEP);holder.pack(fill='both',expand=True,padx=(11,8),pady=(0,11))
        self.log=tk.Text(holder,wrap='word',width=36,highlightthickness=0,borderwidth=0,bg=DEEP,fg=LOG,
                         insertbackground=LOG,font=(FONT,9),padx=3,pady=4,state='disabled')
        scroll=ttk.Scrollbar(holder,command=self.log.yview);self.log.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.log.pack(side='left',fill='both',expand=True)
        self.log_line('选择目标来源：资产快照，或 Saluki 的 MW7 导出目录。')

    def toggle_advanced(self):
        self.advanced_panel.pack_forget()
        if self.advanced.get() and self.source_mode.get()==SNAPSHOT_MODE:
            self.advanced_panel.pack(fill='x',before=self.search_bottom,pady=(4,4))

    def change_search_mode(self):
        self.show_search_mode()
        self.refresh_buttons()
        self.save_settings()

    def show_search_mode(self):
        exporting=self.source_mode.get()==EXPORT_MODE
        self.snapshot_files.pack_forget();self.export_files.pack_forget()
        current=self.export_files if exporting else self.snapshot_files
        current.pack(fill='x',before=self.shared_files,pady=(4,0))
        for widget in (self.detect_btn,self.task_load,*self.snapshot_options,self.resume_btn,self.alias_btn,self.stop_btn):
            widget.pack_forget()
        if exporting:
            self.saluki_label.set('Saluki 原库（留空时从标准导出路径自动识别）')
        else:
            self.saluki_label.set('Saluki 安装目录或 hash_pkg（选填）')
            self.detect_btn.pack(side='right')
            self.task_load.pack(side='right',padx=(0,5))
            for widget in self.snapshot_options:
                widget.pack(before=self.search_bar,fill='x',pady=(0,4))
            self.resume_btn.pack(side='left',padx=5)
            self.alias_btn.pack(side='right')
        self.stop_btn.pack(side='left',padx=(5,0))
        self.stop_btn.configure(text='取消' if exporting else '暂停')
        self.start_btn.configure(text='验证并生成 CDB' if exporting else '开始查找并生成 CDB',style='Primary.TButton')
        self.toggle_advanced()
        self.search_canvas.yview_moveto(0)

    def start_selected(self):
        if self.source_mode.get()==EXPORT_MODE:
            self.start_export_verification()
        else:
            self.start(False)

    def start_export_verification(self):
        if self.busy:return
        path=lambda fields,k:fields[k].get().strip().strip('"')
        src,clips=(path(self.export_fields,k) for k in ('source','clips'))
        out,local,table=(path(self.fields,k) for k in ('output','saluki','tables'))
        if not src or not out:
            self.status.set('请选择 MW7 导出目录和结果目录。');return
        csv_dirs=()
        if table:
            p=Path(table)
            csv_dirs=(p/'csv' if (p/'csv').is_dir() else p,)
        self.export_cancel.clear()
        self.save_settings()
        def check():
            if self.export_cancel.is_set():raise InterruptedError('已取消名称验证。')
        def work(log):
            known=()
            try:
                synced,_=sources.latest_cdb(self.settings)
                if synced:known=(synced,)
            except (OSError,ValueError,KeyError) as e:
                log(f'未读取到同步缓存：{e}')
            return animpkg.run(src,clips,out,saluki=local,csv_dirs=csv_dirs,cdb_dirs=known,check=check,log=log)
        def finish(value):
            folder,report=value
            self.log_line(f'待确认 {report.packages_unresolved:,} 个包 ID、{report.clips_unresolved:,} 个动画 ID。')
            self.show_export_result(folder,*animpkg.read_confirmed(folder/animpkg.CONFIRMED))
        self.go(CONFIRM_WORK,work,finish)

    def result_layout(self):
        p=self.result
        surface=self.frame(p);surface.pack(fill='both',expand=True,padx=23,pady=18)
        self.label(surface,'搜索结果',19,weight='bold').pack(anchor='w')
        self.label(surface,textvariable=self.count_text,color=MUTED,wraplength=880).pack(anchor='w',fill='x',pady=(6,12))
        self.tree=ttk.Treeview(surface,columns=('type','new','file'),show='headings',height=8)
        for col,text,width in [('type','资产类型',145),('new','确认名称',128),('file','导出文件',400)]:
            self.tree.heading(col,text=text);self.tree.column(col,width=width,stretch=col=='file')
        self.tree.pack(fill='both',expand=True)
        self.label(surface,'TXT 保存在 txt；合并后的 CDB 保存在 saluki-ready/hash_pkg，可放入 Saluki 的 hash_pkg。',10,MUTED,wraplength=840).pack(anchor='w',pady=(12,8))
        actions=self.frame(surface);actions.pack(fill='x')
        self.result_open=ttk.Button(actions,text='打开结果目录',command=self.open_result,state='disabled');self.result_open.pack(side='left')
        self.txt_open=ttk.Button(actions,text='打开 TXT',command=lambda:self.open_result('txt'),state='disabled');self.txt_open.pack(side='left',padx=(6,0))
        self.cdb_open=ttk.Button(actions,text='打开合并 CDB',command=lambda:self.open_result('saluki-ready/hash_pkg'),state='disabled');self.cdb_open.pack(side='left',padx=8)
        self.unresolved_open=ttk.Button(actions,text='打开缺名清单',command=lambda:self.open_result('unresolved_names.csv'),state='disabled');self.unresolved_open.pack(side='left')
        self.load_btn=ttk.Button(actions,text='载入以前的结果',command=self.load_result);self.load_btn.pack(side='right')

    def pr_layout(self):
        pane=self.contribution
        surface=self.frame(pane);surface.pack(fill='both',expand=True,padx=23,pady=18)
        self.label(surface,'更新与 PR 预览',19,weight='bold').pack(anchor='w')
        self.label(surface,'搜索结束自动保留本地 PR 预览，不提交到 GitHub。联网更新与核对是可选操作。',10,MUTED,wraplength=860).pack(anchor='w',pady=(6,12))
        tokenbox=self.frame(surface);tokenbox.pack(fill='x')
        self.label(tokenbox,'GitHub Token',9,MUTED).pack(side='left')
        ttk.Entry(tokenbox,textvariable=self.token,show='*',width=33).pack(side='left',padx=9)
        ttk.Button(tokenbox,text='申请 public_repo Token',command=lambda:webbrowser.open('https://github.com/settings/tokens/new?scopes=public_repo&description=Slasher%20Studio')).pack(side='left')
        self.label(surface,'Token 仅用于本次打开的软件，不保存到磁盘。',9,MUTED).pack(anchor='w',pady=(5,12))
        actions=self.frame(surface);actions.pack(fill='x',pady=(0,10))
        self.sync_btn=ttk.Button(actions,text='同步 PR',command=self.sync);self.sync_btn.pack(side='left')
        self.sync_cdb_btn=ttk.Button(actions,text='同步最新 CDB',command=self.sync_cdb);self.sync_cdb_btn.pack(side='left',padx=(5,0))
        self.preview_btn=ttk.Button(actions,text='生成 PR 预览',command=self.make_preview,state='disabled');self.preview_btn.pack(side='left',padx=7)
        self.preview_open=ttk.Button(actions,text='打开完整预览',command=self.open_preview,state='disabled');self.preview_open.pack(side='left')
        self.publish_btn=ttk.Button(actions,text='公开提交 PR',style='Primary.TButton',command=self.send,state='disabled')
        self.label(surface,textvariable=self.pr_status,color=GREEN,wraplength=900).pack(fill='x',anchor='w',pady=(0,9))
        self.url_btn=ttk.Button(surface,text='在 GitHub 查看 PR',state='disabled',command=lambda:webbrowser.open(self.pr_url))
        self.url_btn.pack(side='bottom',anchor='e',pady=(9,0))
        details=self.frame(surface);details.pack(fill='both',expand=True)
        self.preview_text=tk.Text(details,wrap='word',height=10,fg=INK,bg='#eff5ef',relief='flat',bd=0,
                                  highlightthickness=1,highlightbackground=LINE,padx=11,pady=11,font=(FONT,10),state='disabled')
        sb=ttk.Scrollbar(details,command=self.preview_text.yview);self.preview_text.configure(yscrollcommand=sb.set)
        sb.pack(side='right',fill='y');self.preview_text.pack(side='left',fill='both',expand=True)

    def pick(self,key,mode,fields=None):
        if self.busy:return
        fields=self.fields if fields is None else fields
        if mode=='dir':
            value=filedialog.askdirectory(parent=self.root)
        else:
            masks={'snapshot':[('资产快照','*.ids')],'candidates':[('名称名单','*.txt *.csv')],
                   'aliases':[('原始 64 位键','*.txt *.gz')]}
            value=filedialog.askopenfilename(parent=self.root,filetypes=masks[mode]+[('所有文件','*')])
        if value:
            fields[key].set(value)
            self.status.set('路径已更新，可开始核对。')

    def detect(self):
        if self.busy:return
        root=filedialog.askdirectory(parent=self.root)
        if not root:return
        p=Path(root)
        areas=[p,p/'snapshots',p/'fresh-retail-with-aliases',p/'mw7-snapshots'/'fresh-retail-with-aliases']
        ids=[i for area in areas for i in area.glob('*.ids') if i.with_suffix('.pools.txt').exists()]
        preferred=[i for i in ids if i.name.casefold()=='modwar7.ids']
        if len(ids)==1 or len(preferred)==1:
            self.fields['snapshot'].set(str((preferred or ids)[0]))
        for t in [p,p/'csv',p/'cod-name-db',p/'cod-name-db'/'csv']:
            if (t/'fnv1a_xanims_v2.csv').is_file():self.fields['tables'].set(str(t));break
        for t in [p/'hash_pkg',p/'saluki'/'hash_pkg',p/'saluki',p]:
            if (t/'fnv1a_xanims_v2.cdb').is_file():self.fields['saluki'].set(str(t));break
        self.status.set('已填入识别到的资料；未识别的路径可手动选择。')

    def inputs(self):
        vals={k:f.get().strip() for k,f in self.fields.items()}
        vals['continuous']=self.continuous.get()
        vals['adaptive']=self.adaptive.get()
        vals['engine']=ENGINES[self.engine.get()]
        try: vals['millions']=int(vals['millions'])
        except ValueError: vals['millions']=0
        if not 1<=vals['millions']<=500:
            if not vals['continuous']:raise ValueError('限额应为 1 到 500 百万。')
            vals['millions']=10
        vals['kinds']=tuple(k for k,f in self.kinds.items() if f.get())
        return core.Inputs(**vals)

    def save_settings(self):
        try:
            self.settings.mkdir(parents=True,exist_ok=True)
            try:d=asdict(self.inputs())
            except ValueError:
                try:d=json.loads((self.settings/'settings.json').read_text(encoding='utf-8'))
                except (OSError,ValueError):d={}
            d['active_task']=str(self.task_dir) if self.task_dir else ''
            d.pop('package_fields',None)
            d['source_mode']=self.source_mode.get()
            d['priority_version']=1
            d['export_inputs']={k:v.get() for k,v in self.export_fields.items()}
            sources.atomic_bytes(self.settings/'settings.json',json.dumps(d,ensure_ascii=False,indent=2).encode('utf-8'))
        except (OSError,ValueError):pass

    def log_line(self,message):
        self.log.configure(state='normal')
        self.log.insert('end',str(message)+'\n')
        self.log_len+=1
        if self.log_len>550:
            self.log.delete('1.0','51.0');self.log_len-=50
        self.log.see('end');self.log.configure(state='disabled')

    def refresh_search_mode(self):
        limited=not self.continuous.get()
        self.continuous_btn.configure(state='disabled' if self.busy else 'normal')
        self.adaptive_btn.configure(state='disabled' if self.busy else 'normal')
        self.limit_spin.configure(state='normal' if limited and not self.busy else 'disabled')
        self.limit_label.configure(fg=MUTED if limited else '#65736a')

    def refresh_buttons(self):
        running=bool(self.busy)
        exporting=self.source_mode.get()==EXPORT_MODE
        self.refresh_search_mode()
        self.engine_picker.configure(state='disabled' if running else 'readonly')
        self.mode_picker.configure(state='disabled' if running else 'readonly')
        for b in (self.start_btn,self.sync_btn,self.sync_cdb_btn,self.load_btn,self.detect_btn,self.task_load):
            b.configure(state='disabled' if running else 'normal')
        self.start_btn.configure(style='TButton' if self.task_dir and not exporting else 'Primary.TButton')
        self.resume_btn.configure(state='normal' if self.task_dir and not running else 'disabled')
        self.stop_btn.configure(state='normal' if self.busy in ('搜索',CONFIRM_WORK) else 'disabled')
        self.preview_btn.configure(state='normal' if not running and self.ctx and any(k in core.TABLE for k in self.rows)
                                   else 'disabled')
        self.publish_btn.configure(state='normal' if not running and self.preview and self.preview.count and not self.pr_url else 'disabled')
        self.result_open.configure(state='normal' if self.run_dir else 'disabled')
        self.txt_open.configure(state='normal' if self.run_dir and (self.run_dir/'txt').is_dir() else 'disabled')
        self.cdb_open.configure(state='normal' if self.run_dir and (self.run_dir/animpkg.CDB_DIR).is_dir() else 'disabled')
        self.unresolved_open.configure(state='normal' if self.run_dir and (self.run_dir/'unresolved_names.csv').is_file() else 'disabled')
        self.preview_open.configure(state='normal' if self.preview or
                                    (self.run_dir and (self.run_dir/'pr-preview.txt').is_file()) else 'disabled')
        self.url_btn.configure(state='normal' if self.pr_url else 'disabled')

    def go(self,label,work,finish):
        if self.busy:return
        self.busy=label;self.callback=finish;self.status.set(label+'中…');self.run_text.set(label+'中')
        self.progress.start(12);self.refresh_buttons()
        def thread():
            try: self.events.put(('done',work(lambda s:self.events.put(('log',s)))))
            except Exception as e: self.events.put(('error',str(e)))
        threading.Thread(target=thread,daemon=True).start()

    def tick(self):
        for _ in range(100):
            try: kind,value=self.events.get_nowait()
            except queue.Empty:break
            if kind=='log':self.log_line(value);continue
            if kind=='task':
                self.task_dir=value;self.save_settings();continue
            if kind=='checkpoint':
                self.update_checkpoint(*value);continue
            label=self.busy
            self.busy=None;self.progress.stop()
            self.run_text.set('已完成' if kind=='done' else '已停止')
            if kind=='done':
                self.status.set(label+'完成');self.callback(value)
            else:
                self.status.set(value);self.run_text.set('未完成')
                if label not in ('搜索',CONFIRM_WORK):self.pr_status.set(value)
                self.log_line(value)
            self.refresh_buttons()
            if self.close_requested:
                self.save_settings();self.token.set('');self.root.destroy();return
        self.root.after(70,self.tick)

    def api(self,secret):
        return community.GitHubApi(secret,self.settings/'github-cache')

    def update_checkpoint(self,done,maximum):
        suffix='当前候选已检查' if done==maximum else f'{done*100/maximum:.1f}%'
        self.task_text.set(f'已完成 {done:,} / {maximum:,} 个候选 · {suffix}')

    def load_task(self):
        p=filedialog.askdirectory(title='选择含 task.sqlite3 的任务目录',parent=self.root)
        if not p:return
        folder=Path(p)
        try:
            saved,done,maximum,_=tasks.overview(folder)
        except Exception as e:
            self.status.set(str(e));return
        self.task_dir=folder
        for k,field in self.fields.items():
            if k in saved:field.set(saved[k])
            elif k=='evidence':field.set('')
        self.fields['output'].set(str(folder.parent))
        for k,flag in self.kinds.items():flag.set(k in saved['kinds'])
        if any(s in saved.get('snapshot','').lower() for s in ('mw7','modwar7')):
            self.kinds['xmodel'].set(True)
        self.update_checkpoint(done,maximum)
        self.status.set('任务已载入。原电脑路径失效时，重新指定相同快照和可用的名称库。')
        self.save_settings();self.refresh_buttons()

    def start(self, continuing):
        try: source=self.inputs()
        except ValueError as e: self.status.set(str(e));return
        selected=self.task_dir if continuing else None
        if not continuing and self.task_dir and not messagebox.askyesno('新建搜索任务','新任务会重新生成候选队列。要继续已经跑过的队列，请点「继续搜索」。确定新建？',parent=self.root):
            return
        self.save_settings()
        self.job=tasks.Pause()
        self.tabs.select(self.search)
        self.preview=None;self.pr_url=''
        def work(log):
            ctx=core.inspect(source)
            log(f'{ctx.game}：加载 {ctx.count:,} 条资产记录。')
            path=selected or tasks.create(ctx,self.job,log)
            self.events.put(('task',path))
            out,rows,done,maximum,status=tasks.resume(path,ctx,self.settings,self.job,log,lambda d,m:self.events.put(('checkpoint',(d,m))))
            return ctx,out,rows,done,maximum,status
        def finish(value):
            ctx,out,rows,done,maximum,status=value
            self.update_checkpoint(done,maximum)
            self.show_result((ctx,out,rows))
            self.status.set(f'{status}；累计确认 {sum(map(len,rows.values())):,} 条。')
            if done<maximum:self.tabs.select(self.search)
        self.go('搜索',work,finish)

    def stop(self):
        if self.job and self.busy=='搜索':
            self.job.stop();self.status.set('当前批结束后暂停，进度会保留。')
        elif self.busy==CONFIRM_WORK:
            self.export_cancel.set();self.status.set('正在取消名称验证…')

    def show_result(self,value):
        self.ctx,self.run_dir,self.rows=value
        self.preview=None;self.pr_url=''
        self.tree.delete(*self.tree.get_children())
        for kind,values in self.rows.items():
            output=f'txt/{kind}.txt → '+core.TABLE[kind]+'.cdb' if kind in core.TABLE else 'new_names.csv（本地）'
            self.tree.insert('','end',values=(core.LABEL[kind],f'{len(values):,}',output))
        count=sum(map(len,self.rows.values()))
        self.count_text.set(f'{self.ctx.game} · 确认 {count:,} 条名称\n{self.run_dir}')
        self.status.set(f'已生成 TXT 和 CDB，新增 {count:,} 条名称。' if count else '本轮没有新命中；已保存进度，有新线索后可继续。')
        self.pr_status.set('已保留本地 PR 预览，未公开提交。' if any(k in core.TABLE for k in self.rows)
                           else '当前只有本地模型结果，不进入投稿。' if count else '当前结果没有可投稿的新增名称。')
        self.set_preview_text('')
        if (self.run_dir/'pr-preview.txt').is_file():
            self.set_preview_text((self.run_dir/'pr-preview.txt').read_text(encoding='utf-8'))
        self.tabs.select(self.result)

    def open_result(self,child=''):
        if self.run_dir and (self.run_dir/child).exists():os.startfile(self.run_dir/child)

    def show_export_result(self,folder,confirmed,added):
        self.ctx=None;self.run_dir=folder;self.rows=added
        self.preview=None;self.pr_url=''
        self.tree.delete(*self.tree.get_children())
        for kind,found in confirmed.items():
            cdb=core.TABLE[kind]+'.cdb'
            filename=cdb if (folder/animpkg.CDB_DIR/cdb).is_file() else '无需更新'
            self.tree.insert('','end',values=(core.LABEL[kind],f'{len(found):,}',filename))
        count=sum(map(len,confirmed.values()))
        new=sum(map(len,added.values()))
        result=(f'确认 {count:,} 条，已合进原库副本 {new:,} 条。' if new else
                f'确认 {count:,} 条，原库无须更新。' if count else '没有确认出名称，原库未改变。')
        self.status.set(result);self.count_text.set(f'MW7 · {result}\n{folder}')
        self.pr_status.set('此结果用于 Saluki 名称库。快照搜索的投稿流程仍可用。')
        self.set_preview_text('')
        self.tabs.select(self.result)

    def load_result(self):
        p=filedialog.askdirectory(parent=self.root)
        if not p:return
        directory=Path(p)
        def work(log):
            if (directory/animpkg.CONFIRMED).is_file():
                return True,(directory,*animpkg.read_confirmed(directory/animpkg.CONFIRMED))
            data=json.loads((directory/'project.json').read_text(encoding='utf-8'))
            data['kinds']=tuple(data['kinds'])
            ctx=core.inspect(core.Inputs(**data))
            picked={}
            with (directory/'new_names.csv').open(encoding='utf-8',newline='') as f:
                for row in csv.DictReader(f):
                    k, key, n=row['type'],int(row['hash'],16),row['name']
                    if k not in core.SUPPORTED_KINDS or not core.matches(ctx,k,key,n):raise ValueError('旧结果存在无法回算的名称。')
                    picked.setdefault(k,{})[key]=n
            return False,(ctx,directory,picked)
        def finish(value):
            export,result=value
            if export:self.show_export_result(*result)
            else:self.show_result(result)
        self.go('读取结果',work,finish)

    def secret(self):
        value=self.token.get().strip()
        if not value:
            self.pr_status.set('请输入 GitHub Token。离线搜索不需要 Token。')
        return value

    def sync(self):
        token=self.secret()
        if not token:return
        try: source=self.inputs()
        except ValueError as e: self.pr_status.set(str(e));return
        def action(log):
            ctx=core.inspect(source)
            api=self.api(token);api.request('GET','/user')
            rows,stamp=community.live_claims(ctx,api,log)
            sources.save_survey(self.settings,ctx,rows,stamp)
            return len(rows),stamp
        def finish(result):
            count,stamp=result
            self.pr_status.set(f'已同步 {stamp["open"]} 个开放 PR；匹配 {count:,} 条社区记录，续跑时会排除。')
        self.go('刷新排除',action,finish)

    def sync_cdb(self):
        self.go('同步 CDB',lambda log:sources.fetch_latest_cdb(self.settings,log),
                lambda version:self.pr_status.set(f'已同步官方 CDB {version}。下次搜索或继续时生效。'))

    def make_preview(self):
        if not self.ctx:return
        token=self.secret()
        if not (token and self.rows):return
        self.preview=None;self.pr_url=''
        def finish(pr):
            self.preview=pr
            self.pr_status.set(f'可投稿 {pr.count:,} 条。账号：{pr.login}' if pr.count else '没有符合上游要求的可投稿记录；本地结果仍保留。')
            excluded='\n'.join(f'{k}：{v:,}' for k,v in pr.excluded.items())
            sample='\n'.join(line for file,text in pr.files.items() if file.endswith('.txt') for line in text.splitlines()[:5])
            self.set_preview_text(f'{pr.title}\n\n{excluded}\n\n{sample}\n\n完整投稿内容见“打开完整预览”。')
        self.go('核对投稿',lambda log:community.prepare(self.ctx,self.rows,self.api(token),self.run_dir,log),finish)

    def set_preview_text(self,message):
        self.preview_text.configure(state='normal');self.preview_text.delete('1.0','end');self.preview_text.insert('1.0',message);self.preview_text.configure(state='disabled')

    def open_preview(self):
        if self.run_dir and (self.run_dir/'pr-preview.txt').exists():os.startfile(self.run_dir/'pr-preview.txt')

    def send(self):
        token=self.secret()
        if not (token and self.preview and self.preview.count):return
        choice=messagebox.askyesno('公开投稿',f'以 {self.preview.login} 向 Hash Slinging Slasher 提交预览中的 {self.preview.count:,} 条名称？',parent=self.root)
        if not choice:return
        def finish(url):
            self.pr_url=url
            self.pr_status.set('PR 已创建：'+url)
            (self.run_dir/'pull-request.txt').write_text(url+'\n',encoding='utf-8')
        self.go('提交 PR',lambda log:community.publish(self.preview,self.api(token),log),finish)

    def close(self):
        if self.busy=='提交 PR':
            self.status.set('请等待 GitHub 返回结果再关闭工作台。');return
        if self.busy in ('搜索',CONFIRM_WORK):
            self.close_requested=True
            self.stop()
            return
        self.save_settings();self.token.set('');self.root.destroy()


def main():
    if len(sys.argv)==3 and sys.argv[1]=='--ui-smoke':
        p=Path(sys.argv[2])
        root=tk.Tk();root.withdraw()
        try:
            app=Studio(root);root.update_idletasks()
            report={'title':root.title(),'tabs':len(app.tabs.tabs()),
                    'primary_action':app.start_btn.cget('text'),
                    'advanced_collapsed':not bool(app.advanced_panel.winfo_manager()),
                    'publish_hidden':not bool(app.publish_btn.winfo_manager()),
                    'path_fields':[k for k in ('snapshot','tables','saluki','output') if k in app.fields]}
            assert report['tabs']==3 and report['advanced_collapsed'] and report['publish_hidden']
            app.advanced.set(True);app.toggle_advanced();root.update_idletasks()
            assert app.advanced_panel.winfo_manager()=='pack'
            app.source_mode.set(EXPORT_MODE);app.show_search_mode();root.update_idletasks()
            assert not app.advanced_panel.winfo_manager()
            p.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:root.destroy()
        return
    if len(sys.argv)==3 and sys.argv[1]=='--animpkg-smoke':
        p=Path(sys.argv[2]);d=json.loads(p.read_text(encoding='utf-8'))
        logs=[]
        out,report=animpkg.run(d['source'],d.get('clips'),d['output'],saluki=d.get('saluki'),
                               csv_dirs=tuple(map(Path,d.get('csv_dirs',()))),cdb_dirs=tuple(map(Path,d.get('cdb_dirs',()))),
                               log=logs.append)
        p.with_suffix('.result.json').write_text(
            json.dumps({'output':str(out),**asdict(report),'log':logs},ensure_ascii=False),encoding='utf-8')
        return
    if len(sys.argv)==3 and sys.argv[1]=='--smoke':
        p=Path(sys.argv[2]); d=json.loads(p.read_text(encoding='utf-8')); d['kinds']=tuple(d['kinds'])
        ctx=core.inspect(core.Inputs(**d))
        logs=[]
        root=tasks.create(ctx,tasks.Pause(),logs.append)
        out,rows,done,maximum,status=tasks.resume(root,ctx,p.parent,tasks.Pause(),logs.append,override_claims=set())
        (p.with_suffix('.result.json')).write_text(json.dumps({'output':str(out),'task':str(root),'rows':sum(map(len,rows.values())),
                                                            'done':done,'maximum':maximum,'log':logs},ensure_ascii=False),encoding='utf-8')
        return
    root=tk.Tk();app=Studio(root);root.mainloop()


if __name__=='__main__':main()
