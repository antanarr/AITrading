import SwiftUI

struct InsightsView: View {
    @Environment(DreamStore.self) private var store
    @Environment(EntitlementsService.self) private var entitlements
    
    private var daysShown: Int {
        guard let first = store.entries.last?.createdAt else { return 0 }
        let days = Calendar.current.dateComponents([.day], from: first, to: Date()).day ?? 0
        return days
    }
    
    var body: some View {
        NavigationView {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    InsightsHeatmapSection(
                        tier: entitlements.tier,
                        isFreeUser: entitlements.tier == .free
                    )
                }
                .padding()
            }
            .navigationTitle("Insights")
        }
    }
}
