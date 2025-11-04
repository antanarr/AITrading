import SwiftUI

@main
struct DreamlineApp: App {
    @State private var firebaseState: FirebaseBootstrapState = .notAvailable
    @State private var store = DreamStore()
    @State private var entitlements = EntitlementsService()
    
    init() {
        FirebaseBoot.configureIfNeeded()
        _firebaseState = State(initialValue: FirebaseService.configureIfPossible())
    }
    
    var body: some Scene {
        WindowGroup {
            RootRouterView(firebaseState: firebaseState)
                .environment(store)
                .environment(entitlements)
                .task {
                    #if canImport(StoreKit)
                    if #available(iOS 15.0, *) {
                        await entitlements.startObservers()
                        await entitlements.currentEntitlements()
                    }
                    #endif
                }
        }
    }
}

struct ContentView: View {
    let firebaseState: FirebaseBootstrapState
    @State private var showMissingPlistBanner = false
    
    var body: some View {
        VStack(spacing: 0) {
            if firebaseState == .missingPlist && showMissingPlistBanner {
                BannerView()
                    .transition(.move(edge: .top))
            }
            
            TabView {
                JournalView()
                    .tabItem {
                        Label("Journal", systemImage: "book")
                    }
                
                TodayView()
                    .tabItem {
                        Label("Today", systemImage: "sun.max")
                    }
                
                InsightsView()
                    .tabItem {
                        Label("Insights", systemImage: "chart.bar")
                    }
                
                ProfileView()
                    .tabItem {
                        Label("Me", systemImage: "person")
                    }
            }
        }
        .onAppear {
            if firebaseState == .missingPlist {
                withAnimation {
                    showMissingPlistBanner = true
                }
            }
        }
    }
}

struct BannerView: View {
    var body: some View {
        HStack {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundColor(.orange)
            Text("Add GoogleService-Info.plist to Config/ and rebuild (Firebase features will remain stubbed).")
                .font(.caption)
                .foregroundColor(.primary)
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.yellow.opacity(0.2))
    }
}
