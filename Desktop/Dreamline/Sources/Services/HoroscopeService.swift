import Foundation

enum HoroscopeRange: String, Codable {
    case day, week, month, year
}

struct HoroscopeItem: Codable, Equatable {
    let dateISO: String
    let headline: String
    let bullets: [String]
}

struct HoroscopeComposeResponse: Codable {
    let range: String
    let text: String
}

@MainActor
final class HoroscopeService: ObservableObject {
    static let shared = HoroscopeService()
    
    private let baseURL = (Bundle.main.object(forInfoDictionaryKey: "FunctionsBaseURL") as? String) ?? ""
    
    func transitsRange(birthISO: String, start: Date, end: Date, range: HoroscopeRange) async -> [HoroscopeItem] {
        guard !baseURL.isEmpty else { return [] }
        
        struct Req: Encodable {
            let birthISO: String
            let startISO: String
            let endISO: String
            let range: String
        }
        
        let req = Req(
            birthISO: birthISO,
            startISO: ISO8601DateFormatter().string(from: start),
            endISO: ISO8601DateFormatter().string(from: end),
            range: range.rawValue
        )
        
        do {
            var urlReq = URLRequest(url: URL(string: "\(baseURL)/astroTransitsRange")!)
            urlReq.httpMethod = "POST"
            urlReq.setValue("application/json", forHTTPHeaderField: "Content-Type")
            urlReq.httpBody = try JSONEncoder().encode(req)
            
            let (data, resp) = try await URLSession.shared.data(for: urlReq)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            
            struct Resp: Decodable {
                let items: [HoroscopeItem]
            }
            
            return (try? JSONDecoder().decode(Resp.self, from: data).items) ?? []
        } catch {
            return []
        }
    }
    
    func compose(range: HoroscopeRange, items: [HoroscopeItem]) async -> String {
        guard !baseURL.isEmpty else { return "" }
        
        struct Req: Encodable {
            let range: String
            let items: [HoroscopeItem]
        }
        
        do {
            var urlReq = URLRequest(url: URL(string: "\(baseURL)/horoscopeCompose")!)
            urlReq.httpMethod = "POST"
            urlReq.setValue("application/json", forHTTPHeaderField: "Content-Type")
            urlReq.httpBody = try JSONEncoder().encode(Req(range: range.rawValue, items: items))
            
            let (data, resp) = try await URLSession.shared.data(for: urlReq)
            guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            
            return (try? JSONDecoder().decode(HoroscopeComposeResponse.self, from: data).text) ?? ""
        } catch {
            return ""
        }
    }
}

