import Foundation

struct SymbolExtractor {
    static let symbolLexicon: [String] = [
        "wall","brick wall","truck","car","road","plaza","mall","store","building","workers",
        "america","song","music","cd","holiday","easter","christmas","friend","crowd","sign",
        "ditch","danger","renovation","construction","speed","hiding","crouch","pickup"
    ]

    static func extract(from text: String) -> [String] {
        let lower = text.lowercased()
        // rank longer phrases first to avoid splitting them
        let sorted = symbolLexicon.sorted { $0.count > $1.count }
        var found = Set<String>()
        for s in sorted {
            if lower.contains(s) { found.insert(s) }
        }
        return Array(found)
    }

    static func inferThemes(symbols: [String]) -> [String] {
        var t = Set<String>()
        if symbols.contains(where: { ["renovation","construction","building"].contains($0) }) {
            t.insert("rebuilding/change")
        }
        if symbols.contains(where: { ["wall","brick wall","sign"].contains($0) }) {
            t.insert("protection/boundaries")
        }
        if symbols.contains(where: { ["truck","car","road","speed","pickup"].contains($0) }) {
            t.insert("external pressure/fast change")
        }
        if symbols.contains(where: { ["america","song","music","cd","holiday","easter","christmas"].contains($0) }) {
            t.insert("identity/ritual/values")
        }
        return Array(t)
    }
}
