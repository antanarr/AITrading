import AppKit
import Darwin
import Foundation
import Security

enum CredentialStore {
  static let service = "com.antanarr.AITrading.paper.credentials"
  static let names = [
    "BIRDEYE_API_KEY", "NANSEN_API_KEY", "SOLANA_RPC_URL",
    "OPENAI_API_KEY", "COPYBOT_OPENAI_MODEL",
  ]

  static func query(_ name: String, serviceName: String = service) -> [String: Any] {
    return [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: serviceName,
      kSecAttrAccount as String: name,
    ]
  }

  static func save(_ name: String, value: String, serviceName: String = service) -> OSStatus {
    let item = query(name, serviceName: serviceName)
    let data = Data(value.utf8)
    let status = SecItemUpdate(
      item as CFDictionary, [kSecValueData as String: data] as CFDictionary)
    if status != errSecItemNotFound { return status }
    var addition = item
    addition[kSecValueData as String] = data
    addition[kSecAttrLabel as String] = "AITrading: \(name)"
    // Use the local login keychain; do not request iCloud synchronization.
    return SecItemAdd(addition as CFDictionary, nil)
  }

  static func exists(_ name: String) -> Bool {
    var item = query(name)
    item[kSecReturnAttributes as String] = true
    var result: CFTypeRef?
    return SecItemCopyMatching(item as CFDictionary, &result) == errSecSuccess
  }

  static func read(_ name: String, serviceName: String = service) -> (OSStatus, Data?) {
    var item = query(name, serviceName: serviceName)
    item[kSecReturnData as String] = true
    var result: CFTypeRef?
    let status = SecItemCopyMatching(item as CFDictionary, &result)
    return (status, result as? Data)
  }

  static func selfTest() -> Bool {
    let testService = service + ".test." + UUID().uuidString
    let name = "test-only"
    defer { SecItemDelete(query(name, serviceName: testService) as CFDictionary) }
    guard save(name, value: "fictional-first", serviceName: testService) == errSecSuccess else {
      return false
    }
    guard save(name, value: "fictional-replacement", serviceName: testService) == errSecSuccess
    else { return false }
    let (status, value) = read(name, serviceName: testService)
    guard status == errSecSuccess && value == Data("fictional-replacement".utf8) else {
      return false
    }
    guard SecItemDelete(query(name, serviceName: testService) as CFDictionary) == errSecSuccess
    else { return false }
    return read(name, serviceName: testService).0 == errSecItemNotFound
  }
}

@MainActor
final class SetupDelegate: NSObject, NSApplicationDelegate {
  var window: NSWindow!
  var fields: [String: NSTextField] = [:]
  var indicators: [String: NSTextField] = [:]
  var message: NSTextField!
  var saveButton: NSButton!

  func text(_ value: String, size: CGFloat = 13, bold: Bool = false) -> NSTextField {
    let label = NSTextField(wrappingLabelWithString: value)
    label.font = bold ? .boldSystemFont(ofSize: size) : .systemFont(ofSize: size)
    label.lineBreakMode = .byWordWrapping
    return label
  }

