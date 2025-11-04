import SwiftUI

@MainActor
struct OnboardingFlow: View {
    enum Step { case hello, privacy, birth, notifications, sample, done }
    
    @AppStorage("onboarding.completed") private var completed = false
    @AppStorage("app.lock.enabled") private var lockEnabled = true
    
    @State private var step: Step = .hello
    
    // Birth inputs
    @State private var birthDate = Date()
    @State private var birthTime = Date()
    @State private var birthPlace = ""
    
    // Notifications
    @State private var notifTime = Calendar.current.date(bySettingHour: 7, minute: 30, second: 0, of: Date()) ?? Date()
    
    var body: some View {
        ZStack {
            LinearGradient(colors: [.dlSpace, .black], startPoint: .top, endPoint: .bottom).ignoresSafeArea()
            content
        }
        .tint(.dlViolet)
        .foregroundStyle(.dlMoon)
        .animation(.easeInOut, value: step)
    }
    
    @ViewBuilder private var content: some View {
        switch step {
        case .hello:
            VStack(spacing: 24) {
                Text("Your dreams remember what you forget.")
                    .font(DLFont.title(32))
                
                Button("Begin") { step = .privacy }
                    .buttonStyle(.borderedProminent)
            }.padding()
            
        case .privacy:
            DLCard {
                VStack(alignment: .leading, spacing: 16) {
                    Toggle("Lock with Face ID", isOn: $lockEnabled)
                    
                    Text("Local‑first, private by default. You control what's shared.")
                        .font(DLFont.body(14)).foregroundStyle(.secondary)
                    
                    HStack {
                        Spacer()
                        Button("Next") { step = .birth }
                    }
                }
            }.padding()
            
        case .birth:
            DLCard {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Birth Data").font(DLFont.title(24))
                    
                    DatePicker("Date", selection: $birthDate, displayedComponents: .date)
                    DatePicker("Time", selection: $birthTime, displayedComponents: .hourAndMinute)
                    
                    TextField("Birth place (city, country)", text: $birthPlace)
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled(true)
                    
                    HStack {
                        Spacer()
                        Button("Save") {
                            Task {
                                let bd = BirthData(date: birthDate, time: birthTime, placeText: birthPlace)
                                try? await AstroService.shared.saveBirth(bd)
                                step = .notifications
                            }
                        }
                    }
                }
            }.padding()
            
        case .notifications:
            DLCard {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Daily Horoscope Time").font(DLFont.title(24))
                    
                    DatePicker("Time", selection: $notifTime, displayedComponents: .hourAndMinute)
                    
                    HStack {
                        Spacer()
                        Button("Allow & Continue") {
                            Task {
                                let comps = Calendar.current.dateComponents([.hour, .minute], from: notifTime)
                                await NotificationService.requestAndScheduleDaily(at: comps, body: "Your dream symbols meet today's skies.")
                                step = .sample
                            }
                        }
                    }
                }
            }.padding()
            
        case .sample:
            VStack(spacing: 16) {
                DLCard {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Try a sample dream").font(DLFont.title(22))
                        Text("\"Ocean, collapsing house, hidden room…\"")
                            .font(DLFont.body(16))
                        
                        HStack {
                            Spacer()
                            Button("Generate First Insight") { step = .done }
                        }
                    }
                }
            }.padding()
            
        case .done:
            VStack(spacing: 16) {
                Text("You're all set.")
                    .font(DLFont.title(28))
                
                Button("Enter Dreamline") { completed = true }
                    .buttonStyle(.borderedProminent)
            }.padding()
        }
    }
}

