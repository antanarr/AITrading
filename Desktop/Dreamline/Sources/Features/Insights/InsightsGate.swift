import SwiftUI

struct InsightsGate<Content: View>: View {
    @ObservedObject var rc = RemoteConfigService.shared
    var isFreeUser: Bool
    var daysShown: Int
    var content: () -> Content
    
    @State private var showPaywall = false
    
    var body: some View {
        ZStack {
            content()
                .blur(radius: (isFreeUser && daysShown > rc.config.insightsBlurThresholdDays) ? 8 : 0)
            
            if isFreeUser && daysShown > rc.config.insightsBlurThresholdDays {
                VStack(spacing: 8) {
                    Text("See 90‑day patterns and symbol cycles.")
                        .multilineTextAlignment(.center)
                        .font(DLFont.body(16))
                    
                    Button("Unlock Plus") { showPaywall = true }
                        .buttonStyle(.borderedProminent)
                }
                .padding()
                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
            }
        }
        .sheet(isPresented: $showPaywall) { PaywallView() }
    }
}

