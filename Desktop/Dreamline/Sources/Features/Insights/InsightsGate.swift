import SwiftUI

struct InsightsGate<Content: View>: View {
    @ObservedObject var rc = RemoteConfigService.shared
    var isFreeUser: Bool
    var daysShown: Int
    var content: () -> Content
    
    @State private var showPaywall = false
    
    private var shouldBlur: Bool {
        isFreeUser && daysShown > rc.config.insightsBlurThresholdDays
    }
    
    private var gateCopy: (title: String, body: String, cta: String) {
        switch rc.config.paywallVariant {
        case "B":
            return (
                title: "Unlock 90‑Day Insights",
                body: "Discover long‑term patterns, symbol cycles, and deeper meaning across your dream journal.",
                cta: "Upgrade to Plus"
            )
        default: // "A"
            return (
                title: "See 90‑day patterns and symbol cycles",
                body: "Track symbol frequency, emotional patterns, and recurring themes over time.",
                cta: "Unlock Plus"
            )
        }
    }
    
    var body: some View {
        ZStack {
            content()
                .blur(radius: shouldBlur ? 8 : 0)
            
            if shouldBlur {
                VStack(spacing: 20) {
                    VStack(spacing: 12) {
                        Text(gateCopy.title)
                            .font(DLFont.title(22))
                            .multilineTextAlignment(.center)
                            .foregroundStyle(.primary)
                        
                        Text(gateCopy.body)
                            .font(DLFont.body(15))
                            .multilineTextAlignment(.center)
                            .foregroundStyle(.secondary)
                            .lineSpacing(4)
                    }
                    
                    Button(gateCopy.cta) {
                        showPaywall = true
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                }
                .padding(24)
                .frame(maxWidth: 340)
                .background(
                    RoundedRectangle(cornerRadius: 20, style: .continuous)
                        .fill(.ultraThinMaterial)
                        .overlay(
                            RoundedRectangle(cornerRadius: 20, style: .continuous)
                                .stroke(Color.white.opacity(0.1), lineWidth: 1)
                        )
                )
                .shadow(color: .black.opacity(0.2), radius: 20, x: 0, y: 10)
            }
        }
        .sheet(isPresented: $showPaywall) { PaywallView() }
    }
}

