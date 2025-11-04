import Foundation

struct AppEnv {
    static var hasFirebasePlist: Bool {
        Bundle.main.path(forResource: "GoogleService-Info", ofType: "plist") != nil
    }
}
