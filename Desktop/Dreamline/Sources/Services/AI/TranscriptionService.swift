import Foundation
import AVFoundation

@MainActor
final class TranscriptionService: ObservableObject {
    @Published private(set) var isTranscribing = false
    
    func transcribe(url: URL) async throws -> String {
        // Stub: wire to Whisper later. For now, return file name as fake transcript.
        isTranscribing = true
        defer { isTranscribing = false }
        
        return "Transcript of \(url.lastPathComponent) — (stub)"
    }
}
