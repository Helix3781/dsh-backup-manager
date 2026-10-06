import SwiftUI
import AppKit

// MARK: - 数据层

let profileDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".dsh/profiles/desktop")
let trashDir = profileDir.appendingPathComponent(".backup-trash")
let notesFile = profileDir.appendingPathComponent(".backup-notes.json")
let asarPath = "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar"
let asarCache = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent(".dsh/cache/asar-extract")
let backupable = ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "cordis.patch.yml", "cordis.yml"]

struct BackupFile: Identifiable, Hashable {
    let base: String
    let size: Int
    var id: String { base }
}

struct Group: Identifiable {
    let tag: String
    let files: [BackupFile]
    let latest: Date
    let size: Int
    var note: String { NotesStore.shared.note(for: tag) }
    var id: String { tag }
}

final class NotesStore {
    static let shared = NotesStore()
    private var notes: [String: String] = [:]
    init() {
        if let d = try? JSONDecoder().decode([String: String].self, from: Data(contentsOf: notesFile)) {
            notes = d
        }
    }
    func note(for tag: String) -> String { notes[tag] ?? "" }
    func setNote(_ note: String, for tag: String) {
        if note.isEmpty { notes.removeValue(forKey: tag) } else { notes[tag] = note }
        if let data = try? JSONEncoder().encode(notes) {
            try? data.write(to: notesFile)
        }
    }
}

func shell(_ cmd: String, cwd: String? = nil) -> (Int32, String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/zsh")
    p.arguments = ["-lc", cmd]
    if let cwd { p.currentDirectoryURL = URL(fileURLWithPath: cwd) }
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return (-1, "\(error)") }
    let outData = pipe.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    let out = String(decoding: outData, as: UTF8.self)
        .split(separator: "\n").suffix(10).joined(separator: "\n")
    return (p.terminationStatus, out)
}

// MARK: - 状态

@MainActor
final class Store: ObservableObject {
    @Published var groups: [Group] = []
    @Published var selected: Set<String> = []
    @Published var message: String = ""
    @Published var messageOK = true
    @Published var repairLog: [String] = []
    @Published var repairRunning = false
    @Published var repairDone = false
    @Published var repairOK: Bool?

