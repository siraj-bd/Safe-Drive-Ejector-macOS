import Cocoa
import Foundation
import WebKit
import Darwin

// MARK: - Custom Borderless Floating Panel
// Provides 100% transparent container with no native NSPopover frame, no arrow, and no halo.
class CustomCardPanel: NSPanel {
    override var canBecomeKey: Bool { return true }
    override var canBecomeMain: Bool { return true }

    let shadowView = NSView()

    func setupCustomShadow() {
        shadowView.wantsLayer = true
        shadowView.layer?.backgroundColor = NSColor.clear.cgColor
        shadowView.layer?.cornerRadius = 0
        shadowView.layer?.shadowColor = NSColor.clear.cgColor
        shadowView.layer?.shadowOpacity = 0.0
        shadowView.layer?.shadowRadius = 0.0
        shadowView.layer?.shadowOffset = .zero

        shadowView.frame = contentView?.bounds ?? .zero
        shadowView.autoresizingMask = [.width, .height]
    }
}

// MARK: - SafeEject Custom Menu Bar App with Card UI
final class SafeEjectApplicationDelegate: NSObject, NSApplicationDelegate {
    weak var manager: SafeEjectStatusItemManager?
    private var terminationReplyPending = false

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let manager = manager, manager.beforeLogoutEnabled else {
            return .terminateNow
        }

        guard !terminationReplyPending else {
            return .terminateLater
        }

        terminationReplyPending = true

        NSLog("SafeEjectMenuBar: Application termination requested. Starting Before Logout Deep Sleep gate.")

        manager.runBeforeLogoutDeepSleep { [weak self, weak sender] success in
            guard let self = self else { return }

            self.terminationReplyPending = false

            if success {
                NSLog("SafeEjectMenuBar: Before Logout Deep Sleep gate completed successfully.")
                sender?.reply(toApplicationShouldTerminate: true)
            } else {
                NSLog("SafeEjectMenuBar: Before Logout Deep Sleep gate failed. Termination cancelled.")
                sender?.reply(toApplicationShouldTerminate: false)
            }
        }

        return .terminateLater
    }
}

class SafeEjectStatusItemManager: NSObject, WKScriptMessageHandler, NSWindowDelegate, WKUIDelegate, WKNavigationDelegate {
    var statusItem: NSStatusItem!
    var panel: CustomCardPanel!
    var webView: WKWebView!
    var pythonPath = "/usr/bin/python3"
    var scriptPath = ""
    var bundledEnginePath: String? = nil
    var htmlCardPath = ""
    var globalClickMonitor: Any?
    var livePollingTimer: Timer?
    var isSyncingState = false
    var isOperationInProgress = false

    var backgroundIdleTimer: Timer?
    var configuredTimerSeconds: Double = 0.0
    var autoAwakeEnabled: Bool = false
    var deepSleepMode: Bool = true
    var beforeSleepEnabled: Bool = true
    var beforeLogoutEnabled: Bool = false
    var isBeforeLogoutDeepSleepRunning: Bool = false
    var isSleepingDueToIdle: Bool = false
    var lastKnownIdleSeconds: Double = 0.0
    var lastExplicitUserActionTime: TimeInterval = 0.0

    override init() {
        super.init()
        locatePaths()
        setupPanel()
        setupStatusItem()
        setupPowerAndIdleMonitoring()
    }

