# DSH 备份管理器（DSH Backup Manager）

DeepSeek Harness（DSH）桌面向的**原生 macOS 备份/回滚/依赖修复工具**。SwiftUI 编写，单文件约 700 行，零第三方依赖，编译即用。

> 背景事故：2026-10-07 凌晨，批量安装社区插件触发 pnpm 全量重装，把 profile `node_modules` 里手工铺设的整套 `@deepseek-ai` 核心包（cordis / dsh-llm / dsh-session / dsh-settings / dsh-tools 等）当作冗余清除，导致 11+ 插件 `failed to import` 连锁、required 插件不激活、`DesktopHostFatalError` 无法启动。这些核心包全是 peer 依赖且 `autoInstallPeers: false`，`pnpm install` 既不会补装还会继续清除。本工具就是把这次事故的完整人工修复流程固化成的一键按钮。

## 功能

- **⟲ 一键回滚**：扫描 `~/.dsh/profiles/desktop` 下所有 `*.bak-*` 备份，按标签分组；整组一键覆盖当前配置，回滚前自动把现状存为 `*.bak-before-rollback-时间戳`（可再回滚撤销）。
- **⭐ 备注置顶**：给备份组写备注（如"完美状态-全部插件正常"），标注的组金框置顶，一眼识别哪次是完美状态。
- **📝 改名**：整组备份标签重命名，备注自动迁移，重名冲突保护。
- **⭐ 备份当前状态**：装新插件前点一下，勾选要备份的文件（package.json / pnpm-lock.yaml / pnpm-workspace.yaml / cordis.patch.yml / cordis.yml），生成 `名称-时间戳` 备份组。
- **🗑 批量删除**：全选/多选批量删除，先移入 `.backup-trash/` 回收目录，可反悔；失败逐条显式报错。
- **🔧 修复依赖(pnpm)**：一键执行完整修复流程——
  1. 解包宿主 `app.asar`（自动检测 app 更新后重新解包）；
  2. `pnpm install` 恢复依赖树；
  3. 补装 `@earendil-works/pi-ai`（如缺失）；
  4. **补齐 asar 内全部缺失核心包，并将 `@deepseek-ai/dsh-tools` 强制替换为宿主同代版本**（0.1 代 import 旧名 `CallId`，0.2 代已改名 `ToolCallId`，顶层必须与宿主同代）；
  5. 全插件 ESM 导入冒烟测试，输出绿/红结论。

## 已知的关键坑（本工具规避的）

1. **pnpm 会把手铺的核心包当冗余 prune 掉**：所以修复流程里所有 pnpm 命令必须排在补包之前执行。
2. **`cp -Rp` 往已存在目录合并会残留旧版 manifest**：宿主 typert-loader 报 `parameter codec has no create() factory` 致命错。必须先删后整目录拷贝。
3. **List 行高测量 bug**：会换行的长文本被按单行测高导致整行不可见，需 `fixedSize`。
4. **Swift `range(of:options:)` 返回整个匹配范围**：用它取捕获组会得到空标签，所有备份并成一个无名组、按组删除全部失灵。需手动从 `.bak-` 分隔符切片。

## 构建

```bash
git clone https://github.com/Helix3781/dsh-backup-manager.git
cd dsh-backup-manager
./build.sh   # 产出 /Applications/DSH备份管理器.app
```

要求：macOS 13+，Xcode Command Line Tools（swiftc）。

## 命令行模式（与界面按钮同一代码路径）

```bash
# 修复依赖树（退出码 0=全部插件导入通过，1=有失败）
"/Applications/DSH备份管理器.app/Contents/MacOS/DSHBackup" --repair

# 删除指定备份组（移入回收目录）
"/Applications/DSH备份管理器.app/Contents/MacOS/DSHBackup" --delete-tag <tag>
```

## 推荐的防事故 SOP

装新插件前点 **⭐ 备份当前状态** 并写清备注 → 出事后 **⟲ 回滚** 到该备份组 → **🔧 修复依赖** → 完全退出并重启 DSH。社区插件尽量一个一个装，每装一个重启验证。

---

社区工具，非官方出品。DSH 是 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)，"Everything is a Plugin."

## License

MIT
