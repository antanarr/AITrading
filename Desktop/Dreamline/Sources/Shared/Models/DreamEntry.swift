import Foundation

struct DreamEntry: Identifiable, Codable, Hashable {
    var id: String = UUID().uuidString
    var createdAt: Date = .init()
    var rawText: String
    var transcriptURL: URL? = nil
    var symbols: [String] = []
    var sentiment: String? = nil
    var arousal: Double? = nil
    var valence: Double? = nil
    var oracleSummary: String? = nil
    var extractedSymbols: [String] = []
    var themes: [String] = []
}