    func locatePaths() {
        var potentialRoots: [String] = []

        // 1. Environment variable override
        if let envRoot = ProcessInfo.processInfo.environment["SAFEEJECT_PROJECT_DIR"], !envRoot.isEmpty {
            potentialRoots.append(envRoot)
        }

        // 2. Current working directory
        let cwd = FileManager.default.currentDirectoryPath
        potentialRoots.append(cwd)

        // 3. Executable path and ancestor directories
        let execPath = CommandLine.arguments.first ?? Bundle.main.executablePath ?? ""
        if !execPath.isEmpty {
            let execURL = URL(fileURLWithPath: execPath).resolvingSymlinksInPath()
            let execDir = execURL.deletingLastPathComponent().path
            potentialRoots.append(execDir)
            let parentDir = execURL.deletingLastPathComponent().deletingLastPathComponent().path
            potentialRoots.append(parentDir)
            let grandparentDir = execURL.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
            potentialRoots.append(grandparentDir)
        }

        // 4. Bundle path and parent directories
        let bundlePath = Bundle.main.bundlePath
        let bundleURL = URL(fileURLWithPath: bundlePath).resolvingSymlinksInPath()
        potentialRoots.append(bundleURL.path)
        potentialRoots.append(bundleURL.deletingLastPathComponent().path)

        // 5. Detect bundled standalone Python engine (safeeject_core)
        var engineCandidates: [String] = []
        if let execPath = CommandLine.arguments.first ?? Bundle.main.executablePath, !execPath.isEmpty {
            let execURL = URL(fileURLWithPath: execPath).resolvingSymlinksInPath()
            let execDir = execURL.deletingLastPathComponent().path
            engineCandidates.append((execDir as NSString).appendingPathComponent("safeeject_core"))
        }
        if let bundleExecURL = Bundle.main.executableURL {
            let bundleMacOS = bundleExecURL.deletingLastPathComponent().path
            engineCandidates.append((bundleMacOS as NSString).appendingPathComponent("safeeject_core"))
        }
        for candidate in potentialRoots {
            engineCandidates.append((candidate as NSString).appendingPathComponent("safeeject_core"))
            engineCandidates.append((candidate as NSString).appendingPathComponent("bin/safeeject_core"))
            engineCandidates.append((candidate as NSString).appendingPathComponent(".build/dist/safeeject_core"))
        }
        for path in engineCandidates {
            if FileManager.default.isExecutableFile(atPath: path) {
                bundledEnginePath = path
                NSLog("SafeEjectMenuBar: Discovered bundled standalone engine at \(path)")
                break
            }
        }

        // Find root where main.py exists
        var foundRoot: String?
        for candidate in potentialRoots {
            let testMain = (candidate as NSString).appendingPathComponent("main.py")
            if FileManager.default.fileExists(atPath: testMain) {
                foundRoot = candidate
                scriptPath = testMain
                break
            }
        }

        // Locate SafeDriveEjectorCard.html
        var htmlCandidates: [String] = []
        if let root = foundRoot {
            htmlCandidates.append((root as NSString).appendingPathComponent("ui/components/SafeDriveEjectorCard.html"))
        }
        for candidate in potentialRoots {
            htmlCandidates.append((candidate as NSString).appendingPathComponent("ui/components/SafeDriveEjectorCard.html"))
            htmlCandidates.append((candidate as NSString).appendingPathComponent("SafeDriveEjectorCard.html"))
            if let resPath = Bundle.main.resourcePath {
                htmlCandidates.append((resPath as NSString).appendingPathComponent("SafeDriveEjectorCard.html"))
                htmlCandidates.append((resPath as NSString).appendingPathComponent("ui/components/SafeDriveEjectorCard.html"))
            }
        }

        for path in htmlCandidates {
            if FileManager.default.fileExists(atPath: path) {
                htmlCardPath = path
                break
            }
        }

        // Detect python3 executable
        var pythonCandidates: [String] = []
        if let root = foundRoot {
            pythonCandidates.append((root as NSString).appendingPathComponent(".venv/bin/python3"))
            pythonCandidates.append((root as NSString).appendingPathComponent("venv/bin/python3"))
        }
        pythonCandidates.append("/opt/homebrew/bin/python3")
        pythonCandidates.append("/usr/local/bin/python3")
        pythonCandidates.append("/usr/bin/python3")

        for path in pythonCandidates {
            if FileManager.default.fileExists(atPath: path) {
                pythonPath = path
                break
            }
        }
    }

    func setupPanel() {
        let initialSize = NSSize(width: 202, height: 330)
        panel = CustomCardPanel(
            contentRect: NSRect(origin: .zero, size: initialSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isFloatingPanel = true
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.delegate = self

        // Configure WKWebView with script bridge
        let config = WKWebViewConfiguration()
        let userContent = WKUserContentController()
        userContent.add(self, name: "safeEjectBridge")
        config.userContentController = userContent
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        webView = WKWebView(frame: NSRect(origin: .zero, size: initialSize), configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground") // 100% transparent background
        if #available(macOS 12.0, *) {
            webView.underPageBackgroundColor = .clear
        }
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.clear.cgColor
        webView.autoresizingMask = [.width, .height]

        panel.contentView = panel.shadowView
        panel.shadowView.wantsLayer = true
        panel.shadowView.layer?.backgroundColor = NSColor.clear.cgColor
        panel.setupCustomShadow()

        webView.frame = panel.shadowView.bounds
        webView.autoresizingMask = [.width, .height]
        panel.shadowView.addSubview(webView)

        loadCardHTML()
    }

    func loadCardHTML() {
        guard !htmlCardPath.isEmpty else { return }
        let fileURL = URL(fileURLWithPath: htmlCardPath)
        let directoryURL = fileURL.deletingLastPathComponent()
        webView.loadFileURL(fileURL, allowingReadAccessTo: directoryURL)
    }

    func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = createDiamondToggleIcon(isActive: false)
            button.imagePosition = .imageOnly
            button.action = #selector(togglePanel(_:))
            button.target = self
            button.toolTip = "Safe Drive Ejector Tool"
        }
    }

