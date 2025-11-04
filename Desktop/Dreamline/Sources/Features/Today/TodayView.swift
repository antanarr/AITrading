import SwiftUI

struct TodayView: View {
    @Environment(DreamStore.self) private var store
    @StateObject private var vm = TodayViewModel()
    @State private var transit: TransitSummary? = nil
    private let astro = AstroService.shared

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    DreamSyncedCard(transit: transit, recentThemes: recentThemes(), summary: vm.summary)
                }
                .padding()
            }
            .navigationTitle("Today")
            .task {
                await vm.load()
                transit = await astro.transits(for: .now)
            }
        }
    }

    private func recentThemes() -> [String] {
        // Pull themes from the most recent dream that has an oracle summary,
        // otherwise use a shallow keyword pass on the latest rawText.
        guard let latest = store.entries.first else { return [] }
        if !latest.themes.isEmpty { return latest.themes }
        // naive backfill: pick 2-3 words from text
        let words = latest.rawText
            .lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { $0.count > 4 }
        return Array(Set(words)).prefix(3).map { String($0) }
    }
}

private struct DreamSyncedCard: View {
    let transit: TransitSummary?
    let recentThemes: [String]
    let summary: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Dream-Synced Horoscope")
                .font(.title3).bold()
            Text(summary)
                .fixedSize(horizontal: false, vertical: true)
            if let transit {
                Text(transit.notes.joined(separator: " "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if !recentThemes.isEmpty {
                Text("Recent themes: " + recentThemes.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 12).fill(.ultraThinMaterial))
    }
}
