import Foundation

@MainActor
final class TodayRangeViewModel: ObservableObject {
    @Published var composed: String = ""
    @Published var loading = false
    
    func load(range: HoroscopeRange, birth: BirthData?) async {
        guard let birth = birth else {
            composed = ""
            return
        }
        
        loading = true
        defer { loading = false }
        
        let cal = Calendar.current
        let now = Date()
        
        let (start, end): (Date, Date) = {
            switch range {
            case .day:
                return (now, now)
            case .week:
                let s = cal.date(from: cal.dateComponents([.yearForWeekOfYear, .weekOfYear], from: now)) ?? now
                let e = cal.date(byAdding: .day, value: 6, to: s) ?? now
                return (s, e)
            case .month:
                let comps = cal.dateComponents([.year, .month], from: now)
                let s = cal.date(from: comps) ?? now
                let e = cal.date(byAdding: .month, value: 1, to: s)?.addingTimeInterval(-3600) ?? now
                return (s, e)
            case .year:
                let y = cal.component(.year, from: now)
                let s = cal.date(from: DateComponents(year: y, month: 1, day: 1)) ?? now
                let e = cal.date(from: DateComponents(year: y, month: 12, day: 31)) ?? now
                return (s, e)
            }
        }()
        
        let birthISO = ISO8601DateFormatter().string(from: combine(birth.date, birth.time))
        let items = await HoroscopeService.shared.transitsRange(birthISO: birthISO, start: start, end: end, range: range)
        let text = await HoroscopeService.shared.compose(range: range, items: items)
        
        self.composed = text
    }
    
    private func combine(_ d: Date, _ t: Date) -> Date {
        var c = Calendar.current.dateComponents([.year, .month, .day], from: d)
        let tt = Calendar.current.dateComponents([.hour, .minute, .second], from: t)
        c.hour = tt.hour
        c.minute = tt.minute
        c.second = tt.second
        return Calendar.current.date(from: c) ?? d
    }
}

