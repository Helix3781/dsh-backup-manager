#!/usr/bin/env python3
"""DSH 备份管理器（回滚 / 批量删除 / 备注标记 / 一键备份 / pnpm 修复）
扫描 ~/.dsh/profiles/desktop 下的 *.bak-* 备份，按标签分组管理。
零依赖，仅 Python 标准库。启动：python3 dsh_rollback.py [端口]
"""
import json
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROFILE = Path.home() / ".dsh" / "profiles" / "desktop"
TRASH = PROFILE / ".backup-trash"          # 删除的备份先进回收目录
NOTES_FILE = PROFILE / ".backup-notes.json"  # 备份组备注（标签 -> 备注）
ASAR = Path("/Applications/DeepSeek Harness.app/Contents/Resources/app.asar")
ASAR_CACHE = Path.home() / ".dsh" / "cache" / "asar-extract"  # asar 解包缓存
BAK_RE = re.compile(r"^(?P<base>.+)\.bak-(?P<tag>.+)$")
# 备份按钮里可选的 DSH 配置文件（存在才会显示）
BACKUPABLE = ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml",
              "cordis.patch.yml", "cordis.yml"]
PORT = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 19399

JOB = {"running": False, "log": [], "done": False, "ok": None}  # 修复任务状态
LOCK = threading.Lock()


def log(msg):
    with LOCK:
        JOB["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")


def load_notes():
    try:
        return json.loads(NOTES_FILE.read_text())
    except Exception:
        return {}


def save_notes(notes):
    NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=1))


def scan_groups():
    groups = {}
    if not PROFILE.exists():
        return []
    for f in PROFILE.glob("*.bak-*"):
        m = BAK_RE.match(f.name)
        if not m or not f.is_file():
            continue
        g = groups.setdefault(m.group("tag"), [])
        g.append({"base": m.group("base"), "mtime": f.stat().st_mtime, "size": f.stat().st_size})
    notes = load_notes()
    out = []
    for tag, files in groups.items():
        files.sort(key=lambda x: x["base"])
        out.append({"tag": tag, "files": files,
                    "latest": max(f["mtime"] for f in files),
                    "size": sum(f["size"] for f in files),
                    "note": notes.get(tag, "")})
    # 有备注（被标记）的组排前面，其余按时间新→旧
    out.sort(key=lambda g: (g["note"] == "", -g["latest"]))
    return out


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def safety_backup(bases):
    ts = time.strftime("%Y%m%d-%H%M%S")
    made = []
    for b in bases:
        cur = PROFILE / b
        if cur.exists():
            dst = PROFILE / f"{b}.bak-before-rollback-{ts}"
            shutil.copy2(cur, dst)
            made.append(dst.name)
    return made, ts


def restore(tag, bases):
    bases = bases or []
    if not bases:
        return False, [], ["未选择任何文件"], []
    backup_names, _ = safety_backup(bases)
    restored, errors = [], []
    for b in bases:
        src = PROFILE / f"{b}.bak-{tag}"
        dst = PROFILE / b
        if not src.is_file():
            errors.append(f"{b}: 备份不存在 ({src.name})")
            continue
        try:
            shutil.copy2(src, dst)
            restored.append(b)
        except OSError as e:
            errors.append(f"{b}: {e}")
    return not errors, restored, errors, backup_names


def delete_tags(tags):
    if not tags:
        return 0, ["未选择任何备份组"], ""
    ts = time.strftime("%Y%m%d-%H%M%S")
    moved, errors, subdir = 0, [], ""
    for tag in tags:
        if not isinstance(tag, str) or "/" in tag or tag.startswith("."):
            errors.append(f"非法标签: {tag}")
            continue
        sub = TRASH / f"{ts}-{tag}"
        hit = False
        for f in PROFILE.glob(f"*.bak-{tag}"):
            if not f.is_file():
                continue
            hit = True
            try:
                sub.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(sub / f.name))
                moved += 1
                subdir = str(sub)
            except OSError as e:
                errors.append(f"{f.name}: {e}")
        if not hit:
            errors.append(f"{tag}: 没有可删除的备份文件")
        else:
            notes = load_notes()
            if tag in notes:
                del notes[tag]
                save_notes(notes)
    return moved, errors, subdir


