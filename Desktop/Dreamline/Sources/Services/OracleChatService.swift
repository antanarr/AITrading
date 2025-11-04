import Foundation

struct ChatMessage: Codable {
    let role: String
    let content: String
}

struct ChatReply: Codable {
    let reply: String
    let followupPrompt: String?
    let warnings: [String]?
}

@MainActor
final class OracleChatService: ObservableObject {
    static let shared = OracleChatService()
    
    private let baseURL = (Bundle.main.object(forInfoDictionaryKey: "FunctionsBaseURL") as? String) ?? ""
    
    func send(messages: [ChatMessage], dreamContext: String?, history: MotifHistory, transit: TransitSummary) async throws -> ChatReply {
        guard !baseURL.isEmpty else {
            throw URLError(.badURL)
        }
        
        struct Req: Encodable {
            let messages: [ChatMessage]
            let dreamContext: String?
            let history: MotifHistory
            let transit: TransitSummary
        }
        
        var req = URLRequest(url: URL(string: "\(baseURL)/oracleChat")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONEncoder().encode(Req(messages: messages, dreamContext: dreamContext, history: history, transit: transit))
        
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        
        return try JSONDecoder().decode(ChatReply.self, from: data)
    }
}

