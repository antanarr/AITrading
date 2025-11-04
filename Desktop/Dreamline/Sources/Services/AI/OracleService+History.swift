import Foundation

extension CloudOracleClient {
    func interpret(dreamText: String, extraction: OracleExtraction, transit: TransitSummary, history: MotifHistory) async throws -> OracleInterpretation {
        guard !baseURL.isEmpty else {
            return try await StubOracleClient().interpret(dreamText: dreamText, extraction: extraction, transit: transit)
        }
        
        let payload: [String: Any] = [
            "dream": dreamText,
            "extraction": try JSONSerialization.jsonObject(with: try JSONEncoder().encode(extraction)),
            "transit": try JSONSerialization.jsonObject(with: try JSONEncoder().encode(transit)),
            "history": try JSONSerialization.jsonObject(with: try JSONEncoder().encode(history)),
            "model": model
        ]
        
        var req = URLRequest(url: URL(string: "\(baseURL)/oracleInterpret")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: payload)
        
        let (respData, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }
        
        return try JSONDecoder().decode(OracleInterpretation.self, from: respData)
    }
}