def rename_tag(tag, new_tag):
    """把整组 *.bak-<tag> 改名为 *.bak-<new_tag>，备注一并迁移。"""
    tag, new_tag = (tag or "").strip(), (new_tag or "").strip()
    if not tag or not new_tag:
        return False, ["新旧名称都不能为空"], 0
    if "/" in new_tag or new_tag.startswith(".") or new_tag == tag:
        return False, ["新名称非法或与原名相同"], 0
    srcs = sorted(PROFILE.glob(f"*.bak-{tag}"))
    if not srcs:
        return False, [f"{tag}: 没有找到这组备份"], 0
    for s in srcs:
        dst = PROFILE / (s.name[:-len(tag)] + new_tag)
        if dst.exists():
            return False, [f"已存在 {new_tag} 的备份组（{dst.name} 冲突）"], 0
    moved = 0
    for s in srcs:
        dst = PROFILE / (s.name[:-len(tag)] + new_tag)
        try:
            s.rename(dst)
            moved += 1
        except OSError as e:
            return False, [f"{s.name}: {e}"], moved
    notes = load_notes()
    if tag in notes:
        notes[new_tag] = notes.pop(tag)
        save_notes(notes)
    return True, [], moved


def create_backup(label, bases):
    """把当前文件复制为 <base>.bak-<label>-<ts>。"""
    label = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (label or "").strip()).strip("-")
    bases = [b for b in (bases or []) if b in BACKUPABLE]
    if not bases:
        return None, ["未选择要备份的文件"], ""
    if not label:
        label = "手动备份"
    tag = f"{label}-{time.strftime('%Y%m%d-%H%M%S')}"
    errors, made = [], []
    for b in bases:
        cur = PROFILE / b
        if not cur.is_file():
            errors.append(f"{b}: 当前不存在，跳过")
            continue
        try:
            shutil.copy2(cur, PROFILE / f"{b}.bak-{tag}")
            made.append(b)
        except OSError as e:
            errors.append(f"{b}: {e}")
    return (tag if made else None), errors, tag


# ── pnpm / asar 修复 ────────────────────────────────────────────────────────
def run(cmd, **kw):
    log("$ " + " ".join(map(str, cmd)))
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    for line in (p.stdout or "").strip().splitlines()[-8:]:
        log("  " + line)
    if p.returncode != 0:
        for line in (p.stderr or "").strip().splitlines()[-5:]:
            log("  ! " + line)
    return p


