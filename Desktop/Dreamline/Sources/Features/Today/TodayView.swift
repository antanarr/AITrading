import SwiftUI

struct TodayView: View {
    @Environment(DreamStore.self) private var store
    @StateObject private var vm = TodayViewModel()
    @State private var transit: TransitSummary? = nil
    @State private var isLoadingTransit: Bool = true
    private let astro = AstroService.shared

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    DreamSyncedCard(
                        transit: transit,
                        recentThemes: recentThemes(),
                        summary: vm.summary,
                        isLoadingSummary: vm.isLoading,
                        isLoadingTransit: isLoadingTransit
                    )
                    
                    TodayRangeView()
                }
                .padding()
            }
            .background(
                LinearGradient(
                    colors: [Color.dlSpace, Color.dlSpace.opacity(0.8), Color.dlIndigo.opacity(0.1)],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
                .ignoresSafeArea()
            )
            .navigationTitle("Today")
            .task {
                await vm.load()
                isLoadingTransit = true
                transit = await astro.transits(for: .now)
                isLoadingTransit = false
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
    let isLoadingSummary: Bool
    let isLoadingTransit: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Dream-Synced Horoscope")
                .font(DLFont.title(24))
                .foregroundStyle(.primary)
            
            if isLoadingSummary {
                Group {
                    VStack(alignment: .leading, spacing: 8) {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color.secondary.opacity(0.3))
                            .frame(height: 20)
                        RoundedRectangle(cornerRadius: 8)
                            .fill(Color.secondary.opacity(0.2))
                            .frame(height: 20)
                            .frame(width: 200)
                    }
                }
                .shimmer()
            } else {
                Text(summary)
                    .font(DLFont.body(16))
                    .fixedSize(horizontal: false, vertical: true)
                    .foregroundStyle(.primary)
            }
            
            // Transit strip as pill
            if isLoadingTransit {
                Group {
                    HStack {
                        RoundedRectangle(cornerRadius: 20)
                            .fill(Color.secondary.opacity(0.2))
                            .frame(height: 32)
                            .frame(maxWidth: 150)
                    }
                }
                .shimmer()
            } else if let transit {
                HStack(spacing: 8) {
                    Text(transit.headline)
                        .font(DLFont.chip)
                        .foregroundStyle(.primary)
                    
                    if !transit.notes.isEmpty {
                        Text("•")
                            .font(DLFont.chip)
                            .foregroundStyle(.secondary)
                        
                        Text(transit.notes.joined(separator: " • "))
                            .font(DLFont.chip)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(
                    Capsule()
                        .fill(.ultraThinMaterial)
                        .overlay(
                            Capsule()
                                .stroke(Color.white.opacity(0.1), lineWidth: 1)
                        )
                )
            } else {
                HStack(spacing: 8) {
                    Text("Transit data unavailable")
                        .font(DLFont.chip)
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(
                    Capsule()
                        .fill(.ultraThinMaterial)
                        .overlay(
                            Capsule()
                                .stroke(Color.white.opacity(0.1), lineWidth: 1)
                        )
                )
            }
            
            if !recentThemes.isEmpty {
                Text("Recent themes: " + recentThemes.joined(separator: ", "))
                    .font(DLFont.body(12))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(20)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(.ultraThinMaterial)
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .stroke(Color.white.opacity(0.06), lineWidth: 1)
                )
        )
    }
}