    func stamp(_ s: String) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return "[\(f.string(from: Date()))] \(s)"
    }

    func scan() {
        let fm = FileManager.default
        var byTag: [String: [(String, Int, URL)]] = [:]
        guard let items = try? fm.contentsOfDirectory(atPath: profileDir.path) else { return }
        for name in items {
            // 从最后一个 ".bak-" 处切分：base.bak-<tag>（与 Python 版贪婪语义一致）
            guard let r = name.range(of: ".bak-", options: .backwards) else { continue }
            let base = String(name[..<r.lowerBound])
            let tag = String(name[r.upperBound...])
            guard !tag.isEmpty, !base.isEmpty else { continue }
            let url = profileDir.appendingPathComponent(name)
            guard let attr = try? fm.attributesOfItem(atPath: url.path),
                  let sz = attr[.size] as? Int else { continue }
            byTag[tag, default: []].append((base, sz, url))
        }
        groups = byTag.map { tag, files in
            let bfs = files.map { BackupFile(base: $0.0, size: $0.1) }.sorted { $0.base < $1.base }
            let latest = files.compactMap { (try? fm.attributesOfItem(atPath: $0.2.path))?[.modificationDate] as? Date }.max() ?? Date.distantPast
            return Group(tag: tag, files: bfs, latest: latest, size: files.reduce(0) { $0 + $1.1 })
        }.sorted { a, b in
            if (a.note.isEmpty) != (b.note.isEmpty) { return a.note.isEmpty == false }
            return a.latest > b.latest
        }
        selected.formIntersection(Set(groups.map(\.tag)))
    }

    func totalFiles() -> Int { groups.reduce(0) { $0 + $1.files.count } }
    func totalSize() -> Int { groups.reduce(0) { $0 + $1.size } }

    func safetyBackup(_ bases: [String]) {
        let f = DateFormatter(); f.dateFormat = "yyyyMMdd-HHmmss"
        let ts = f.string(from: Date())
        for b in bases {
            let src = profileDir.appendingPathComponent(b)
            if fmExists(src) {
                try? FileManager.default.copyItem(at: src, to: profileDir.appendingPathComponent("\(b).bak-before-rollback-\(ts)"))
            }
        }
    }

    func restore(_ group: Group, only: [String]?) {
        let bases = only ?? group.files.map(\.base)
        safetyBackup(bases)
        var ok: [String] = []
        for b in bases {
            let src = profileDir.appendingPathComponent("\(b).bak-\(group.tag)")
            let dst = profileDir.appendingPathComponent(b)
            guard fmExists(src) else { continue }
            do {
                if fmExists(dst) { try FileManager.default.removeItem(at: dst) }
                try FileManager.default.copyItem(at: src, to: dst)
                ok.append(b)
            } catch { messageOK = false; message = "❌ \(b): \(error.localizedDescription)"; return }
        }
        messageOK = true
        message = "✅ 已覆盖：\(ok.joined(separator: ", "))\n现状已自动备份为 *.bak-before-rollback-*\n请完全退出并重启 DeepSeek Harness。"
        scan()
    }

    func deleteSelected() {
        let tags = Array(selected)
        guard !tags.isEmpty else { return }
        let f = DateFormatter(); f.dateFormat = "yyyyMMdd-HHmmss"
        var moved = 0
        var failures: [String] = []
        let fm = FileManager.default
        for tag in tags {
            let sub = trashDir.appendingPathComponent("\(f.string(from: Date()))-\(tag)")
            do { try fm.createDirectory(at: sub, withIntermediateDirectories: true) }
            catch { failures.append("\(tag): 回收目录创建失败 \(error.localizedDescription)"); continue }
            // 不依赖 groups 快照，直接按文件系统找该组全部文件
            let names = (try? fm.contentsOfDirectory(atPath: profileDir.path)) ?? []
            for name in names where name.hasSuffix(".bak-\(tag)") {
                let src = profileDir.appendingPathComponent(name)
                guard fm.fileExists(atPath: src.path) else { continue }
                do {
                    try fm.moveItem(at: src, to: sub.appendingPathComponent(name))
                    moved += 1
                } catch {
                    failures.append("\(name): \(error.localizedDescription)")
                }
            }
            NotesStore.shared.setNote("", for: tag)
        }
        selected.removeAll()
        if failures.isEmpty {
            messageOK = true
            message = "🗑 已移入回收目录 \(moved) 个文件：\(trashDir.path)"
        } else {
            messageOK = false
            message = "🗑 移动了 \(moved) 个，\(failures.count) 个失败：\n" + failures.joined(separator: "\n")
        }
        scan()
    }

    func saveNote(_ tag: String) {
        let cur = NotesStore.shared.note(for: tag)
        let alert = NSAlert()
        alert.messageText = "备注「\(tag)」"
        alert.informativeText = "例如：完美状态-全部插件正常（留空清除备注）"
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.stringValue = cur
        alert.accessoryView = input
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")
        if alert.runModal() == .alertFirstButtonReturn {
            NotesStore.shared.setNote(input.stringValue.trimmingCharacters(in: .whitespaces), for: tag)
            scan()
        }
    }

    func rename(_ tag: String) {
        let alert = NSAlert()
        alert.messageText = "重命名备份组"
        alert.informativeText = "整组备份文件将从 *.bak-旧名 改为 *.bak-新名，备注一并迁移。"
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.stringValue = tag
        alert.accessoryView = input
        alert.addButton(withTitle: "重命名")
        alert.addButton(withTitle: "取消")
        if alert.runModal() != .alertFirstButtonReturn { return }
        let newTag = input.stringValue.trimmingCharacters(in: .whitespaces)
        guard !newTag.isEmpty, newTag != tag, !newTag.contains("/"), !newTag.hasPrefix(".") else {
            messageOK = false; message = "❌ 名称不能为空、不能含路径分隔符、不能与原名相同"
            return
        }
        let fm = FileManager.default
        // 冲突检查：任一目标文件已存在则整体拒绝
        for g in groups where g.tag == tag {
            for f in g.files where fmExists(profileDir.appendingPathComponent("\(f.base).bak-\(newTag)")) {
                messageOK = false
                message = "❌ 已存在名为 \(newTag) 的备份组（\(f.base) 冲突），换一个名字"
                return
            }
        }
        var moved = 0
        for g in groups where g.tag == tag {
            for f in g.files {
                let src = profileDir.appendingPathComponent("\(f.base).bak-\(tag)")
                let dst = profileDir.appendingPathComponent("\(f.base).bak-\(newTag)")
                guard fmExists(src) else { continue }
                do { try fm.moveItem(at: src, to: dst); moved += 1 } catch {
                    messageOK = false; message = "❌ \(src.lastPathComponent): \(error.localizedDescription)"
                    scan(); return
                }
            }
        }
        // 备注迁移
        let note = NotesStore.shared.note(for: tag)
        NotesStore.shared.setNote("", for: tag)
        if !note.isEmpty { NotesStore.shared.setNote(note, for: newTag) }
        selected.remove(tag)
        messageOK = true
        message = "✅ 已重命名为 \(newTag)（\(moved) 个文件）"
        scan()
    }

    func createBackup(label: String, bases: [String]) {
        let f = DateFormatter(); f.dateFormat = "yyyyMMdd-HHmmss"
        let clean = label.trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: #"[\\/:*?\"<>| ]"#, with: "-", options: .regularExpression)
        let tag = "\(clean.isEmpty ? "手动备份" : clean)-\(f.string(from: Date()))"
        for b in bases {
            let src = profileDir.appendingPathComponent(b)
            guard fmExists(src) else { continue }
            try? FileManager.default.copyItem(at: src, to: profileDir.appendingPathComponent("\(b).bak-\(tag)"))
        }
        NotesStore.shared.setNote(clean.isEmpty ? "手动备份" : clean, for: tag)
        messageOK = true
        message = "✅ 已创建备份组：\(tag)"
        scan()
    }

    // MARK: 修复
    func startRepair() {
        repairRunning = true; repairDone = false; repairOK = nil; repairLog = []
        repairLog.append(stamp("开始修复：pnpm 依赖树 + 核心包对齐"))
        Task.detached { [weak self] in
            await self?.runRepair()
        }
    }

    private func logUI(_ s: String) async {
        await MainActor.run { self.repairLog.append(self.stamp(s)) }
    }

    nonisolated func runRepair() async {
        let fm = FileManager.default
        let asarOv = asarCache.appendingPathComponent("dsh/node_modules/@deepseek-ai")
        await logUI("1/5 解包 app.asar…")
        // app 更新后 asar 会比缓存新，此时必须重新解包，否则会按旧版本对齐
        let asarNewer: Bool = {
            guard let a = try? fm.attributesOfItem(atPath: asarPath)[.modificationDate] as? Date,
                  let c = try? fm.attributesOfItem(atPath: asarCache.path)[.modificationDate] as? Date else { return true }
            return a > c
        }()
        if !fm.fileExists(atPath: asarOv.path) || asarNewer {
            if asarNewer && fm.fileExists(atPath: asarOv.path) { await logUI("  检测到 app 已更新，重新解包") }
            try? fm.removeItem(at: asarCache)
            try? fm.createDirectory(at: asarCache, withIntermediateDirectories: true)
            let (code, out) = shell("npx --yes @electron/asar extract '\(asarPath)' '\(asarCache.path)'")
            await logUI(out.isEmpty ? "  完成" : out)
            if code != 0 || !fm.fileExists(atPath: asarOv.path) {
                await logUI("❌ asar 解包失败，请确认 DeepSeek Harness 已安装"); await finish(ok: false); return
            }
        } else { await logUI("  使用解包缓存") }

        await logUI("2/5 pnpm install 恢复依赖树…")
        let (c2, out2) = shell("pnpm install", cwd: profileDir.path)
        await logUI(c2 == 0 ? "  完成" : out2)

        await logUI("3/5 检查 @earendil-works/pi-ai…")
        if !fm.fileExists(atPath: profileDir.appendingPathComponent("node_modules/@earendil-works/pi-ai").path) {
            let (_, out3) = shell("pnpm add @earendil-works/pi-ai@0.87.1", cwd: profileDir.path)
            await logUI(out3)
        } else { await logUI("  已存在") }

        await logUI("4/5 对齐核心包到宿主同代（补缺 + dsh-tools 强制宿主版）…")
        let nm = profileDir.appendingPathComponent("node_modules")
        var added = 0, replaced = 0
        func fill(src: URL, tgt: URL, force: Bool) {
            if !fm.fileExists(atPath: src.path) { return }
            let exists = fm.fileExists(atPath: tgt.path)
            if exists && !force { return }
            if exists { try? fm.removeItem(at: tgt); replaced += 1 }
            try? fm.createDirectory(at: tgt.deletingLastPathComponent(), withIntermediateDirectories: true)
            try? fm.copyItem(at: src, to: tgt)
            added += 1
        }
        if let scopes = try? fm.contentsOfDirectory(atPath: asarCache.appendingPathComponent("dsh/node_modules").path) {
            for scope in scopes.sorted() where !scope.hasPrefix(".") {
                let sURL = asarCache.appendingPathComponent("dsh/node_modules/\(scope)")
                var isDir: ObjCBool = false
                fm.fileExists(atPath: sURL.path, isDirectory: &isDir)
                guard isDir.boolValue else { continue }
                if scope.hasPrefix("@") {
                    for pkg in (try? fm.contentsOfDirectory(atPath: sURL.path))?.sorted() ?? [] where !pkg.hasPrefix(".") {
                        let force = scope == "@deepseek-ai" && pkg == "dsh-tools"
                        fill(src: sURL.appendingPathComponent(pkg),
                             tgt: nm.appendingPathComponent("\(scope)/\(pkg)"), force: force)
                    }
                } else {
                    fill(src: sURL, tgt: nm.appendingPathComponent(scope), force: false)
                }
            }
        }
        await logUI("   补齐 \(added) 个（其中替换旧版 \(replaced) 个）")

        await logUI("5/5 插件导入冒烟测试…")
        var deps: [String] = []
        if let data = fm.contents(atPath: profileDir.appendingPathComponent("package.json").path),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let dd = obj["dependencies"] as? [String: String] {
            deps = dd.filter { !$0.value.hasPrefix("link:") }.map(\.key).sorted()
        }
        var bad: [String] = []
        for m in deps {
            let (code, _) = shell("node --input-type=module -e \"import('\(m)').then(()=>0).catch(()=>process.exit(1))\"",
                                  cwd: nm.path)
            if code != 0 { bad.append(m) }
        }
        if bad.isEmpty {
            await logUI("✅ 全部 \(deps.count) 个插件导入通过")
            await finish(ok: true)
        } else {
            await logUI("⚠️ \(bad.count) 个插件导入失败：\(bad.prefix(10).joined(separator: ", "))")
            await finish(ok: false)
        }
    }

    @Published var cliDeleteMsg = ""
    @Published var cliDeleteOK = false
    @Published var cliDeleteDone = false
    func cliDelete(tag: String) {
        scan()
        selected = [tag]
        deleteSelected()
        cliDeleteOK = messageOK
        cliDeleteMsg = message
        cliDeleteDone = true
    }
    func deleteFinished() -> Bool { cliDeleteDone }
    func deleteSnapshot() -> (Bool, String) { (cliDeleteOK, cliDeleteMsg) }

    func isRepairDone() -> Bool { repairDone }
    func repairSnapshot() -> (Bool?, String) { (repairOK, repairLog.joined(separator: "\n")) }

    private func finish(ok: Bool) async {
        await MainActor.run {
            self.repairOK = ok
            self.repairRunning = false
            self.repairDone = true
            self.repairLog.append(self.stamp(ok ? "修复流程结束。若改过配置，请完全退出并重启 DeepSeek Harness。"
                                                : "修复流程结束，但仍有插件导入失败（见上方清单）。"))
            self.scan()
        }
    }
}

