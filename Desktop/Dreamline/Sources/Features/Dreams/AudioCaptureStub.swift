import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct AudioCaptureStub: View {
    @Binding var selectedAudioURL: URL?
    var transcriptBinding: Binding<String>? = nil

    @State private var showPicker = false
    @StateObject private var transcriber = TranscriptionService()
    @State private var transcriptText: String? = nil
    @State private var isEditingTranscript = false
    @FocusState private var isTranscriptFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Button("Attach Audio") { showPicker = true }
                if let url = selectedAudioURL {
                    Text(url.lastPathComponent)
                        .font(DLFont.body(13))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .foregroundStyle(.secondary)
                }
            }
            
            if let transcript = transcriptText {
                if let binding = transcriptBinding {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("Transcript")
                                .font(DLFont.body(14))
                                .foregroundStyle(.secondary)
                            
                            Spacer()
                            
                            Button(isEditingTranscript ? "Done" : "Edit transcript") {
                                isEditingTranscript.toggle()
                                if isEditingTranscript {
                                    isTranscriptFocused = true
                                }
                            }
                            .font(DLFont.body(13))
                            .foregroundStyle(.blue)
                        }
                        
                        if isEditingTranscript {
                            TextEditor(text: binding)
                                .font(DLFont.body(14))
                                .frame(minHeight: 100)
                                .padding(8)
                                .background(
                                    RoundedRectangle(cornerRadius: 8)
                                        .fill(Color(.systemBackground))
                                        .overlay(
                                            RoundedRectangle(cornerRadius: 8)
                                                .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
                                        )
                                )
                                .focused($isTranscriptFocused)
                        } else {
                            Text(binding.wrappedValue)
                                .font(DLFont.body(14))
                                .foregroundStyle(.secondary)
                                .padding(.vertical, 4)
                        }
                    }
                } else {
                    Text(transcript)
                        .font(DLFont.body(14))
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                }
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
