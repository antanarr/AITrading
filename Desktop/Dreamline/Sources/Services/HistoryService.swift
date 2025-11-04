import Foundation

#if canImport(FirebaseFirestore)
import FirebaseFirestore
#endif

struct MotifHistory: Codable, Equatable {
    var topSymbols: [String]
    var archetypeTrends: [String]   // e.g., ["threshold↑","rebirth↔"]
    var userPhrases: [String]       // recurring bigrams
    var tones7d: [String: Int]
}

@MainActor
final class HistoryService: ObservableObject {
    static let shared = HistoryService()
    
    #if canImport(FirebaseFirestore)
    private let db = Firestore.firestore()
    #endif
    private var uid: String { "me" } // replace with Auth later
    
    private init() {}
    
    func summarize(days: Int) async -> MotifHistory {
        // Pull last N dreams; this is a simple client-side summarizer.
        // If you have a dreams collection, query and aggregate; else return empty defaults.
        let topSymbols = await recentTopSymbols(days: days)
        let trends = computeTrends()
        let phrases = await commonPhrases(days: days)
        let tones: [String: Int] = ["curious": 3, "anxious": 1] // TODO: compute from stored tone
        
        return MotifHistory(topSymbols: topSymbols, archetypeTrends: trends, userPhrases: phrases, tones7d: tones)
    }
    
    private func recentTopSymbols(days: Int) async -> [String] {
        // TODO: query Firestore users/{uid}/dreams ordered by createdAt desc and tally extraction.symbols
        return ["water", "door", "room"]
    }
    
    private func commonPhrases(days: Int) async -> [String] {
        return ["locked room", "high water"]
    }
    
    private func computeTrends() -> [String] {
        return ["threshold↑", "rebirth↔"]
    }
}

