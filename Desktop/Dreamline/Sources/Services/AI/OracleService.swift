import Foundation

struct SymbolOccurrence: Codable, Hashable {
    var name: String
    var count: Int
}

struct OracleExtraction: Codable {
    var symbols: [SymbolOccurrence]
    var tone: String
    var archetypes: [String]
}

struct SymbolCard: Codable {
    var name: String
    var meaning: String
    var personalNote: String?
}

struct OracleInterpretation: Codable {
    var shortSummary: String
    var longForm: String
    var actionPrompt: String
    var symbolCards: [SymbolCard]
}

protocol OracleClient {
    func extract(from text: String) async throws -> OracleExtraction
    func interpret(dreamText: String,
                   extraction: OracleExtraction,
                   transit: TransitSummary) async throws -> OracleInterpretation
    func interpret(dreamText: String,
                   extraction: OracleExtraction,
                   transit: TransitSummary,
                   history: MotifHistory) async throws -> OracleInterpretation
}

extension OracleClient {
    func interpret(dreamText: String,
                   extraction: OracleExtraction,
                   transit: TransitSummary,
                   history: MotifHistory) async throws -> OracleInterpretation {
        return try await interpret(dreamText: dreamText, extraction: extraction, transit: transit)
    }
}

final class CloudOracleClient: OracleClient {
    private let baseURL = (Bundle.main.object(forInfoDictionaryKey: "FunctionsBaseURL") as? String) ?? ""
    let model: String = (Bundle.main.object(forInfoDictionaryKey: "OracleModel") as? String) ?? "gpt-4.1-mini"
    
    func extract(from text: String) async throws -> OracleExtraction {
        guard !baseURL.isEmpty else {
            throw URLError(.badURL)
        }
        
        struct Req: Encodable {
            let dream: String
            let model: String
        }
        
        var req = URLRequest(url: URL(string: "\(baseURL)/oracleExtract")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(Req(dream: text, model: model))
        
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        
        return try JSONDecoder().decode(OracleExtraction.self, from: data)
    }
    
    func interpret(dreamText: String,
                   extraction: OracleExtraction,
                   transit: TransitSummary) async throws -> OracleInterpretation {
        return try await interpret(dreamText: dreamText, extraction: extraction, transit: transit, history: MotifHistory(topSymbols: [], archetypeTrends: [], userPhrases: [], tones7d: [:]))
    }
    
    func interpret(dreamText: String,
                   extraction: OracleExtraction,
                   transit: TransitSummary,
                   history: MotifHistory) async throws -> OracleInterpretation {
        guard !baseURL.isEmpty else {
            throw URLError(.badURL)
        }
        
        struct Req: Encodable {
            let dream: String
            let extraction: OracleExtraction
            let transit: TransitSummary
            let history: MotifHistory
            let model: String
        }
        
        var req = URLRequest(url: URL(string: "\(baseURL)/oracleInterpret")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(Req(dream: dreamText, extraction: extraction, transit: transit, history: history, model: model))
        
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        
        return try JSONDecoder().decode(OracleInterpretation.self, from: data)
    }
}

final class StubOracleClient: OracleClient {
    func extract(from text: String) async throws -> OracleExtraction {
        // Stupid-simple heuristic stub; replace with LLM later.
        let dictionary = ["water","ocean","river","house","room","door","flight","teeth","death","bird"]
        
        let lower = text.lowercased()
        let counts = dictionary.map { word in
            SymbolOccurrence(name: word, count: lower.components(separatedBy: word).count - 1)
        }.filter { $0.count > 0 }
        
        let tone = lower.contains("fear") || lower.contains("anxious") ? "anxious" : "curious"
        let archetypes = counts.contains(where: { $0.name == "door" }) ? ["threshold"] : ["journey"]
        
        return OracleExtraction(symbols: counts, tone: tone, archetypes: archetypes)
    }
    
    func interpret(dreamText: String,
                   extraction: OracleExtraction,
                   transit: TransitSummary) async throws -> OracleInterpretation {
        return try await interpret(dreamText: dreamText, extraction: extraction, transit: transit, history: MotifHistory(topSymbols: [], archetypeTrends: [], userPhrases: [], tones7d: [:]))
    }
    
    func interpret(dreamText: String,
                   extraction: OracleExtraction,
                   transit: TransitSummary,
                   history: MotifHistory) async throws -> OracleInterpretation {
        let top = extraction.symbols.sorted(by: { $0.count > $1.count }).first?.name ?? "symbol"
        let short = "\(top.capitalized) + \(transit.headline): a day for gentle, honest reflection."
        let long = """
        Your dream surfaces \(extraction.archetypes.joined(separator: ", ")). \
        Given \(transit.headline), prioritize intuition over speed. Track occurrences of \(top).
        """
        
        let cards = extraction.symbols.prefix(4).map {
            SymbolCard(name: $0.name,
                       meaning: "\($0.name.capitalized) can point to an emotional current seeking motion.",
                       personalNote: nil)
        }
        
        return OracleInterpretation(shortSummary: short,
                                    longForm: long,
                                    actionPrompt: "Name one emotion and one small action today.",
                                    symbolCards: Array(cards))
    }
}

// Compatibility adapter for existing code
// Note: This synchronous wrapper is temporary for backward compatibility
// Prefer using StubOracleClient directly with async/await in new code
struct OracleService {
    private let client = StubOracleClient()
    
    func interpret(text: String) -> OracleResult {
        // Synchronous wrapper - blocks until async work completes
        // This is a compatibility layer; new code should use async methods directly
        let group = DispatchGroup()
        var result: OracleResult?
        
        group.enter()
        Task { @MainActor in
            defer { group.leave() }
            do {
                let extraction = try await client.extract(from: text)
                let transit = await AstroService.shared.transits(for: .now)
                let interpretation = try await client.interpret(dreamText: text, extraction: extraction, transit: transit)
                
                result = OracleResult(
                    summary: interpretation.shortSummary,
                    symbols: extraction.symbols.map { $0.name },
                    themes: extraction.archetypes
                )
            } catch {
                result = OracleResult(summary: "Interpretation failed", symbols: [], themes: [])
            }
        }
        
        group.wait()
        return result ?? OracleResult(summary: "Interpretation failed", symbols: [], themes: [])
    }
}

struct OracleResult: Codable, Hashable {
    var summary: String
    var symbols: [String]
    var themes: [String]
}
