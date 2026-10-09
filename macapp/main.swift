// 「今日」报纸 App 外壳：探活本地 Python 服务（必要时自己拉起），用 WKWebView 显示 /paper/，
// 退出时只关掉本 App 自己起的服务。编译：macapp/build.sh（swiftc 直接编，不需要 Xcode 工程）。
// 关窗不退出（常驻）+ 状态栏图标 + 登录时启动 + 每 60 秒轮询推送系统通知 + 点通知跳页面 + 前后台信号。

import Cocoa
import WebKit
import UserNotifications
import ServiceManagement

// 项目目录由 build.sh 编译时写进 Info.plist（App 要用项目里的 .venv/bin/pil 起服务）
let projectRoot = Bundle.main.object(forInfoDictionaryKey: "PILProjectRoot") as? String ?? ""
let port = 8766
let baseURL = URL(string: "http://127.0.0.1:\(port)/")!
let paperURL = baseURL.appendingPathComponent("paper/")
let pipelinePageURL = URL(string: "http://127.0.0.1:\(port)/paper/#/pipeline")!
let healthURL = baseURL.appendingPathComponent("api/paper/pipeline")
let zoomDefaultsKey = "pageZoom"
let notifSinceKey = "notificationSince"          // 上一次轮询返回的 now
let notificationsURL = baseURL.appendingPathComponent("api/paper/notifications")

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                         NSMenuDelegate, UNUserNotificationCenterDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?          // 只有本 App 起的服务，退出时才结束
    var showingError = false

    // 常驻 / 通知 / 信号
    var statusItem: NSStatusItem?
    var statusMenu: NSMenu?
    var launchAtLoginError: String?   // SMAppService 注册失败时的一行灰字
    var pollTimer: Timer?
    let launchedAt = Date()           // 首次 since 的基准时刻
    // UTC 带 Z 的 ISO 时间串（如 2026-10-04T08:26:13Z）
    let isoUTC: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        setupStatusItem()
        observeActivity()
        setupNotifications()
        connectAndLoad()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    /// Dock 图标点击：窗口不可见（被关闭/没有可见窗口）时重新显示或重建。
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showMainWindow() }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        guard let p = server, p.isRunning else { return }
        p.terminate()
        p.waitUntilExit()
    }

    // MARK: 服务探活 / 启动

    /// GET /api/paper/pipeline，超时 2 秒，HTTP 200 即算活。
    func serverReady() -> Bool {
        var req = URLRequest(url: healthURL)
        req.timeoutInterval = 2
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2)
        return ok
    }

    /// 活 → 加载；不活且 PILProjectRoot 可用 → 起服务后每 0.5 秒探活，最多 20 秒；仍不活 → 原生提示。
    func connectAndLoad() {
        DispatchQueue.global().async {
            if self.serverReady() {
                DispatchQueue.main.async { self.loadPaper() }
                return
            }
            if self.canStartServer() {
                self.startServer()
                for _ in 0..<40 {
                    if self.serverReady() { break }
                    Thread.sleep(forTimeInterval: 0.5)
                }
            }
            DispatchQueue.main.async {
                if self.serverReady() { self.loadPaper() } else { self.showError() }
            }
        }
    }

    /// Info.plist 里的 PILProjectRoot 不是占位值（也不是空）才允许自己起服务。
    func canStartServer() -> Bool {
        !projectRoot.isEmpty && projectRoot != "__PROJECT_ROOT__"
    }

    func startServer() {
        // 已有活着的服务进程就不再拉起。connectAndLoad 无重入守卫，服务未起时
        // 连点「重试」/reopen 会并发走到这里；重复起进程抢占 8766 端口后，server 句柄被
        // 后一次赋值覆盖成死进程，applicationWillTerminate 只关句柄那个，活着的成孤儿。
        if let s = server, s.isRunning { return }
        let logDir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/TodayPaper")
        try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
        let logURL = logDir.appendingPathComponent("server.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try? FileHandle(forWritingTo: logURL)
        log?.seekToEndOfFile()

        let p = Process()
        p.executableURL = URL(fileURLWithPath: projectRoot + "/.venv/bin/pil")
        p.arguments = ["serve", "--host", "127.0.0.1", "--port", "\(port)"]
        p.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env
        p.standardInput = FileHandle.nullDevice
        p.standardOutput = log
        p.standardError = log
        do {
            try p.run()
            server = p
        } catch {
            NSLog("今日：启动服务失败 \(error.localizedDescription)")
        }
    }

    // MARK: 窗口

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.applicationNameForUserAgent = "TodayPaperApp"
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "今日"
        // 不设置 appearance / backgroundColor：跟随系统深浅色
        window.isReleasedWhenClosed = false   // 常驻：关窗口不销毁，reopen 直接再显示
        window.minSize = NSSize(width: 720, height: 600)
        window.contentView = webView
        window.setFrameAutosaveName("TodayPaperMain")
        if !window.setFrameUsingName("TodayPaperMain") { window.center() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        applyZoom(zoom)
    }

    func loadPaper() {
        showWebView()
        webView.load(URLRequest(url: paperURL))
    }

    /// 原生提示（不是网页弹窗）：标题 + 「重试」按钮 + 日志位置小字。
    func showError() {
        let box = NSView()

        let title = NSTextField(labelWithString: "后台服务没有运行（127.0.0.1:\(port)）")
        title.font = NSFont.systemFont(ofSize: 17, weight: .semibold)
        title.translatesAutoresizingMaskIntoConstraints = false

        let note = NSTextField(labelWithString: "日志：~/Library/Logs/TodayPaper/server.log")
        note.font = NSFont.systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        note.translatesAutoresizingMaskIntoConstraints = false

        let retry = NSButton(title: "重试", target: self, action: #selector(retryServer))
        retry.bezelStyle = .rounded
        retry.keyEquivalent = "\r"
        retry.translatesAutoresizingMaskIntoConstraints = false

        let stack = NSStackView(views: [title, retry, note])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: box.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: box.centerYAnchor),
        ])

        window.contentView = box
        showingError = true
    }

    func showWebView() {
        if showingError || window.contentView !== webView {
            window.contentView = webView
            showingError = false
        }
    }

    @objc func retryServer(_ sender: Any?) { connectAndLoad() }

    // MARK: 链接：127.0.0.1 的 /paper、/api/ 留在 App 里，其它交给默认浏览器

    func isInternal(_ url: URL) -> Bool {
        guard url.host == "127.0.0.1" else { return false }
        return url.path.hasPrefix("/paper") || url.path.hasPrefix("/api/")
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url, url.scheme?.hasPrefix("http") == true else {
            decisionHandler(.allow)   // about:blank / data: 之类不动作
            return
        }
        if isInternal(url) {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    // target=_blank 之类要开新窗口的，一律给默认浏览器
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // MARK: 网页弹窗（网页本身不该弹，万一弹了用原生 NSAlert，不能卡死）

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "今日"
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "今日"
        alert.informativeText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { resp in
            completionHandler(resp == .alertFirstButtonReturn)
        }
    }

    // MARK: 缩放（记住到 UserDefaults）

    var zoom: Double {
        get { UserDefaults.standard.object(forKey: zoomDefaultsKey) as? Double ?? 1.0 }
        set { UserDefaults.standard.set(newValue, forKey: zoomDefaultsKey) }
    }

    func applyZoom(_ z: Double) {
        if #available(macOS 14.0, *) {
            webView.pageZoom = CGFloat(z)
        } else {
            // macOS 13 没有 pageZoom，用 magnification 兜底（语义相近：整页缩放）
            webView.allowsMagnification = true
            webView.setMagnification(CGFloat(z), centeredAt: .zero)
        }
    }

    func setZoom(_ z: Double) {
        let clamped = min(max(z, 0.5), 3.0)
        zoom = clamped
        applyZoom(clamped)
    }

    @objc func zoomIn(_ sender: Any?) { setZoom(zoom + 0.1) }
    @objc func zoomOut(_ sender: Any?) { setZoom(zoom - 0.1) }
    @objc func zoomActual(_ sender: Any?) { setZoom(1.0) }

    // MARK: 菜单

    @objc func goPaper(_ sender: Any?) { loadPaper() }
    @objc func goPipeline(_ sender: Any?) {
        showWebView()
        webView.load(URLRequest(url: pipelinePageURL))
    }
    @objc func reloadPage(_ sender: Any?) { webView.reload() }
    @objc func goBack(_ sender: Any?) { if webView.canGoBack { webView.goBack() } }
    @objc func goForward(_ sender: Any?) { if webView.canGoForward { webView.goForward() } }

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 今日",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 今日",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        // 没有编辑菜单，网页输入框里的 ⌘X/⌘C/⌘V/⌘A 不起作用
        let editItem = NSMenuItem(); main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let view = NSMenu(title: "显示")
        view.addItem(withTitle: "今日报纸", action: #selector(goPaper(_:)), keyEquivalent: "0")
        view.addItem(withTitle: "管道", action: #selector(goPipeline(_:)), keyEquivalent: "l")
        view.addItem(.separator())
        view.addItem(withTitle: "后退", action: #selector(goBack(_:)), keyEquivalent: "[")
        view.addItem(withTitle: "前进", action: #selector(goForward(_:)), keyEquivalent: "]")
        view.addItem(withTitle: "刷新", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        view.addItem(.separator())
        view.addItem(withTitle: "放大", action: #selector(zoomIn(_:)), keyEquivalent: "=")
        view.addItem(withTitle: "缩小", action: #selector(zoomOut(_:)), keyEquivalent: "-")
        view.addItem(withTitle: "实际大小", action: #selector(zoomActual(_:)), keyEquivalent: "9")
        viewItem.submenu = view

        let winItem = NSMenuItem(); main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.miniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = win

        NSApp.mainMenu = main
    }

    // MARK: 常驻 —— 状态栏图标（NSStatusItem，SF Symbol newspaper）

    func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "newspaper", accessibilityDescription: "今日")
        item.button?.toolTip = "今日"
        let menu = NSMenu()
        menu.delegate = self          // 打开前 menuNeedsUpdate 刷新勾选态/错误行
        item.menu = menu
        statusItem = item
        statusMenu = menu
        fillStatusMenu(menu)
    }

    /// 状态栏菜单：「打开今日」「管道」「登录时启动」（勾选态）「退出」；注册失败在其下加一行灰字。
    func fillStatusMenu(_ menu: NSMenu) {
        menu.removeAllItems()
        menu.autoenablesItems = false

        let open = NSMenuItem(title: "打开今日", action: #selector(statusOpen(_:)), keyEquivalent: "")
        open.target = self
        menu.addItem(open)

        let pipe = NSMenuItem(title: "管道", action: #selector(statusPipeline(_:)), keyEquivalent: "")
        pipe.target = self
        menu.addItem(pipe)

        let login = NSMenuItem(title: "登录时启动", action: #selector(toggleLaunchAtLogin(_:)), keyEquivalent: "")
        login.target = self
        login.state = launchAtLoginEnabled ? .on : .off
        menu.addItem(login)

        if let err = launchAtLoginError {
            let errItem = NSMenuItem(title: err, action: nil, keyEquivalent: "")
            errItem.isEnabled = false
            errItem.attributedTitle = NSAttributedString(string: err, attributes: [
                .foregroundColor: NSColor.secondaryLabelColor,
                .font: NSFont.menuFont(ofSize: 11),
            ])
            menu.addItem(errItem)
        }

        menu.addItem(.separator())

        let quit = NSMenuItem(title: "退出", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        quit.target = NSApp
        menu.addItem(quit)
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        if menu === statusMenu { fillStatusMenu(menu) }
    }

    @objc func statusOpen(_ sender: Any?) { showMainWindow() }
    @objc func statusPipeline(_ sender: Any?) { showMainWindow(); goPipeline(sender) }

    /// 显示主窗口：窗口仍在（isReleasedWhenClosed=false）就 order front；极端情况才重建。
    func showMainWindow() {
        if window == nil {
            buildWindow()
        } else {
            window.makeKeyAndOrderFront(nil)
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: 登录时启动（SMAppService.mainApp，macOS 13+）

    var launchAtLoginEnabled: Bool { SMAppService.mainApp.status == .enabled }

    @objc func toggleLaunchAtLogin(_ sender: NSMenuItem) {
        do {
            if launchAtLoginEnabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
            launchAtLoginError = nil
        } catch {
            // 不弹窗：把错误当一行灰字挂在「登录时启动」下面
            launchAtLoginError = error.localizedDescription
        }
        if let menu = statusMenu { fillStatusMenu(menu) }
    }

    // MARK: 推送轮询（每 60 秒一次；服务不可达静默跳过本轮）

    func setupNotifications() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        // 首次启动请求通知权限；授权返回后再开始轮询，避免首轮通知被丢弃
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in
            DispatchQueue.main.async { self.startPolling() }
        }
    }

    func startPolling() {
        pollTimer?.invalidate()
        pollNotifications()          // 先立即跑一次
        pollTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.pollNotifications()
        }
    }

    /// GET /api/paper/notifications?since=<上次返回的 now>（首次 = 启动时刻 ISO, UTC 带 Z）。
    func pollNotifications() {
        let since = UserDefaults.standard.string(forKey: notifSinceKey) ?? isoUTC.string(from: launchedAt)
        guard var comp = URLComponents(url: notificationsURL, resolvingAgainstBaseURL: false) else { return }
        comp.queryItems = [URLQueryItem(name: "since", value: since)]
        guard let url = comp.url else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 10
        URLSession.shared.dataTask(with: req) { data, resp, error in
            // 不可达 / 非 200 / 解析失败 → 静默跳过本轮，不动 since
            guard error == nil, (resp as? HTTPURLResponse)?.statusCode == 200,
                  let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            if let now = obj["now"] as? String, !now.isEmpty {
                UserDefaults.standard.set(now, forKey: notifSinceKey)
            }
            let items = (obj["notifications"] as? [[String: Any]]) ?? (obj["items"] as? [[String: Any]]) ?? []
            for item in items { self.postNotification(item) }
        }.resume()
    }

    /// 单条 → UNMutableNotificationContent（title/body 照返回，identifier = id，userInfo 带 open_path）。
    func postNotification(_ item: [String: Any]) {
        let content = UNMutableNotificationContent()
        content.title = item["title"] as? String ?? "今日"
        content.body = item["body"] as? String ?? ""
        content.sound = .default
        if let path = item["open_path"] as? String { content.userInfo = ["open_path": path] }

        let idStr: String
        if let s = item["id"] as? String {
            idStr = s
        } else if let n = item["id"] as? Int {
            idStr = String(n)
        } else {
            idStr = UUID().uuidString
        }
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: idStr, content: content, trigger: nil))
    }

    // MARK: 点通知（UNUserNotificationCenterDelegate）

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let path = response.notification.request.content.userInfo["open_path"] as? String
        DispatchQueue.main.async {
            self.showMainWindow()
            self.openNotificationPath(path)
        }
        completionHandler()
    }

    /// App 在前台时也要显示横幅。
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    /// 加载 http://127.0.0.1:8766 + open_path；没有 open_path 时回到报纸页。
    func openNotificationPath(_ path: String?) {
        guard let path = path, !path.isEmpty else { loadPaper(); return }
        let base = "http://127.0.0.1:\(port)"
        let full = path.hasPrefix("/") ? base + path : base + "/" + path
        guard let url = URL(string: full) else { loadPaper(); return }
        showWebView()
        webView.load(URLRequest(url: url))
    }

    // MARK: 前后台信号（通知网页 window.__todayAppActive）

    func observeActivity() {
        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(onAppActive),
                       name: NSApplication.didBecomeActiveNotification, object: nil)
        nc.addObserver(self, selector: #selector(onAppResign),
                       name: NSApplication.didResignActiveNotification, object: nil)
        // 窗口最小化 / 恢复（object: nil 覆盖本 App 唯一窗口）
        nc.addObserver(self, selector: #selector(onWindowRestore),
                       name: NSWindow.didDeminiaturizeNotification, object: nil)
        nc.addObserver(self, selector: #selector(onWindowMiniaturize),
                       name: NSWindow.didMiniaturizeNotification, object: nil)
    }

    @objc func onAppActive() { signalWebActive(true) }
    @objc func onAppResign() { signalWebActive(false) }
    @objc func onWindowRestore() { signalWebActive(true) }
    @objc func onWindowMiniaturize() { signalWebActive(false) }

    func signalWebActive(_ active: Bool) {
        let arg = active ? "true" : "false"
        webView.evaluateJavaScript("window.__todayAppActive && window.__todayAppActive(\(arg))",
                                   completionHandler: nil)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
