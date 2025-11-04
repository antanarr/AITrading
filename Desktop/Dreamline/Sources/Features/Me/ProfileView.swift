import SwiftUI

struct ProfileView: View {
    @Environment(EntitlementsService.self) private var entitlements
    @State private var showPaywall = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Subscription") {
                    HStack {
                        Text("Current Tier")
                        Spacer()
                        Text(entitlements.tier.rawValue.capitalized)
                            .foregroundStyle(.secondary)
                    }
                    Button("Manage Subscription") { showPaywall = true }
                }
            }
            .navigationTitle("Me")
            .sheet(isPresented: $showPaywall) {
                PaywallView()
            }
        }
    }
}
