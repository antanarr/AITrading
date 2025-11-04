import Foundation

#if canImport(FirebaseFirestore)
import FirebaseFirestore
#endif

@MainActor
final class UsageService: ObservableObject {
    static let shared = UsageService()
    
    #if canImport(FirebaseFirestore)
    private let db = Firestore.firestore()
    #endif
    private var uid: String { "me" } // replace with real auth later
    
    func weeklyInterpretCount(weekStart: Date) async -> Int {
        let key = "oracle.\(OracleQuotaKey.weekKey(weekStart))"
        
        #if canImport(FirebaseFirestore)
        let snap = try? await db.collection("users").document(uid).collection("usage").document(key).getDocument()
        return (snap?.get("count") as? Int) ?? 0
        #else
        return 0
        #endif
    }
    
    func incrementWeeklyInterpret(weekStart: Date) async {
        let key = "oracle.\(OracleQuotaKey.weekKey(weekStart))"
        
        #if canImport(FirebaseFirestore)
        let currentCount = await weeklyInterpretCount(weekStart: weekStart)
        try? await db.collection("users").document(uid).collection("usage").document(key).setData([
            "count": currentCount + 1,
            "updatedAt": Date()
        ], merge: true)
        #endif
    }
}

enum OracleQuotaKey {
    static func weekKey(_ date: Date) -> String {
        let cal = Calendar.current
        let comp = cal.dateComponents([.yearForWeekOfYear, .weekOfYear], from: date)
        return "w\(comp.yearForWeekOfYear ?? 0)-\(comp.weekOfYear ?? 0)"
    }
}

