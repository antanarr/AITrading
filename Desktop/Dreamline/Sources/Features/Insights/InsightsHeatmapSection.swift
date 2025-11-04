import SwiftUI

struct InsightsHeatmapSection: View {
    let tier: Tier
    let isFreeUser: Bool
    @ObservedObject var rc = RemoteConfigService.shared
    
    var body: some View {
        InsightsGate(isFreeUser: isFreeUser, daysShown: tier == .pro ? 90 : (tier == .plus ? 30 : 7)) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Symbol Seasonality")
                    .font(DLFont.title(20))
                
                SymbolHeatmapView(days: tier == .pro ? 90 : (tier == .plus ? 30 : 7), data: demo())
            }
            .padding()
        }
    }
    
    private func demo() -> [[Int]] {
        // placeholder data; wire to real counts later
        let cols = 13, rows = 7
        return (0..<cols).map { _ in (0..<rows).map { _ in Int.random(in: 0...4) } }
    }
}

