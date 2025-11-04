import Foundation

enum GateReason {
    case ok
    case needsPlus
    case needsPro
    case quotaDepleted
}

struct OracleGate {
    static func canInterpret(tier: Tier, weeklyCount: Int, rc: DLRemoteConfig) -> GateReason {
        switch tier {
        case .pro, .plus:
            return .ok
        case .free:
            return weeklyCount < rc.freeInterpretationsPerWeek ? .ok : .quotaDepleted
        }
    }
    
    static func canChat(tier: Tier, sentCount: Int, trialCount: Int) -> GateReason {
        switch tier {
        case .pro:
            return .ok
        case .plus, .free:
            return sentCount < trialCount ? .ok : .needsPro
        }
    }
}

