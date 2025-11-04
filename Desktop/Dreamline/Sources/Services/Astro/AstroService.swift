import Foundation

#if canImport(FirebaseFirestore)
import FirebaseFirestore
import FirebaseFirestoreSwift
#endif

struct BirthData: Codable, Equatable {
    var date: Date
    var time: Date
    var placeText: String
    // Future: lat/long
}

struct TransitSummary: Codable, Equatable {
    var headline: String      // e.g., "Neptune trine Mercury"
    var notes: [String]       // short bullets
}

@MainActor
final class AstroService: ObservableObject {
    static let shared = AstroService()
    
    @Published private(set) var birth: BirthData?
    
    #if canImport(FirebaseFirestore)
    private let db = Firestore.firestore()
    #endif
    private var uid: String { "me" } // replace with Auth later
    
    private init() {
        Task {
            await loadBirth()
        }
    }
    
    func saveBirth(_ data: BirthData) async throws {
        birth = data
        
        #if canImport(FirebaseFirestore)
        try await db.collection("users").document(uid)
            .collection("astro").document("birth")
            .setData(from: data, merge: true)
        #endif
    }
    
    func loadBirth() async {
        #if canImport(FirebaseFirestore)
        do {
            let snap = try await db.collection("users").document(uid)
                .collection("astro").document("birth").getDocument()
            if let data = try? snap.data(as: BirthData.self) {
                self.birth = data
            }
        } catch { }
        #endif
    }
    
    func transits(for date: Date) async -> TransitSummary {
        // Stub: deterministic, replace with Swiss Ephemeris-backed server later.
        let weekday = Calendar.current.component(.weekday, from: date)
        let headline = (weekday % 2 == 0) ? "Neptune trine Mercury" : "Venus sextile Moon"
        let notes = [
            "Heightened intuition; trust soft signals.",
            "Light social ease; emotional articulation improves."
        ]
        return TransitSummary(headline: headline, notes: notes)
    }
}
