import XCTest
@testable import Dreamline

final class HistorySummarizerTests: XCTestCase {
    func testMotifHistoryShape() async throws {
        let hist = await HistoryService.shared.summarize(days: 7)
        XCTAssertFalse(hist.topSymbols.isEmpty)
        XCTAssertFalse(hist.archetypeTrends.isEmpty)
    }
}

