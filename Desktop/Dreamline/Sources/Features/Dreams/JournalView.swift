import SwiftUI

struct JournalView: View {
    @Environment(DreamStore.self) private var store
    @State private var isComposing = false

    var body: some View {
        NavigationStack {
            List {
                ForEach(Array(store.entries.enumerated()), id: \.element.id) { index, entry in
                    NavigationLink {
                        DreamDetailView(entry: Binding(
                            get: { store.entries[index] },
                            set: { store.entries[index] = $0 }
                        ))
                    } label: {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(entry.rawText).lineLimit(3)
                            Text(entry.createdAt.formatted(date: .abbreviated, time: .shortened))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .swipeActions {
                        Button("Interpret") {
                            let r = OracleService().interpret(text: entry.rawText)
                            store.entries[index].oracleSummary = r.summary
                            store.entries[index].extractedSymbols = r.symbols
                            store.entries[index].themes = r.themes
                        }.tint(.indigo)
                    }
                }
            }
            .navigationTitle("Journal")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        isComposing = true
                    } label: {
                        Image(systemName: "plus.circle.fill")
                    }
                }
            }
            .sheet(isPresented: $isComposing) {
                ComposeDreamView(store: store)
            }
        }
    }
}

struct ComposeDreamView: View {
    @Environment(\.dismiss) private var dismiss
    @State private var text: String = ""
    @State private var audioURL: URL? = nil
    @StateObject private var transcriber = TranscriptionService()

    var store: DreamStore

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                TextEditor(text: $text)
                    .frame(minHeight: 200)
                    .padding(8)
                    .overlay(RoundedRectangle(cornerRadius: 8).stroke(.quaternary))
                    .onAppear { if text.isEmpty { text = "" } }

                AudioCaptureStub(selectedAudioURL: $audioURL, transcriptBinding: $text)

                HStack {
                    if let _ = audioURL {
                        Button("Transcribe (stub)") {
                            Task {
                                if let url = audioURL {
                                    let t = try? await transcriber.transcribe(url: url)
                                    text = (text.isEmpty ? "" : text + "\n") + (t ?? "")
                                }
                            }
                        }
                    }
                    Spacer()
                    Button("Save") {
                        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !trimmed.isEmpty {
                            store.add(rawText: trimmed, transcriptURL: audioURL)
                            dismiss()
                        }
                    }
                    .buttonStyle(.borderedProminent)
                }
            }
            .padding()
            .navigationTitle("New Dream")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            }
        }
    }
}
