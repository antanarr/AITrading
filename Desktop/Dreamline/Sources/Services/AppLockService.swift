import Foundation
import LocalAuthentication

enum AppLockService {
    static func canEvaluate() -> Bool {
        var error: NSError?
        let ctx = LAContext()
        return ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error)
    }
    
    static func evaluate(reason: String = "Unlock Dreamline") async -> Bool {
        let ctx = LAContext()
        do {
            return try await ctx.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason)
        } catch {
            return false
        }
    }
}

