import SwiftUI

struct OracleChatView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var rc = RemoteConfigService.shared
    @ObservedObject var astro = AstroService.shared
    @StateObject var hist = HistoryService.shared
    @StateObject var chat = OracleChatService.shared
    let tier: Tier
    
    @State private var messages: [ChatMessage] = []
    @State private var input: String = ""
    @State private var sentCount: Int = 0
    @State private var loading = false
    @State private var showPaywall = false
    
    var body: some View {
        VStack {
            ScrollView {
                ForEach(messages.indices, id: \.self) { i in
                    let m = messages[i]
                    HStack {
                        if m.role == "assistant" {
                            Text(m.content)
                                .padding()
                                .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12))
                        } else {
                            Spacer()
                            Text(m.content)
                                .padding()
                                .background(Color.dlIndigo.opacity(0.2), in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
                    .padding(.vertical, 4)
                }
                
                if loading {
                    Text("…")
                        .shimmer()
                }
            }
            .padding()
            
            HStack {
                TextField("Ask about a symbol, feeling, or pattern…", text: $input)
                    .textFieldStyle(.roundedBorder)
                
                Button("Send") {
                    Task {
                        await send()
                    }
                }
                .disabled(input.isEmpty || loading)
            }
            .padding()
        }
        .navigationTitle("Oracle")
        .sheet(isPresented: $showPaywall) {
            PaywallView()
        }
        .onAppear {
            if messages.isEmpty {
                messages.append(ChatMessage(role: "assistant", content: "What symbol stands out from your last dream?"))
            }
        }
    }
    
    private func send() async {
        let trial = rc.config.freeChatTrialCount
        let gate = OracleGate.canChat(tier: tier, sentCount: sentCount, trialCount: trial)
        
        guard gate == .ok else {
            showPaywall = true
            return
        }
        
        loading = true
        defer { loading = false }
        
        let user = ChatMessage(role: "user", content: input)
        messages.append(user)
        input = ""
        
        let histo = await HistoryService.shared.summarize(days: tier == .pro ? 90 : (tier == .plus ? 30 : 7))
        let transit = await AstroService.shared.transits(for: Date())
        
        do {
            let r = try await OracleChatService.shared.send(messages: messages, dreamContext: nil, history: histo, transit: transit)
            messages.append(ChatMessage(role: "assistant", content: r.reply))
            sentCount += 1
        } catch {
            messages.append(ChatMessage(role: "assistant", content: "Connection hiccup. Try again in a moment."))
        }
    }
}