func fmExists(_ url: URL) -> Bool { FileManager.default.fileExists(atPath: url.path) }

// MARK: - 界面

struct ContentView: View {
    @ObservedObject var store: Store
    @State var showBackupSheet = false
    @State var showRepairSheet = false
    @State var backupLabel = ""
    @State var backupSelection: Set<String> = []

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            if !store.message.isEmpty {
                Text(store.message)
                    .font(.callout)
                    .foregroundColor(store.messageOK ? Color(nsColor: .systemGreen) : Color(nsColor: .systemRed))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(8)
                    .background(store.messageOK ? Color.green.opacity(0.12) : Color.red.opacity(0.12))
            }
            List(store.groups) { g in
                GroupRow(group: g, store: store)
            }
            .listStyle(.inset)
            .scrollContentBackground(.visible)
        }
        .frame(minWidth: 720, minHeight: 560)
        .onAppear { store.scan() }
        .sheet(isPresented: $showBackupSheet) { backupSheet }
        .sheet(isPresented: $showRepairSheet) { repairSheet }
    }

    var toolbar: some View {
        HStack(spacing: 8) {
            Text("共 \(store.groups.count) 组 / \(store.totalFiles()) 个备份，占 \(ByteCountFormatter.string(fromByteCount: Int64(store.totalSize()), countStyle: .file))")
                .font(.footnote).foregroundColor(.secondary)
            Spacer()
            Toggle("全选", isOn: Binding(
                get: { store.selected.count == store.groups.count && !store.groups.isEmpty },
                set: { on in store.selected = on ? Set(store.groups.map(\.tag)) : [] }))
                .toggleStyle(.checkbox).font(.footnote)
            Button("🗑 删除选中 (\(store.selected.count))", role: .destructive) {
                guard !store.selected.isEmpty else { return }
                let alert = NSAlert()
                alert.messageText = "确定删除选中的 \(store.selected.count) 组备份？"
                alert.informativeText = "会先移入 .backup-trash/ 回收目录，不是永久删除。"
                alert.addButton(withTitle: "删除")
                alert.addButton(withTitle: "取消")
                if alert.runModal() == .alertFirstButtonReturn { store.deleteSelected() }
            }
            .disabled(store.selected.isEmpty)
            Button("⭐ 备份当前状态") { showBackupSheet = true }
            Button("🔧 修复依赖(pnpm)") { showRepairSheet = true }
        }
        .padding(10)
    }

    var backupSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("⭐ 备份当前状态").font(.headline)
            Text("生成「名称-时间戳」标签，并自动写备注方便置顶识别。")
                .font(.footnote).foregroundColor(.secondary)
            TextField("例如：完美状态-全部插件正常", text: $backupLabel)
                .textFieldStyle(.roundedBorder)
            ForEach(backupable, id: \.self) { b in
                let exists = fmExists(profileDir.appendingPathComponent(b))
                Toggle(isOn: Binding(
                    get: { exists && backupSelection.contains(b) },
                    set: { on in if on { backupSelection.insert(b) } else { backupSelection.remove(b) } })) {
                    Text(b + (exists ? "" : "（当前不存在）"))
                        .font(.footnote)
                        .foregroundColor(exists ? .primary : .secondary)
                }
                .toggleStyle(.checkbox)
                .disabled(!exists)
                .onAppear { if exists { backupSelection.insert(b) } }
            }
            HStack {
                Spacer()
                Button("取消") { showBackupSheet = false }
                Button("创建备份") {
                    store.createBackup(label: backupLabel, bases: Array(backupSelection))
                    backupLabel = ""
                    showBackupSheet = false
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .padding(20)
        .frame(width: 420)
    }

    var repairSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("🔧 修复依赖树").font(.headline)
            Text("解包宿主 asar → pnpm install → 补 pi-ai → 补齐缺失核心包（dsh-tools 强制宿主版）→ 全插件导入冒烟测试。")
                .font(.footnote).foregroundColor(.secondary)
            ScrollView {
                Text(store.repairLog.joined(separator: "\n"))
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(height: 260)
            .background(Color(nsColor: .textBackgroundColor))
            .foregroundColor(Color(nsColor: .textColor))
            .cornerRadius(8)
            HStack {
                if store.repairRunning { ProgressView().scaleEffect(0.7) }
                if store.repairDone {
                    Text(store.repairOK == true ? "✅ 修复完成" : "⚠️ 流程结束，仍有失败项")
                        .font(.footnote)
                        .foregroundColor(store.repairOK == true ? .green : .orange)
                }
                Spacer()
                Button("关闭") { showRepairSheet = false }
                Button(store.repairRunning ? "修复中…" : "开始修复") {
                    store.startRepair()
                }
                .buttonStyle(.borderedProminent)
                .disabled(store.repairRunning)
            }
        }
        .padding(20)
        .frame(width: 640)
    }
}

