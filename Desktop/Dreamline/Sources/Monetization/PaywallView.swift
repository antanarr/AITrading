import SwiftUI

#if canImport(StoreKit)
import StoreKit

@available(iOS 15.0, *)
struct PaywallView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var store = DreamlineStore.shared
    @State private var isPurchasing = false
    
    var body: some View {
        VStack(spacing: 24) {
            VStack(spacing: 8) {
                Text("See what your symbols are trying to say.")
                    .font(DLFont.title(28))
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.primary)
            }
            .padding(.top, 8)
            
            VStack(spacing: 16) {
                // Plus tier
                DLCard {
                    VStack(alignment: .leading, spacing: 12) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Plus")
                                .font(DLFont.title(24))
                                .foregroundStyle(.primary)
                            
                            Text("Unlimited interpretations • 30‑day trends • Daily Dream‑Synced Horoscope")
                                .font(DLFont.body(15))
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        
                        Button(action: { Task { await buy(id: store.plusMonthly) } }) {
                            Text("Start 7‑day trial")
                                .font(DLFont.body(16))
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.large)
                    }
                }
                
                // Pro tier
                DLCard {
                    VStack(alignment: .leading, spacing: 12) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Pro")
                                .font(DLFont.title(24))
                                .foregroundStyle(.primary)
                            
                            Text("Everything in Plus, plus voice transcription • Oracle chat • 90‑day patterns")
                                .font(DLFont.body(15))
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        
                        Button(action: { Task { await buy(id: store.proMonthly) } }) {
                            Text("Upgrade to Pro")
                                .font(DLFont.body(16))
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.large)
                    }
                }
                
                // Deep Read row
                HStack(spacing: 12) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Deep Read")
                            .font(DLFont.title(18))
                            .foregroundStyle(.primary)
                        
                        Text("One‑off PDF insight for a single dream")
                            .font(DLFont.body(13))
                            .foregroundStyle(.secondary)
                    }
                    
                    Spacer()
                    
                    Button(action: { Task { await buy(id: store.deepRead) } }) {
                        Text("Buy")
                            .font(DLFont.body(14))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }
                .padding(16)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .fill(.ultraThinMaterial)
                        .overlay(
                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                .stroke(Color.white.opacity(0.06), lineWidth: 1)
                        )
                )
            }
            
            VStack(spacing: 12) {
                Button("Restore Purchases") {
                    Task { await store.restore() }
                }
                .font(DLFont.body(14))
                .foregroundStyle(.secondary)
                
                Button("Close") {
                    dismiss()
                }
                .font(DLFont.body(14))
                .foregroundStyle(.secondary)
            }
            .padding(.top, 8)
        }
        .padding(20)
        .task { await store.loadProducts() }
        .disabled(isPurchasing)
    }
    
    private func buy(id: String) async {
        guard let p = store.products.first(where: { $0.id == id }) else { return }
        isPurchasing = true
        defer { isPurchasing = false }
        _ = await store.purchase(p)
    }
}

#else

struct PaywallView: View {
    @Environment(\.dismiss) private var dismiss
    
    var body: some View {
        VStack {
            Text("StoreKit not available")
            Button("Close") { dismiss() }
        }
        .padding()
    }
}

#endif

