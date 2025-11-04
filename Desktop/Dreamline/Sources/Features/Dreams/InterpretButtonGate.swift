import SwiftUI

struct InterpretButtonGate: View {
    let tier: Tier
    @ObservedObject var rc = RemoteConfigService.shared
    @StateObject var usage = UsageService.shared
    @State private var showPaywall = false
    
    var onInterpret: () -> Void
    
    var body: some View {
        Button("Interpret") {
            Task {
                let count = await usage.weeklyInterpretCount(weekStart: Date())
                let gate = OracleGate.canInterpret(tier: tier, weeklyCount: count, rc: rc.config)
                
                switch gate {
                case .ok:
                    onInterpret()
                    await usage.incrementWeeklyInterpret(weekStart: Date())
                case .quotaDepleted, .needsPlus, .needsPro:
                    showPaywall = true
                }
            }
        }
        .buttonStyle(.borderedProminent)
        .sheet(isPresented: $showPaywall) {
            PaywallView()
        }
    }
}