def repair_job():
    try:
        JOB.update(running=True, done=False, ok=None, log=[])
        log("开始修复：pnpm 依赖树 + @deepseek-ai 核心包")
        # 1. 解包 asar（带缓存）
        if not (ASAR_CACHE / "dsh/node_modules/@deepseek-ai").is_dir():
            log("1/5 解包 app.asar（首次较慢）…")
            if ASAR_CACHE.exists():
                shutil.rmtree(ASAR_CACHE)
            ASAR_CACHE.mkdir(parents=True, exist_ok=True)
            p = run(["npx", "--yes", "@electron/asar", "extract", str(ASAR), str(ASAR_CACHE)])
            if p.returncode != 0 or not (ASAR_CACHE / "dsh/node_modules/@deepseek-ai").is_dir():
                raise RuntimeError("asar 解包失败，请确认 DeepSeek Harness 已安装")
        else:
            log("1/5 使用 asar 解包缓存")
        asar_ov = ASAR_CACHE / "dsh/node_modules/@deepseek-ai"
        # 2. pnpm install（先恢复依赖树）
        log("2/5 pnpm install 恢复依赖树…")
        run(["pnpm", "install"], cwd=PROFILE)
        # 3. pi-ai 依赖（必须是最后一个 pnpm 操作，之后不再跑 install/add，否则会 prune 掉第 4 步补的包）
        if not (PROFILE / "node_modules/@earendil-works/pi-ai").exists():
            log("3/5 补装 @earendil-works/pi-ai …")
            run(["pnpm", "add", "@earendil-works/pi-ai@0.87.1"], cwd=PROFILE)
        else:
            log("3/5 @earendil-works/pi-ai 已存在")
        # 4. 补齐/对齐 asar 里的所有缺失包（放在所有 pnpm 操作之后，避免被 prune）
        log("4/5 对齐核心包到宿主同代（补缺 + dsh-tools 强制宿主版）…")
        nm = PROFILE / "node_modules"
        nm.mkdir(parents=True, exist_ok=True)
        asar_nm = ASAR_CACHE / "dsh/node_modules"
        added = replaced = 0

        def copy_pkg(src, tgt, force=False):
            """复制一个包目录；已存在时仅 force 才替换。返回 'added'/'replaced'/None。"""
            nonlocal added, replaced
            if tgt.exists():
                if not force:
                    return None
                shutil.rmtree(tgt)
                replaced += 1
            shutil.copytree(src, tgt)
            added += 1
            return "added"

        for scope in sorted(asar_nm.iterdir()):
            if scope.name.startswith(".") or not scope.is_dir():
                continue
            if scope.name.startswith("@"):
                (nm / scope.name).mkdir(parents=True, exist_ok=True)
                for sub in sorted(scope.iterdir()):
                    if sub.name.startswith(".") or not sub.is_dir():
                        continue
                    force = scope.name == "@deepseek-ai" and sub.name == "dsh-tools"
                    copy_pkg(sub, nm / scope.name / sub.name, force)
            else:
                copy_pkg(scope, nm / scope.name)
        log(f"   补齐 {added} 个缺失包（其中强制替换 dsh-tools 等旧版 {replaced} 个）")
        # 5. 导入冒烟测试
        log("5/5 插件导入冒烟测试…")
        deps = []
        try:
            deps = [k for k, v in json.loads((PROFILE / "package.json").read_text())["dependencies"].items()
                    if not v.startswith("link:")]
        except Exception:
            pass
        bad = []
        for m in deps:
            p = subprocess.run(["node", "--input-type=module", "-e",
                                f"import('{m}').then(()=>0).catch(()=>process.exit(1))"],
                               capture_output=True, cwd=PROFILE / "node_modules", timeout=60)
            if p.returncode != 0:
                bad.append(m)
        if bad:
            log(f"⚠️ {len(bad)} 个插件导入失败：{', '.join(bad[:10])}")
            JOB["ok"] = False
        else:
            log(f"✅ 全部 {len(deps)} 个插件导入通过")
            JOB["ok"] = True
        log("修复流程结束。若改过配置，请完全退出并重启 DeepSeek Harness。")
    except Exception as e:  # noqa: BLE001
        log(f"❌ 修复失败：{e}")
        JOB["ok"] = False
    finally:
        JOB["running"] = False
        JOB["done"] = True


HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>DSH 备份管理器</title>
<style>
 body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:820px;margin:24px auto;padding:0 16px 40px;color:#1d1d1f;background:#f5f5f7}
 h1{font-size:22px} .sub{color:#6e6e73;font-size:13px;margin-bottom:12px}
 .toolbar{position:sticky;top:0;z-index:5;background:#f5f5f7;padding:10px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #e5e5ea;margin-bottom:14px}
 .toolbar .stat{font-size:13px;color:#6e6e73;margin-right:auto}
 button{background:#0a84ff;color:#fff;border:0;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}
 button:hover{background:#006edb} button:disabled{background:#b8b8bd;cursor:default}
 button.danger{background:#ff3b30}button.danger:hover{background:#d70015}
 button.green{background:#30a14e}button.green:hover{background:#268a40}
 button.gray{background:#8e8e93}
 label{font-size:12px;color:#6e6e73}
 .card{background:#fff;border-radius:12px;padding:14px 16px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
 .card.star{outline:2px solid #ffd60a;background:#fffdf2}
 .tag{font-weight:600;font-size:14px;word-break:break-all}
 .note{display:inline-block;background:#ffd60a33;border-radius:6px;padding:1px 8px;font-size:12px;margin-left:6px;color:#7a5b00}
 .meta{color:#86868b;font-size:12px;margin-top:2px}
 .files{color:#6e6e73;font-size:12px;margin:6px 0}
 .files code{background:#f0f0f2;border-radius:4px;padding:1px 5px;margin-right:6px;display:inline-block;margin-top:3px}
 .files code.off{opacity:.35}
 .msg{margin-top:6px;font-size:12px;white-space:pre-wrap}
 .ok{color:#1a7f37}.err{color:#d70015}
 .warn{background:#fff3cd;border:1px solid #ffe08a;border-radius:10px;padding:10px 14px;font-size:13px;margin-bottom:12px}
 dialog{border:0;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.25);padding:18px 20px;max-width:440px;width:92%}
 dialog h3{margin:0 0 10px;font-size:16px}
 dialog .row{margin:8px 0}
 dialog pre{background:#f0f0f2;border-radius:8px;padding:10px;font-size:11px;max-height:260px;overflow:auto;white-space:pre-wrap}
 dialog .btns{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
 input[type=text]{width:100%;box-sizing:border-box;padding:7px 10px;border:1px solid #ccc;border-radius:8px;font-size:13px}
</style></head><body>
<h1>DSH 备份管理器</h1>
<div class="sub" id="profile"></div>
<div class="warn">⟲ 回滚 = 用备份覆盖当前文件（回滚前自动把现状存为 <code>*.bak-before-rollback-*</code>）。&nbsp;⭐ 备注过的组会置顶。<br>
🗑 删除先进 <code>.backup-trash/</code> 回收目录，不是永久删除。&nbsp;改完配置都要<b>完全退出并重启 DeepSeek Harness</b>。</div>
<div class="toolbar">
 <span class="stat" id="stat"></span>
 <label><input type="checkbox" id="selall"> 全选</label>
 <button class="danger" id="del-sel">🗑 删除选中</button>
 <button class="green" id="btn-backup">⭐ 备份当前状态</button>
 <button class="gray" id="btn-repair">🔧 修复依赖(pnpm)</button>
</div>
<div id="list">加载中…</div>

<dialog id="dlg-backup">
 <h3>⭐ 备份当前状态</h3>
 <div class="row">备注名（会生成 <code>名称-时间戳</code> 标签）：<input type="text" id="bk-label" placeholder="例如：完美状态-全部插件正常"></div>
 <div class="row" id="bk-files"></div>
 <div class="btns"><button class="gray" onclick="dlgBackup.close()">取消</button><button id="bk-go">创建备份</button></div>
</dialog>
<dialog id="dlg-repair">
 <h3>🔧 修复依赖树</h3>
 <div class="row">流程：解包宿主 asar → <code>pnpm install</code> → 对齐 @deepseek-ai 核心包（dsh-tools 强制宿主 0.2 版）→ 补 pi-ai → 全插件导入冒烟测试。</div>
 <div class="row" id="rp-log" style="display:none"><pre id="rp-pre"></pre></div>
 <div class="btns"><button class="gray" onclick="dlgRepair.close()">关闭</button><button id="rp-go">开始修复</button></div>
</dialog>

<script>
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSize=n=>n>1048576?(n/1048576).toFixed(1)+'MB':n>1024?(n/1024).toFixed(1)+'KB':n+'B';
const fmtTime=ts=>new Date(ts*1000).toLocaleString('zh-CN',{hour12:false});
let poll=null;
async function load(){
 const r=await fetch('/api/backups');const d=await r.json();
 document.getElementById('profile').textContent='配置目录：'+d.profile;
 const nf=d.groups.reduce((s,g)=>s+g.files.length,0), sz=d.groups.reduce((s,g)=>s+g.size,0);
 document.getElementById('stat').textContent=`共 ${d.groups.length} 组 / ${nf} 个备份，占 ${fmtSize(sz)}`;
 document.getElementById('selall').checked=false;
 const list=document.getElementById('list');
 if(!d.groups.length){list.innerHTML='<div class="card">没有找到任何 *.bak-* 备份</div>';return;}
 list.innerHTML=d.groups.map(g=>`
  <div class="card ${g.note?'star':''}">
   <div class="tag"><input type="checkbox" class="pick" data-tag="${esc(g.tag)}"> ${esc(g.tag)}${g.note?`<span class="note">⭐ ${esc(g.note)}</span>`:''}</div>
   <div class="meta">${fmtTime(g.latest)} · ${g.files.length} 个文件 · ${fmtSize(g.size)}</div>
   <div class="files">${g.files.map(f=>`<code data-base="${esc(f.base)}">${esc(f.base)}</code>`).join('')}</div>
   <label><input type="checkbox" class="sel-all" checked> 全部文件都回滚</label>
   <button data-tag="${esc(g.tag)}">⟲ 回滚</button>
   <button class="gray" data-note="${esc(g.tag)}" data-cur="${esc(g.note||'')}">✏️ 备注</button>
   <button class="gray" data-rename="${esc(g.tag)}">📝 改名</button>
   <div class="msg"></div>
  </div>`).join('');
 document.getElementById('selall').onchange=e=>{
   document.querySelectorAll('.pick').forEach(c=>c.checked=e.target.checked);
 };
 document.querySelectorAll('.sel-all').forEach(cb=>cb.onchange=e=>{
   const card=e.target.closest('.card');
   card.querySelectorAll('.files code').forEach(c=>{c.classList.toggle('off',!e.target.checked);});
 });
 document.querySelectorAll('button[data-tag]').forEach(btn=>btn.onclick=async()=>{
   if(!confirm('确定用「'+btn.dataset.tag+'」覆盖当前文件？'))return;
   const card=btn.closest('.card');
   const bases=[...card.querySelectorAll('.files code')].filter(c=>!c.classList.contains('off')).map(c=>c.dataset.base);
   btn.disabled=true;const msg=card.querySelector('.msg');msg.textContent='回滚中…';msg.className='msg';
   try{
     const r=await fetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:btn.dataset.tag,bases})});
     const d=await r.json();
     msg.className='msg '+(d.ok?'ok':'err');
     msg.textContent=d.ok?('✅ 已覆盖：'+d.restored.join(', ')+(d.backups.length?('\\n现状已自动备份：'+d.backups.join(', ')):'')+'\\n请完全退出并重启 DeepSeek Harness。'):('❌ '+(d.errors||[]).join('\\n'));
   }catch(e){msg.className='msg err';msg.textContent='❌ 请求失败：'+e;}
   btn.disabled=false;
 });
 document.querySelectorAll('button[data-rename]').forEach(btn=>btn.onclick=async()=>{
   const nt=prompt('重命名这组备份（整组文件改名，备注一并迁移）：',btn.dataset.rename);
   if(nt===null||nt.trim()===''||nt.trim()===btn.dataset.rename)return;
   const r=await fetch('/api/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:btn.dataset.rename,new_tag:nt.trim()})});
   const d=await r.json();
   if(d.ok){load();}else{alert('❌ '+(d.errors||[]).join('\n'));}
 });
 document.querySelectorAll('button[data-note]').forEach(btn=>btn.onclick=async()=>{
   const note=prompt('给这组备份写个备注（留空清除），例如：完美状态-一切正常：',btn.dataset.cur);
   if(note===null)return;
   await fetch('/api/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tag:btn.dataset.note,note})});
   load();
 });
 document.getElementById('del-sel').onclick=async()=>{
   const tags=[...document.querySelectorAll('.pick')].filter(c=>c.checked).map(c=>c.dataset.tag);
   if(!tags.length){alert('先勾选要删除的备份组（或用顶部「全选」）');return;}
   if(!confirm('确定删除选中的 '+tags.length+' 组备份？\\n\\n'+tags.slice(0,8).join('\\n')+(tags.length>8?'\\n…':'')+'\\n\\n会先移入 .backup-trash/ 回收目录。'))return;
   const btn=document.getElementById('del-sel');btn.disabled=true;
   try{
     const r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tags})});
     const d=await r.json();
     if(d.ok){alert('✅ 已移入回收目录 '+d.moved+' 个文件\\n位置：'+d.trash);load();}
     else{alert('❌ '+(d.errors||[]).join('\\n'));}
   }catch(e){alert('❌ 请求失败：'+e);}
   btn.disabled=false;
 };
}
// 备份当前状态
const dlgBackup=document.getElementById('dlg-backup');
document.getElementById('btn-backup').onclick=async()=>{
  const r=await fetch('/api/backups');const d=await r.json();
  document.getElementById('bk-files').innerHTML=d.backupable.map(f=>{
    const miss=!f.exists;
    return `<label style="display:block;margin:4px 0"><input type="checkbox" class="bk-f" value="${esc(f.name)}" ${miss?'disabled':''} ${!miss?'checked':''}> ${esc(f.name)}${miss?'（当前不存在）':''}</label>`;
  }).join('');
  dlgBackup.showModal();
};
document.getElementById('bk-go').onclick=async()=>{
  const label=document.getElementById('bk-label').value;
  const bases=[...document.querySelectorAll('.bk-f')].filter(c=>c.checked).map(c=>c.value);
  const r=await fetch('/api/backup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label,bases})});
  const d=await r.json();
  if(d.ok){dlgBackup.close();document.getElementById('bk-label').value='';alert('✅ 已创建备份组：'+d.tag+'\\n包含：'+d.made.join(', '));load();}
  else{alert('❌ '+(d.errors||[]).join('\\n'));}
};
// pnpm 修复
const dlgRepair=document.getElementById('dlg-repair');
document.getElementById('btn-repair').onclick=()=>{dlgRepair.showModal();};
document.getElementById('rp-go').onclick=async()=>{
  const btn=document.getElementById('rp-go');btn.disabled=true;
  document.getElementById('rp-log').style.display='block';
  const pre=document.getElementById('rp-pre');
  await fetch('/api/repair',{method:'POST'});
  if(poll)clearInterval(poll);
  poll=setInterval(async()=>{
    const d=await(await fetch('/api/repair/status')).json();
    pre.textContent=d.log.join('\\n');
    if(d.done){
      clearInterval(poll);poll=null;btn.disabled=false;
      pre.textContent+='\\n\\n'+(d.ok?'✅ 修复完成，全部插件导入通过。':'⚠️ 修复流程结束，但仍有插件导入失败（见上方清单）。');
    }
  },1000);
};
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML, "text/html; charset=utf-8")
        elif self.path == "/api/backups":
            self._send(200, json.dumps({
                "profile": str(PROFILE), "groups": scan_groups(),
                "backupable": [{"name": b, "exists": (PROFILE / b).is_file()} for b in BACKUPABLE],
            }, ensure_ascii=False))
        elif self.path == "/api/repair/status":
            with LOCK:
                self._send(200, json.dumps(JOB, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path not in ("/api/restore", "/api/delete", "/api/note", "/api/backup", "/api/repair", "/api/rename"):
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/restore":
                ok, restored, errors, backups = restore(payload.get("tag", ""), payload.get("bases") or [])
                self._send(200, json.dumps({"ok": ok, "restored": restored, "errors": errors, "backups": backups}, ensure_ascii=False))
            elif self.path == "/api/delete":
                moved, errors, subdir = delete_tags(payload.get("tags") or [])
                self._send(200, json.dumps({"ok": not errors, "moved": moved, "errors": errors, "trash": subdir}, ensure_ascii=False))
            elif self.path == "/api/note":
                tag, note = payload.get("tag", ""), (payload.get("note") or "").strip()
                if not tag or "/" in tag:
                    self._send(200, json.dumps({"ok": False, "errors": ["非法标签"]}, ensure_ascii=False))
                    return
                notes = load_notes()
                if note:
                    notes[tag] = note
                else:
                    notes.pop(tag, None)
                save_notes(notes)
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            elif self.path == "/api/backup":
                tag, errors, made_tag = create_backup(payload.get("label", ""), payload.get("bases") or [])
                made = [b for b in (payload.get("bases") or []) if (PROFILE / f"{b}.bak-{made_tag}").is_file()] if made_tag else []
                self._send(200, json.dumps({"ok": bool(made_tag), "tag": made_tag, "made": made, "errors": errors}, ensure_ascii=False))
            elif self.path == "/api/rename":
                ok, errors, moved = rename_tag(payload.get("tag", ""), payload.get("new_tag", ""))
                self._send(200, json.dumps({"ok": ok, "moved": moved, "errors": errors}, ensure_ascii=False))
            else:  # /api/repair
                if JOB["running"]:
                    self._send(200, json.dumps({"ok": False, "errors": ["修复已在进行中"]}, ensure_ascii=False))
                else:
                    threading.Thread(target=repair_job, daemon=True).start()
                    self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"ok": False, "errors": [str(e)]}, ensure_ascii=False))

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"工具已在运行，直接打开 http://127.0.0.1:{PORT}")
        subprocess.run(["open", f"http://127.0.0.1:{PORT}"])
        raise SystemExit(0)
    print(f"DSH 备份管理器已启动: http://127.0.0.1:{PORT}  (配置目录 {PROFILE})")
    print("用完回到本终端按 Ctrl+C 退出，或直接关闭终端窗口。")
    try:
        subprocess.run(["open", f"http://127.0.0.1:{PORT}"])
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("已退出。")
