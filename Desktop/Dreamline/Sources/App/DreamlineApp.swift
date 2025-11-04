import SwiftUI
import UIKit

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
    @AppStorage("app.lock.enabled") private var lockEnabled = false
    @State private var isLocked = false
    @State private var showLockScreen = false
    
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
            .opacity(showLockScreen ? 0 : 1)
            .disabled(showLockScreen)
        }
        .onAppear {
            if firebaseState == .missingPlist {
                withAnimation {
                    showMissingPlistBanner = true
                }
            }
            checkLock()
        }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.willEnterForegroundNotification)) { _ in
            checkLock()
        }
        .sheet(isPresented: $showLockScreen) {
            LockScreenView(isUnlocked: $isLocked)
        }
        .onChange(of: isLocked) { _, newValue in
            if newValue {
                showLockScreen = false
            }
        }
    }
    
    private func checkLock() {
        guard lockEnabled && AppLockService.canEvaluate() else { return }
        showLockScreen = true
        isLocked = false
        Task {
            let unlocked = await AppLockService.evaluate(reason: "Unlock Dreamline")
            await MainActor.run {
                isLocked = unlocked
                if !unlocked {
                    showLockScreen = false
                }
            }
        }
    }
}

private struct LockScreenView: View {
    @Binding var isUnlocked: Bool
    @Environment(\.dismiss) private var dismiss
    
    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "lock.shield.fill")
                .font(.system(size: 64))
                .foregroundStyle(.secondary)
            
            Text("Unlock Dreamline")
                .font(DLFont.title(24))
            
            Text("Use Face ID or Touch ID to continue")
                .font(DLFont.body(16))
                .foregroundStyle(.secondary)
            
            Button("Unlock") {
                Task {
                    let unlocked = await AppLockService.evaluate(reason: "Unlock Dreamline")
                    await MainActor.run {
                        isUnlocked = unlocked
                        if unlocked {
                            dismiss()
                        }
                    }
                }
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(40)
        .interactiveDismissDisabled(true)
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
