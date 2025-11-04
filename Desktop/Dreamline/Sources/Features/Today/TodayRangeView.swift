import SwiftUI

struct TodayRangeView: View {
    @ObservedObject var astro = AstroService.shared
    @StateObject var vm = TodayRangeViewModel()
    @State private var sel: HoroscopeRange = .day
    
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Picker("", selection: $sel) {
                Text("Day").tag(HoroscopeRange.day)
                Text("Week").tag(HoroscopeRange.week)
                Text("Month").tag(HoroscopeRange.month)
                Text("Year").tag(HoroscopeRange.year)
            }
            .pickerStyle(.segmented)
            
            if vm.loading {
                Text("Composing…").oracleShimmer(true)
            } else {
                Text(vm.composed).font(DLFont.body(16))
            }
        }
        .padding()
        .task {
            await vm.load(range: sel, birth: astro.birth)
        }
        .onChange(of: sel) { _, _ in
            Task {
                await vm.load(range: sel, birth: astro.birth)
            }
        }
    }
}