  func applicationDidFinishLaunching(_ notification: Notification) {
    window = NSWindow(
      contentRect: NSRect(x: 0, y: 0, width: 610, height: 700),
      styleMask: [.titled, .closable, .miniaturizable],
      backing: .buffered, defer: false)
    window.title = "AITrading Credentials"
    window.isReleasedWhenClosed = false
    let stack = NSStackView()
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = 12
    stack.translatesAutoresizingMaskIntoConstraints = false
    window.contentView!.addSubview(stack)
    NSLayoutConstraint.activate([
      stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 28),
      stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -28),
      stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24),
    ])
    stack.addArrangedSubview(text("Connect your paper trading bot", size: 23, bold: true))
    let intro = text(
      "Credentials are stored in your Mac's Keychain. This window does not send them to a website, chat, or Git repository."
    )
    intro.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    stack.addArrangedSubview(intro)

    let labels = [
      ("BIRDEYE_API_KEY", "Birdeye API key", "Wallet discovery and market data"),
      ("NANSEN_API_KEY", "Nansen API key", "Optional additional trader discovery"),
      ("SOLANA_RPC_URL", "Solana RPC URL", "Full HTTPS endpoint from your RPC provider"),
      ("OPENAI_API_KEY", "OpenAI API key", "Optional AI assessments"),
      ("COPYBOT_OPENAI_MODEL", "OpenAI model", "Model ID for AI assessments; optional for now"),
    ]
    for (name, title, help) in labels {
      let heading = NSStackView()
      heading.orientation = .horizontal
      heading.spacing = 10
      heading.addArrangedSubview(text(title, bold: true))
      let indicator = text(
        CredentialStore.exists(name) ? "Saved in Keychain" : "Not configured", size: 11)
      indicator.textColor = .secondaryLabelColor
      heading.addArrangedSubview(indicator)
      indicators[name] = indicator
      stack.addArrangedSubview(heading)
      let field: NSTextField = name == "COPYBOT_OPENAI_MODEL" ? NSTextField() : NSSecureTextField()
      field.placeholderString = help
      field.font = .systemFont(ofSize: 14)
      field.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
      field.heightAnchor.constraint(equalToConstant: 30).isActive = true
      stack.addArrangedSubview(field)
      fields[name] = field
    }
    let note = text(
      "Leave a field blank to keep its saved value. Only enter provider API keys and the RPC endpoint here. Wallet seed phrases, private keys, and exchange withdrawal credentials are not needed.",
      size: 12)
    note.textColor = .secondaryLabelColor
    note.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    stack.addArrangedSubview(note)
    saveButton = NSButton(
      title: "Save to Keychain", target: self, action: #selector(saveCredentials))
    saveButton.bezelStyle = .rounded
    saveButton.keyEquivalent = "\r"
    stack.addArrangedSubview(saveButton)
    message = text("Saving does not start the bot or test any provider connection.", size: 12)
    message.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    stack.addArrangedSubview(message)
    window.center()
    window.makeKeyAndOrderFront(nil)
    NSApp.activate(ignoringOtherApps: true)
  }

  @objc func saveCredentials() {
    var values: [String: String] = [:]
    for name in CredentialStore.names {
      let value = fields[name]!.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
      if value.isEmpty { continue }
      guard !value.contains(where: { $0.isWhitespace }), !value.contains("\0") else {
        message.stringValue = "Each field must contain a single value. Nothing was saved."
        return
      }
      if name == "SOLANA_RPC_URL" {
        guard let url = URL(string: value), url.scheme == "https", url.host != nil,
          url.user == nil, url.password == nil
        else {
          message.stringValue = "The RPC endpoint must be a complete HTTPS URL. Nothing was saved."
          return
        }
      }
      values[name] = value
    }
    guard !values.isEmpty else {
      message.stringValue =
        "Enter at least one credential to save. Blank fields keep their existing values."
      return
    }
    saveButton.isEnabled = false
    fields.values.forEach { $0.stringValue = "" }
    message.stringValue = "Saving to Keychain..."
    DispatchQueue.global(qos: .userInitiated).async { [values] in
      var saved: [String] = []
      var failed: [String] = []
      for (name, value) in values {
        if CredentialStore.save(name, value: value) == errSecSuccess {
          saved.append(name)
        } else {
          failed.append(name)
        }
      }
      DispatchQueue.main.async {
        for name in saved { self.indicators[name]?.stringValue = "Saved in Keychain" }
        self.saveButton.isEnabled = true
        if failed.isEmpty {
          self.message.stringValue =
            "Saved \(saved.count) value(s) in Keychain. You can close this window. Trading remains off."
        } else {
          self.message.stringValue =
            "Saved \(saved.count) value(s). Keychain could not save: \(failed.sorted().joined(separator: ", ")). Please re-enter those fields."
        }
      }
    }
  }

  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
enum CredentialApp {
  @MainActor static func main() {
    let arguments = Array(CommandLine.arguments.dropFirst())
    if arguments == ["--self-test"] {
      let passed = CredentialStore.selfTest()
      print(passed ? "Keychain save/update/read/delete test passed." : "Keychain test failed.")
      exit(passed ? 0 : 1)
    } else if arguments == ["--status"] {
      let result = Dictionary(
        uniqueKeysWithValues: CredentialStore.names.map { ($0, CredentialStore.exists($0)) })
      if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]) {
        FileHandle.standardOutput.write(data)
        exit(0)
      }
      exit(1)
    } else if arguments.count == 2 && arguments[0] == "--read"
      && CredentialStore.names.contains(arguments[1])
    {
      guard isatty(STDOUT_FILENO) == 0 else {
        fputs("Credential values cannot be displayed in a terminal.\n", stderr)
        exit(2)
      }
      let (status, data) = CredentialStore.read(arguments[1])
      if status == errSecItemNotFound { exit(44) }
      guard status == errSecSuccess, let data = data else { exit(1) }
      // Only used through a captured subprocess pipe by the bot. Never log the value.
      FileHandle.standardOutput.write(data)
      exit(0)
    } else if !arguments.isEmpty {
      fputs("Unsupported credential-helper command.\n", stderr)
      exit(2)
    }

    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let delegate = SetupDelegate()
    app.delegate = delegate
    withExtendedLifetime(delegate) { app.run() }
  }
}
