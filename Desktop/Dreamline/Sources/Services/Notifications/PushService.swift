import Foundation

#if canImport(FirebaseMessaging)

import FirebaseMessaging

#endif

final class PushService: NSObject {
    func registerForNotifications() {
        // Implement later; placeholder so we compile
        #if canImport(FirebaseMessaging)
        print("Push: FirebaseMessaging available")
        #else
        print("Push: stub")
        #endif
    }
}
