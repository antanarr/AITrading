import SwiftUI

struct SymbolHeatmapView: View {
    let days: Int
    let data: [[Int]] // simple 7 x N/7 grid of symbol density
    
    var body: some View {
        let cols = Array(repeating: GridItem(.flexible(minimum: 8, maximum: 24), spacing: 4), count: 7)
        
        LazyVGrid(columns: cols, spacing: 4) {
            ForEach(0..<data.count, id: \.self) { col in
                ForEach(0..<data[col].count, id: \.self) { row in
                    Rectangle()
                        .fill(Color.dlViolet.opacity(opacityFor(data[col][row])))
                        .frame(height: 12)
                        .cornerRadius(2)
                }
            }
        }
        .padding(.vertical, 8)
    }
    
    private func opacityFor(_ v: Int) -> Double {
        v == 0 ? 0.08 : min(0.9, 0.15 + Double(v) * 0.12)
    }
}

