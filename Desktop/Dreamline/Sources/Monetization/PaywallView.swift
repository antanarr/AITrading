import SwiftUI

#if canImport(StoreKit)
import StoreKit

@available(iOS 15.0, *)
struct PaywallView: View {
    @Environment(\.dismiss) private var dismiss
    @StateObject private var store = DreamlineStore.shared
    @State private var isPurchasing = false
    
    var body: some View {
        VStack(spacing: 16) {
            Text("See what your symbols are trying to say.")
                .font(DLFont.title(28))
            
            HStack {
                DLCard {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Plus").font(DLFont.title(22))
                        Text("Unlimited interpretations • 30‑day trends • Daily Dream‑Synced Horoscope")
                            .font(DLFont.body(14))
                        Button(action: { Task { await buy(id: store.plusMonthly) } }) {
                            Text("Start 7‑day trial")
                        }.buttonStyle(.borderedProminent)
                    }
                }
                
                DLCard {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Pro").font(DLFont.title(22))
                        Text("Adds voice transcription • Oracle chat • 90‑day patterns")
                            .font(DLFont.body(14))
                        Button(action: { Task { await buy(id: store.proMonthly) } }) {
                            Text("Upgrade to Pro")
                        }.buttonStyle(.bordered)
                    }
                }
            }
            
            DLCard {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Deep Read").font(DLFont.title(20))
                    Text("One‑off PDF insight for a single dream.")
                        .font(DLFont.body(14))
                    Button(action: { Task { await buy(id: store.deepRead) } }) {
                        Text("Buy Once")
                    }.buttonStyle(.bordered)
                }
            }
            
            Button("Restore Purchases") { Task { await store.restore() } }
            Button("Close") { dismiss() }.padding(.top, 8)
        }
        .padding()
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

