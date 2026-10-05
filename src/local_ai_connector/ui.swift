import Cocoa

final class ConnectorWindow: NSObject, NSApplicationDelegate {
    let data = CommandLine.arguments.count > 1 ? URL(fileURLWithPath: CommandLine.arguments[1]) : Bundle.main.bundleURL.deletingLastPathComponent()
    let engine = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : Bundle.main.object(forInfoDictionaryKey: "ConnectorPython") as! String
    var config: [String: Any] = [:]
    var channels: [[String: Any]] = []
    var window: NSWindow!
    var loading = false
    var latestIncident: [String: Any]?
    var timer: Timer?
    let chooser = NSPopUpButton()
    let original = NSTextView()
    let status = NSTextField(wrappingLabelWithString: "正在连接本机服务…")
    let modelStatus = NSTextField(wrappingLabelWithString: "模型仅解释异常并提出建议。")
    let address = NSTextField()
    let model = NSTextField()
    let key = NSSecureTextField()
    let noThinking = NSButton(checkboxWithTitle: "关闭模型思考（需接口支持）", target: nil, action: nil)
    let font = NSFont(name: "Songti SC", size: 14) ?? NSFont.systemFont(ofSize: 14)

    func label(_ text: String, size: CGFloat = 14) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = NSFont(name: "Songti SC", size: size)
        field.textColor = .black
        return field
    }

    func button(_ text: String, _ action: Selector) -> NSButton {
        let b = NSButton(title: text, target: self, action: action)
        b.font = font
        return b
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        do {
            config = try JSONSerialization.jsonObject(with: Data(contentsOf: data.appendingPathComponent("server.json"))) as! [String: Any]
        } catch { fatalError("无法读取本地服务配置") }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 740, height: 680), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "本地 AI 连接器"
        window.appearance = NSAppearance(named: .aqua)
        window.backgroundColor = .white
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        window.contentView = stack
        for f in [status, modelStatus, address, model, key] { f.font = font; f.textColor = .black }
        stack.addArrangedSubview(label("连接批准", size: 19))
        stack.addArrangedSubview(status)
        chooser.target = self
        chooser.action = #selector(selectChannel)
        chooser.font = font
        stack.addArrangedSubview(chooser)
        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        original.isEditable = false
        original.font = font
        original.textColor = .black
        original.backgroundColor = .white
        original.isHorizontallyResizable = false
        original.textContainer?.widthTracksTextView = true
        scroll.documentView = original
        stack.addArrangedSubview(scroll)
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 190).isActive = true
        let actions = NSStackView(views: [button("批准本次交流", #selector(approve)), button("拒绝", #selector(deny)), button("撤销连接", #selector(revoke)), button("刷新", #selector(refresh))])
        stack.addArrangedSubview(actions)
        stack.addArrangedSubview(label("监管模型", size: 19))
        for (title, field) in [("服务地址", address), ("模型名称", model), ("密钥（可选）", key)] {
            let row = NSStackView(views: [label(title), field])
            stack.addArrangedSubview(row)
            field.widthAnchor.constraint(greaterThanOrEqualToConstant: 500).isActive = true
        }
        stack.addArrangedSubview(NSStackView(views: [button("保存模型配置", #selector(saveModel)), button("测试连接", #selector(testModel)), button("解释最近异常", #selector(explainIncident))]))
        noThinking.font = font
        stack.addArrangedSubview(noThinking)
        stack.addArrangedSubview(modelStatus)
        for view in [status, chooser, scroll, modelStatus] {
            view.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -32).isActive = true
        }
        address.stringValue = "http://127.0.0.1:11434/v1"
        let modelFile = data.appendingPathComponent("model.json")
        if FileManager.default.fileExists(atPath: modelFile.path) {
            do {
                let saved = try JSONSerialization.jsonObject(with: Data(contentsOf: modelFile)) as! [String: String]
                address.stringValue = saved["base_url"] ?? ""
                model.stringValue = saved["model"] ?? ""
                key.stringValue = saved["api_key"] ?? ""
                noThinking.state = saved["reasoning_effort"] == "none" ? .on : .off
            } catch { modelStatus.stringValue = "模型配置无法读取，请重新填写。" }
        }
        let menu = NSMenu()
        let item = NSMenuItem()
        let submenu = NSMenu()
        submenu.addItem(withTitle: "退出连接器窗口", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.submenu = submenu
        menu.addItem(item)
        NSApp.mainMenu = menu
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        refresh()
        timer = Timer.scheduledTimer(timeInterval: 2, target: self, selector: #selector(refresh), userInfo: nil, repeats: true)
    }

    func admin(_ action: String? = nil, cid: String? = nil, done: @escaping (Result<[String: Any], Error>) -> Void) {
        var request = URLRequest(url: URL(string: config["url"] as! String + "/admin")!)
        request.timeoutInterval = 5
        request.setValue("Bearer " + (config["admin_token"] as! String), forHTTPHeaderField: "Authorization")
        if let action = action {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try! JSONSerialization.data(withJSONObject: ["action": action, "channel": cid!])
        }
        URLSession.shared.dataTask(with: request) { bytes, response, error in
            let result: Result<[String: Any], Error>
            do {
                if let error = error { throw error }
                let value = try JSONSerialization.jsonObject(with: bytes ?? Data()) as! [String: Any]
                if (response as? HTTPURLResponse)?.statusCode != 200 {
                    throw NSError(domain: "connector", code: 1, userInfo: [NSLocalizedDescriptionKey: value["message"] as? String ?? "管理请求失败"])
                }
                result = .success(value)
            } catch { result = .failure(error) }
            DispatchQueue.main.async { done(result) }
        }.resume()
    }

    @objc func refresh() {
        if loading { return }
        loading = true
        let previous = chooser.selectedItem?.representedObject as? String
        admin { result in
            self.loading = false
            switch result {
            case .failure(let error): self.status.stringValue = "连接失败：" + error.localizedDescription
            case .success(let value):
                self.latestIncident = (value["incidents"] as? [[String: Any]])?.first
                self.channels = value["channels"] as? [[String: Any]] ?? []
                self.chooser.removeAllItems()
                let labels = ["pending": "待批准", "active": "交流中", "closed": "已关闭", "expired": "已到期", "denied": "已拒绝", "revoked": "已撤销"]
                for c in self.channels {
                    self.chooser.addItem(withTitle: "\(labels[c["status"] as! String] ?? "未知")　\(c["requester"]!) → \(c["responder"]!)")
                    self.chooser.lastItem?.representedObject = c["id"]
                    if c["id"] as? String == previous { self.chooser.select(self.chooser.lastItem) }
                }
                self.status.stringValue = self.channels.isEmpty ? "暂无求助。等待桌面工作者发起连接。" : "选择求助查看原文。批准后，本次双向交流在期限内放行。"
                self.selectChannel()
            }
        }
    }

    @objc func selectChannel() {
        let index = chooser.indexOfSelectedItem
        guard channels.indices.contains(index) else { original.string = ""; return }
        let c = channels[index]
        let date = Date(timeIntervalSince1970: c["expires"] as! Double)
        original.string = "\(c["requester"]!) → \(c["responder"]!)\n通道：\(c["id"]!)\n授权截止：\(date.formatted())\n\n\(c["original"]!)"
    }

    func decide(_ action: String) {
        guard let cid = chooser.selectedItem?.representedObject as? String else { return }
        admin(action, cid: cid) { result in
            switch result {
            case .success: self.refresh()
            case .failure(let error):
                let alert = NSAlert(); alert.messageText = error.localizedDescription; alert.runModal()
            }
        }
    }
    @objc func approve() { decide("approve") }
    @objc func deny() { decide("deny") }
    @objc func revoke() { decide("revoke") }

    @discardableResult @objc func saveModel() -> Bool {
        guard let url = URLComponents(string: address.stringValue),
              ["http", "https"].contains(url.scheme ?? ""), let host = url.host,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.scheme == "https" || ["localhost", "127.0.0.1", "::1"].contains(host),
              !model.stringValue.isEmpty else { modelStatus.stringValue = "请填写有效接口地址和模型名称；云端须使用 HTTPS。"; return false }
        do {
            let path = data.appendingPathComponent("model.json")
            var value = ["base_url": address.stringValue, "model": model.stringValue, "api_key": key.stringValue]
            if noThinking.state == .on { value["reasoning_effort"] = "none" }
            let bytes = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
            try bytes.write(to: path, options: [.atomic])
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
            modelStatus.stringValue = "配置已保存。"
            return true
        } catch { modelStatus.stringValue = "配置保存失败：" + error.localizedDescription; return false }
    }

    @objc func testModel() { runModel(["model-test"]) }
    @objc func explainIncident() {
        guard let incident = latestIncident, let id = incident["id"] as? String else { modelStatus.stringValue = "暂无已记录异常。"; return }
        runModel(["model-explain", id])
    }
    func runModel(_ arguments: [String]) {
        guard saveModel() else { return }
        modelStatus.stringValue = "正在请求监管模型…"
        DispatchQueue.global().async {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: self.engine)
            process.arguments = ["-m", "local_ai_connector.cli", "--data", self.data.path] + arguments
            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = FileHandle.nullDevice
            let text: String
            do {
                try process.run()
                let bytes = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard process.terminationStatus == 0 else { throw NSError(domain: "model", code: 1) }
                let result = try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
                let advice = result["advice"] as! [String: Any]
                let suggestions = ["retry": "以相同编号重试", "clarify": "先澄清问题", "wait": "等待当前事件", "stop": "停止当前操作", "inspect": "先核查记录"]
                text = "用时 \(result["latency_seconds"]!) 秒。\(advice["explanation"]!) 建议：\(suggestions[advice["suggestion"] as! String]!)。"
            } catch { text = "模型测试失败，请检查服务地址、模型、密钥及接口兼容性。" }
            DispatchQueue.main.async { self.modelStatus.stringValue = text }
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
umask(0o077)
let delegate = ConnectorWindow()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
