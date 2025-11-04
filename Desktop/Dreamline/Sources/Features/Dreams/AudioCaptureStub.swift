import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct AudioCaptureStub: View {
    @Binding var selectedAudioURL: URL?
    var transcriptBinding: Binding<String>? = nil

    @State private var showPicker = false
    @StateObject private var transcriber = TranscriptionService()
    @State private var transcriptText: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button("Attach Audio") { showPicker = true }
                if let url = selectedAudioURL {
                    Text(url.lastPathComponent).lineLimit(1).truncationMode(.middle)
                }
            }
            
            if let transcript = transcriptText {
                Text(transcript)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.top, 4)
            }
        }
        .fileImporter(isPresented: $showPicker, allowedContentTypes: [UTType.audio]) { result in
            if case .success(let url) = result {
                selectedAudioURL = url
                Task {
                    if let transcript = try? await transcriber.transcribe(url: url) {
                        transcriptText = transcript
                        if let binding = transcriptBinding {
                            binding.wrappedValue = transcript
                        }
                    }
                }
            }
        }
    }
}
