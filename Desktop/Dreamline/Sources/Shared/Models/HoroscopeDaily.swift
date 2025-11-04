import Foundation

struct HoroscopeDaily: Codable, Hashable {
    let date: String       // "YYYY-MM-DD"
    let text: String       // short transit summary
    let aspects: [String]  // simple tags
}
