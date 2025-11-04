import Foundation

#if canImport(FirebaseFirestore)
import FirebaseFirestore
#if canImport(FirebaseFirestoreSwift)
import FirebaseFirestoreSwift
#endif
#endif

struct DLRemoteConfig: Codable {
    var freeInterpretationsPerWeek: Int
    var trialDaysPlus: Int
    var upsellDelaySeconds: Int
    var insightsBlurThresholdDays: Int
    var paywallVariant: String
    
    static let `default` = DLRemoteConfig(
        freeInterpretationsPerWeek: 1,
        trialDaysPlus: 7,
        upsellDelaySeconds: 3,
        insightsBlurThresholdDays: 7,
        paywallVariant: "A"
    )
}

@MainActor
final class RemoteConfigService: ObservableObject {
    static let shared = RemoteConfigService()
    
    @Published private(set) var config: DLRemoteConfig = .default
    @Published private(set) var loading = false
    @Published private(set) var error: Error?
    
    #if canImport(FirebaseFirestore)
    private let db = Firestore.firestore()
    #endif
    private let path = "config/current"
    private let cacheKey = "dreamline.remoteConfig.cache"
    
    private init() {
        loadCache()
        fetch()
    }
    
    private func loadCache() {
        guard let data = UserDefaults.standard.data(forKey: cacheKey),
              let cached = try? JSONDecoder().decode(DLRemoteConfig.self, from: data) else { return }
        config = cached
    }
    
    private func saveCache(_ rc: DLRemoteConfig) {
        if let data = try? JSONEncoder().encode(rc) {
            UserDefaults.standard.set(data, forKey: cacheKey)
        }
    }
    
    func fetch() {
        guard !loading else { return }
        loading = true
        error = nil
        
        #if canImport(FirebaseFirestore)
        db.document(path).getDocument(as: DLRemoteConfig.self) { [weak self] result in
            guard let self else { return }
            Task { @MainActor in
                self.loading = false
                switch result {
                case .success(let rc):
                    self.config = rc
                    self.saveCache(rc)
                case .failure(let err):
                    self.error = err
                    // keep defaults
                }
            }
        }
        #else
        loading = false
        // Keep defaults when Firestore is not available
        #endif
    }
}