    // Creates the diamond toggle icon (Red 45deg when closed, Green 135deg when open)
    private func createDiamondToggleIcon(isActive: Bool = false) -> NSImage {
        let size = NSSize(width: 22, height: 22)
        let img = NSImage(size: size)
        img.lockFocus()

        let context = NSGraphicsContext.current?.cgContext

        // Outer rotated diamond
        context?.saveGState()
        context?.translateBy(x: 11, y: 11)
        let rotationAngle = isActive ? (CGFloat.pi * 3.0 / 4.0) : (CGFloat.pi / 4.0)
        context?.rotate(by: rotationAngle)

        let diamondRect = CGRect(x: -7, y: -7, width: 14, height: 14)
        let diamondPath = NSBezierPath(roundedRect: diamondRect, xRadius: 1.5, yRadius: 1.5)
        let strokeColor = isActive ? NSColor(red: 100/255, green: 253/255, blue: 31/255, alpha: 1.0) : NSColor(red: 255/255, green: 51/255, blue: 51/255, alpha: 1.0)
        strokeColor.setStroke()
        diamondPath.lineWidth = 1.5
        diamondPath.stroke()

        // Three hamburger lines inside (rotated back)
        context?.rotate(by: -rotationAngle)

        let line1 = NSBezierPath(rect: NSRect(x: -5, y: 3, width: 10, height: 1.5))
        let line2 = NSBezierPath(rect: NSRect(x: -5, y: -0.75, width: 10, height: 1.5))
        let line3 = NSBezierPath(rect: NSRect(x: -5, y: -4.5, width: 10, height: 1.5))

        NSColor.white.setFill()
        line1.fill()
        line2.fill()
        line3.fill()

        context?.restoreGState()

        img.unlockFocus()
        img.isTemplate = false
        return img
    }

    func setupPowerAndIdleMonitoring() {
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(handleSystemSleep),
            name: NSWorkspace.willSleepNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(handleSystemWake),
            name: NSWorkspace.didWakeNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(handleSystemPowerOff),
            name: NSWorkspace.willPowerOffNotification,
            object: nil
        )

        refreshConfig()

        backgroundIdleTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.checkIdleState()
        }
    }

    func refreshConfig() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let status = self.queryStatusJSON()
            if let config = status["config"] as? [String: Any] {
                let sec = (config["sleep_timer_seconds"] as? NSNumber)?.doubleValue ?? 0.0
                let deepSleep = (config["deep_sleep_mode"] as? Bool) ?? true
                let awake = (config["remount_on_wake"] as? Bool) ?? (!deepSleep)
                let sleepEject = (config["eject_on_sleep"] as? Bool) ?? true
                let logoutEject = (config["eject_before_logout"] as? Bool) ?? false
                DispatchQueue.main.async {
                    self.configuredTimerSeconds = sec
                    self.deepSleepMode = deepSleep
                    self.autoAwakeEnabled = awake
                    self.beforeSleepEnabled = sleepEject
                    self.beforeLogoutEnabled = logoutEject
                }
            }
        }
    }

    @objc func handleSystemSleep(_ notification: Notification) {
        NSLog("SafeEjectMenuBar: macOS System will sleep (beforeSleepEnabled: \(beforeSleepEnabled))")
        if beforeSleepEnabled {
            runCLICommand(["idle-sleep"], isBackground: true)
        }
    }

    @objc func handleSystemWake(_ notification: Notification) {
        NSLog("SafeEjectMenuBar: macOS System did wake (deepSleepMode: \(deepSleepMode), autoAwakeEnabled: \(autoAwakeEnabled), isSleepingDueToIdle: \(isSleepingDueToIdle))")
        // Deep Sleep ON: External drives remain 100% asleep until manual Mount.
        // Deep Sleep OFF: Only resume if put to sleep automatically by idle timer.
        if !deepSleepMode && autoAwakeEnabled && isSleepingDueToIdle {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard let self = self, self.isSleepingDueToIdle, !self.deepSleepMode else { return }
                self.isSleepingDueToIdle = false
                self.runCLICommand(["remount-all", "--only-recorded"], isBackground: true)
            }
        }
    }

    @objc func handleSystemPowerOff(_ notification: Notification) {
        NSLog("SafeEjectMenuBar: Before Logout/Power Off event received (beforeLogoutEnabled: \(beforeLogoutEnabled))")

        guard beforeLogoutEnabled else {
            return
        }

        guard !isBeforeLogoutDeepSleepRunning else {
            NSLog("SafeEjectMenuBar: Before Logout fallback skipped because termination Deep Sleep gate is already running.")
            return
        }

        let task = Process()

        if let engine = self.bundledEnginePath {
            task.executableURL = URL(fileURLWithPath: engine)
            task.arguments = ["idle-sleep"]
        } else {
            task.executableURL = URL(fileURLWithPath: self.pythonPath)
            task.arguments = [self.scriptPath, "idle-sleep"]
        }

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

        let projectDir = !self.scriptPath.isEmpty
            ? (self.scriptPath as NSString).deletingLastPathComponent
            : FileManager.default.currentDirectoryPath

        env["PYTHONPATH"] = projectDir
        task.environment = env
        task.currentDirectoryPath = projectDir

        do {
            NSLog("SafeEjectMenuBar: Before Logout → starting synchronous idle-sleep.")

            try task.run()
            task.waitUntilExit()

            NSLog(
                "SafeEjectMenuBar: Before Logout → idle-sleep finished. " +
                "status=\(task.terminationStatus)"
            )
        } catch {
            NSLog("SafeEjectMenuBar: Before Logout → idle-sleep failed: \(error)")
        }
    }

    func checkIdleState() {
        let now = Date().timeIntervalSince1970
        if now - lastExplicitUserActionTime < 10.0 {
            // User recently performed an explicit action (e.g. Unmount/Mount).
            // Enforce a 10s cooldown to prevent cursor movement from triggering touch-wake.
            return
        }

        let idleSec = CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: CGEventType(rawValue: ~0)!)
        lastKnownIdleSeconds = idleSec

        // Touch Wake: ONLY fires if drive was put to sleep by genuine inactivity AND Deep Sleep is OFF!
        // When Deep Sleep is ON: External drives remain 100% asleep until manual Mount.
        if idleSec < 3.0 {
            if isSleepingDueToIdle {
                isSleepingDueToIdle = false
                if !deepSleepMode && autoAwakeEnabled {
                    NSLog("SafeEjectMenuBar: User touch detected (<3s) with Deep Sleep OFF. Auto-awakening idle drives...")
                    runCLICommand(["remount-all", "--only-recorded"], isBackground: true)
                } else {
                    NSLog("SafeEjectMenuBar: User touch detected (<3s), but Deep Sleep is ON. Drive remains 100% asleep until manual Mount.")
                }
            }
        }

        // Idle Sleep: Inactivity reached or passed configured timer (> 0)
        if configuredTimerSeconds > 0 && idleSec >= configuredTimerSeconds {
            if !isSleepingDueToIdle && !isOperationInProgress {
                isSleepingDueToIdle = true
                NSLog("SafeEjectMenuBar: Mac idle for \(Int(idleSec))s (>= \(Int(configuredTimerSeconds))s). Triggering idle sleep...")
                runCLICommand(["idle-sleep"], isBackground: true)
            }
        }
    }

    @objc func togglePanel(_ sender: AnyObject?) {
        if panel.isVisible && panel.alphaValue > 0.0 {
            closePanel()
        } else {
            showPanel()
        }
    }

    func showPanel() {
        guard let button = statusItem.button else { return }
        if webView.url == nil {
            loadCardHTML()
        }
        positionPanel()
        panel.alphaValue = 0.0
        panel.makeKeyAndOrderFront(nil)
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.12
            panel.animator().alphaValue = 1.0
        }
        button.image = createDiamondToggleIcon(isActive: true)

        // Start real-time drive status polling while panel is visible
        startLivePolling()

        // Close panel when user clicks outside
        if globalClickMonitor == nil {
            globalClickMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) { [weak self] _ in
                guard let self = self, self.panel.isVisible else { return }
                if let button = self.statusItem.button, let window = button.window {
                    let mouseLoc = NSEvent.mouseLocation
                    let buttonFrame = window.convertToScreen(button.convert(button.bounds, to: nil))
                    if buttonFrame.contains(mouseLoc) {
                        return // Handled by status button toggle
                    }
                }
                DispatchQueue.main.async {
                    self.closePanel()
                }
            }
        }
    }

    func closePanel() {
        guard panel.isVisible else { return }
        stopLivePolling()
        if let monitor = globalClickMonitor {
            NSEvent.removeMonitor(monitor)
            globalClickMonitor = nil
        }
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.12
            panel.animator().alphaValue = 0.0
        }, completionHandler: { [weak self] in
            self?.panel.orderOut(nil)
            self?.statusItem.button?.image = self?.createDiamondToggleIcon(isActive: false)
        })
    }

    func startLivePolling() {
        stopLivePolling()
        syncRealDriveState()
        livePollingTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            guard let self = self, self.panel.isVisible else { return }
            if self.isOperationInProgress { return }
            self.syncRealDriveState()
        }
        if let timer = livePollingTimer {
            RunLoop.main.add(timer, forMode: .common)
        }
    }

    func stopLivePolling() {
        livePollingTimer?.invalidate()
        livePollingTimer = nil
    }

    func windowDidResignKey(_ notification: Notification) {
        closePanel()
    }

    func positionPanel() {
        guard let button = statusItem.button, let window = button.window else { return }
        let buttonRectOnScreen = window.convertToScreen(button.convert(button.bounds, to: nil))
        let panelSize = panel.frame.size

        var x = buttonRectOnScreen.midX - (panelSize.width / 2.0)
        var y = buttonRectOnScreen.minY - panelSize.height - 4

        if let screen = window.screen ?? NSScreen.main {
            let screenFrame = screen.visibleFrame
            if x < screenFrame.minX + 4 {
                x = screenFrame.minX + 4
            } else if x + panelSize.width > screenFrame.maxX - 4 {
                x = screenFrame.maxX - panelSize.width - 4
            }
            if y < screenFrame.minY + 4 {
                y = screenFrame.minY + 4
            }
        }

        panel.setFrameOrigin(NSPoint(x: x, y: y))
    }

    func syncRealDriveState(force: Bool = false) {
        if isOperationInProgress { return }
        if isSyncingState && !force { return }
        isSyncingState = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            defer { self?.isSyncingState = false }
            guard let self = self else { return }
            if self.isOperationInProgress { return }
            let statusObj = self.queryStatusJSON()
            guard !statusObj.isEmpty else { return }

            if let cfg = statusObj["config"] as? [String: Any] {
                let sec = (cfg["sleep_timer_seconds"] as? NSNumber)?.doubleValue ?? self.configuredTimerSeconds
                let deepSleep = (cfg["deep_sleep_mode"] as? Bool) ?? self.deepSleepMode
                let awake = (cfg["remount_on_wake"] as? Bool) ?? (!deepSleep)
                DispatchQueue.main.async {
                    self.configuredTimerSeconds = sec
                    self.deepSleepMode = deepSleep
                    self.autoAwakeEnabled = awake
                }
            }

            if let jsonData = try? JSONSerialization.data(withJSONObject: statusObj),
               let jsonString = String(data: jsonData, encoding: .utf8) {
                DispatchQueue.main.async {
                    let js = "if(window.updateUIState){ window.updateUIState(\(jsonString)); }"
                    self.webView.evaluateJavaScript(js) { _, error in
                        if let error = error {
                            NSLog("SafeEjectMenuBar evaluateJavaScript error: \(error.localizedDescription)")
                        }
                    }
                }
            }
        }
    }

    private func getFloat(_ val: Any?) -> CGFloat? {
        if let v = val as? CGFloat { return v }
        if let v = val as? Double { return CGFloat(v) }
        if let v = val as? Int { return CGFloat(v) }
        if let v = val as? NSNumber { return CGFloat(v.doubleValue) }
        return nil
    }

    // MARK: - WKScriptMessageHandler (IPC from SafeDriveEjectorCard.html)
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "safeEjectBridge", let body = message.body as? [String: Any] else { return }

        let action = body["action"] as? String ?? ""

        switch action {
        case "mount":
            let target = body["target"] as? String ?? (body["driverId"] != nil ? "\(body["driverId"]!)" : "")
            let drivers = body["drivers"] as? [String] ?? []
            if (target == "all" || target == "checked") && !drivers.isEmpty {
                runCLICommand(["remount"] + drivers)
            } else if !target.isEmpty && target != "all" {
                runCLICommand(["remount", target])
            } else if target == "all" {
                if !drivers.isEmpty {
                    runCLICommand(["remount"] + drivers)
                } else {
                    runCLICommand(["remount-all"])
                }
            }
        case "unmount":
            let target = body["target"] as? String ?? (body["driverId"] != nil ? "\(body["driverId"]!)" : "all")
            let drivers = body["drivers"] as? [String] ?? []
            self.isSleepingDueToIdle = false
            if (target == "all" || target == "checked") && !drivers.isEmpty {
                runCLICommand(["eject"] + drivers)
            } else if target == "all" {
                runCLICommand(["eject-now"])
            } else {
                runCLICommand(["eject", target])
            }
        case "ejectNow":
            let drivers = body["drivers"] as? [String] ?? []
            if !drivers.isEmpty {
                runCLICommand(["eject-now"] + drivers)
            } else {
                runCLICommand(["eject-now"])
            }
        case "resizePopover":
            if let w = getFloat(body["width"]), let h = getFloat(body["height"]) {
                DispatchQueue.main.async {
                    let screenHeight = NSScreen.main?.visibleFrame.height ?? 800
                    let maxHeight = max(316, screenHeight - 60)
                    let finalH = min(h, maxHeight)
                    let newSize = NSSize(width: w, height: finalH)

                    if self.panel.isVisible {
                        var frame = self.panel.frame
                        let deltaH = finalH - frame.height
                        let deltaW = w - frame.width
                        frame.origin.y -= deltaH
                        frame.origin.x -= deltaW / 2.0
                        frame.size = newSize
                        self.panel.setFrame(frame, display: true, animate: true)
                    } else {
                        self.panel.setContentSize(newSize)
                    }
                }
            }
        case "timer":
            if let preset = body["preset"] as? String {
                var sec: Double = 300.0
                switch preset {
                case "2m": sec = 120.0
                case "5m": sec = 300.0
                case "10m": sec = 600.0
                case "15m": sec = 900.0
                case "30m": sec = 1800.0
                case "1h": sec = 3600.0
                case "2h": sec = 7200.0
                case "never": sec = 0.0
                default: sec = 300.0
                }
                self.configuredTimerSeconds = sec
                self.isSleepingDueToIdle = false
                NSLog("SafeEjectMenuBar: Idle sleep timer changed to \(preset) (\(Int(sec))s)")
                runCLICommand(["timer", preset])
            }
        case "checkboxToggle":
            if let id = body["id"] as? String, let checked = body["checked"] as? Bool {
                if id == "deepSleepCheck" || id == "autoAwakeCheck" {
                    let isDeepSleep = (id == "deepSleepCheck") ? checked : (!checked)
                    self.deepSleepMode = isDeepSleep
                    self.autoAwakeEnabled = !isDeepSleep
                    runCLICommand(["config", "set", "deep_sleep_mode", isDeepSleep ? "true" : "false"])
                    runCLICommand(["config", "set", "remount_on_wake", (!isDeepSleep) ? "true" : "false"])
                } else if id == "beforeSleepCheck" {
                    self.beforeSleepEnabled = checked
                    runCLICommand(["config", "set", "eject_on_sleep", checked ? "true" : "false"])
                } else if id == "startAtLoginCheck" {
                    runCLICommand(["config", "set", "start_at_login", checked ? "true" : "false"])
                } else if id == "beforeLogoutCheck" {
                    self.beforeLogoutEnabled = checked
                    runCLICommand(["config", "set", "eject_before_logout", checked ? "true" : "false"])
                } else if id == "notificationToggle" {
                    runCLICommand(["config", "set", "show_notifications", checked ? "true" : "false"])
                } else if id == "ejectedDiskCheck" {
                    if checked {
                        runCLICommand(["eject-now"])
                    } else {
                        runCLICommand(["remount-all"])
                    }
                }
            }
        case "driveSelection":
            if let targets = body["targets"] as? [String] {
                if targets.isEmpty {
                    runCLICommand(["select-sleep", "--set", "__none__"])
                } else {
                    runCLICommand(["select-sleep", "--set"] + targets)
                }
            } else if let driverId = body["driverId"] as? String {
                if let checked = body["checked"] as? Bool {
                    runCLICommand(["select-sleep", driverId, checked ? "true" : "false"])
                } else {
                    runCLICommand(["select-sleep", driverId])
                }
            }
        case "checkLocks":
            self.isOperationInProgress = true
            DispatchQueue.main.async {
                let startJs = "if(window.onOperationStart){ window.onOperationStart('check-locks', 'External Drives'); }"
                self.webView.evaluateJavaScript(startJs, completionHandler: nil)
            }
            DispatchQueue.global(qos: .userInitiated).async {
                let task = Process()
                if let engine = self.bundledEnginePath {
                    task.launchPath = engine
                    task.arguments = ["check-locks"]
                } else {
                    task.launchPath = self.pythonPath
                    task.arguments = [self.scriptPath, "check-locks"]
                }
                var env = ProcessInfo.processInfo.environment
                env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
                let projectDir = !self.scriptPath.isEmpty ? (self.scriptPath as NSString).deletingLastPathComponent : FileManager.default.currentDirectoryPath
                env["PYTHONPATH"] = projectDir
                task.environment = env
                task.currentDirectoryPath = projectDir

                let outPipe = Pipe()
                let errPipe = Pipe()
                task.standardOutput = outPipe
                task.standardError = errPipe

                let watchdog = DispatchWorkItem { [weak task] in
                    if let task = task, task.isRunning {
                        task.terminate()
                    }
                }
                DispatchQueue.global(qos: .background).asyncAfter(deadline: .now() + 15.0, execute: watchdog)

                try? task.run()
                let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                watchdog.cancel()

                let outStr = String(data: outData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                let success = (task.terminationStatus == 0)

                DispatchQueue.main.async {
                    self.isOperationInProgress = false
                    let finishJs = "if(window.onOperationComplete){ window.onOperationComplete('check-locks', \(success), ''); }"
                    self.webView.evaluateJavaScript(finishJs, completionHandler: nil)

                    let alert = NSAlert()
                    alert.messageText = "Safe Drive Ejector: File Locks"
                    alert.informativeText = outStr.isEmpty ? "All mounted external drives are clean. No processes are holding open files." : outStr
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                }
            }
        case "openFinder":
            let path = body["path"] as? String ?? ""
            if !path.isEmpty && FileManager.default.fileExists(atPath: path) {
                NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: path)
            } else {
                NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: "/Volumes")
            }
        case "openURL":
            if let urlStr = body["url"] as? String, let url = URL(string: urlStr) {
                NSWorkspace.shared.open(url)
            }
        case "update":
            checkForUpdates()
        case "requestSync":
            syncRealDriveState(force: true)
        case "quit":
            NSApplication.shared.terminate(self)
        default:
            break
        }
    }

    func checkForUpdates() {
        let currentVersion = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0.0"
        guard let url = URL(string: "https://api.github.com/repos/siraj-bd/Safe-Drive-Ejector-macOS/releases/latest") else { return }

        var request = URLRequest(url: url)
        request.setValue("application/vnd.github.v3+json", forHTTPHeaderField: "Accept")
        request.setValue("Safe-Drive-Ejector-macOS", forHTTPHeaderField: "User-Agent")
        request.timeoutInterval = 10.0

        let task = URLSession.shared.dataTask(with: request) { [weak self] (data, response, error) in
            DispatchQueue.main.async {
                guard let self = self else { return }

                if let error = error {
                    let alert = NSAlert()
                    alert.messageText = "Safe Drive Ejector: Update Check"
                    alert.informativeText = "Unable to check for updates: \(error.localizedDescription)\n\nPlease verify your internet connection or check releases on GitHub."
                    alert.addButton(withTitle: "Visit GitHub")
                    alert.addButton(withTitle: "OK")
                    if alert.runModal() == .alertFirstButtonReturn {
                        if let ghUrl = URL(string: "https://github.com/siraj-bd/Safe-Drive-Ejector-macOS/releases") {
                            NSWorkspace.shared.open(ghUrl)
                        }
                    }
                    return
                }

                guard let data = data,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let tagName = json["tag_name"] as? String else {
                    let alert = NSAlert()
                    alert.messageText = "Safe Drive Ejector: Update Check"
                    alert.informativeText = "Could not parse update response from GitHub."
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                    return
                }

                let releaseURL = (json["html_url"] as? String).flatMap { URL(string: $0) }
                var dmgDownloadURL: URL? = nil

                if let assets = json["assets"] as? [[String: Any]] {
                    for asset in assets {
                        if let name = asset["name"] as? String, name.hasSuffix(".dmg"),
                           let dlStr = asset["browser_download_url"] as? String {
                            dmgDownloadURL = URL(string: dlStr)
                            break
                        }
                    }
                }

                let latestVerStr = tagName.trimmingCharacters(in: CharacterSet(charactersIn: "vV"))
                let isNewer = self.isVersion(latestVerStr, newerThan: currentVersion)

                let alert = NSAlert()
                if isNewer {
                    alert.messageText = "Update Available (v\(latestVerStr))"
                    alert.informativeText = "A new version of Safe Drive Ejector is available!\n\nCurrent Version: v\(currentVersion)\nLatest Version: v\(latestVerStr)\n\nWould you like to download the update now?"
                    alert.addButton(withTitle: "Download Now (DMG)")
                    alert.addButton(withTitle: "View Release Notes")
                    alert.addButton(withTitle: "Later")
                    let res = alert.runModal()
                    if res == .alertFirstButtonReturn {
                        if let dl = dmgDownloadURL ?? releaseURL {
                            NSWorkspace.shared.open(dl)
                        }
                    } else if res == .alertSecondButtonReturn {
                        if let rUrl = releaseURL {
                            NSWorkspace.shared.open(rUrl)
                        }
                    }
                } else {
                    alert.messageText = "You are up to date!"
                    alert.informativeText = "Safe Drive Ejector v\(currentVersion) is currently the latest version."
                    alert.addButton(withTitle: "OK")
                    alert.runModal()
                }
            }
        }
        task.resume()
    }

    private func isVersion(_ v1: String, newerThan v2: String) -> Bool {
        let p1 = v1.components(separatedBy: ".").compactMap { Int($0) }
        let p2 = v2.components(separatedBy: ".").compactMap { Int($0) }
        let maxLen = max(p1.count, p2.count)
        for i in 0..<maxLen {
            let num1 = i < p1.count ? p1[i] : 0
            let num2 = i < p2.count ? p2[i] : 0
            if num1 > num2 { return true }
            if num1 < num2 { return false }
        }
        return false
    }

    // MARK: - WKUIDelegate (Opens external links in macOS default browser)
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "Safe Drive Ejector Tool"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Safe Drive Ejector Tool"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        let response = alert.runModal()
        completionHandler(response == .alertFirstButtonReturn)
    }

    // MARK: - WKNavigationDelegate (Sync real drives as soon as card page finishes loading)
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        syncRealDriveState(force: true)
    }

    func queryStatusJSON() -> [String: Any] {
        guard bundledEnginePath != nil || !scriptPath.isEmpty else { return [:] }
        let task = Process()
        if let engine = bundledEnginePath {
            task.launchPath = engine
            task.arguments = ["json-status"]
        } else {
            task.launchPath = pythonPath
            task.arguments = [scriptPath, "json-status"]
        }
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        let projectDir = !scriptPath.isEmpty ? (scriptPath as NSString).deletingLastPathComponent : FileManager.default.currentDirectoryPath
        env["PYTHONPATH"] = projectDir
        task.environment = env
        task.currentDirectoryPath = projectDir

        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()

        do {
            try task.run()

            let watchdog = DispatchWorkItem { [weak task] in
                if let task = task, task.isRunning {
                    task.terminate()
                }
            }
            DispatchQueue.global(qos: .background).asyncAfter(deadline: .now() + 15.0, execute: watchdog)

            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            watchdog.cancel()

            if let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                return json
            }
        } catch {
            NSLog("SafeEjectMenuBar queryStatusJSON error: \(error)")
        }
        return [:]
    }

    func runBeforeLogoutDeepSleep(completion: @escaping (Bool) -> Void) {
        guard !isBeforeLogoutDeepSleepRunning else {
            NSLog("SafeEjectMenuBar: Before Logout gate already running; ignoring duplicate request.")
            return
        }

        isBeforeLogoutDeepSleepRunning = true

        let task = Process()

        if let engine = self.bundledEnginePath {
            task.executableURL = URL(fileURLWithPath: engine)
            task.arguments = ["idle-sleep"]
        } else {
            task.executableURL = URL(fileURLWithPath: self.pythonPath)
            task.arguments = [self.scriptPath, "idle-sleep"]
        }

        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

        let projectDir = !self.scriptPath.isEmpty
            ? (self.scriptPath as NSString).deletingLastPathComponent
            : FileManager.default.currentDirectoryPath

        env["PYTHONPATH"] = projectDir
        task.environment = env
        task.currentDirectoryPath = projectDir

        DispatchQueue.global(qos: .userInitiated).async {
            do {
                NSLog("SafeEjectMenuBar: Before Logout gate → starting idle-sleep.")

                try task.run()
                task.waitUntilExit()

                let success = task.terminationStatus == 0

                NSLog(
                    "SafeEjectMenuBar: Before Logout gate → idle-sleep finished. " +
                    "status=\(task.terminationStatus)"
                )

                DispatchQueue.main.async {
                    self.isBeforeLogoutDeepSleepRunning = false
                    completion(success)
                }
            } catch {
                NSLog("SafeEjectMenuBar: Before Logout gate → idle-sleep failed: \(error)")

                DispatchQueue.main.async {
                    self.isBeforeLogoutDeepSleepRunning = false
                    completion(false)
                }
            }
        }
    }

    func runCLICommand(_ args: [String], isBackground: Bool = false) {
        guard bundledEnginePath != nil || !scriptPath.isEmpty else { return }
        let cmd = args.first ?? ""
        let targetParam = args.dropFirst().joined(separator: ", ")

        // Only explicit user commands from the UI card (not background idle sleep) clear isSleepingDueToIdle
        if !isBackground {
            self.lastExplicitUserActionTime = Date().timeIntervalSince1970
            if (cmd == "eject" || cmd == "deep-sleep" || cmd == "eject-now" || cmd == "remount" || cmd == "remount-all") {
                self.isSleepingDueToIdle = false
            }
        }

        self.isOperationInProgress = true

        if !isBackground {
            DispatchQueue.main.async {
                let startJs = "if(window.onOperationStart){ window.onOperationStart('\(cmd)', '\(targetParam)'); }"
                self.webView.evaluateJavaScript(startJs, completionHandler: nil)
            }
        }

        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            if let engine = self.bundledEnginePath {
                task.launchPath = engine
                task.arguments = args
            } else {
                task.launchPath = self.pythonPath
                task.arguments = [self.scriptPath] + args
            }
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            let projectDir = !self.scriptPath.isEmpty ? (self.scriptPath as NSString).deletingLastPathComponent : FileManager.default.currentDirectoryPath
            env["PYTHONPATH"] = projectDir
            task.environment = env
            task.currentDirectoryPath = projectDir

            let errPipe = Pipe()
            task.standardError = errPipe

            let watchdog = DispatchWorkItem { [weak task] in
                if let task = task, task.isRunning {
                    task.terminate()
                }
            }
            DispatchQueue.global(qos: .background).asyncAfter(deadline: .now() + 15.0, execute: watchdog)

            try? task.run()
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            watchdog.cancel()

            let success = (task.terminationStatus == 0)
            var errMsg = ""
            if !success {
                let rawErr = String(data: errData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                let lines = rawErr.components(separatedBy: .newlines).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
                let cleanErr = lines.last ?? rawErr
                errMsg = cleanErr.replacingOccurrences(of: "\n", with: " ")
                    .replacingOccurrences(of: "'", with: "\\'")
                    .replacingOccurrences(of: "\"", with: "\\\"")
            }
            DispatchQueue.main.async {
                let delay = (cmd == "eject" || cmd == "deep-sleep" || cmd == "eject-now" || cmd == "idle-sleep") ? 1.5 : 0.05
                DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
                    self.isOperationInProgress = false
                    self.syncRealDriveState()
                    if !isBackground {
                        let finishJs = "if(window.onOperationComplete){ window.onOperationComplete('\(cmd)', \(success), '\(errMsg)'); }"
                        self.webView.evaluateJavaScript(finishJs, completionHandler: nil)
                    }
                }
            }
        }
    }
}

// Single-instance process lock to prevent duplicate menu bar icons
let lockFilePath = "/tmp/com.user.safeeject.lock"
let lockFd = open(lockFilePath, O_CREAT | O_RDWR, 0o644)
if lockFd >= 0 {
    if flock(lockFd, LOCK_EX | LOCK_NB) != 0 {
        NSLog("SafeEjectMenuBar: Another instance is already running. Exiting.")
        exit(0)
    }
}

// Application entry point
let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let applicationDelegate = SafeEjectApplicationDelegate()
let manager = SafeEjectStatusItemManager()
applicationDelegate.manager = manager
app.delegate = applicationDelegate

app.run()
