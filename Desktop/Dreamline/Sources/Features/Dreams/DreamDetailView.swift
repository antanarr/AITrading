import SwiftUI

struct DreamDetailView: View {
    @Binding var entry: DreamEntry
    @Environment(EntitlementsService.self) private var entitlements
    @ObservedObject var rc = RemoteConfigService.shared
    @State private var showPaywall = false
    @State private var showDeepReadPurchase = false
    @State private var deepReadMessage: String? = nil
    @State private var generatedPDFURL: URL? = nil
    @State private var hasShownInterpretation = false
    private let oracle = OracleService()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(entry.rawText)
                    .font(.body)
                Text(entry.createdAt.formatted(date: .abbreviated, time: .shortened))
                    .font(.caption).foregroundStyle(.secondary)

                if let sum = entry.oracleSummary {
                    Divider()
                    Text("Oracle Summary").font(.headline)
                    Text(sum)
                        .onAppear {
                            if !hasShownInterpretation {
                                hasShownInterpretation = true
                                // Schedule upsell after delay
                                DispatchQueue.main.asyncAfter(deadline: .now() + .seconds(rc.config.upsellDelaySeconds)) {
                                    if entitlements.tier == .free {
                                        showPaywall = true
                                    }
                                }
                            }
                        }
                    if !entry.extractedSymbols.isEmpty {
                        Text("Symbols: " + entry.extractedSymbols.joined(separator: ", "))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if !entry.themes.isEmpty {
                        Text("Themes: " + entry.themes.joined(separator: ", "))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }

                Divider()
                Button("Interpret (stub)") {
                    let r = oracle.interpret(text: entry.rawText)
                    entry.oracleSummary = r.summary
                    entry.extractedSymbols = r.symbols
                    entry.themes = r.themes
                    hasShownInterpretation = false // Reset to trigger upsell on next appearance
                }
                .buttonStyle(.borderedProminent)
                
                Divider()
                
                // Deep Read section
                if entitlements.tier != .free {
                    Button("Generate Deep Read") {
                        if let url = DeepReadGenerator.generate(for: entry) {
                            generatedPDFURL = url
                        }
                    }
                    .buttonStyle(.borderedProminent)
                } else {
                    VStack(alignment: .leading, spacing: 8) {
                        Button("Generate Deep Read (Requires Upgrade)") {
                            showPaywall = true
                        }
                        .buttonStyle(.borderedProminent)
                        
                        Button("Buy Deep Read ($4.99 one-time)") {
                            showDeepReadPurchase = true
                            deepReadMessage = nil
                            Task {
                                let success = await entitlements.buyDeepRead()
                                await MainActor.run {
                                    if success {
                                        deepReadMessage = "Deep Read purchased! You can now generate reports."
                                        if let url = DeepReadGenerator.generate(for: entry) {
                                            generatedPDFURL = url
                                        }
                                    } else {
                                        deepReadMessage = "Purchase cancelled or failed."
                                    }
                                    showDeepReadPurchase = false
                                }
                            }
                        }
                        .buttonStyle(.bordered)
                        
                        if let msg = deepReadMessage {
                            Text(msg).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
                
                if let pdfURL = generatedPDFURL {
                    ShareLink(item: pdfURL) {
                        Label("Share PDF", systemImage: "square.and.arrow.up")
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding()
        }
        .navigationTitle("Dream")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showPaywall) {
            PaywallView()
        }
    }
}
