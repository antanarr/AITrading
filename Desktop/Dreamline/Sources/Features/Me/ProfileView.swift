import SwiftUI

struct ProfileView: View {
    @Environment(EntitlementsService.self) private var entitlements
    @State private var showPaywall = false
    @AppStorage("app.lock.enabled") private var lockEnabled = false

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
                
                Section("Security") {
                    Toggle("Require Face ID on Resume", isOn: $lockEnabled)
                        .disabled(!AppLockService.canEvaluate())
                    
                    if !AppLockService.canEvaluate() {
                        Text("Face ID or Touch ID not available on this device")
                            .font(DLFont.body(12))
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Me")
            .sheet(isPresented: $showPaywall) {
                PaywallView()
            }
        }
    }
}