struct GroupRow: View {
    let group: Group
    @ObservedObject var store: Store

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Toggle("", isOn: Binding(
                get: { store.selected.contains(group.tag) },
                set: { on in if on { store.selected.insert(group.tag) } else { store.selected.remove(group.tag) } }))
                .toggleStyle(.checkbox).labelsHidden()
            VStack(alignment: .leading, spacing: 5) {
                HStack {
                    Text(group.tag)
                        .font(.system(size: 14, weight: .semibold, design: .monospaced))
                        .foregroundColor(.primary)
                    if !group.note.isEmpty {
                        Text("⭐ \(group.note)")
                            .font(.caption).padding(.horizontal, 6).padding(.vertical, 1)
                            .background(Color.yellow.opacity(0.30)).cornerRadius(5)
                            .foregroundColor(.primary)
                    }
                }
                .fixedSize(horizontal: false, vertical: true)
                Text("\(group.latest.formatted(date: .abbreviated, time: .shortened)) · \(group.files.count) 个文件 · \(ByteCountFormatter.string(fromByteCount: Int64(group.size), countStyle: .file))")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text(group.files.map(\.base).joined(separator: "\n"))
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundColor(.secondary)
                    .lineLimit(nil)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("⟲ 回滚") { store.restore(group, only: nil) }
                        .buttonStyle(.bordered).controlSize(.small)
                    Button("✏️ 备注") { store.saveNote(group.tag) }
                        .buttonStyle(.bordered).controlSize(.small)
                    Button("📝 改名") { store.rename(group.tag) }
                        .buttonStyle(.bordered).controlSize(.small)
                }
            }
            Spacer()
        }
        .padding(.vertical, 4)
        .listRowBackground(
            group.note.isEmpty
                ? Color(nsColor: .controlBackgroundColor)
                : Color.yellow.opacity(0.10)
        )
    }
}

