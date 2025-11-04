import Foundation
import Combine

#if canImport(FirebaseAuth)

import FirebaseAuth

#endif

final class AuthService: ObservableObject {
    @Published var uid: String? = nil

    func signInAnonymously() async {
        #if canImport(FirebaseAuth)
        do {
            let result = try await Auth.auth().signInAnonymously()
            await MainActor.run { self.uid = result.user.uid }
        } catch {
            print("Auth error: \(error)")
        }
        #else
        await MainActor.run { self.uid = "LOCAL-ANON-\(UUID().uuidString.prefix(8))" }
        #endif
    }
}
