import SwiftUI

extension Color {
    init(hex: UInt, alpha: Double = 1.0) {
        let r = Double((hex >> 16) & 0xff)/255
        let g = Double((hex >> 8) & 0xff)/255
        let b = Double(hex & 0xff)/255
        self = Color(.sRGB, red: r, green: g, blue: b, opacity: alpha)
    }
    
    static let dlSpace         = Color(hex: 0x0B1020)
    static let dlIndigo        = Color(hex: 0x4C4FD6)
    static let dlViolet        = Color(hex: 0x7A5CFF)
    static let dlLilac         = Color(hex: 0xC9B6FF)
    static let dlMoon          = Color(hex: 0xF4F1FF)
    static let dlMint          = Color(hex: 0x3CE6A8)
    static let dlAmber         = Color(hex: 0xFFAC5F)
}

enum DLFont {
    static func title(_ size: CGFloat) -> Font { .system(size: size, weight: .semibold, design: .serif) }
    static func body(_ size: CGFloat) -> Font { .system(size: size, weight: .regular, design: .rounded) }
    static let chip = Font.system(size: 14, weight: .medium, design: .rounded)
}

enum DLGradients {
    static let oracle = LinearGradient(colors: [.dlIndigo, .dlViolet], startPoint: .topLeading, endPoint: .bottomTrailing)
}

struct DLCard<Content: View>: View {
    var content: () -> Content
    
    var body: some View {
        content()
            .padding(16)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.white.opacity(0.06)))
    }
}

