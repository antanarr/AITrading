import Foundation

#if canImport(FirebaseCore)

import FirebaseCore

#endif

enum FirebaseBootstrapState {
    case configured, missingPlist, notAvailable
}

struct FirebaseService {
    static func configureIfPossible() -> FirebaseBootstrapState {
        #if canImport(FirebaseCore)
        // Detect presence of GoogleService-Info.plist in bundle
        if Bundle.main.path(forResource: "GoogleService-Info", ofType: "plist") == nil {
            return .missingPlist
        }
        if FirebaseApp.app() == nil {
            FirebaseApp.configure()
        }
        return .configured
        #else
        return .notAvailable
        #endif
    }
}

enum FirebaseBoot {
    static func configureIfNeeded() {
        _ = FirebaseService.configureIfPossible()
    }
}

