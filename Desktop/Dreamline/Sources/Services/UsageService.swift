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
    private var uid: String { "me" }
    
    func weeklyInterpretCount(weekStart: Date) async -> Int {
        let key = "oracle.\(ISO8601DateFormatter().string(from: weekStart.startOfWeek()))"
        
        #if canImport(FirebaseFirestore)
        let snap = try? await db.collection("users").document(uid).collection("usage").document(key).getDocument()
        return (snap?.get("count") as? Int) ?? 0
        #else
        return 0
        #endif
    }
    
    func incrementWeeklyInterpret(weekStart: Date) async {
        let key = "oracle.\(ISO8601DateFormatter().string(from: weekStart.startOfWeek()))"
        
        #if canImport(FirebaseFirestore)
        let ref = db.collection("users").document(uid).collection("usage").document(key)
        try? await db.runTransaction { tx in
            let snap = try? tx.getDocument(ref)
            let current = (snap?.get("count") as? Int) ?? 0
            tx.setData(["count": current + 1, "updatedAt": Date()], forDocument: ref, merge: true)
            return nil
        }
        #endif
    }
}

private extension Date {
    func startOfWeek() -> Date {
        var cal = Calendar.current
        cal.firstWeekday = 2
        let comps = cal.dateComponents([.yearForWeekOfYear, .weekOfYear], from: self)
        return cal.date(from: comps) ?? self
    }
}
