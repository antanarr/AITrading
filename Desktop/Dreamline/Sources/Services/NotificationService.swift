import Foundation
import UserNotifications

enum NotificationService {
    static func requestAndScheduleDaily(at components: DateComponents, body: String) async {
        let center = UNUserNotificationCenter.current()
        let _ = try? await center.requestAuthorization(options: [.alert, .sound, .badge])
        
        let content = UNMutableNotificationContent()
        content.title = "Dream‑Synced Horoscope"
        content.body = body
        content.sound = .default
        
        var comps = components
        comps.second = 0
        let trigger = UNCalendarNotificationTrigger(dateMatching: comps, repeats: true)
        let req = UNNotificationRequest(identifier: "daily.today", content: content, trigger: trigger)
        
        try? await center.add(req)
    }
}

