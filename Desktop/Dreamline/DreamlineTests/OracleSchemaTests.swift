import XCTest
@testable import Dreamline

final class OracleSchemaTests: XCTestCase {
    func testStubExtraction() async throws {
        let o = StubOracleClient()
        
        let ex = try await o.extract(from: "Ocean and a hidden room with a door.")
        
        XCTAssertTrue(ex.symbols.contains(where: { $0.name == "ocean" || $0.name == "door" }))
    }
}

