import SwiftUI

struct RootRouterView: View {
    @AppStorage("onboarding.completed") private var completed = false
    let firebaseState: FirebaseBootstrapState
    @Environment(DreamStore.self) private var store
    @Environment(EntitlementsService.self) private var entitlements
    
    var body: some View {
        if completed {
            ContentView(firebaseState: firebaseState)
        } else {
            OnboardingFlow()
        }
    }
}

