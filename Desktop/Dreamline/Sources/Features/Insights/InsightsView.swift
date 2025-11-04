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
            InsightsGate(isFreeUser: entitlements.tier == .free, daysShown: daysShown) {
                VStack {
                    Text("Insights")
                        .font(.largeTitle)
                        .fontWeight(.bold)
                }
            }
            .navigationTitle("Insights")
        }
    }
}
