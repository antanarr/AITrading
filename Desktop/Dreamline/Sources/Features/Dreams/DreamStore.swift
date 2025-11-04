import Foundation
import Observation

@Observable final class DreamStore {
    var entries: [DreamEntry] = []

    func add(rawText: String, transcriptURL: URL? = nil) {
        let e = DreamEntry(rawText: rawText, transcriptURL: transcriptURL)
        entries.insert(e, at: 0)
    }
}