@main
struct DSHBackupApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject var store = Store()
    var body: some Scene {
        WindowGroup("DSH 备份管理器") {
            ContentView(store: store).environmentObject(store)
        }
        .windowStyle(.automatic)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // 命令行模式：DSHBackup --repair 跑与界面按钮相同的修复流程后退出
        //            DSHBackup --delete-tag <tag>  删除指定备份组（移入回收目录）
        if let tag = CommandLine.arguments.firstIndex(of: "--delete-tag").map({ CommandLine.arguments[$0+1] }) {
            let store = Store()
            Task.detached {
                await store.cliDelete(tag: tag)
            }
            Task.detached {
                while !(await store.deleteFinished()) {
                    try? await Task.sleep(nanoseconds: 200_000_000)
                }
                let (ok, msg) = await store.deleteSnapshot()
                print(msg)
                print(ok ? "RESULT: OK" : "RESULT: FAILED")
                exit(ok ? 0 : 1)
            }
        }
        if CommandLine.arguments.contains("--repair") {
            let store = Store()
            Task.detached { await store.runRepair() }
            Task.detached {
                while !(await store.isRepairDone()) {
                    try? await Task.sleep(nanoseconds: 500_000_000)
                }
                let (ok, logText) = await store.repairSnapshot()
                print(logText)
                print(ok == true ? "RESULT: OK" : "RESULT: FAILED")
                exit(ok == true ? 0 : 1)
            }
        }
    }
}
