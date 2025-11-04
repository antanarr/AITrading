import XCTest
@testable import Dreamline

final class GateTests: XCTestCase {
    func testFreeQuota() {
        let rc = DLRemoteConfig.default
        XCTAssertEqual(
            OracleGate.canInterpret(tier: .free, weeklyCount: rc.freeInterpretationsPerWeek, rc: rc),
            .quotaDepleted
        )
        XCTAssertEqual(
            OracleGate.canInterpret(tier: .free, weeklyCount: rc.freeInterpretationsPerWeek - 1, rc: rc),
            .ok
        )
        XCTAssertEqual(
            OracleGate.canChat(tier: .free, sentCount: 3, trialCount: 3),
            .needsPro
        )
        XCTAssertEqual(
            OracleGate.canChat(tier: .free, sentCount: 2, trialCount: 3),
            .ok
        )
    }
}

