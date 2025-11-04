import Foundation

#if canImport(FirebaseFirestore)
import FirebaseFirestore
#if canImport(FirebaseFirestoreSwift)
import FirebaseFirestoreSwift
#endif
#endif

struct MotifHistory: Codable, Equatable {
    var topSymbols: [String]
    var archetypeTrends: [String]
    var userPhrases: [String]
    var tones7d: [String: Int]
}

@MainActor
final class HistoryService: ObservableObject {
    static let shared = HistoryService()
    
    #if canImport(FirebaseFirestore)
    private let db = Firestore.firestore()
    #endif
    private var uid: String { "me" } // TODO: replace with real Auth
    
    func summarize(days: Int) async -> MotifHistory {
        let since = Calendar.current.date(byAdding: .day, value: -days, to: Date()) ?? Date().addingTimeInterval(-86400 * Double(days))
        
        var symbolCounts: [String: Int] = [:]
        var archetypeCounts: [String: Int] = [:]
        var toneCounts: [String: Int] = [:]
        var bigramCounts: [String: Int] = [:]
        
        // Query recent dreams
        #if canImport(FirebaseFirestore)
        do {
            let qs = try await db.collection("users").document(uid)
                .collection("dreams")
                .whereField("createdAt", isGreaterThan: since)
                .order(by: "createdAt", descending: true)
                .limit(to: 300)
                .getDocuments()
            
            for doc in qs.documents {
                if let extraction = doc.data()["extraction"] as? [String: Any],
                   let symbols = extraction["symbols"] as? [[String: Any]] {
                    for s in symbols {
                        if let name = s["name"] as? String, !name.isEmpty {
                            symbolCounts[name, default: 0] += (s["count"] as? Int ?? 1)
                        }
                    }
                    if let arche = extraction["archetypes"] as? [String] {
                        for a in arche {
                            archetypeCounts[a, default: 0] += 1
                        }
                    }
                }
                
                if let tone = doc.data()["tone"] as? String, !tone.isEmpty {
                    toneCounts[tone, default: 0] += 1
                }
                
                if let text = doc.data()["text"] as? String {
                    extractBigrams(from: text).forEach { bigramCounts[$0, default: 0] += 1 }
                }
            }
        } catch {}
        #endif
        
        let topSymbols = symbolCounts.sorted { $0.value > $1.value }.prefix(5).map { $0.key }
        let archetypeTrends = archetypeCounts.sorted { $0.value > $1.value }.prefix(3).map { "\($0.key)↑" }
        let userPhrases = bigramCounts.sorted { $0.value > $1.value }.prefix(3).map { $0.key }
        let tones7d = toneCounts
        
        return MotifHistory(topSymbols: topSymbols, archetypeTrends: archetypeTrends, userPhrases: userPhrases, tones7d: tones7d)
    }
    
    private func extractBigrams(from text: String) -> [String] {
        let tokens = text.lowercased().split(whereSeparator: { !$0.isLetter && !$0.isNumber }).map(String.init)
        let stops: Set<String> = ["the", "a", "an", "and", "of", "to", "in", "on", "for", "with", "at", "by", "from", "as", "is", "it", "this", "that", "i", "you", "he", "she", "we", "they"]
        
        var grams: [String] = []
        for i in 0..<(max(0, tokens.count - 1)) {
            let a = tokens[i], b = tokens[i + 1]
            if stops.contains(a) || stops.contains(b) { continue }
            grams.append("\(a) \(b)")
        }
        return grams
    }
}
