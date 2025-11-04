import XCTest
@testable import Dreamline

final class RemoteConfigTests: XCTestCase {
    func testDefaults() {
        let rc = RemoteConfigService.shared.config
        
        XCTAssertGreaterThanOrEqual(rc.freeInterpretationsPerWeek, 1)
        XCTAssertEqual(rc.trialDaysPlus, 7)
        XCTAssertEqual(rc.insightsBlurThresholdDays, 7)
    }
}

