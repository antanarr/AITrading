import Foundation

@MainActor
final class TodayViewModel: ObservableObject {
    @Published var summary: String = "Loading…"
    @Published var isLoading: Bool = true
    
    func load() async {
        isLoading = true
        let transit = await AstroService.shared.transits(for: Date())
        
        // Pull last dream text from storage if available; stub for now:
        let top = "water"
        summary = "\(top.capitalized) × \(transit.headline): a day for gentle clarity."
        isLoading = false
    }
}

